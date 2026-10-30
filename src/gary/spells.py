"""Cleric and Wizard spells: what a caster has, and what casting one does (Dungeon World,
the Cleric and Wizard classes).

On the sheet, beside the rest:

- ``spellbook`` -- a Wizard's spells (not the cantrips, which every Wizard has).
- ``borrowed_spell`` -- a Human's one spell from the other class's list ("Your faith is
  diverse"; "Choose one cleric spell").
- ``spells`` -- what is prepared or granted now, chosen at Commune or Prepare Spells.
- ``lost`` -- spells revoked or forgotten on a 7-9, gone until the next Commune or
  Prepare Spells.
- ``cast_penalty`` -- how many times a 7-9 took "-1 ongoing to cast a spell"; cleared at
  the next Commune or Prepare Spells.
- ``ongoing`` -- spells still running, as ``{"spell", "target"}``. Most cost -1 to cast
  while they last; a few forbid casting at all.
- ``preparing`` -- an hour of prayer or study has happened and the choice is the
  player's to make (the Choose spells button).

Rotes and cantrips are never stored: a caster always has their own.

The spell text is the authors', extracted by scripts/extract_dw_spells.py. What a spell
does to a number Gary tracks -- Cure Light Wounds heals 1d8, Fireball's 2d6 ignores armor
-- is hand-written below, like the moves. Everything else a spell does is the model's to
narrate from the text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_DATA = Path(__file__).parent / "data" / "spells.json"

CASTERS = {"Cleric": "cleric", "Wizard": "wizard"}

#: What Commune is called for each class, for the table.
PREPARE_NAMES = {"cleric": "Commune", "wizard": "Prepare Spells"}

#: A Wizard starts with this many first-level spells in their spellbook.
STARTING_SPELLBOOK = 3

#: Discord shows at most 25 options in a list. A Human's borrowed spell is chosen once,
#: at level 1, from a list of 30; levels 7 and 9 are left out, as nobody can cast them
#: until levelling exists.
MAX_BORROWED_LEVEL = 5

BLESS = "Bless"
MAGIC_WEAPON = "Magic Weapon"


class SpellError(ValueError):
    """A choice the rules do not allow. The message is shown to the player as is."""


@dataclass(frozen=True)
class Spell:
    name: str
    level: int  # 0 for rotes and cantrips
    cls: str  # "cleric" | "wizard": whose list it is on
    ongoing: bool
    school: str
    text: str
    # What it does to the numbers Gary tracks; see _RULES.
    heals: Optional[str] = None  # dice, or "max_hp" (Heal: up to your own maximum)
    damage: Optional[str] = None  # dice
    ignores_armor: bool = False
    self_damage: Optional[str] = None  # Harm: what it costs the caster, ignoring armor
    area: bool = False  # Fireball: "your target and everyone nearby"
    blocks: bool = False  # while running, no casting at all

    @property
    def free(self) -> bool:
        return self.level == 0

    @property
    def penalty(self) -> bool:
        """"While this spell is ongoing you take -1 to cast a spell.\""""
        return "-1 to cast a spell" in self.text

    @property
    def blocks_casting(self) -> bool:
        return self.blocks or "can’t cast a spell" in self.text or "cannot cast a spell" in self.text

    @property
    def ends_when_casting(self) -> bool:
        """Hold Person, Cage, Polymorph: they last until the caster casts again."""
        return "until you cast" in self.text.lower()

    @property
    def lasts(self) -> bool:
        """Still running after it is cast, so it goes on the sheet's ongoing list."""
        return self.ongoing or self.ends_when_casting

    @property
    def target(self) -> Optional[str]:
        """What the spell needs named: a creature in the scene, a character, or nothing."""
        if self.damage:
            return "creature"
        if self.heals or self.name == BLESS:
            return "character"
        return None

    def label(self) -> str:
        level = "rote" if self.free and self.cls == "cleric" else "cantrip" if self.free \
            else f"level {self.level}"
        return f"{self.name} ({level}{', ongoing' if self.ongoing else ''})"


