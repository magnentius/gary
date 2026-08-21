"""Prompt assembly (ARCHITECTURE.md §8) and injection defence (§9.1).

v1 of the design never said what goes into a Gemini call. A campaign runs for thousands
of turns, so "append everything" is not a strategy -- it guarantees runaway cost and then
truncation of exactly the facts the system depends on.

Four layers, assembled fresh every call:

    1. system instruction   static persona + authority rules       never trimmed
    2. live state           rebuilt from the store each turn       never trimmed
    3. retrieved memory     notes + rolling summary                trimmed second
    4. recent turns         last N verbatim                        trimmed first

Layer 2 being rebuilt every call is the mechanism by which the state server actually *is*
the source of truth rather than merely being drawn that way on the architecture diagram.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .state.schema import format_world_time

DEFAULT_RECENT_TURNS = 30
DEFAULT_TOKEN_BUDGET = 40_000
CHARS_PER_TOKEN = 4  # rough; replace with the real tokenizer when metering cost


DM_SYSTEM_INSTRUCTION = """\
You are an expert Tabletop RPG Game Master running D&D 2024 (not the 2014 rules).

AUTHORITY — this is absolute:
- You do NOT decide whether a roll succeeded. When an action needs a check, call
  open_check with the skill, a DC, and both outcomes. Then STOP and wait.
- Roll outcomes arrive as structured tool results. They are established fact. Narrate
  them. Never re-evaluate, soften, or reverse a result.
- Never write an Avrae command yourself. open_check renders it.
- During combat, Avrae owns hit points, conditions, and durations. Describe them; do not
  contradict or recalculate them.

CONTINUITY — prose is not memory. Only tool calls survive the session:
- Call set_scene the first time you describe where the party is, and again whenever they
  move or the situation changes around them. Do this even if they have not travelled: a
  room you described but never recorded is gone by next session, and you will open the
  next one blind. This is the one most easily forgotten.
  Write the summary as STANDING STATE, not as a moment. You are shown it again on every
  later turn, so anything phrased as an arrival gets re-narrated forever. Third person,
  present tense, no "you", no verbs of entering. Staging only: what is here, who is
  present, what is reachable, what is unresolved.
      Good: "Half-empty common room, rain on the shutters. Barkeep watches the door.
             Back stairs to the rooms; cellar hatch behind the bar."
      Bad:  "You push open the door and the smell of wet wool hits you."
  Do not put the time of day in it — the clock above is authoritative. Do not restate NPC
  attitudes or quest status; those are recorded separately and would drift.
- The moment you give someone a name, call upsert_npc. A named character who lives only
  in your prose is forgotten, and you will contradict yourself about them in three
  sessions. Set disposition when their attitude is clear.
- Call record_note whenever you put information in an NPC's mouth, or draw attention to
  something nobody asked about. Those are plants: you invented them for a reason, and the
  reason is what you will have forgotten in three sessions while the player has not.
  Concretely, record it when you write —
      gossip, rumour, or news of something offscreen        kind=clue
      a person or object noticed as watchful or out of place kind=foreshadow
      a debt, a favour, a deadline, a threat made            kind=promise
      a lie you told, or a name you gave a place             kind=general
  Subject is what a player would say to bring it up again: "the harbour master", "the
  cloaked figure". Do not wait until it matters — by then it is already contradicted.
- When time passes outside combat, call advance_time. "Meaningful" is not a judgement
  call — use these anchors:
      searching a room 10 · a conversation 5 · crossing a town 30
      a night's watch 120 · overland travel by the hour
  Under a minute is not worth recording. For rests use request_rest instead, which moves
  the clock and posts the Avrae command together.
- When the party rests, call request_rest ONCE — not once per character. A rest resets
  each player's sheet individually, but there is only one night: calling it four times
  for four characters burns four nights and expires everyone's effects four times over.
Record while you are narrating. Do NOT issue these alongside open_check: once you ask for
a roll, the turn ends and anything else in it is discarded.

BOOKKEEPING IS YOURS, NOT THE PLAYERS':
- You hold inventory, time and world state. Record them with the tools.
- You do NOT hold coin. Avrae and the character sheet own it. Name a price in the
  fiction — "two copper, or a silver for the table" — then call request_payment so the
  player has a command to run. Their purse refuses an overspend on its own, so never
  assert what someone can or cannot afford, and never claim a payment happened: you are
  not told the result.
- Never tell a player to note something on their sheet, track something themselves, or
  remember something for you. If it is worth tracking, call the tool and say nothing
  about it. Avrae owns the character sheet; you own the world.

