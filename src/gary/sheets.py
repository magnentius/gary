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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .gear import (
    CLASS_GEAR, ENHANCEMENTS, SIGNATURE_BASES, SIGNATURE_RANGES, ClassGear, Item, ammo_of,
    armor_of, item_for, rations_of, signature_weapon,
)

STATS = ("str", "dex", "con", "int", "wis", "cha")
STAT_NAMES = {
    "str": "Strength", "dex": "Dexterity", "con": "Constitution",
    "int": "Intelligence", "wis": "Wisdom", "cha": "Charisma",
}

#: Debilities, each -1 to one stat's modifier until cured (Dungeon World, "Debilities").
DEBILITIES = {"weak": "str", "shaky": "dex", "sick": "con", "stunned": "int",
              "confused": "wis", "scarred": "cha"}

#: "Your load is 12+Str." -- each class's base, from its gear section.
LOAD_BASE = {"bard": 9, "cleric": 10, "druid": 6, "fighter": 12, "paladin": 12, "ranger": 11,
             "thief": 9, "wizard": 7}

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


def debility_on(sheet: Dict[str, Any], stat: str) -> Optional[str]:
    """The debility on this stat ("weak"), or None."""
    return next((d for d in sheet.get("debilities") or () if DEBILITIES.get(d) == stat), None)


def stat_modifier(sheet: Dict[str, Any], stat: str) -> int:
    """What a roll adds for this stat: its modifier, -1 for a debility on it."""
    return modifier(int(sheet["stats"][stat])) - (1 if debility_on(sheet, stat) else 0)


def load(sheet: Dict[str, Any]) -> int:
    return LOAD_BASE.get(str(sheet.get("class", "")).lower(), 9) + stat_modifier(sheet, "str")


def carried(sheet: Dict[str, Any]) -> int:
    """Weight carried: every item, and coin at 100 to the weight."""
    return sum(int(g.get("weight", 0)) for g in sheet.get("gear") or ()) + int(sheet.get("coins", 0)) // 100


def encumbrance(sheet: Dict[str, Any]) -> str:
    """Encumbrance: "fine" up to load, "burdened" (-1) at load+1 or +2, "overloaded" past
    that. A sheet without gear carries nothing."""
    if "gear" not in sheet:
        return "fine"
    over = carried(sheet) - load(sheet)
    return "fine" if over <= 0 else "burdened" if over <= 2 else "overloaded"


