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
from dataclasses import dataclass, replace
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
    def until_prepared(self) -> bool:
        """Alarm: "Until you prepare spells again"."""
        return "until you prepare spells again" in self.text.lower()

    @property
    def lasts(self) -> bool:
        """Still running after it is cast, so it goes on the sheet's ongoing list. Mirror Image
        lasts until a blow takes the image; Alarm until they prepare spells again."""
        return self.ongoing or self.ends_when_casting or self.until_prepared or self.name == "Mirror Image"

    @property
    def target(self) -> Optional[str]:
        """What the spell needs named: a creature in the scene, a character, or nothing."""
        if self.damage or self.name == "Consume Unlife":  # "the mindless undead creature you touch"
            return "creature"
        if self.name in ("Sleep", "Hold Person", "Cage", "Cause Fear"):
            return "creatures"  # whoever it takes, from the scene, so Gary can mark them
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


def _named_any(name: str, first: str) -> Optional[Spell]:
    """A spell from ``first``'s list, else any class's: Expanded Spellbook takes "a new
    spell from the spell list of any class"."""
    return _named(first, name) or next((s for c in SPELLS if c != first for s in [_named(c, name)] if s), None)


def lowered(sheet: Dict[str, Any], spell: Optional[Spell]) -> Optional[Spell]:
    """Chosen One, Anointed, Prodigy, Master: the chosen spell is "granted (or prepared) as
    if it was one level lower". A first-level spell lowered so becomes a rote or cantrip."""
    if spell is None or spell.name not in (sheet.get("lowered_spells") or ()):
        return spell
    return replace(spell, level=max(0, spell.level - 1))


def _other(cls: str) -> str:
    return "wizard" if cls == "cleric" else "cleric"


# -- what a caster has --------------------------------------------------------------------


#: Advanced moves that make a Paladin or Ranger a cleric for spells: "You gain the commune
#: and cast a spell cleric moves. When you select this move, treat yourself as a cleric of
#: level 1 for using spells. Every time you gain a level thereafter, increase your
#: effective cleric level by 1."
FAVOR_MOVES = ("Divine Favor", "God Amidst the Wastes")


def favored(sheet: Optional[Dict[str, Any]]) -> bool:
    """A Paladin with Divine Favor or a Ranger with God Amidst the Wastes."""
    return (sheet or {}).get("class") in ("Paladin", "Ranger") and any(
        m in ((sheet or {}).get("advanced_moves") or ()) for m in FAVOR_MOVES)


def caster_class(sheet: Optional[Dict[str, Any]]) -> Optional[str]:
    """"cleric" or "wizard", or None for everyone else."""
    return CASTERS.get((sheet or {}).get("class", "")) or ("cleric" if favored(sheet) else None)


def caster_level(sheet: Dict[str, Any]) -> int:
    """The level spells go by: a caster's own, or a favored Paladin's or Ranger's cleric
    level -- 1 at the level they took the move (``favor_from``), +1 for each since."""
    level = int(sheet.get("level", 1))
    if sheet.get("class") in CASTERS:
        return level
    return max(1, level - int(sheet.get("favor_from", level)) + 1)


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
    from .advanced import rote_spells

    for name in rote_spells(sheet):  # First Aid, Greater First Aid
        spell = _named("cleric", name)
        if spell is not None:
            out.append(Spell(**{**spell.__dict__, "level": 0}))
    for name in sheet.get("lowered_spells") or ():
        # A first-level spell made one level lower: always theirs now. A Wizard's must be in
        # their spellbook, as anything they prepare must.
        if cls == "wizard" and name not in (sheet.get("spellbook") or ()):
            continue
        spell = lowered(sheet, _named_any(name, cls))
        if spell is not None and spell.free and spell.name not in {s.name for s in out}:
            out.append(spell)
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
    level = caster_level(sheet)
    free = {s.name for s in free_spells(sheet)}
    if cls == "cleric":
        pool = [s for s in SPELLS["cleric"] if not s.free]
    else:
        pool = [s for s in (_named_any(n, "wizard") for n in sheet.get("spellbook") or ()) if s]
    borrowed = _borrowed(sheet)
    if borrowed is not None:
        pool.append(borrowed)
    pool = [lowered(sheet, s) for s in pool]
    return [s for s in pool if s.level <= level and s.name not in free]


