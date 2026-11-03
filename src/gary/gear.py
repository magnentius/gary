"""Dungeon World starting gear, and what gear does to the rules.

Hand-written from each class's gear section in the authors' XML (Sagelt/Dungeon-World),
as the moves are: the choices bundle items ("Dagger and short sword"), and the Druid's
section in the XML has lost its group headings entirely, so a parser would offer one
nine-way choice where the book has three.

What gear does:
- **Armor.** The best worn armor, plus a shield's +1. Armor does not stack; shields do.
- **Weapons.** A move that attacks needs a weapon that reaches: Hack and Slash a hand,
  close or reach weapon, Volley a near, far or thrown one. A weapon's +damage and
  piercing go into the damage roll.
- **Ammo.** Bows and throwing daggers need ammo; Volley's "take several shots" spends one.

Dungeon World is CC-BY 3.0, by Sage LaTorra and Adam Koebel.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MELEE = ("hand", "close", "reach")
RANGED = ("near", "far", "thrown")


@dataclass(frozen=True)
class Item:
    name: str
    tags: Tuple[str, ...] = ()  # as the book lists them: "close", "+1 damage", "precise"...
    armor: int = 0  # worn armor
    shield: int = 0  # +armor from a shield
    damage: int = 0  # +damage when attacking with it
    piercing: int = 0
    ammo: int = 0  # ammo this item provides (arrows, throwing daggers)
    needs_ammo: bool = False  # bows and throwing daggers fire nothing without it
    weight: int = 0
    rations: int = 0  # ration uses this item provides; Make Camp eats one

    @property
    def ranges(self) -> Tuple[str, ...]:
        return tuple(t for t in self.tags if t in MELEE + RANGED)

    def label(self) -> str:
        return self.name + (f" ({', '.join(self.tags)})" if self.tags else "")

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Item":
        """An item from a saved gear entry. Entries carry more than an Item -- "uses"
        once one is spent -- and older ones carry less; neither may break loading."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{**{k: v for k, v in d.items() if k in known},
                      "tags": tuple(d.get("tags", ()))})

    def to_dict(self) -> Dict[str, Any]:
        """JSON-shaped, so a sheet built here equals the same sheet read back from the
        store -- which saves it as JSON, turning a tuple into a list."""
        return {**asdict(self), "tags": list(self.tags)}


def _i(name, *tags, armor=0, shield=0, damage=0, piercing=0, ammo=0, needs_ammo=False,
       weight=0, rations=0):
    return Item(name, tuple(tags), armor, shield, damage, piercing, ammo, needs_ammo, weight,
                rations)


# -- the catalog: every item a class can start with ---------------------------------------

RATIONS = _i("Dungeon rations", "5 uses", weight=1, rations=5)
LEATHER = _i("Leather armor", "1 armor", armor=1, weight=1)
CHAINMAIL = _i("Chainmail", "1 armor", armor=1, weight=1)
SCALE = _i("Scale armor", "2 armor", armor=2, weight=3)
HIDE = _i("Hide armor", "1 armor", armor=1, weight=1)
SHIELD = _i("Shield", "+1 armor", shield=1, weight=2)
WOODEN_SHIELD = _i("Wooden shield", "+1 armor", shield=1, weight=1)
ARROWS = _i("Bundle of arrows", "3 ammo", ammo=3, weight=1)
GEAR = _i("Adventuring gear", "5 uses", weight=1)
POTION = _i("Healing potion")
BANDAGES = _i("Bandages", "3 uses", "slow")
PIPELEAF = _i("Halfling pipeleaf", "6 uses")
ANTITOXIN = _i("Antitoxin")
POULTICES = _i("Poultices and herbs", "2 uses", "slow", weight=1)
BOOKS = _i("Bag of books", "5 uses", weight=2)

