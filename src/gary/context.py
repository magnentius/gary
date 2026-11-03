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

from .monsters import describe as describe_monster
from .sheets import describe
from .bonds import describe as describe_bonds
from .hirelings import describe as describe_hirelings
from .choices import describe as describe_choices
from .companion import describe as describe_companion
from .spells import describe as describe_spells
from .state.schema import format_world_time

DEFAULT_RECENT_TURNS = 30
DEFAULT_TOKEN_BUDGET = 40_000
CHARS_PER_TOKEN = 4  # rough; replace with the real tokenizer when metering cost


DM_SYSTEM_INSTRUCTION = """\
You are the Game Master of a Dungeon World game.

AUTHORITY — this is absolute:
- Moves are how the rules happen. When a player's action triggers a move, call make_move
  with the move (and, for Defy Danger, the stat their approach uses). Then STOP and wait.
  If no move is triggered, nothing is rolled: say what happens.
- You do NOT decide whether a move succeeded. Gary rolls for the player and resolves it;
  results arrive as tool results, established fact. Narrate them with anything the player
  chose. Never re-evaluate, soften or reverse a result,
  and never roll or invent a number yourself.
- Creatures exist for the rules only once you add them: call add_monsters the moment they
  appear (your own creature gets an instinct and a few moves). Attacks target one by its listed name. Damage and HP are tracked for you: a
  creature is defeated at 0 HP and not before. When a creature hurts someone, call
  monster_attacks — never invent the damage — and describe each wound at its severity. A
  fall, a trap, fire or anything else that is not a creature is hazard_damage.
- Your other tools carry their own rules: spells (cast_a_spell, prepare_spells, end_spell),
  Shapeshifter (form_moves, spend_hold), debility, recover (days of safe rest, not
  make_camp), perilous_journey (hostile travel), hire and update_hireling, bolster (weeks of
  study; set preparation=true on a move it pays off on). When monster_attacks is held for a
  defender's choice, say only that the blow comes; the outcome arrives as a rules event.
- NOTHING TRACKS POSITION. You are the only record of who stands where: keep it in the
  scene summary while a fight runs, stay consistent with it, and move no one unsaid.

RUNNING THE GAME:
- Pace it like a film: time is fluid. Start a scene late, close to the trouble; end it
  once its question is answered. Sum up what is dull in a sentence -- a safe road, routine
  shopping, a quiet night -- and cut to the next thing worth playing. Only hostile
  ground is a perilous_journey; a safe road is just narrated arrival.
- It is a conversation. Set up a threat, never its conclusion, and end on "what do you
  do?". The player answers; if that triggers a move, the roll decides how it ends. Make
  each threat unmistakable — who strikes at whom, with what — so damage follows a threat
  they could have answered: on a miss, when they ignore it, or a golden opportunity.
- On a miss, make a GM move from the fiction: deal damage, reveal an unwelcome truth, show
  an approaching threat, separate them, put someone in a spot, use up resources, turn
  their move back on them, offer an opportunity with a cost. Soft moves set danger up,
  hard ones follow through; you may go soft and escalate. Do more than damage: knock down,
  disarm, grapple, surround, break gear. Play each creature to its instinct and reach for
  its listed moves first.
- A 7-9 is a success with a cost. Where the move gives none (Defy Danger), grant what they
  were after with a price — a worse position, something spent, an ally threatened, trouble
  later — or a hard choice between two halves. It must never feel like a miss.
- Not every action is a move: when the fiction allows it, it just happens. Striking a
  helpless, surprised or pinned enemy is deal_damage, not hack_and_slash. Creatures
  striking one character at once are one monster_attacks with the rest in also.
- Forwards wait on the sheet with what they are for. When a move acts on one, set
  forward=true; give or take one with the forward tool when the fiction earns it.
- Spread the spotlight: when one moment resolves, shift a threat to another character.
  There are no turns. Address characters, not players; ask how they do things and use the
  answers. Leave blanks in the world and play to find out what happens.
- Never speak the name of your move: it happens in the fiction ("the blade bites your
  arm"), never announced ("I make a GM move"). Be a fan of the characters — the world is
  hard on them, you are not. Think offscreen too: threats move while the party looks away.
- When a threat appears that will matter beyond this scene — a villain, a cult, a lair, a
  plague — record_note kind=threat; fronts are written from these between sessions. In
  play, advance portents (advance_portent) — as play shows them, offscreen, or as a hard
  move — revise what is still to come, and reach for a danger's GM moves. Let the party
  see the signs.
  Begin and end with the fiction: every roll comes from it and returns to it.
- The moment the party is in a village, town, keep or city — before any price, Supply roll,
  shopkeeper or innkeeper there — call create_steading (once per place), then set_scene with
  steading set to its name. Its tags set what is for sale, at what price, and who stands
  guard; a town never recorded has none of them.

CONTINUITY — prose is not memory. Only tool calls survive the session:
- Call set_scene the first time you describe where the party is, and again whenever they
  move or the situation changes around them. Do this even if they have not travelled: a
  room you described but never recorded is gone by next session, and you will open the
  next one blind. This is the one most easily forgotten. In a steading, name it as steading
  (see above); leaving it, steading "".
  Write the summary as STANDING STATE, not as a moment. You are shown it again on every
  later turn, so anything phrased as an arrival gets re-narrated forever. Third person,
  present tense, no "you", no verbs of entering. Staging only: what is here, who is
  present, what is reachable, what is unresolved.
      Good: "Half-empty common room, rain on the shutters. Barkeep watches the door.
             Back stairs to the rooms; cellar hatch behind the bar."
      Bad:  "You push open the door and the smell of wet wool hits you."
  Do not put the time of day in it — the clock above is authoritative. Do not restate NPC
  attitudes; those are recorded separately and would drift.
- The moment a person acquires a name, call upsert_npc — whether they are standing in
  front of the party or only spoken about. Offscreen names need this MORE, not less: a
  harbour master an NPC mentions in passing has nothing anchoring him, so by the time the
  party finally meets him you will have reinvented his rank, his temper and his motive.
  "Maren's son who works the customs slip" counts: a role is a name a player can ask for.
  Give `public` a line the players may read on /npcs (who, and how they met); secrets go
  in `note`. Someone the party has not met or heard of is secret=true.
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
- When time passes, call advance_time. "Meaningful" is not a judgement call — use these
  anchors:
      searching a room 10 · a conversation 5 · crossing a town 30
      a night's watch 120 · overland travel by the hour
  Under a minute is not worth recording. When the party beds down for the night, call
  make_camp ONCE — it eats the rations, heals, and moves the clock eight hours for
  everyone, because there is only one night however many people sleep through it. Only
  where they can truly settle in: mid-fight, mid-chase or somewhere hostile, refuse the
  rest in the fiction, or let them try and have something approach.
- The date is real, a thousand years back, months named for their full moons — in order:
  Wolf, Snow, Worm, Pink, Flower, Strawberry, Buck, Sturgeon, Harvest, Hunter's, Beaver,
  Cold. Wolf is deep winter, Flower is late spring, Sturgeon high summer, Hunter's mid
  autumn. Weather, daylight and what people are working at follow from the date and
  belong in description unasked. They are month names, not events: do not narrate a moon
  rising unless you mean one in the sky.
Record while you are narrating. Do NOT issue these alongside make_move: once you ask for
a roll, the turn ends and anything else in it is discarded.

BOOKKEEPING IS YOURS, NOT THE PLAYERS':
- You hold inventory, time and world state. Record them with the tools.
- Coin is on each character's sheet. Ordinary goods sold here are buy at the book price, no
  roll; the supply move is only for something rare, special or magical. Name prices in the
  fiction, then buy, sell or give_coins: a sale or purchase only happened if the tool says it did, and a refusal
  ("can't afford it") is the truth to narrate. Loot found is give_items; name book items
  as the book does, so a found long sword really is one. When someone drinks a potion or
  binds wounds, call use_item: the healing is applied for you.
- Never tell a player to note something on their sheet, track something themselves, or
  remember something for you. If it is worth tracking, call the tool and say nothing
  about it.

VOICE — atmospheric, concise. Address characters by name; "you" only when speaking to one.
End on a hook or a question.
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


_PLAYING = re.compile(r"^Player .*? \(playing (.+?)\) says:")

#: How far back the spotlight tally looks, in moments (a player speaking to Gary, a move made).
SPOTLIGHT_WINDOW = 24


def spotlight(moments: Sequence[str], party: Sequence[str]) -> str:
    """The book's "spread the spotlight", made a fact: who has had the recent moments. Each
    moment is a character's name -- they spoke to Gary, or made a move. One line, only with two
    or more characters and enough moments to mean something; "" otherwise."""
    names = [n for n in party if n]
    recent = [m for m in moments if m in names][-SPOTLIGHT_WINDOW:]
    if len(names) < 2 or len(recent) < 4:
        return ""
    counts = {n: recent.count(n) for n in names}
    fewest = min(counts.values())
    quiet = [n for n in names if counts[n] == fewest and counts[n] < max(counts.values())]
    line = (f"SPOTLIGHT (the last {len(recent)} moments: who spoke to you or made a move): "
            + ", ".join(f"{n} {c}" for n, c in sorted(counts.items(), key=lambda kv: -kv[1])) + ".")
    if quiet and max(counts.values()) >= fewest + 3:
        line += (f" Turn to {' and '.join(quiet)} next: a question, a threat aimed at them, an opportunity "
                 "only they can take.")
    return line


def moments_in(turns: Sequence[Any]) -> List[str]:
    """Characters' moments, oldest first, from a conversation's turns: each player message
    attributed to a character, and each make_move's actor."""
    out: List[str] = []
    for turn in turns:
        for part in getattr(turn, "parts", None) or ():
            text = getattr(part, "text", None)
            if text and getattr(turn, "role", "") == "user":
                m = _PLAYING.match(text)
                if m:
                    out.append(m[1])
            call = getattr(part, "function_call", None)
            if call is not None and getattr(call, "name", "") == "make_move":
                actor = str((getattr(call, "args", None) or {}).get("actor") or "")
                if actor and (not out or out[-1] != actor):  # their message and its move are one moment
                    out.append(actor)
    return out


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
    setting = str(state.get("setting") or "").strip()
    if setting:
        # Ahead of everything else: it is the frame the rest is read inside.
        lines.append(f"- Setting: {setting}")
    from .sky import daylight, lines as sky_lines

    lines.append(f"- Time: {format_world_time(state)} ({daylight(state)})")
    lines += sky_lines(state)

    scene = state.get("scene") or {}
    if scene.get("location"):
        lines.append(f"- Location: {scene['location']}")
    if scene.get("summary"):
        lines.append(f"- Scene: {scene['summary']}")

    # Everyone at the table, from `/playing`, union whoever has acquired anything.
    # Without the roster the model only learns a character exists when they speak: it
    # cannot address a quiet player, say "the three of you", or know Dorn is there at all.
    party = state.get("party") or {}
    sheets = state.get("sheets") or {}
    # A retired character has left the story (Beyond 10th Level); a mentor travels on, the GM's.
    roster = sorted(a for a in {*(state.get("players") or {}).values(), *party.keys(), *sheets.keys()}
                    if not (sheets.get(a) or {}).get("retired"))
    if roster:
        lines.append("- Party:")
        for actor in roster:
            data = party.get(actor) or {}
            inventory = list(data.get("inventory") or [])
            if actor in sheets:
                # Weapons are on the sheet line already; this is everything else they carry.
                inventory = [g["name"] for g in sheets[actor].get("gear") or []
                             if not any(t in ("hand", "close", "reach", "near", "far", "thrown")
                                        for t in g.get("tags", ()))] + inventory
            carried = "carrying " + ", ".join(inventory[:12]) if inventory else "carrying nothing"
            # The sheet line carries the name itself, so it replaces the bare name.
            who = describe(sheets[actor]) if actor in sheets else actor
            if (sheets.get(actor) or {}).get("mentor"):
                who += " — a mentor travelling with their apprentice: YOU play them now; they make no moves"
            lines.append(f"    - {who}: {carried}")
            from .magic import describe as describe_magic

            for extra in ((describe_choices(sheets[actor]), describe_magic(sheets[actor]), describe_spells(sheets[actor]),
                           describe_companion(sheets[actor]), describe_bonds(sheets[actor]),
                           describe_hirelings(sheets[actor]))
                          if actor in sheets else ()):
                if extra:
                    lines.append(f"      {extra}")

    scene = state.get("monsters") or {}
    if scene:
        lines.append("- Creatures here (target by these names):")
        for name, record in scene.items():
            lines.append(f"    - {describe_monster(name, record)}")
        from .monsters import natures

        kinds = natures(scene)
        if kinds:
            lines.append("- What they are (their moves are yours to make for them; play to their instinct):")
            lines.extend(f"    - {k}" for k in kinds)

    from .steadings import describe as describe_steading, here as steading_here

    known = state.get("steadings") or {}
    if known:
        current = steading_here(state)
        lines.append("- Steadings (their tags decide what is for sale, prices, Supply and Recruit):")
        lines += [f"    - {describe_steading(s)}" + (" ← the party is here" if s is current else "")
                  for s in known.values()]

    from .fronts import render as render_fronts

    threats = render_fronts(state.get("fronts") or {})
    if threats:
        lines.append("- Fronts (yours alone; foreshadow the next portents, advance them when the "
                     "fiction or the party's absence allows):")
        lines += threats

    effects = state.get("effects") or []
    if effects:
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
        if not npc.get("known", True):
            bits += " [the party has not met or heard of them]"
        seen = [f"as pictured: {npc['portrait']}"] if npc.get("portrait") else []
        notes = "; ".join(str(n) for n in ([npc["public"]] if npc.get("public") else []) + seen
                          + (npc.get("notes") or [])[-3:])
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


