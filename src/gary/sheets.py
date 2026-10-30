"""Dungeon World character sheets (ARCHITECTURE.md §3).

Gary owns the sheet now that nothing else holds one. What is here is what a roll needs --
class, race, stats and their modifiers, hit points, damage, armor, level -- and nothing a
roll does not, yet: alignment, bonds, gear and moves come with the move registry.

Creation is a ``Draft`` filled in step by step and checked at every step, so a sheet that
reaches the store is valid by construction. The Discord form drives a Draft; it decides
nothing itself, and no model is involved anywhere: these are the numbers every later roll
is computed from, which is the class of decision §5 keeps away from the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

STATS = ("str", "dex", "con", "int", "wis", "cha")
STAT_NAMES = {
    "str": "Strength", "dex": "Dexterity", "con": "Constitution",
    "int": "Intelligence", "wis": "Wisdom", "cha": "Charisma",
}

#: Every character assigns exactly these, one to each stat (Character Creation, step 5).
STAT_ARRAY = (16, 15, 13, 12, 9, 8)

MAX_NAME_CHARS = 40

_DATA = Path(__file__).parent / "data" / "classes.json"


def _load_classes() -> Dict[str, Dict[str, Any]]:
    """The extracted classes. Fails loudly: a sheet cannot be built without them, and an
    empty class list would show players a form with nothing to choose."""
    try:
        return json.loads(_DATA.read_text())["classes"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"Dungeon World class data missing or unreadable at {_DATA} ({exc}). "
            "Regenerate it with scripts/extract_dw_classes.py."
        ) from exc


CLASSES: Dict[str, Dict[str, Any]] = _load_classes()


class SheetError(ValueError):
    """A choice the rules do not allow. The message is shown to the player as is."""


def modifier(score: int) -> int:
    """Character Creation, step 6."""
    if score <= 3:
        return -3
    if score <= 5:
        return -2
    if score <= 8:
        return -1
    if score <= 12:
        return 0
    if score <= 15:
        return 1
    if score <= 17:
        return 2
    return 3


@dataclass
class Draft:
    """A character part-way through creation. Each setter refuses what the rules forbid."""

    class_key: Optional[str] = None
    race: Optional[str] = None
    # stat -> score, filled one array value at a time
    stats: Dict[str, int] = field(default_factory=dict)
    name: Optional[str] = None

    def choose_class(self, class_key: str) -> None:
        key = str(class_key or "").strip().lower()
        if key not in CLASSES:
            raise SheetError(f"There is no {class_key!r} class.")
        if key != self.class_key:
            self.race = None  # races differ by class
        self.class_key = key

    def races(self) -> List[str]:
        return list(CLASSES[self.class_key]["races"]) if self.class_key else []

    def choose_race(self, race: str) -> None:
        if self.class_key is None:
            raise SheetError("Choose a class first.")
        if race not in self.races():
            raise SheetError(
                f"A {CLASSES[self.class_key]['name']} can be "
                f"{', '.join(self.races())} — not {race}."
            )
        self.race = race

    def unassigned_scores(self) -> List[int]:
        left = list(STAT_ARRAY)
        for score in self.stats.values():
            left.remove(score)
        return left

    def unassigned_stats(self) -> List[str]:
        return [s for s in STATS if s not in self.stats]

    def next_score(self) -> Optional[int]:
        """The highest score not yet placed -- the one the form asks about next."""
        left = self.unassigned_scores()
        return left[0] if left else None

    def assign(self, stat: str, score: int) -> None:
        stat = str(stat or "").strip().lower()
        if stat not in STATS:
            raise SheetError(f"There is no {stat!r} stat.")
        if stat in self.stats:
            raise SheetError(f"{STAT_NAMES[stat]} already has {self.stats[stat]}.")
        if score not in self.unassigned_scores():
            raise SheetError(f"{score} is not left to assign.")
        self.stats[stat] = int(score)
        # The last score has only one place to go; asking would be a formality.
        if len(self.unassigned_stats()) == 1:
            (last,) = self.unassigned_stats()
            (final,) = self.unassigned_scores()
            self.stats[last] = final

    def reset_stats(self) -> None:
        self.stats.clear()

    def choose_name(self, name: str) -> None:
        name = " ".join(str(name or "").split())
        if not name:
            raise SheetError("Your character needs a name.")
        if len(name) > MAX_NAME_CHARS:
            raise SheetError(f"Keep the name to {MAX_NAME_CHARS} characters.")
        self.name = name

    @property
    def complete(self) -> bool:
        return bool(self.class_key and self.race and self.name) and not self.unassigned_stats()

    def build(self) -> Dict[str, Any]:
        """The finished sheet, as stored. Refuses anything incomplete."""
        if not self.complete:
            missing = [
                label for label, done in (
                    ("a class", self.class_key), ("a race", self.race),
                    ("stats", not self.unassigned_stats()), ("a name", self.name),
                ) if not done
            ]
            raise SheetError("Still needs " + ", ".join(missing) + ".")
        cls = CLASSES[self.class_key]
        max_hp = int(cls["base_hp"]) + self.stats["con"]
        return {
            "name": self.name,
            "class": cls["name"],
            "race": self.race,
            "level": 1,
            "xp": 0,
            "stats": {s: self.stats[s] for s in STATS},
            "max_hp": max_hp,
            "hp": max_hp,
            "damage": cls["damage"],
            # Armor comes from gear, which this sheet does not track yet.
            "armor": 0,
        }


def describe(sheet: Dict[str, Any], with_name: bool = True) -> str:
    """One line for the table and for the model: who, what, and the numbers rolls use.

    ``with_name=False`` for a line that already starts with the name, like "Kael joins
    the party — …", which would otherwise say it twice.
    """
    stats = " ".join(
        f"{s.upper()} {sheet['stats'][s]} ({modifier(sheet['stats'][s]):+d})" for s in STATS
    )
    who = f"{sheet['name']} — " if with_name else ""
    return (
        f"{who}{sheet['race']} {sheet['class']} {sheet['level']}, "
        f"HP {sheet['hp']}/{sheet['max_hp']}, armor {sheet['armor']}, "
        f"damage {sheet['damage']} · {stats}"
    )
