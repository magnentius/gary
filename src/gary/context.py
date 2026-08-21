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

from .encounters import benchmark
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
- When a fight starts, call start_combat BEFORE anyone swings. Avrae tracks turn order,
  hit points and durations only once combat has begun: a fight started without it has no
  rounds, and the enemies never get to act.
- NOTHING TRACKS POSITION. Avrae has no map, no distances and no notion of who is next to
  whom, and neither does Gary. You are the only record of where anyone is standing, so:
  keep it in the scene summary while a fight runs — who is where, what is between them,
  roughly how far. Then stay consistent with it: do not let someone in the doorway stab
  a target across the room, and do not move people without saying so. When you attack,
  say whether it is `ranged`; that is the only way anything can know.
- Conditions do not exist until you apply them. Avrae tracks poisoned, restrained,
  frightened, prone and the rest faithfully once told, and never infers one — a character
  knocked to 0 HP is not marked Prone unless you say so. If you narrate a condition and do
  not call apply_effect, it is fiction only and nobody can see it.
- In combat, someone attacking is the `attack` tool, never open_check. An attack resolves
  against the target's AC, which Avrae owns — it rolls, compares and applies the damage
  itself. You commit no DC and decide no outcome; inventing an AC would be deciding one.
  Target by the tracker name, "NO1", not "Vane".
- A character at 0 HP is unconscious and dying, not dead. On their turn they roll a death
  save: open_check with kind=save, key=death. Three successes stabilise, three failures
  kill, and Avrae counts them. Narrate the moment; never decide whether they live.
- When initiative reaches a creature you control, narrate what it does and call
  enemy_attack. Monsters do not act on their own: Avrae pings whoever added them, and
  nothing else in the fight will make them move. A fight where only the players ever act
  is not a fight.
- During combat, Avrae owns hit points, conditions, and durations. Describe them; do not
  contradict or recalculate them. The world clock is frozen until combat ends.

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
- The moment a person acquires a name, call upsert_npc — whether they are standing in
  front of the party or only spoken about. Offscreen names need this MORE, not less: a
  harbour master an NPC mentions in passing has nothing anchoring him, so by the time the
  party finally meets him you will have reinvented his rank, his temper and his motive.
  "Maren's son who works the customs slip" counts: a role is a name a player can ask for.
  Set disposition when their attitude is clear — someone who lowers their voice and tells
  you what their son saw is not neutral.
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
- Before putting more than one creature in front of the party, or anything you suspect is
  large, call weigh_encounter and name every creature. It looks their CR up in the SRD and
  does the sum, so do not supply CRs from memory — 5.2.1 revised many of them, and six
  CR 1/4s is exactly the arithmetic that goes wrong unnoticed. Deadly is a legitimate
  choice; make it deliberately, not by mistake.
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
        combat = state.get("combat") or {}
        fighters = combat.get("combatants") or []
        if fighters:
            round_number = combat.get("round")
            lines.append(
                "- Initiative"
                + (f" (round {round_number})" if round_number is not None else "")
                + " — target by these names:"
            )
            for c in fighters:
                marks = []
                if c.get("hp"):
                    marks.append(str(c["hp"]))
                if c.get("conditions"):
                    marks.append(", ".join(str(x) for x in c["conditions"]))
                if c.get("dying"):
                    marks.append("DOWN")
                turn = " <- their turn" if c.get("current_turn") else ""
                lines.append(
                    f"    - {c.get('name', '?')}"
                    + (f" [{'; '.join(marks)}]" if marks else "")
                    + turn
                )

    # Everyone at the table, from `?dm playing`, union whoever has acquired anything.
    # Without the roster the model only learns a character exists when they speak: it
    # cannot address a quiet player, say "the three of you", or know Dorn is there at all.
    party = state.get("party") or {}
    roster = sorted({*(state.get("players") or {}).values(), *party.keys()})
    if roster:
        lines.append("- Party:")
        for actor in roster:
            # Coin is deliberately absent: Avrae owns it (§3). A stale number inside a
            # block headed "authoritative" is worse than no number at all.
            data = party.get(actor) or {}
            inventory = data.get("inventory") or []
            carried = "carrying " + ", ".join(inventory[:8]) if inventory else "carrying nothing"
            # Class and level as `!sheet` last reported them — this is what encounter
            # difficulty is judged from, and Gary has no other way to know it.
            sheet = str(data.get("sheet") or "").strip()
            who = f"{actor} ({sheet})" if sheet else actor
            lines.append(f"    - {who}: {carried}")

        bench = benchmark(party, roster)
        if bench is not None:
            gauge = (
                f"    - Encounter gauge: total party level {bench.total_level}; an "
                f"encounter may be deadly above CR {bench.deadly_total_cr:g} total, or a "
                f"single monster above CR {bench.deadly_single_cr:g}. A loose gauge, not "
                "a rule."
            )
            if bench.partial:
                gauge += (
                    f" ({bench.known} of {bench.characters} levels known — the rest have "
                    "not run !sheet, so the total is understated.)"
                )
            lines.append(gauge)

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


