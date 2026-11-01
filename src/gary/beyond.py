"""Beyond 10th Level (Dungeon World, "Leveling Up"):

    "Once you've reached 10th level things change a little. When you have enough XP to go to
    11th level instead you choose one of these:
      - Retire to safety
      - Take on an apprentice
      - Change entirely to a new class
    If you retire you create a new character to play instead and work with the GM to establish
    your place in the world. If you take on an apprentice you play a new character (the
    apprentice) alongside your current character, who stops gaining XP. Changing classes means
    keeping your ability scores, race, HP, and whatever moves you and the GM agree are core to
    who your character is. You lose all other class moves, replacing them with the starting
    moves of your new class."

Gary seats one character per player, so the apprentice is the player's from then on, and the
old character travels with them as a mentor the GM plays (``mentor``), gaining no XP. A
retired character (``retired``) leaves the party. Core moves kept through a change of class
become ``multiclass_moves`` from the old class, so they keep their text and what Gary
applies of them.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .sheets import CLASSES, SheetError, xp_needed

CHOICES = ("retire", "apprentice", "change")
LABELS = {"retire": "Retire to safety", "apprentice": "Take on an apprentice",
          "change": "Change entirely to a new class"}
MAX_KEPT = 3  # core moves kept through a change of class: the book leaves it to the table

#: What belongs to a class and goes with it when the class changes.
_CLASS_STATE = ("spellbook", "prepared", "preparing", "spells", "lowered_spells", "favor_from", "companion",
                "poison_uses", "shape", "balance", "quest", "sworn_enemy", "challenged", "more_appetites",
                "resolved_appetites", "minions", "ooze", "blood", "faraday", "wraith_marks")


def ready(sheet: Dict[str, Any]) -> bool:
    return (int(sheet.get("level", 1)) >= 10 and int(sheet.get("xp", 0)) >= xp_needed(sheet)
            and not sheet.get("retired") and not sheet.get("mentor"))


def out_of_play(sheet: Dict[str, Any]) -> bool:
    """Retired, or a mentor the GM plays: no longer anyone's character to roll."""
    return bool(sheet.get("retired") or sheet.get("mentor"))


class Beyond:
    """The choice at 11th level, made step by step: what, then for a change of class the new
    class and the core moves kept. ``result()`` is the sheet after."""

    def __init__(self, sheet: Dict[str, Any]) -> None:
        if not ready(sheet):
            raise SheetError(f"{sheet['name']} needs {xp_needed(sheet)} XP at level 10 to go beyond it.")
        self.sheet = sheet
        self.choice: Optional[str] = None
        self.new_class: Optional[str] = None
        self.kept: Optional[List[str]] = None

    def step(self) -> Optional[str]:
        if self.choice is None:
            return "choice"
        if self.choice == "change" and self.new_class is None:
            return "class"
        if self.choice == "change" and self.kept is None and self.keep_options():
            return "keep"
        return None

    def choose(self, choice: str) -> None:
        if choice not in CHOICES:
            raise SheetError("Retire, take on an apprentice, or change class.")
        self.choice = choice

    def class_options(self) -> List[str]:
        own = str(self.sheet.get("class", "")).lower()
        return [k for k in CLASSES if k != own]

    def choose_class(self, key: str) -> None:
        if key not in self.class_options():
            raise SheetError("Choose a class other than your own.")
        self.new_class = key

    def keep_options(self) -> List[str]:
        return list(self.sheet.get("advanced_moves") or ())

    def choose_kept(self, names: List[str]) -> None:
        if len(names) > MAX_KEPT or any(n not in self.keep_options() for n in names):
            raise SheetError(f"Keep up to {MAX_KEPT} of your own advanced moves as core to who you are.")
        self.kept = list(names)

    def result(self) -> Dict[str, Any]:
        if self.step() is not None:
            raise SheetError("Finish choosing first.")
        sheet = dict(self.sheet)
        spent = int(sheet.get("xp", 0)) - xp_needed(sheet)
        if self.choice == "retire":
            return {**sheet, "xp": spent, "retired": True}
        if self.choice == "apprentice":
            return {**sheet, "xp": spent, "mentor": True}
        old = str(sheet.get("class", "")).lower()
        new = CLASSES[self.new_class]
        for key in _CLASS_STATE:
            sheet.pop(key, None)
        # The look stays: it is who they are, not their class.
        sheet["choices"] = {k: v for k, v in (sheet.get("choices") or {}).items() if k.startswith("look_")}
        sheet["multiclass_moves"] = [*(sheet.get("multiclass_moves") or []),
                                     *({"class": old, "name": n} for n in self.kept or ())]
        sheet.update({"class": new["name"], "damage": new["damage"], "advanced_moves": [], "xp": spent,
                      "former_classes": [*(sheet.get("former_classes") or []), self.sheet["class"]]})
        return sheet

    def summary(self) -> str:
        if self.choice == "change" and self.new_class:
            kept = f", keeping {', '.join(self.kept)}" if self.kept else ""
            return f"{self.sheet['name']} becomes a {CLASSES[self.new_class]['name']}{kept}."
        return f"{self.sheet['name']}: {LABELS.get(self.choice or '', '')}."
