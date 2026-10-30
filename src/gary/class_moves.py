"""Each class's starting moves, and what Gary does with each (Dungeon World, the classes).

Three kinds:
- **Rolled** -- a move in moves.py, rolled through make_move like the basic moves, by a
  character of that class only.
- **Narrative** -- no roll, or a rule Gary does not track. The model is shown the book's
  text every turn so it honours the move ("ask their player a question… they must answer
  truthfully").
- **Spells** -- Commune and Prepare Spells are the prepare_spells tool; Cast a Spell is
  rolled. The caster's spells are listed with their text (spells.py).
- **Not yet** -- shapeshifting and the animal companion, which are whole systems of their
  own. Shown with a note, so the model narrates around them rather than inventing how
  they work.

A few narrative moves also have a number Gary applies where it already runs the rules:
By Nature Sustained (no ration at camp), the Halfling Druid's +1d6 at camp, the Halfling
Thief's +2 ranged damage. Those live where the rule runs; here they are listed as
narrative, which is also true -- the model still needs to know them.

The text is the authors', extracted by scripts/extract_dw_class_moves.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import spells

_DATA = Path(__file__).parent / "data" / "class_moves.json"

ROLLED, NARRATIVE, NOT_YET, PREPARE = "rolled", "narrative", "not yet", "prepare"


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_DATA.read_text())["classes"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"Dungeon World class move data missing or unreadable at {_DATA} ({exc}). "
            "Regenerate it with scripts/extract_dw_class_moves.py."
        ) from exc


TEXTS = _load()

#: Starting move name -> the move key Gary rolls, or NOT_YET. Anything unlisted is
#: narrative.
_KINDS: Dict[str, Dict[str, str]] = {
    "bard": {"Arcane Art": "arcane_art"},
    "cleric": {"Turn Undead": "turn_undead", "Commune": PREPARE, "Cast a Spell": "cast_a_spell"},
    "druid": {"Shapeshifter": NOT_YET, "Studied Essence": NOT_YET},
    "fighter": {"Bend Bars, Lift Gates": "bend_bars"},
    "paladin": {"Lay on Hands (Cha)": "lay_on_hands", "I Am the Law": "i_am_the_law"},
    "ranger": {"Hunt and Track (Wis)": "hunt_and_track", "Called Shot": "called_shot",
               "Animal Companion": NOT_YET, "Command": NOT_YET},
    "thief": {"Trap Expert": "trap_expert", "Tricks of the Trade": "tricks_of_the_trade",
              "Backstab": "backstab"},
    "wizard": {"Prepare Spells": PREPARE, "Cast a Spell (Int)": "cast_a_spell"},
}


def moves_of(class_name: str, race: str) -> List[Dict[str, Any]]:
    """A character's starting moves: their race move first, then their class's.

    Each is {name, text, kind, key}: ``key`` is the make_move name for a rolled move.
    """
    cls = class_name.lower()
    book = TEXTS.get(cls)
    if book is None:
        return []
    out: List[Dict[str, Any]] = []
    if race in book["race"]:
        out.append({"name": f"{race} ({class_name})", "text": book["race"][race],
                    "kind": NARRATIVE, "key": None})
    for move in book["starting"]:
        mapped = _KINDS.get(cls, {}).get(move["name"])
        if mapped in (NOT_YET, PREPARE):
            kind, key = mapped, None
        elif mapped:
            kind, key = ROLLED, mapped
        else:
            kind, key = NARRATIVE, None
        out.append({"name": move["name"], "text": move["text"], "kind": kind, "key": key})
    return out


def render(sheets: Dict[str, Dict[str, Any]]) -> str:
    """What the model is shown about the party's moves. One block, every turn.

    Rolled moves by name and make_move key only -- their rules are in make_move. The rest
    in the book's words, since honouring them is the model's job.
    """
    if not sheets:
        return ""
    lines = ["THE PARTY'S CLASS MOVES (theirs alone; honour them):"]
    for actor, sheet in sorted(sheets.items()):
        if "class" not in sheet or sheet.get("dead"):
            continue
        moves = moves_of(sheet["class"], sheet.get("race", ""))
        if not moves:
            continue
        lines.append(f"- {actor} ({sheet.get('race', '')} {sheet['class']}):")
        if sheet.get("alignment"):
            from .sheets import alignment_line

            lines.append(f"    - {alignment_line(sheet)}")
        for m in moves:
            if m["kind"] == ROLLED:
                lines.append(f"    - {m['name']}: call make_move with move={m['key']}.")
            elif m["kind"] == PREPARE:
                lines.append(f"    - {m['name']}: when they spend an undisturbed hour at it, "
                             "call prepare_spells.")
            elif m["kind"] == NOT_YET:
                lines.append(
                    f"    - {m['name']}: not yet playable through the rules engine. If they "
                    "try it, narrate an effect modest enough to need no roll, or tell them "
                    "it is not ready yet."
                )
            else:
                text = m["text"].replace("\n", " ")
                lines.append(f"    - {m['name']}: {text}")
        book = {m["name"]: m for m in TEXTS.get(sheet["class"].lower(), {}).get("advanced", ())}
        for name in sheet.get("advanced_moves") or ():
            if name in book:
                text = book[name]["text"].replace("\n", " ")
                lines.append(f"    - {name} (advanced): {text}")
        if spells.caster_class(sheet):
            have = spells.castable(sheet)
            lines.append(f"    - Spells {actor} can cast now (make_move cast_a_spell, spell=…):"
                         + ("" if have else " none."))
            for spell in have:
                text = spell.text.replace("\n", " ")
                lines.append(f"        - {spell.label()}: {text}")
    return "\n".join(lines) if len(lines) > 1 else ""


def owner_classes(key: str) -> Optional[tuple]:
    """The classes that roll this move, or None if everyone does."""
    from .moves import MOVES

    move = MOVES.get(key)
    return move.classes if move and move.classes else None