# Words too common to identify anything on their own.
_SUBJECT_STOPWORDS = frozenset(
    "the a an and of at in on to for with his her their its that this some my your".split()
)


def _subject_terms(subject: str) -> List[str]:
    """The whole subject, plus each word distinctive enough to stand for it.

    A note filed under "Toby Cole" has to answer to "Toby", because that is what a player
    says. Requiring the full filed name meant retrieval worked only when someone happened
    to use it — which they do not.

    A word qualifies at four characters, or three if it was capitalised in the subject: a
    short proper noun ("Vex") is distinctive, a short common word ("son", "boy") is not.
    """
    subject = subject.strip()
    terms = [subject] if len(subject) >= 3 else []
    for word in re.findall(r"[A-Za-z][A-Za-z']*", subject):
        if word.lower() in _SUBJECT_STOPWORDS:
            continue
        if len(word) >= 4 or (len(word) >= 3 and word[0].isupper()):
            terms.append(word)
    return terms


def _mentions(subject: str, text: str) -> bool:
    """Is this note's subject named in the text? Whole-word, case-insensitive.

    Substring matching would fire "Vane" inside "vanish"; word boundaries keep a note
    about a harbour master out of a sentence about weathervanes.
    """
    return any(
        re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE)
        for term in _subject_terms(subject)
    )


def _render_people(npcs: Dict[str, Any], subjects: Sequence[str], mentioned_in: str) -> str:
    """NPCs who have just come up, with what is known about them.

    ``npcs`` was written by ``upsert_npc`` and rendered by nothing: neither layer 2 nor
    layer 3 touched it, so a cast with names, dispositions and history was invisible to the
    model that recorded it. Retrieved on mention rather than listed wholesale, because the
    cast only grows and most of it is not in the room.
    """
    wanted = {s.lower() for s in subjects}
    lines = []
    for npc in npcs.values():
        name = str(npc.get("name") or "").strip()
        if not name:
            continue
        if name.lower() not in wanted and not (mentioned_in and _mentions(name, mentioned_in)):
            continue
        disposition = npc.get("disposition")
        bits = f"{name}"
        if isinstance(disposition, int):
            bits += f" (disposition {disposition:+d})"
        notes = "; ".join(str(n) for n in (npc.get("notes") or [])[-3:])
        lines.append(f"- {bits}" + (f": {notes}" if notes else ""))
    return "PEOPLE:\n" + "\n".join(lines) if lines else ""


def render_memory(
    notes: Sequence[Dict[str, Any]],
    summary: str,
    subjects: Sequence[str],
    mentioned_in: str = "",
    npcs: Optional[Dict[str, Any]] = None,
) -> str:
    """Layer 3: rolling summary plus notes relevant to who and where we are.

    ``subjects`` is the standing context — party members and the current location.
    ``mentioned_in`` is what was just said, and it is what makes the tool's promise to the
    model true: "retrieved into your context when the subject reappears". Matching only on
    ``subjects`` meant a note about the harbour master could never surface, because he is
    neither a party member nor a place, so notes were written and never read back.
    """
    blocks: List[str] = []
    if summary:
        blocks.append("STORY SO FAR:\n" + summary)

    wanted = {s.lower() for s in subjects}
    if not wanted and not mentioned_in:
        # No signal at all — an empty campaign, or a call with nothing to match on. Show
        # everything rather than nothing; this is the original behaviour and only applies
        # when there is genuinely nothing to filter by.
        relevant = list(notes)
    else:
        relevant = [
            n
            for n in notes
            if n.get("subject", "").lower() in wanted
            or (mentioned_in and _mentions(n.get("subject", ""), mentioned_in))
        ]
    people = _render_people(npcs or {}, subjects, mentioned_in)
    if people:
        blocks.append(people)

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