@dataclass
class Draft:
    """A character part-way through creation. Each setter refuses what the rules forbid."""

    class_key: Optional[str] = None
    race: Optional[str] = None
    # stat -> score, filled one array value at a time
    stats: Dict[str, int] = field(default_factory=dict)
    name: Optional[str] = None
    # group index -> option indices picked, in the order the class lists its groups
    gear_picks: Dict[int, Tuple[int, ...]] = field(default_factory=dict)
    # The Fighter's signature weapon, built in steps: base, range, two enhancements, and
    # a second range if one enhancement is Versatile.
    signature_base: Optional[str] = None
    signature_ranges: List[str] = field(default_factory=list)
    signature_enhancements: Tuple[int, ...] = ()
    alignment: Optional[str] = None
    # The class's creation choices (choices.py): deity, land, lore, poison...
    choices: Dict[str, str] = field(default_factory=dict)

    def choose_class(self, class_key: str) -> None:
        key = str(class_key or "").strip().lower()
        if key not in CLASSES:
            raise SheetError(f"There is no {class_key!r} class.")
        if key != self.class_key:
            self.race = None  # races differ by class
            self.reset_gear()  # and so does gear
            self.alignment = None  # and alignments
            self.choices = {}  # and what each class chooses
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
        if race != self.race:
            self.choices = {}  # an Elf Fighter chooses a precise weapon; others do not
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

    # -- gear ------------------------------------------------------------------------

    def class_gear(self) -> Optional[ClassGear]:
        return CLASS_GEAR[self.class_key] if self.class_key else None

    def next_gear_group(self) -> Optional[int]:
        """The first of the class's gear groups not yet chosen from, or None."""
        cg = self.class_gear()
        if cg is None:
            return None
        return next((i for i in range(len(cg.groups)) if i not in self.gear_picks), None)

    def choose_gear(self, group: int, picks: Sequence[int]) -> None:
        cg = self.class_gear()
        if cg is None:
            raise SheetError("Choose a class first.")
        if not 0 <= group < len(cg.groups):
            raise SheetError("There is no such gear choice.")
        g = cg.groups[group]
        picks = tuple(sorted(int(p) for p in picks))
        if len(picks) != g.count or len(set(picks)) != g.count:
            raise SheetError(f"{g.prompt}: pick {g.count}.")
        if not all(0 <= p < len(g.options) for p in picks):
            raise SheetError(f"{g.prompt}: that is not one of the options.")
        self.gear_picks[group] = picks

    def needs_signature(self) -> bool:
        cg = self.class_gear()
        return bool(cg and cg.signature_weapon)

    def signature_step(self) -> Optional[str]:
        """What the signature weapon still needs: base, range, enhancements, extra_range."""
        if not self.needs_signature():
            return None
        if self.signature_base is None:
            return "base"
        if not self.signature_ranges:
            return "range"
        if len(self.signature_enhancements) != 2:
            return "enhancements"
        versatile = any(ENHANCEMENTS[i].versatile for i in self.signature_enhancements)
        if versatile and len(self.signature_ranges) < 2:
            return "extra_range"
        return None

    def choose_signature_base(self, base: str) -> None:
        if base not in SIGNATURE_BASES:
            raise SheetError(f"Choose one of {', '.join(SIGNATURE_BASES)}.")
        self.signature_base = base

    def choose_signature_range(self, rng: str) -> None:
        if rng not in SIGNATURE_RANGES:
            raise SheetError(f"Choose one of {', '.join(SIGNATURE_RANGES)}.")
        if rng in self.signature_ranges:
            raise SheetError(f"Your weapon is already {rng}.")
        if len(self.signature_ranges) >= (2 if self.signature_step() == "extra_range" else 1):
            raise SheetError("Your weapon has its range.")
        self.signature_ranges.append(rng)

    def choose_signature_enhancements(self, picks: Sequence[int]) -> None:
        picks = tuple(sorted(int(p) for p in picks))
        if len(picks) != 2 or len(set(picks)) != 2:
            raise SheetError("Choose two enhancements.")
        if not all(0 <= p < len(ENHANCEMENTS) for p in picks):
            raise SheetError("That is not one of the enhancements.")
        self.signature_enhancements = picks

    def reset_gear(self) -> None:
        self.gear_picks.clear()
        self.signature_base = None
        self.signature_ranges = []
        self.signature_enhancements = ()

    @property
    def gear_complete(self) -> bool:
        return self.class_key is not None and self.next_gear_group() is None \
            and self.signature_step() is None

    def gear_items(self) -> List[Item]:
        """Everything the character carries: the class's fixed gear, what was picked, and
        the signature weapon if there is one. Refuses anything incomplete."""
        if not self.gear_complete:
            raise SheetError("Gear is not chosen yet.")
        cg = self.class_gear()
        items = list(cg.fixed)
        for group, picks in sorted(self.gear_picks.items()):
            for p in picks:
                items.extend(cg.groups[group].options[p][1])
        if cg.signature_weapon:
            items.insert(0, signature_weapon(
                self.signature_base, self.signature_ranges, self.signature_enhancements
            ))
        return items

    def alignments(self) -> Dict[str, str]:
        """The class's alignments: name -> what fulfils it."""
        return alignments_of(self.class_key or "")

    def choose_alignment(self, alignment: str) -> None:
        if alignment not in self.alignments():
            raise SheetError(f"Choose one of {', '.join(self.alignments())}.")
        self.alignment = alignment

    def class_choices(self):
        from .choices import Choices  # choices -> class_moves; imported late to keep sheets light

        return Choices(CLASSES[self.class_key]["name"] if self.class_key else "", self.race or "",
                       self.choices)

    def choose_class_choice(self, value: str) -> None:
        choices = self.class_choices()
        choices.choose(value)
        self.choices = choices.made

    def choose_name(self, name: str) -> None:
        name = " ".join(str(name or "").split())
        if not name:
            raise SheetError("Your character needs a name.")
        if len(name) > MAX_NAME_CHARS:
            raise SheetError(f"Keep the name to {MAX_NAME_CHARS} characters.")
        self.name = name

    @property
    def complete(self) -> bool:
        return (
            bool(self.class_key and self.race and self.name and self.alignment)
            and self.class_choices().complete
            and not self.unassigned_stats()
            and self.gear_complete
        )

    def build(self) -> Dict[str, Any]:
        """The finished sheet, as stored. Refuses anything incomplete."""
        if not self.complete:
            missing = [
                label for label, done in (
                    ("a class", self.class_key), ("a race", self.race),
                    ("stats", not self.unassigned_stats()),
                    ("gear", self.gear_complete), ("an alignment", self.alignment),
                    ("your class's choices", self.class_key and self.class_choices().complete),
                    ("a name", self.name),
                ) if not done
            ]
            raise SheetError("Still needs " + ", ".join(missing) + ".")
        cls = CLASSES[self.class_key]
        max_hp = int(cls["base_hp"]) + self.stats["con"]
        from .choices import on_sheet

        return on_sheet({
            "name": self.name,
            "class": cls["name"],
            "race": self.race,
            "level": 1,
            "xp": 0,
            "stats": {s: self.stats[s] for s in STATS},
            "max_hp": max_hp,
            "hp": max_hp,
            "damage": cls["damage"],
            "alignment": self.alignment,
            **with_gear({}, self.gear_items()),
        }, self.choices)