DUELING_RAPIER = _i("Dueling rapier", "close", "1 piercing", "precise", piercing=1, weight=2)
RAPIER = _i("Rapier", "close", "precise", weight=1)
SHORT_SWORD = _i("Short sword", "close", weight=1)
DAGGER = _i("Dagger", "hand", weight=1)
WARHAMMER = _i("Warhammer", "close", weight=1)
MACE = _i("Mace", "close", weight=1)
STAFF = _i("Staff", "close", "two-handed", weight=1)
SHILLELAGH = _i("Shillelagh", "close", weight=2)
DRUID_SPEAR = _i("Spear", "close", "thrown", "near", weight=1)
SPEAR = _i("Spear", "reach", weight=1)
HALBERD = _i("Halberd", "reach", "+1 damage", "two-handed", damage=1, weight=2)
LONG_SWORD = _i("Long sword", "close", "+1 damage", damage=1, weight=1)
WORN_BOW = _i("Worn bow", "near", needs_ammo=True, weight=2)
RAGGED_BOW = _i("Ragged bow", "near", needs_ammo=True, weight=2)
HUNTERS_BOW = _i("Hunter's bow", "near", "far", needs_ammo=True, weight=1)
THROWING_DAGGERS = _i("Throwing daggers", "thrown", "near", "3 ammo", ammo=3, needs_ammo=True)
AXE = _i("Axe", "close", weight=1)
TWO_HANDED_SWORD = _i("Two-handed sword", "close", "+1 damage", damage=1, weight=2)


@dataclass(frozen=True)
class Group:
    """"Choose your armament:" -- pick ``count`` of ``options``, each a bundle of items."""

    prompt: str
    options: Tuple[Tuple[str, Tuple[Item, ...]], ...]  # (label shown, items it gives)
    count: int = 1


def _opt(*items: Item, label: str = "") -> Tuple[str, Tuple[Item, ...]]:
    return (label or " and ".join(i.label() for i in items), items)


@dataclass(frozen=True)
class ClassGear:
    fixed: Tuple[Item, ...]
    groups: Tuple[Group, ...]
    signature_weapon: bool = False  # the Fighter's: built, not chosen


def _flavour(name: str) -> Item:
    return _i(name)


CLASS_GEAR: Dict[str, ClassGear] = {
    "bard": ClassGear(
        fixed=(RATIONS,),
        groups=(
            Group("Choose one instrument (0 weight for you)", tuple(
                _opt(_flavour(n)) for n in (
                    "Your father's mandolin, repaired", "A fine lute, a gift from a noble",
                    "The pipes with which you courted your first love", "A stolen horn",
                    "A fiddle, never before played", "A songbook in a forgotten tongue",
                )
            )),
            Group("Choose your clothing", (
                _opt(LEATHER), _opt(_i("Ostentatious clothes")),
            )),
            Group("Choose your armament", (
                _opt(DUELING_RAPIER),
                _opt(WORN_BOW, ARROWS, SHORT_SWORD),
            )),
            Group("Choose one", (
                _opt(GEAR), _opt(BANDAGES), _opt(PIPELEAF), _opt(_i("3 coins")),
            )),
        ),
    ),
    "cleric": ClassGear(
        fixed=(RATIONS, _i("Symbol of the divine")),
        groups=(
            Group("Choose your defenses", (_opt(CHAINMAIL), _opt(SHIELD))),
            Group("Choose your armament", (_opt(WARHAMMER), _opt(MACE), _opt(STAFF, BANDAGES))),
            Group("Choose one", (_opt(GEAR, RATIONS), _opt(POTION))),
        ),
    ),
    "druid": ClassGear(
        fixed=(_i("Token of your land"),),
        groups=(
            Group("Choose your defenses", (_opt(HIDE), _opt(WOODEN_SHIELD))),
            Group("Choose your armament", (_opt(SHILLELAGH), _opt(STAFF), _opt(DRUID_SPEAR))),
            Group("Choose one", (
                _opt(GEAR), _opt(POULTICES), _opt(PIPELEAF), _opt(_i("3 antitoxins")),
            )),
        ),
    ),
    "fighter": ClassGear(
        fixed=(RATIONS,),
        groups=(
            Group("Choose your defenses", (_opt(CHAINMAIL, GEAR), _opt(SCALE))),
            Group("Choose two", (
                _opt(_i("2 healing potions")), _opt(SHIELD),
                _opt(ANTITOXIN, RATIONS, POULTICES), _opt(_i("22 coins")),
            ), count=2),
        ),
        signature_weapon=True,
    ),
    "barbarian": ClassGear(
        fixed=(RATIONS, DAGGER, _i("Token of where you've travelled or where you're from")),
        groups=(
            Group("Choose your weapon", (_opt(AXE), _opt(TWO_HANDED_SWORD))),
            Group("Choose one", (_opt(GEAR, RATIONS), _opt(CHAINMAIL))),
        ),
    ),
    "paladin": ClassGear(
        fixed=(RATIONS, SCALE, _i("Mark of faith")),
        groups=(
            Group("Choose your weapon", (_opt(HALBERD), _opt(LONG_SWORD, SHIELD))),
            Group("Choose one", (_opt(GEAR), _opt(RATIONS, POTION))),
        ),
    ),
    "ranger": ClassGear(
        fixed=(RATIONS, LEATHER, ARROWS),
        groups=(
            Group("Choose your armament", (
                _opt(HUNTERS_BOW, SHORT_SWORD), _opt(HUNTERS_BOW, SPEAR),
            )),
            Group("Choose one", (_opt(GEAR, RATIONS), _opt(GEAR, ARROWS))),
        ),
    ),
    "thief": ClassGear(
        fixed=(RATIONS, LEATHER, _i("Poison", "3 uses"), _i("10 coins")),
        groups=(
            Group("Choose your arms", (_opt(DAGGER, SHORT_SWORD), _opt(RAPIER))),
            Group("Choose a ranged weapon", (_opt(THROWING_DAGGERS), _opt(RAGGED_BOW, ARROWS))),
            Group("Choose one", (_opt(GEAR), _opt(POTION))),
        ),
    ),
    "wizard": ClassGear(
        fixed=(_i("Spellbook", weight=1), RATIONS),
        groups=(
            Group("Choose your defenses", (
                _opt(LEATHER), _opt(BOOKS, _i("3 healing potions")),
            )),
            Group("Choose your weapon", (_opt(DAGGER), _opt(STAFF))),
            Group("Choose one", (_opt(POTION), _opt(_i("3 antitoxins")))),
        ),
    ),
}