VOICE — atmospheric, concise, second person. End on a hook or a question.
"""


# -- injection defence --------------------------------------------------------------------

# v1 marked trusted data with a literal "[SYSTEM: ...]" prefix in a flat text stream --
# a prefix any player could type, while the model held state-mutating tools. Trust now
# rides on the transport (structured parts), and these patterns are stripped as defence
# in depth.
# The trailing [a-z_]* matters: "[tool_result]" and "[function_call: ...]" must be caught
# too, and \b alone stops at the underscore.
_IMPERSONATION = re.compile(
    r"\[\s*(?:system|tool|function|assistant|user|model|dm|gm)[a-z_]*\b[^\]]*\]",
    re.IGNORECASE,
)
_FENCE = re.compile(r"^\s*(<\|.*?\|>|```+\s*(system|tool))", re.IGNORECASE | re.MULTILINE)


def sanitize_player_text(text: str, max_chars: int = 2000) -> str:
    """Strip role-impersonation markers from player input.

    Not a security boundary on its own -- the structured transport is. This is the second
    layer, and it also stops players confusing the model by accident.
    """
    cleaned = _IMPERSONATION.sub("", text)
    cleaned = _FENCE.sub("", cleaned)
    cleaned = cleaned.replace("\x00", "")
    return cleaned.strip()[:max_chars]


def attribute(display_name: str, actor: Optional[str], text: str) -> str:
    """Render player speech with explicit, unforgeable attribution."""
    who = f"{display_name} (playing {actor})" if actor else display_name
    return f"Player {who} says: {sanitize_player_text(text)}"


# -- layers -------------------------------------------------------------------------------


@dataclass
class Turn:
    role: str  # "user" | "model"
    text: str = ""
    parts: List[dict] = field(default_factory=list)  # structured function responses

    @property
    def approx_chars(self) -> int:
        return len(self.text) + sum(len(str(p)) for p in self.parts)


def render_live_state(state: Dict[str, Any]) -> str:
    """Layer 2: a compact render of campaign state. Rebuilt every call."""
    lines = ["CURRENT WORLD STATE (authoritative — prefer this over anything in history):"]
    lines.append(f"- Time: {format_world_time(state)}")

    scene = state.get("scene") or {}
    if scene.get("location"):
        lines.append(f"- Location: {scene['location']}")
    if scene.get("summary"):
        lines.append(f"- Scene: {scene['summary']}")

    if state.get("encounter"):
        lines.append(
            "- COMBAT ACTIVE: Avrae owns hit points, conditions, and durations. "
            "World time is frozen until initiative ends."
        )

    party = state.get("party") or {}
    if party:
        lines.append("- Party:")
        for actor, data in sorted(party.items()):
            # Coin is deliberately absent: Avrae owns it (§3). A stale number inside a
            # block headed "authoritative" is worse than no number at all.
            inventory = data.get("inventory") or []
            carried = "carrying " + ", ".join(inventory[:8]) if inventory else "carrying nothing"
            lines.append(f"    - {actor}: {carried}")

    effects = state.get("effects") or []
    if effects and not state.get("encounter"):
        lines.append("- Active effects:")
        for e in effects:
            lines.append(f"    - {e['actor']}: {e['name']} (until minute {e['expires_at_minute']})")

    quests = {k: v for k, v in (state.get("quests") or {}).items() if v.get("status") != "done"}
    if quests:
        lines.append("- Open quests:")
        for qid, q in sorted(quests.items()):
            lines.append(f"    - {q.get('title', qid)} [{q.get('status', 'open')}]")

    return "\n".join(lines)


def render_memory(notes: Sequence[Dict[str, Any]], summary: str, subjects: Sequence[str]) -> str:
    """Layer 3: rolling summary plus notes relevant to who and where we are."""
    blocks: List[str] = []
    if summary:
        blocks.append("STORY SO FAR:\n" + summary)

    wanted = {s.lower() for s in subjects}
    relevant = [
        n for n in notes if not wanted or (n.get("subject", "").lower() in wanted)
    ]
    if relevant:
        rendered = "\n".join(
            f"- [{n.get('kind', 'note')}] {n.get('subject', '')}: {n.get('body', '')}"
            for n in relevant[-25:]
        )
        blocks.append("RELEVANT MEMORY:\n" + rendered)
    return "\n\n".join(blocks)


@dataclass(frozen=True)
class AssembledContext:
    system_instruction: List[str]
    turns: List[Turn]
    approx_tokens: int
    dropped_turns: int
    dropped_memory: bool


def assemble(
    *,
    state: Dict[str, Any],
    history: Sequence[Turn],
    notes: Sequence[Dict[str, Any]] = (),
    summary: str = "",
    subjects: Sequence[str] = (),
    recent_turns: int = DEFAULT_RECENT_TURNS,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> AssembledContext:
    """Build one call's context, trimming layer 4 then layer 3 to fit the budget."""
    live = render_live_state(state)
    memory = render_memory(notes, summary, subjects)

    fixed_chars = len(DM_SYSTEM_INSTRUCTION) + len(live)
    budget_chars = token_budget * CHARS_PER_TOKEN

    window = list(history[-recent_turns:])
    dropped = len(history) - len(window)

    def total() -> int:
        return fixed_chars + len(memory) + sum(t.approx_chars for t in window)

    # Layer 4 first: recent turns are the cheapest thing to lose, because anything load-
    # bearing in them should already be in state or memory.
    while window and total() > budget_chars:
        window.pop(0)
        dropped += 1

    dropped_memory = False
    if memory and total() > budget_chars:
        memory = ""
        dropped_memory = True

    system = [DM_SYSTEM_INSTRUCTION, live]
    if memory:
        system.append(memory)

    return AssembledContext(
        system_instruction=system,
        turns=window,
        approx_tokens=total() // CHARS_PER_TOKEN,
        dropped_turns=dropped,
        dropped_memory=dropped_memory,
    )