def with_gear(sheet: Dict[str, Any], items: Sequence[Item]) -> Dict[str, Any]:
    """The sheet carrying these items: gear, and the armor and ammo they come to.

    Used for a new character and for giving gear to one made before gear existed -- so
    their HP, stats and everything else are left exactly as they are.
    """
    # "10 coins" in a starting kit is money, not something to carry.
    coins = sum(int(m[1]) for i in items if (m := re.fullmatch(r"(\d+) coins?", i.name.lower())))
    gear = []
    for i in items:
        if re.fullmatch(r"\d+ coins?", i.name.lower()):
            continue
        # "2 healing potions" is two potions, each drunk on its own -- not one item.
        if (m := re.fullmatch(r"(\d+) (.+)", i.name)) and item_for(m[2])[1]:
            gear.extend(item_for(m[2])[0].to_dict() for _ in range(int(m[1])))
        else:
            gear.append(i.to_dict())
    return {**sheet, "gear": gear, "armor": armor_of(gear), "ammo": ammo_of(gear),
            "rations": rations_of(gear), "coins": int(sheet.get("coins", 0)) + coins}


def weapons_line(sheet: Dict[str, Any]) -> str:
    weapons = [Item.from_dict(g) for g in sheet.get("gear") or []]
    return ", ".join(w.label() for w in weapons if w.ranges)


def carrying_line(sheet: Dict[str, Any]) -> str:
    """Everything carried, for the player: "Carrying: Chainmail (1 armor), …".

    Counted items show what is left ("Bandages (2 uses left)"), and repeats are grouped,
    so a player can see whether there is a potion before reaching for one.
    """
    gear = sheet.get("gear")
    if gear is None:
        return ""
    labels: List[str] = []
    for g in gear:
        item = Item.from_dict(g)
        label = item.label()
        if "uses" in g:
            label = f"{item.name} ({g['uses']} uses left)"
        labels.append(label)
    grouped: Dict[str, int] = {}
    for label in labels:
        grouped[label] = grouped.get(label, 0) + 1
    shown = [label if n == 1 else f"{n} × {label}" for label, n in grouped.items()]
    return "Carrying: " + (", ".join(shown) if shown else "nothing") + "."