# -- the Fighter's signature weapon ---------------------------------------------------------

SIGNATURE_BASES = ("Sword", "Axe", "Hammer", "Spear", "Flail", "Fists")
SIGNATURE_RANGES = MELEE


@dataclass(frozen=True)
class Enhancement:
    text: str
    tags: Tuple[str, ...] = ()
    damage: int = 0
    piercing: int = 0
    weight: int = 0
    versatile: bool = False  # adds a second range


ENHANCEMENTS: Tuple[Enhancement, ...] = (
    Enhancement("Hooks and spikes. +1 damage, but +1 weight.", ("+1 damage",), damage=1, weight=1),
    Enhancement("Sharp. +2 piercing.", ("2 piercing",), piercing=2),
    Enhancement("Perfectly weighted. Add precise.", ("precise",)),
    Enhancement("Serrated edges. +1 damage.", ("+1 damage",), damage=1),
    Enhancement("Glows in the presence of one type of creature, your choice.", ("glows",)),
    Enhancement("Huge. Add messy and forceful.", ("messy", "forceful")),
    Enhancement("Versatile. Choose an additional range.", versatile=True),
    Enhancement("Well-crafted. -1 weight.", weight=-1),
)


def signature_weapon(base: str, ranges: Sequence[str], enhancements: Sequence[int]) -> Item:
    """Build the Fighter's weapon. All bases are 2 weight; enhancements stack."""
    chosen = [ENHANCEMENTS[i] for i in enhancements]
    tags: List[str] = list(ranges)
    damage = sum(e.damage for e in chosen)
    piercing = sum(e.piercing for e in chosen)
    if damage:
        tags.append(f"+{damage} damage")
    if piercing:
        tags.append(f"{piercing} piercing")
    for e in chosen:
        tags.extend(t for t in e.tags if "damage" not in t and "piercing" not in t)
    return Item(
        name=f"Signature {base.lower()}",
        tags=tuple(tags),
        damage=damage,
        piercing=piercing,
        weight=max(0, 2 + sum(e.weight for e in chosen)),
    )


# -- what gear does --------------------------------------------------------------------------


def items_of(gear: Sequence[Dict[str, Any]]) -> List[Item]:
    return [Item.from_dict(g) for g in gear]


GRIPS = ("shield", "both hands")


def has_grip(gear: Sequence[Dict[str, Any]]) -> bool:
    """A shield and a two-handed weapon: one or the other is in hand -- "It takes two hands to use
    it effectively." Only then does a grip matter."""
    items = items_of(gear)
    return any(i.shield for i in items) and any("two-handed" in i.tags for i in items)


def armor_of(gear: Sequence[Dict[str, Any]], grip: str = "") -> int:
    """The best worn armor plus a shield. Two coats of mail are not twice the armor, and one arm
    holds one shield. With both hands on a two-handed weapon, the shield isn't up."""
    items = items_of(gear)
    shield = max((i.shield for i in items), default=0)
    if grip == "both hands" and has_grip(gear):
        shield = 0
    return max((i.armor for i in items), default=0) + shield


