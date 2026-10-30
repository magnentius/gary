"""Monsters: the Dungeon World bestiary, and the creatures in the current scene.

The bestiary is extracted from the authors' XML (scripts/extract_dw_monsters.py). A
monster the model names is looked up there, never taken from the model's memory -- stats
are exactly the numbers a model recalls confidently and wrongly. A creature that is not
in the bestiary is the model's own, and its stats are checked against the rules for
making a monster before they are accepted.

Creatures in a scene are numbered instances ("Goblin 1", "Goblin 2"), each with its own
HP, so an attack lands on one of them and the model can see who is hurt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import damage

_DATA = Path(__file__).parent / "data" / "monsters.json"


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_DATA.read_text())["monsters"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"Dungeon World monster data missing or unreadable at {_DATA} ({exc}). "
            "Regenerate it with scripts/extract_dw_monsters.py."
        ) from exc


BESTIARY: Dict[str, Dict[str, Any]] = _load()

MAX_PER_CALL = 12

#: The rules for making a monster (Monsters, "Making Monsters") give a horde 3 HP, a group
#: 6 and a solitary 12, before adjustments, and armor from 0 (no armor) to 4 or so; the
#: biggest things in the bestiary go a little past both. Generous bounds, not a formula:
#: they catch a model that has invented a 200 HP goblin, not one that has made a choice.
HP_RANGE = (1, 30)
ARMOR_RANGE = (0, 5)
DAMAGE_SIDES = (4, 6, 8, 10, 12)


def lookup(name: str) -> Optional[Dict[str, Any]]:
    return BESTIARY.get(" ".join(str(name or "").lower().split()))


class MonsterError(ValueError):
    """A request the rules refuse. The message goes back to the model as is."""


def _homebrew(entry: Dict[str, Any]) -> Dict[str, Any]:
    name = entry["name"]
    missing = [k for k in ("hp", "armor", "damage") if entry.get(k) in (None, "")]
    if missing:
        raise MonsterError(
            f"{name!r} is not in the Dungeon World bestiary, so give its "
            f"{', '.join(missing)} yourself (horde 3 HP, group 6, solitary 12; armor 0-4; "
            "damage like d6, d8+2 or b[2d10])."
        )
    hp, armor = int(entry["hp"]), int(entry["armor"])
    if not HP_RANGE[0] <= hp <= HP_RANGE[1]:
        raise MonsterError(f"{name}: {hp} HP is outside {HP_RANGE[0]}-{HP_RANGE[1]}.")
    if not ARMOR_RANGE[0] <= armor <= ARMOR_RANGE[1]:
        raise MonsterError(f"{name}: armor {armor} is outside {ARMOR_RANGE[0]}-{ARMOR_RANGE[1]}.")
    try:
        spec = damage.parse(str(entry["damage"]))
    except ValueError as exc:
        raise MonsterError(f"{name}: {exc}") from exc
    if spec.sides not in DAMAGE_SIDES:
        raise MonsterError(f"{name}: damage dice are d4 to d12, not d{spec.sides}.")
    return {"kind": name, "hp": hp, "armor": armor, "damage": str(spec),
            "piercing": 0, "ignores_armor": False, "srd": False}


def stats_for(entry: Dict[str, Any]) -> Dict[str, Any]:
    """One creature's stats: from the bestiary if it is there, else checked homebrew."""
    known = lookup(entry["name"])
    if known is None:
        return _homebrew(entry)
    if known["hp"] is None:
        raise MonsterError(
            f"{known['name']} has no combat stats in Dungeon World — it is not something "
            "you fight with a blade. Narrate it instead."
        )
    return {"kind": known["name"], "hp": known["hp"], "armor": known["armor"],
            "damage": known["damage"], "piercing": known["piercing"],
            "ignores_armor": known["ignores_armor"], "srd": True}


def prepare(
    entries: Iterable[Dict[str, Any]], existing: Iterable[str]
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Numbered instances for a scene, and the reasons any entry was refused.

    Numbering continues from what is already there, so a second wave of goblins is
    Goblin 3 and 4 rather than a second Goblin 1.
    """
    taken = {n.lower() for n in existing}
    out: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    total = 0
    for entry in entries:
        count = int(entry.get("count") or 1)
        if count < 1:
            continue
        total += count
        if total > MAX_PER_CALL:
            errors.append(f"At most {MAX_PER_CALL} creatures per call.")
            break
        try:
            stats = stats_for(entry)
        except MonsterError as exc:
            errors.append(str(exc))
            continue
        n = 1
        for _ in range(count):
            while f"{stats['kind']} {n}".lower() in taken:
                n += 1
            name = f"{stats['kind']} {n}"
            taken.add(name.lower())
            out[name] = {**stats, "max_hp": stats["hp"]}
    return out, errors


def describe(name: str, record: Dict[str, Any]) -> str:
    if record["hp"] <= 0:
        return f"{name} (defeated)"
    pierce = (
        ", ignores armor" if record.get("ignores_armor")
        else f", {record['piercing']} piercing" if record.get("piercing") else ""
    )
    return (
        f"{name} (HP {record['hp']}/{record['max_hp']}, armor {record['armor']}, "
        f"damage {record['damage']}{pierce})"
    )
