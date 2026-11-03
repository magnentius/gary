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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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


#: A homebrew creature's instinct and each move, at most this long; at most this many moves.
MAX_NATURE_TEXT = 160
MAX_MOVES = 4


def lookup(name: str) -> Optional[Dict[str, Any]]:
    return BESTIARY.get(" ".join(str(name or "").lower().split()))


class MonsterError(ValueError):
    """A request the rules refuse. The message goes back to the model as is."""


# -- Making Monsters: the book's questions, and the numbers each answer sets --------------------

#: How does it usually hunt or fight?
ORGANIZATION = {"horde": (3, 6, "Horde"), "group": (6, 8, "Group"), "solitary": (12, 10, "Solitary")}
#: How big is it? (HP, damage, tags)
SIZES = {"tiny": (0, -2, ("Tiny", "hand")), "small": (0, 0, ("Small", "close")), "human": (0, 0, ("close",)),
         "large": (4, 1, ("Large", "close", "reach")), "huge": (8, 3, ("Huge", "reach"))}
#: What is its most important defense?
DEFENSES = {"flesh": 0, "leather": 1, "mail": 2, "plate": 3, "magic": 4}
#: What is it known for? (choose all that apply)
KNOWN_FOR = ("strength", "offense", "defense", "deft", "endurance", "deceit", "adaptation", "divine", "magic")
#: Its most common attack -- these describe it.
ATTACK_TRAITS = ("vicious", "reach", "weak", "slices_metal", "tears_metal", "ignores_armor", "ranged")
#: Which of these describe it?
TRAITS = ("devious", "organized", "intelligent", "cautious", "hoarder", "planar", "unnatural", "construct",
          "terrifying", "amorphous", "ancient", "abhors_violence")
_DIE = (4, 6, 8, 10, 12)

#: Answers that ask for a move or a special quality to be written.
_WRITE = {"deceit": "a move about its dirty tricks", "magic": "a move about its spells",
          "adaptation": "a special quality for its adaptation", "devious": "a move about why it is dangerous",
          "organized": "a move about calling on others for help",
          "planar": "a move about using its otherworldly knowledge and power",
          "construct": "a special quality about its construction or purpose",
          "terrifying": "a special quality about why it is so horrendous"}


def build(recipe: Dict[str, Any]) -> Dict[str, Any]:
    """Stats from the book's Making Monsters answers: HP, armor, damage, piercing, tags, and what
    still has to be written. "If a combination of answers would reduce HP or damage below 1
    they stay at 1." """
    def one(key: str, table: Dict[str, Any]) -> Any:
        value = str(recipe.get(key) or "").strip().lower()
        if value not in table:
            raise MonsterError(f"Making a monster: {key} is one of {', '.join(table)}.")
        return value

    def many(key: str, allowed: Sequence[str]) -> List[str]:
        chosen = [str(v).strip().lower() for v in recipe.get(key) or ()]
        wrong = [v for v in chosen if v not in allowed]
        if wrong:
            raise MonsterError(f"Making a monster: {key} are from {', '.join(allowed)}; not {', '.join(wrong)}.")
        return chosen

    org, size, defense = one("organization", ORGANIZATION), one("size", SIZES), one("defense", DEFENSES)
    known, attack, traits = many("known_for", KNOWN_FOR), many("attack_traits", ATTACK_TRAITS), many("traits", TRAITS)
    hp, die, org_tag = ORGANIZATION[org]
    size_hp, size_damage, size_tags = SIZES[size]
    tags: List[str] = [org_tag, *size_tags]
    armor, bonus, piercing, ignores = DEFENSES[defense], size_damage, 0, False
    hp += size_hp
    mode = ""  # "b" best of two, "w" worst of two
    if defense == "magic":
        tags.append("Magical")
    if "strength" in known:
        bonus += 2
        tags.append("forceful")
    if "offense" in known:
        mode = "b"
    if "defense" in known:
        armor += 1
    if "deft" in known:
        piercing += 1
    if "endurance" in known:
        hp += 4
    if "deceit" in known:
        tags.append("Stealthy")
    if "divine" in known:
        favor = str(recipe.get("divine_favor") or "both").lower()
        bonus += 2 if favor in ("damage", "both") else 0
        hp += 2 if favor in ("hp", "both") else 0
        tags.append("Divine")
    if "magic" in known:
        tags.append("Magical")
    if "vicious" in attack:
        bonus += 2
    if "reach" in attack:
        tags.append("reach")
    if "tears_metal" in attack:
        piercing += 3
        tags.append("messy")
    elif "slices_metal" in attack:
        piercing += 1
        tags.append("messy")
    if "ignores_armor" in attack:
        ignores = True
        tags.append("ignores armor")
    if "ranged" in attack:
        tags += ["near", "far"]
    steps = (-1 if "weak" in attack else 0) + (-1 if "devious" in traits else 0) + (1 if "ancient" in traits else 0)
    die = _DIE[max(0, min(len(_DIE) - 1, _DIE.index(die) + steps))]
    if "abhors_violence" in traits:
        mode = "w" if mode != "b" else ""  # best and worst cancel
    for trait, tag in (("devious", "Devious"), ("organized", "Organized"), ("intelligent", "Intelligent"),
                       ("cautious", "Cautious"), ("hoarder", "Hoarder"), ("planar", "Planar"),
                       ("construct", "Construct"), ("terrifying", "Terrifying"), ("amorphous", "Amorphous")):
        if trait in traits:
            tags.append(tag)
    if "cautious" in traits:
        armor += 1
    if "unnatural" in traits:
        hp += 4
    if "amorphous" in traits:
        armor, hp = armor + 1, hp + 3
    hp = max(1, hp)
    dice = f"{mode}[2d{die}]" if mode else f"d{die}"
    expression = dice + (f"{bonus:+d}" if bonus else "")
    to_write = [_WRITE[a] for a in [*known, *traits] if a in _WRITE]
    return {"hp": hp, "armor": armor, "damage": expression, "piercing": piercing, "ignores_armor": ignores,
            "tags": list(dict.fromkeys(tags)), "write": to_write}