def weapon_named(gear: Sequence[Dict[str, Any]], name: str, ranged: bool, ammo: int) -> Optional[Item]:
    """The weapon they said they use, if they carry it and it suits the move: "a player would
    have a hard time justifying" a Hand weapon at ten yards, and the GM judges the distance."""
    reach = RANGED if ranged else MELEE
    wanted = " ".join(str(name or "").split()).lower()
    fits = [i for i in items_of(gear) if any(r in reach for r in i.ranges) and (not i.needs_ammo or ammo > 0)]
    return next((i for i in fits if i.name.lower() == wanted), None) or next(
        (i for i in fits if wanted and wanted in i.name.lower()), None)


def weapon_for(gear: Sequence[Dict[str, Any]], ranged: bool, ammo: int, grip: str = "") -> Optional[Item]:
    """The weapon a character attacks with at that range, or None if they have none.

    The best of what reaches -- most damage, then most piercing -- and a weapon that
    needs ammo only counts while there is some.
    """
    reach = RANGED if ranged else MELEE
    usable = [
        i for i in items_of(gear)
        if any(r in reach for r in i.ranges) and (not i.needs_ammo or ammo > 0)
    ]
    if grip != "both hands" and has_grip(gear):  # shield up: a two-handed weapon only if nothing else
        usable = [i for i in usable if "two-handed" not in i.tags] or usable
    # On a tie a bow beats the spear: their best never throws away a weapon unasked.
    return max(usable, key=lambda i: (i.damage, i.piercing, "thrown" not in i.tags), default=None)


def rations_of(gear: Sequence[Dict[str, Any]]) -> int:
    """Ration uses carried. Gear saved before rations were counted has no "rations"
    field, so those items are read as the catalog has them -- "Dungeon rations" is 5."""
    return sum(
        int(g["rations"]) if "rations" in g else _CATALOG.get(g.get("name", ""), Item("")).rations
        for g in gear
    )


def ammo_of(gear: Sequence[Dict[str, Any]]) -> int:
    return sum(i.ammo for i in items_of(gear))


#: Every catalog item by name, for reading gear saved before a field existed.
_CATALOG: Dict[str, Item] = {v.name: v for v in list(globals().values()) if isinstance(v, Item)}


# -- the book's equipment list: what a found or bought item is -----------------------------

_EQUIPMENT_DATA = Path(__file__).parent / "data" / "equipment.json"


def _load_equipment() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_EQUIPMENT_DATA.read_text())["items"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"Dungeon World equipment data missing or unreadable at {_EQUIPMENT_DATA} ({exc}). "
            "Regenerate it with scripts/extract_dw_equipment.py."
        ) from exc


#: name (lowercase, straight quotes) -> the book's entry, with price and description.
EQUIPMENT: Dict[str, Dict[str, Any]] = {
    k.replace("’", "'"): v for k, v in _load_equipment().items()
}

_ARTICLE = re.compile(r"^(a|an|the|some|one)\s+", re.IGNORECASE)


def _key(name: str) -> str:
    return _ARTICLE.sub("", " ".join(str(name or "").replace("’", "'").split())).lower()


def catalog_entry(name: str) -> Optional[Dict[str, Any]]:
    """The book's entry for a name, forgiving articles, curly quotes and a plural."""
    key = _key(name)
    words = key.split()
    candidates = [key]
    # A plural can sit on any word: "healing potions", but "bundles of arrows".
    for i, word in enumerate(words):
        for cut in ("es", "s"):
            if word.endswith(cut) and len(word) > len(cut) + 1:
                candidates.append(" ".join(words[:i] + [word[: -len(cut)]] + words[i + 1:]))
    return next((EQUIPMENT[c] for c in candidates if c in EQUIPMENT), None)


def item_for(name: str) -> Tuple[Item, bool]:
    """(the item, whether the book knows it). An item the book does not list is carried
    with no effect on the rules -- a locket, a letter, a bone flute."""
    entry = catalog_entry(name)
    if entry is None:
        clean = _ARTICLE.sub("", " ".join(str(name).split()))
        return Item(clean[:1].upper() + clean[1:]), False
    return Item(
        name=entry["name"].replace("’", "'"),
        tags=tuple(entry["tags"]),
        armor=entry["armor"], shield=entry["shield"], damage=entry["damage"],
        piercing=entry["piercing"], ammo=entry["ammo"], needs_ammo=entry["needs_ammo"],
        weight=entry["weight"], rations=entry["rations"],
    ), True


def price_of(name: str) -> Optional[int]:
    entry = catalog_entry(name)
    return None if entry is None else int(entry["price"])
