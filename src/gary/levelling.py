"""Level Up (Dungeon World, "Level Up"): what a character may choose, checked like a Draft.

    When you have downtime (hours or days) and XP equal to (or greater than) your
    current level+7, subtract your current level+7 from your XP, increase your level by
    1, and choose a new advanced move from your class. If you are the wizard, you also
    get to add a new spell to your spellbook. Choose one of your stats and increase it by
    1 (this may change your modifier). Changing your Constitution increases your maximum
    and current HP. Ability scores can't go higher than 18.

Advanced moves are recorded on the sheet (``advanced_moves``) and shown to the model in
the book's words, like the narrative starting moves. Most change something only the
fiction tracks; the few that change a number Gary tracks get wired up one at a time.
A move that names another ("Choose one spell", "Get one move from another class") is
recorded with its text, and the choice is the table's to honour.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import spells
from .class_moves import TEXTS
from .sheets import STAT_NAMES, STATS, SheetError, xp_needed

MAX_LEVEL = 10
MAX_SCORE = 18


def ready(sheet: Dict[str, Any]) -> bool:
    return int(sheet.get("xp", 0)) >= xp_needed(sheet) and int(sheet.get("level", 1)) < MAX_LEVEL


def advanced_moves(sheet: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The advanced moves this character has taken, with the book's text."""
    book = {m["name"]: m for m in TEXTS.get(sheet.get("class", "").lower(), {}).get("advanced", ())}
    return [book[n] for n in sheet.get("advanced_moves") or () if n in book]


def move_options(sheet: Dict[str, Any], level: int) -> List[Dict[str, Any]]:
    """What can be taken on reaching ``level``: the 2-5 list, and from 6 the 6-10 list too;
    nothing already taken or replaced, and nothing whose requirement is not met."""
    taken = set(sheet.get("advanced_moves") or ())
    replaced = {m["replaces"] for m in advanced_moves(sheet) if m.get("replaces")}
    out = []
    for m in TEXTS.get(sheet.get("class", "").lower(), {}).get("advanced", ()):
        if m["name"] in taken or m["name"] in replaced:
            continue
        if m["tier"] == "6-10" and level < 6:
            continue
        if m.get("requires") and m["requires"] not in taken:
            continue
        out.append(m)
    return out


class Advance:
    """One level gained, chosen step by step: a stat, an advanced move, and -- for a
    Wizard -- a spell for the spellbook. ``result()`` is the new sheet."""

    def __init__(self, sheet: Dict[str, Any]) -> None:
        if not ready(sheet):
            if int(sheet.get("level", 1)) >= MAX_LEVEL:
                raise SheetError(f"{sheet['name']} is level {MAX_LEVEL}, the highest there is.")
            raise SheetError(
                f"{sheet['name']} has {int(sheet.get('xp', 0))} XP and needs {xp_needed(sheet)}."
            )
        self.sheet = sheet
        self.level = int(sheet.get("level", 1)) + 1
        self.stat: Optional[str] = None
        self.move: Optional[str] = None
        self.spell: Optional[str] = None

    def step(self) -> Optional[str]:
        """"stat", "move", "spell", or None when done."""
        if self.stat is None:
            return "stat"
        if self.move is None:
            return "move"
        if spells.caster_class(self.sheet) == "wizard" and self.spell is None and self.spell_options():
            return "spell"
        return None

    def stat_options(self) -> List[str]:
        return [s for s in STATS if int(self.sheet["stats"][s]) < MAX_SCORE]

    def spell_options(self) -> List["spells.Spell"]:
        """"Whenever you gain a level, you add a new spell of your level or lower to your
        spellbook.\""""
        have = set(self.sheet.get("spellbook") or ())
        return [s for s in spells.SPELLS["wizard"]
                if 0 < s.level <= self.level and s.name not in have]

    def choose_stat(self, stat: str) -> None:
        if stat not in self.stat_options():
            raise SheetError(f"{STAT_NAMES.get(stat, stat)} can't go higher than {MAX_SCORE}."
                             if stat in STATS else f"There is no {stat!r} stat.")
        self.stat = stat

    def choose_move(self, name: str) -> None:
        if name not in [m["name"] for m in move_options(self.sheet, self.level)]:
            raise SheetError(f"{name} is not one of the moves you can take.")
        self.move = name

    def choose_spell(self, name: str) -> None:
        if name not in [s.name for s in self.spell_options()]:
            raise SheetError(f"{name} is not a spell you can add.")
        self.spell = name

    def result(self) -> Dict[str, Any]:
        if self.step() is not None:
            raise SheetError("Still choosing.")
        sheet = dict(self.sheet)
        sheet["xp"] = int(sheet.get("xp", 0)) - xp_needed(self.sheet)
        sheet["level"] = self.level
        sheet["stats"] = {**sheet["stats"], self.stat: int(sheet["stats"][self.stat]) + 1}
        if self.stat == "con":
            # Max HP is the class's base plus the Constitution score.
            sheet["max_hp"] = int(sheet["max_hp"]) + 1
            sheet["hp"] = int(sheet["hp"]) + 1
        chosen = next(m for m in move_options(self.sheet, self.level) if m["name"] == self.move)
        moves = [n for n in sheet.get("advanced_moves") or () if n != chosen.get("replaces")]
        sheet["advanced_moves"] = [*moves, self.move]
        if self.spell:
            sheet["spellbook"] = [*(sheet.get("spellbook") or ()), self.spell]
        return sheet

    def summary(self) -> str:
        parts = [f"level {self.level}", f"{STAT_NAMES[self.stat]} +1", self.move]
        if self.spell:
            parts.append(f"{self.spell} in the spellbook")
        return ", ".join(parts)