#: The spells that change a number Gary tracks, by (list, name).
_RULES: Dict[tuple, Dict[str, Any]] = {
    ("cleric", "Cure Light Wounds"): {"heals": "1d8"},
    ("cleric", "Cure Moderate Wounds"): {"heals": "2d8"},
    ("cleric", "Cure Critical Wounds"): {"heals": "3d8"},
    ("cleric", "Heal"): {"heals": "max_hp"},
    ("cleric", "Harm"): {"damage": "2d8", "ignores_armor": True, "self_damage": "1d6"},
    ("wizard", "Magic Missile"): {"damage": "2d4"},
    ("wizard", "Fireball"): {"damage": "2d6", "ignores_armor": True, "area": True},
    # "While this spell is ongoing you lose access to all your wizard moves" -- casting
    # among them.
    ("wizard", "Mimic"): {"blocks": True},
}


def _load() -> Dict[str, List[Spell]]:
    try:
        raw = json.loads(_DATA.read_text())["classes"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(
            f"Dungeon World spell data missing or unreadable at {_DATA} ({exc}). "
            "Regenerate it with scripts/extract_dw_spells.py."
        ) from exc
    return {
        cls: [Spell(name=s["name"], level=int(s["level"]), cls=cls, ongoing=bool(s["ongoing"]),
                    school=s.get("school", ""), text=s["text"], **_RULES.get((cls, s["name"]), {}))
              for s in spells]
        for cls, spells in raw.items()
    }


SPELLS: Dict[str, List[Spell]] = _load()


def _named(cls: str, name: str) -> Optional[Spell]:
    folded = str(name or "").strip().lower()
    return next((s for s in SPELLS.get(cls, ()) if s.name.lower() == folded), None)


def _other(cls: str) -> str:
    return "wizard" if cls == "cleric" else "cleric"


# -- what a caster has --------------------------------------------------------------------


def caster_class(sheet: Optional[Dict[str, Any]]) -> Optional[str]:
    """"cleric" or "wizard", or None for everyone else."""
    return CASTERS.get((sheet or {}).get("class", ""))


def free_spells(sheet: Dict[str, Any]) -> List[Spell]:
    """Rotes or cantrips, always prepared, plus what the race adds."""
    cls = caster_class(sheet)
    if cls is None:
        return []
    out = [s for s in SPELLS[cls] if s.free]
    if cls == "wizard" and sheet.get("race") == "Elf":
        # "Detect Magic is a cantrip for you."
        out.append(_as_free(_named("wizard", "Detect Magic")))
    if cls == "cleric" and sheet.get("race") == "Dwarf":
        # "...a special version of Words of the Unspeaking as a rote which only works on
        # stone."
        words = _named("cleric", "Words of the Unspeaking")
        out.append(Spell(name="Words of the Unspeaking (stone)", level=0, cls="cleric",
                         ongoing=False, school="", text=words.text + " Only works on stone."))
    return out


def _as_free(spell: Spell) -> Spell:
    return Spell(**{**spell.__dict__, "level": 0})


def _borrowed(sheet: Dict[str, Any]) -> Optional[Spell]:
    cls = caster_class(sheet)
    name = sheet.get("borrowed_spell")
    return _named(_other(cls), name) if cls and name else None


def preparable(sheet: Dict[str, Any]) -> List[Spell]:
    """What Commune or Prepare Spells may choose from: a Cleric's whole list, a Wizard's
    spellbook, and a Human's borrowed spell -- none above the caster's level."""
    cls = caster_class(sheet)
    if cls is None:
        return []
    level = int(sheet.get("level", 1))
    free = {s.name for s in free_spells(sheet)}
    if cls == "cleric":
        pool = [s for s in SPELLS["cleric"] if not s.free]
    else:
        pool = [s for s in (_named("wizard", n) for n in sheet.get("spellbook") or ()) if s]
    borrowed = _borrowed(sheet)
    if borrowed is not None:
        pool.append(borrowed)
    return [s for s in pool if s.level <= level and s.name not in free]


def budget(sheet: Dict[str, Any]) -> int:
    """"...whose total levels don't exceed your own level+1.\""""
    return int(sheet.get("level", 1)) + 1


def find(sheet: Dict[str, Any], name: str) -> Optional[Spell]:
    """Any spell this caster could have, by name, however the model cased it."""
    folded = str(name or "").strip().lower()
    for s in [*free_spells(sheet), *preparable(sheet)]:
        if s.name.lower() == folded:
            return s
    cls = caster_class(sheet)
    if cls is None:
        return None
    borrowed = _borrowed(sheet)
    if borrowed is not None and borrowed.name.lower() == folded:
        return borrowed
    return _named(cls, name)


def castable(sheet: Dict[str, Any]) -> List[Spell]:
    """What they can cast right now: free spells and what is prepared, less what is lost."""
    lost = {n.lower() for n in sheet.get("lost") or ()}
    have = [*free_spells(sheet), *(s for s in (find(sheet, n) for n in sheet.get("spells") or ()) if s)]
    return [s for s in have if s.name.lower() not in lost]


def running(sheet: Dict[str, Any]) -> List[tuple]:
    """(spell, target) for each ongoing spell, oldest first."""
    out = []
    for entry in sheet.get("ongoing") or ():
        spell = find(sheet, entry.get("spell", ""))
        if spell is not None:
            out.append((spell, entry.get("target", "")))
    return out


def casting_modifier(sheet: Dict[str, Any]) -> tuple:
    """(the penalty to Cast a Spell, why), from 7-9 penalties and ongoing spells."""
    reasons = []
    n = int(sheet.get("cast_penalty", 0))
    if n:
        reasons.append(f"-{n} ongoing")
    for spell, _ in running(sheet):
        if spell.penalty:
            n += 1
            reasons.append(f"-1 {spell.name}")
    return -n, reasons


def blocking(sheet: Dict[str, Any]) -> Optional[Spell]:
    """A running spell that forbids casting (Invisibility, Dominate, Mimic), if any."""
    return next((s for s, _ in running(sheet) if s.blocks_casting), None)


def describe(sheet: Dict[str, Any]) -> str:
    """One line on a caster's spells, for the player and the model. "" for anyone else."""
    cls = caster_class(sheet)
    if cls is None:
        return ""
    lost = {n.lower() for n in sheet.get("lost") or ()}
    prepared = [s for s in (find(sheet, n) for n in sheet.get("spells") or ()) if s]
    shown = [s.name + (" (lost)" if s.name.lower() in lost else "") for s in prepared]
    word = "Granted" if cls == "cleric" else "Prepared"
    parts = [f"{word}: {', '.join(shown) if shown else 'none'}"]
    free = [s.name + (" (lost)" if s.name.lower() in lost else "") for s in free_spells(sheet)]
    parts.append(f"{'rotes' if cls == 'cleric' else 'cantrips'}: {', '.join(free)}")
    if cls == "wizard":
        parts.append(f"spellbook: {', '.join(sheet.get('spellbook') or ()) or 'empty'}")
    if sheet.get("borrowed_spell"):
        parts.append(f"borrowed: {sheet['borrowed_spell']}")
    ongoing = [s.name + (f" on {t}" if t else "") for s, t in running(sheet)]
    if ongoing:
        parts.append(f"ongoing: {', '.join(ongoing)}")
    penalty, _ = casting_modifier(sheet)
    if penalty:
        parts.append(f"{penalty} to cast")
    if sheet.get("preparing"):
        parts.append(f"choosing new spells ({PREPARE_NAMES[cls]})")
    return "Spells — " + " · ".join(parts) + "."


# -- choosing spells: Commune, Prepare Spells, and the first time -------------------------


class Preparation:
    """A caster choosing spells, step by step, checked at every step like a sheets.Draft.

    The first time, a Wizard fills their spellbook and a Human picks their borrowed spell;
    after that it is only the preparing. ``changes()`` is what goes on the sheet.
    """

    def __init__(self, sheet: Dict[str, Any]) -> None:
        if caster_class(sheet) is None:
            raise SpellError(f"{sheet.get('name', 'They')} casts no spells.")
        self.sheet = sheet
        self.spellbook: Optional[List[str]] = None
        self.borrowed: Optional[str] = None
        self.prepared: Optional[List[str]] = None

    @property
    def cls(self) -> str:
        return caster_class(self.sheet)

    def _with_choices(self) -> Dict[str, Any]:
        out = dict(self.sheet)
        if self.spellbook is not None:
            out["spellbook"] = self.spellbook
        if self.borrowed is not None:
            out["borrowed_spell"] = self.borrowed
        return out

    def step(self) -> Optional[str]:
        """"spellbook", "borrowed", "prepare", or None when done."""
        if self.cls == "wizard" and "spellbook" not in self.sheet and self.spellbook is None:
            return "spellbook"
        if self.sheet.get("race") == "Human" and "borrowed_spell" not in self.sheet \
                and self.borrowed is None:
            return "borrowed"
        if self.prepared is None:
            return "prepare"
        return None

    def options(self) -> List[Spell]:
        step = self.step()
        if step == "spellbook":
            free = {s.name for s in free_spells(self.sheet)}  # an Elf's Detect Magic
            return [s for s in SPELLS["wizard"] if s.level == 1 and s.name not in free]
        if step == "borrowed":
            return [s for s in SPELLS[_other(self.cls)] if 0 < s.level <= MAX_BORROWED_LEVEL]
        if step == "prepare":
            return preparable(self._with_choices())
        return []

    def counts(self) -> tuple:
        """(fewest, most) to pick at this step."""
        step = self.step()
        if step == "spellbook":
            return STARTING_SPELLBOOK, STARTING_SPELLBOOK
        if step == "borrowed":
            return 1, 1
        if step == "prepare":
            return 1, max(1, min(len(self.options()), budget(self.sheet)))
        return 0, 0

    def choose(self, names: Sequence[str]) -> None:
        step = self.step()
        offered = {s.name: s for s in self.options()}
        names = list(names)
        unknown = [n for n in names if n not in offered]
        if unknown:
            raise SpellError(f"{', '.join(unknown)} is not one of the choices.")
        if len(set(names)) != len(names):
            raise SpellError("Choose each spell once.")
        fewest, most = self.counts()
        if not fewest <= len(names) <= most:
            raise SpellError(f"Choose {fewest}." if fewest == most else f"Choose {fewest} to {most}.")
        if step == "spellbook":
            self.spellbook = names
        elif step == "borrowed":
            self.borrowed = names[0]
        elif step == "prepare":
            total = sum(offered[n].level for n in names)
            if total > budget(self.sheet):
                raise SpellError(
                    f"Those come to {total} levels; you can hold {budget(self.sheet)} "
                    f"(your level + 1)."
                )
            self.prepared = names

    def changes(self) -> Dict[str, Any]:
        if self.step() is not None:
            raise SpellError("Still choosing.")
        out: Dict[str, Any] = {"spells": list(self.prepared), "lost": [], "cast_penalty": 0,
                               "preparing": False}
        if self.spellbook is not None:
            out["spellbook"] = list(self.spellbook)
        if self.borrowed is not None:
            out["borrowed_spell"] = self.borrowed
        return out


def forgetting() -> Dict[str, Any]:
    """The sheet changes at the start of Commune or Prepare Spells: "Lose any spells you
    already have prepared" -- and with them what was lost and the -1 ongoing, both of
    which last only until now."""
    return {"spells": [], "lost": [], "cast_penalty": 0, "preparing": True}


def needs_setup(sheet: Dict[str, Any]) -> bool:
    """A caster who has never chosen spells: made before spells existed, or just made."""
    return caster_class(sheet) is not None and "spells" not in sheet
