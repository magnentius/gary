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
Chosen One, Anointed, Prodigy and Master name a spell as they are taken (``lowered_spells``:
it counts one level lower, so a first-level one becomes a rote or cantrip), and Expanded
Spellbook adds a spell from any class's list. A move that names another class's move
("Get one move from another class") is recorded with its text, and the choice is the
table's to honour.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import compendium, multiclass, spells
from .class_moves import TEXTS
from .sheets import STAT_NAMES, STATS, SheetError, xp_needed

MAX_LEVEL = 10
MAX_SCORE = 18

#: Advanced moves that need a spell chosen as they are taken: what the choice does.
SPELL_MOVES = {
    "Chosen One": "lower",  # Cleric: "granted that spell as if it was one level lower"
    "Anointed": "lower",
    "Prodigy": "lower",  # Wizard: "You prepare that spell as if it were one level lower"
    "Master": "lower",
    "Expanded Spellbook": "spellbook",  # "a new spell from the spell list of any class"
}


def ready(sheet: Dict[str, Any]) -> bool:
    return int(sheet.get("xp", 0)) >= xp_needed(sheet) and int(sheet.get("level", 1)) < MAX_LEVEL


def advanced_moves(sheet: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The advanced moves this character has taken, with the book's text -- compendium
    moves included."""
    book = {m["name"]: m for m in TEXTS.get(sheet.get("class", "").lower(), {}).get("advanced", ())}
    return [book.get(n) or compendium.move(n) for n in sheet.get("advanced_moves") or ()
            if n in book or compendium.move(n)]


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
    # Compendium classes: a starting move "the next time you level up" after the requirement
    # is met, and its follow-on moves, which "count as class moves", ever after.
    return out + compendium.options(sheet)


class Advance:
    """One level gained, chosen step by step: a stat, an advanced move, and -- for a
    Wizard -- a spell for the spellbook. ``result()`` is the new sheet."""

    def __init__(self, sheet: Dict[str, Any]) -> None:
        if not ready(sheet):
            if int(sheet.get("level", 1)) >= MAX_LEVEL:
                raise SheetError(f"{sheet['name']} is level {MAX_LEVEL}. With {xp_needed(sheet)} XP they go beyond "
                                 "it: retire, take on an apprentice, or change class.")
            raise SheetError(
                f"{sheet['name']} has {int(sheet.get('xp', 0))} XP and needs {xp_needed(sheet)}."
            )
        self.sheet = sheet
        self.level = int(sheet.get("level", 1)) + 1
        self.stat: Optional[str] = None
        self.move: Optional[str] = None
        self.move_spell: Optional[str] = None  # what Chosen One, Prodigy... chose
        self.move_choice: Optional[str] = None  # Wraithlander's mark
        self.multi_class: Optional[str] = None  # Multiclass: the class, then the move
        self.multi_move: Optional[str] = None
        self.spell: Optional[str] = None

    def step(self) -> Optional[str]:
        """"stat", "move", "move_spell", "spell", or None when done."""
        if self.stat is None:
            return "stat"
        if self.move is None:
            return "move"
        if self.move in SPELL_MOVES and self.move_spell is None and self.move_spell_options():
            return "move_spell"
        if (self.move in compendium.MOVE_CHOICES or self.move == "Still Hungry") and self.move_choice is None \
                and self.move_choice_options():
            return "move_choice"
        if self.move in multiclass.GRANTING:
            if self.multi_class is None:
                return "multi_class"
            if self.multi_move is None:
                return "multi_move"
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

    def move_spell_options(self) -> List["spells.Spell"]:
        """The spells the chosen advanced move may name."""
        cls = spells.caster_class(self.sheet)
        if self.move not in SPELL_MOVES or cls is None:
            return []
        if SPELL_MOVES[self.move] == "spellbook":
            have = set(self.sheet.get("spellbook") or ())
            return [s for c in ("wizard", "cleric") for s in spells.SPELLS[c] if s.level > 0 and s.name not in have]
        taken = set(self.sheet.get("lowered_spells") or ())
        return [s for s in spells.SPELLS[cls] if s.level > 0 and s.name not in taken]

    def choose_move_spell(self, name: str) -> None:
        if name not in [s.name for s in self.move_spell_options()]:
            raise SheetError(f"{name} is not a spell {self.move} can take.")
        self.move_spell = name

    def move_choice_options(self) -> List[str]:
        """Wraithlander: the body part that bears the mark -- one not already marked. Still
        Hungry: "Choose an additional appetite" -- one they do not have."""
        if self.move == "Still Hungry":
            from .choices import appetites_of
            from .playbooks import appetites

            return [a for a in appetites() if a not in appetites_of(self.sheet)]
        have = set(self.sheet.get("wraith_marks") or ())
        return [c for c in compendium.MOVE_CHOICES.get(self.move, ()) if c not in have]

    def choose_move_choice(self, choice: str) -> None:
        if choice not in self.move_choice_options():
            raise SheetError(f"{choice} is not a choice {self.move} offers.")
        self.move_choice = choice

    def multi_class_options(self) -> List[str]:
        return [c for c in multiclass.classes(self.sheet, self.move)
                if multiclass.options(self.sheet, c, self.level, self.move)]

    def multi_move_options(self) -> List[Dict[str, Any]]:
        return multiclass.options(self.sheet, self.multi_class, self.level, self.move) if self.multi_class else []

    def choose_multi_class(self, cls: str) -> None:
        if cls not in self.multi_class_options():
            raise SheetError(f"{cls} is not a class you can take a move from.")
        self.multi_class = cls

    def choose_multi_move(self, name: str) -> None:
        if name not in [m["name"] for m in self.multi_move_options()]:
            raise SheetError(f"{name} is not a {self.multi_class} move you can take.")
        self.multi_move = name

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
        if self.move_spell and SPELL_MOVES[self.move] == "lower":
            sheet["lowered_spells"] = [*(sheet.get("lowered_spells") or ()), self.move_spell]
        elif self.move_spell:
            sheet["spellbook"] = [*(sheet.get("spellbook") or ()), self.move_spell]
        if self.spell:
            sheet["spellbook"] = [*(sheet.get("spellbook") or ()), self.spell]
        if self.move == "Unnatural Ally" and sheet.get("companion"):
            # "Give it +2 ferocity and +1 instinct, plus a new training" (the training is the table's).
            pet = sheet["companion"]
            sheet["companion"] = {**pet, "ferocity": int(pet["ferocity"]) + 2, "instinct": int(pet["instinct"]) + 1,
                                  "monster": True}
        if self.move == "Ambeoid Overlord":
            # "A piece of the thing that engulfed you is now yours to command."
            from .slimes import make as slime

            sheet["minions"] = [*(sheet.get("minions") or []), slime(f"{sheet['name']}'s slime")]
        if self.move == "Dovahkiin":
            sheet["blood"] = 1  # "You gain a new stat called Blood, which starts at +1."
        if self.move_choice and self.move == "Still Hungry":
            sheet["more_appetites"] = [*(sheet.get("more_appetites") or ()), self.move_choice]
        elif self.move_choice:
            sheet["wraith_marks"] = [*(sheet.get("wraith_marks") or ()), self.move_choice]
        # "...the next time you level up you may choose this move": a requirement met is
        # spent on this level whether or not its move was taken.
        if sheet.get("compendium_qualified"):
            sheet["compendium_qualified"] = []
        if self.move in spells.FAVOR_MOVES:
            sheet["favor_from"] = self.level  # a cleric of level 1 for spells, from here on
        if self.multi_move:
            sheet["multiclass_moves"] = [*(sheet.get("multiclass_moves") or ()),
                                         {"class": self.multi_class, "name": self.multi_move}]
        return sheet

    def summary(self) -> str:
        move = self.move + (f" ({self.move_spell})" if self.move_spell else "") + (
            f" ({self.move_choice})" if self.move_choice else "") + (
            f" ({self.multi_move}, from the {self.multi_class.title()})" if self.multi_move else "")
        parts = [f"level {self.level}", f"{STAT_NAMES[self.stat]} +1", move]
        if self.spell:
            parts.append(f"{self.spell} in the spellbook")
        return ", ".join(parts)