def xp_needed(sheet: Dict[str, Any]) -> int:
    """Level Up: "When you have downtime and you have XP equal to (or greater than) your
    current level+7"."""
    return int(sheet.get("level", 1)) + 7


def alignments_of(class_name: str) -> Dict[str, str]:
    """A class's alignments, name -> what fulfils it (the book's words)."""
    from .class_moves import TEXTS  # class_moves -> spells; imported late to keep sheets light

    return dict(TEXTS.get(class_name.lower(), {}).get("alignments", {}))


def alignment_line(sheet: Dict[str, Any]) -> str:
    """"Alignment — Good: Endanger yourself to heal another." "" if none is chosen."""
    name = sheet.get("alignment")
    if not name:
        return ""
    return f"Alignment — {name}: {alignments_of(sheet.get('class', '')).get(name, '')}"


#: Said once, when XP first reaches the mark.
LEVEL_UP_HINT = " Run `?dm levelup` when there's downtime."


def xp_line(sheet: Dict[str, Any]) -> str:
    """"5/8 XP", or "8/8 XP, ready to level up"."""
    xp, needed = int(sheet.get("xp", 0)), xp_needed(sheet)
    return f"{xp}/{needed} XP" + (", ready to level up" if xp >= needed else "")


def describe(sheet: Dict[str, Any], with_name: bool = True) -> str:
    """One line for the table and for the model: who, what, and the numbers rolls use.

    ``with_name=False`` for a line that already starts with the name, like "Kael joins
    the party — …", which would otherwise say it twice.
    """
    stats = " ".join(
        f"{s.upper()} {sheet['stats'][s]} ({modifier(sheet['stats'][s]):+d})" for s in STATS
    )
    who = f"{sheet['name']} — " if with_name else ""
    line = (
        f"{who}{sheet['race']} {sheet['class']} {sheet['level']} ({xp_line(sheet)}), "
        f"HP {sheet['hp']}/{sheet['max_hp']}, armor {sheet['armor']}, "
        f"damage {sheet['damage']} · {stats}"
    )
    if sheet.get("debilities"):
        line += " · " + ", ".join(d.title() for d in sheet["debilities"]) + " (-1 each)"
    if "gear" in sheet:
        weapons = weapons_line(sheet)
        line += f" · wields {weapons or 'nothing'}"
        if any(g.get("needs_ammo") for g in sheet["gear"]):
            line += f" · ammo {sheet.get('ammo', 0)}"
        rations = sheet["rations"] if "rations" in sheet else rations_of(sheet["gear"])
        line += f" · rations {rations} · {int(sheet.get('coins', 0))} coins"
        line += f" · weight {carried(sheet)}/{load(sheet)}"
        state = encumbrance(sheet)
        if state != "fine":
            line += " (burdened, -1)" if state == "burdened" else " (overloaded)"
    for f in sheet.get("forwards") or ():
        what = f"+{f['damage']} damage" if f.get("damage") else f"{int(f['amount']):+d}"
        line += f" · {what} forward ({f['reason']})"
    guard = sheet.get("defending")
    if guard:
        line += f" · defending {guard['charge']} (hold {guard['hold']})"
    shape = sheet.get("shape")
    if shape:
        moves = f": {', '.join(shape['moves'])}" if shape.get("moves") else ""
        line += f" · in the shape of a {shape['form']} (hold {shape['hold']}{moves})"
    if sheet.get("dead"):
        line += " · DEAD"
    elif int(sheet.get("hp", 1)) <= 0:
        line += " · DOWN" + (", marked as Death's own" if sheet.get("deaths_mark") else "")
    return line