def _homebrew(entry: Dict[str, Any]) -> Dict[str, Any]:
    name = entry["name"]
    if entry.get("recipe"):
        built = build(dict(entry["recipe"]))
        record = {"kind": name, "hp": built["hp"], "armor": built["armor"],
                  "damage": str(damage.parse(built["damage"])), "piercing": built["piercing"],
                  "ignores_armor": built["ignores_armor"], "tags": built["tags"], "srd": False, "recipe": True}
        instinct = " ".join(str(entry.get("instinct") or "").split())[:MAX_NATURE_TEXT]
        moves = [" ".join(str(m).split())[:MAX_NATURE_TEXT] for m in entry.get("moves") or () if str(m).strip()]
        if not instinct or not moves:
            raise MonsterError(f"{name}: a monster made by the book has an instinct (what it wants that causes "
                               "problems for others) and at least one move (what it is known to do)"
                               + (f"; its answers also call for {'; '.join(built['write'])}" if built["write"] else "")
                               + ".")
        record.update(instinct=instinct, moves=moves[:MAX_MOVES])
        return record
    missing = [k for k in ("hp", "armor", "damage") if entry.get(k) in (None, "")]
    if missing:
        raise MonsterError(
            f"{name!r} is not in the Dungeon World bestiary: give a recipe (the book's Making Monsters "
            "answers: organization, size, defense, and what it is known for) with an instinct and a move, "
            f"and Gary works out its numbers -- or give its {', '.join(missing)} yourself (horde 3 HP, "
            "group 6, solitary 12; armor 0-4; damage like d6, d8+2 or b[2d10])."
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
    record = {"kind": name, "hp": hp, "armor": armor, "damage": str(spec),
              "piercing": 0, "ignores_armor": False, "srd": False}
    # What the GM does with it, when the model gives it: kept so the next turn still knows.
    instinct = " ".join(str(entry.get("instinct") or "").split())[:MAX_NATURE_TEXT]
    moves = [" ".join(str(m).split())[:MAX_NATURE_TEXT] for m in entry.get("moves") or () if str(m).strip()]
    if instinct:
        record["instinct"] = instinct
    if moves:
        record["moves"] = moves[:MAX_MOVES]
    return record


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


def nature(record: Dict[str, Any]) -> str:
    """What a kind of creature wants and does, for the GM: instinct, moves, qualities, tags
    and how it attacks -- from the bestiary, or what the model gave a homebrew one. ""
    when there is nothing to say."""
    known = lookup(record.get("kind", "")) if record.get("srd", True) else None
    source = known or record
    parts = []
    if source.get("instinct"):
        parts.append(f"instinct: {source['instinct']}")
    if source.get("moves"):
        parts.append("moves: " + "; ".join(source["moves"]))
    if source.get("qualities"):
        parts.append("qualities: " + ", ".join(source["qualities"]))
    if known and known.get("tags"):
        parts.append("tags: " + ", ".join(known["tags"]))
    if known and known.get("attack"):
        reach = ", ".join(known.get("attack_tags") or ())
        parts.append(f"attacks with {known['attack']}" + (f" ({reach})" if reach else ""))
    return f"{record.get('kind', '')} — " + " · ".join(parts) if parts else ""


def natures(scene: Dict[str, Dict[str, Any]]) -> List[str]:
    """One nature line per kind of creature still standing in the scene."""
    seen, out = set(), []
    for record in scene.values():
        kind = record.get("kind", "")
        if record.get("hp", 0) <= 0 or kind in seen:
            continue
        seen.add(kind)
        line = nature(record)
        if line:
            out.append(line)
    return out


def describe(name: str, record: Dict[str, Any]) -> str:
    if record["hp"] <= 0:
        return f"{name} (defeated" + (", searched)" if record.get("looted") else ")")
    pierce = (
        ", ignores armor" if record.get("ignores_armor")
        else f", {record['piercing']} piercing" if record.get("piercing") else ""
    )
    return (
        f"{name} (HP {record['hp']}/{record['max_hp']}, armor {record['armor']}, "
        f"damage {record['damage']}{pierce})"
    )