def budget(sheet: Dict[str, Any]) -> int:
    """"...whose total levels don't exceed your own level+1.\""""
    return caster_level(sheet) + 1


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
        return lowered(sheet, borrowed)
    if cls == "wizard" and any(n.lower() == folded for n in sheet.get("spellbook") or ()):
        return lowered(sheet, _named_any(name, "wizard"))  # Expanded Spellbook's other-class spell
    return lowered(sheet, _named(cls, name))


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
    from .advanced import ongoing_relief

    relief = ongoing_relief(sheet)  # Serenity, Providence: the first penalties are ignored
    for spell, _ in running(sheet):
        if spell.penalty:
            if relief:
                relief -= 1
                continue
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
    servant = next((e for e in sheet.get("ongoing") or () if e.get("spell") == "Unseen Servant"), None)
    if servant is not None and ongoing:
        load = servant.get("carrying") or []
        weight = sum(int(g.get("weight", 0)) for g in load)
        ongoing = [o + (f" (carrying {', '.join(g['name'] for g in load) or 'nothing'}: {weight} of 3)"
                        if o.startswith("Unseen Servant") else "") for o in ongoing]
    if ongoing:
        parts.append(f"ongoing: {', '.join(ongoing)}")
    penalty, _ = casting_modifier(sheet)
    if penalty:
        parts.append(f"{penalty} to cast")
    if sheet.get("preparing"):
        parts.append(f"choosing new spells ({PREPARE_NAMES[cls]})")
    if sheet.get("contingency"):
        held = sheet["contingency"]
        parts.append(f"contingency: {held['spell']} until \"{held['trigger']}\" (release when it happens)")
    if sheet.get("summoning"):
        parts.append(f"summoning waiting: {sheet['summoning']['spell']}, up to {sheet['summoning']['traits']} traits "
                     "(summon_ally)")
    hold = [e for e in sheet.get("ongoing") or () if e.get("spell") == "Dominate"]
    if hold:
        parts.append(f"Dominate hold: {hold[0].get('hold', 0)}")
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
        if self.sheet.get("race") == "Human" and self.sheet.get("class") in CASTERS \
                and "borrowed_spell" not in self.sheet and self.borrowed is None:
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


# -- what spells do to creatures, and to the world ----------------------------------------

#: The condition a spell leaves on each creature it takes, until the spell ends.
CONDITION_OF: Dict[str, str] = {
    "Sleep": "asleep", "Charm Person": "charmed", "Hold Person": "held", "Cage": "caged",
    "Cause Fear": "afraid", "Polymorph": "polymorphed", "Dominate": "dominated",
}
#: Harm breaks these: Sleep ("They awake as normal: loud noises, jolts, pain"), Charm Person
#: ("until they take damage"), Hold Person ("ends immediately if the target takes damage").
BROKEN_BY_HARM = ("asleep", "charmed", "held")
#: A creature in one of these can't attack anyone.
HELPLESS = ("asleep", "held", "caged")
#: Spells laid on a place: they stay where they were cast (and Cloudkill, "so long as you can see
#: the affected area", ends when the party leaves it).
AREA_SPELLS = ("Cloudkill", "Darkness", "Sanctuary", "Alarm")

#: Summon Monster and Animate Dead: the dice for how many traits, and the traits.
SUMMONS: Dict[str, Dict[str, Any]] = {
    "Summon Monster": {"dice": "1d6", "traits": {
        "boost": "+2 instead of +1 to one stat", "careful": "it's not reckless", "d8": "it does 1d8 damage",
        "durable": "its bond to your plane is strong: +2 HP for each level you have",
        "adaptation": "it has some useful adaptation"}},
    "Animate Dead": {"dice": "1d4", "traits": {
        "boost": "it's talented: +2 to one stat", "durable": "it's durable: +2 HP for each level you have",
        "brain": "it has a functioning brain and can complete complex tasks",
        "lifelike": "it does not appear obviously dead, at least for a day or two"}},
}
DOMINATE_COMMANDS = ("speak a few words of your choice", "give you something they hold",
                     "make a concerted attack on a target of your choice", "truthfully answer one question")
