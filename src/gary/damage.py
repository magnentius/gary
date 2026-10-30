"""Damage dice and armor (Dungeon World, "Damage").

Every damage expression in the rules is one of three shapes -- a die (``d8+1``), the best
of two (``b[2d10]+4``), or the worst of two (``w[2d8]``) -- with an optional modifier,
sometimes written inside the brackets (``w[2d8-2]``). A move can add or take away a d6
on top (Hack and Slash's +1d6, Volley's -1d6). Armor takes its value off what lands;
piercing ignores that much armor, and some attacks ignore it entirely.

Pure: the dice come from the ``Dice`` port, so a test can say exactly what lands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .ports import Dice

_EXPR = re.compile(
    r"^\s*(?:(?P<mode>[bw])\[2d(?P<bsides>\d+)(?P<inner>[+-]\d+)?\]|d(?P<sides>\d+))"
    r"(?P<mod>[+-]\d+)?\s*$"
)

#: The extra a move's option adds: "+1d6" or "-1d6".
_EXTRA = re.compile(r"^\s*(?P<sign>[+-])1d6\s*$")


@dataclass(frozen=True)
class DamageSpec:
    sides: int
    mode: str = "single"  # single | best | worst
    modifier: int = 0

    @property
    def dice(self) -> str:
        return f"{2 if self.mode != 'single' else 1}d{self.sides}"

    def __str__(self) -> str:
        core = f"d{self.sides}" if self.mode == "single" else f"{self.mode[0]}[2d{self.sides}]"
        return core + (f"{self.modifier:+d}" if self.modifier else "")


def parse(expr: str) -> DamageSpec:
    """``"b[2d10]+4"`` -> DamageSpec(10, "best", 4). Raises ValueError on anything else."""
    m = _EXPR.match(expr or "")
    if not m:
        raise ValueError(f"not a damage expression: {expr!r}")
    modifier = int(m["inner"] or 0) + int(m["mod"] or 0)
    if m["mode"]:
        return DamageSpec(int(m["bsides"]), "best" if m["mode"] == "b" else "worst", modifier)
    return DamageSpec(int(m["sides"]), "single", modifier)


@dataclass(frozen=True)
class DamageRoll:
    spec: DamageSpec
    faces: tuple  # every die the spec rolled
    kept: int  # the face that counts: the only one, the best, or the worst
    extra: Optional[str] = None  # "+1d6" / "-1d6"
    extra_face: Optional[int] = None

    @property
    def total(self) -> int:
        value = self.kept + self.spec.modifier
        if self.extra_face is not None:
            value += self.extra_face if self.extra.startswith("+") else -self.extra_face
        return max(0, value)

    def render(self) -> str:
        """`d10+3: 7 +3 = 10`, `b[2d8]: 3, 7 → 7`, `d6 -1d6: 4 - 2 = 2`."""
        parts = [str(self.spec)]
        if self.extra:
            parts.append(self.extra)
        rolled = (
            str(self.kept) if len(self.faces) == 1
            else ", ".join(map(str, self.faces)) + f" → {self.kept}"
        )
        arithmetic = rolled
        if self.spec.modifier:
            arithmetic += f" {self.spec.modifier:+d}"
        if self.extra_face is not None:
            arithmetic += f" {self.extra[0]} {self.extra_face}"
        if self.spec.modifier or self.extra_face is not None:
            arithmetic += f" = {self.total}"
        return f"{' '.join(parts)}: {arithmetic}"


def roll(dice: Dice, spec: DamageSpec, extra: Optional[str] = None) -> DamageRoll:
    faces = dice.roll(spec.dice).faces
    kept = _kept(spec, faces)
    extra_face = None
    if extra:
        if not _EXTRA.match(extra):
            raise ValueError(f"not a damage extra: {extra!r}")
        extra_face = dice.roll("1d6").faces[0]
    return DamageRoll(spec, faces, kept, extra, extra_face)


def _kept(spec: DamageSpec, faces) -> int:
    return max(faces) if spec.mode == "best" else min(faces) if spec.mode == "worst" else faces[0]


def landed(spec: DamageSpec, faces) -> DamageRoll:
    """A damage roll already made, rebuilt from its faces -- one held while a defender
    decides how to spend hold, across a restart."""
    return DamageRoll(spec, tuple(faces), _kept(spec, tuple(faces)))


def severity(dealt: int, max_hp: int, hp_after: int) -> str:
    """How bad a hit is, in words the model should narrate it at.

    Measured against the target's max HP, not the raw number: 2 damage is a graze on a
    fighter with 25 HP and half of a goblin. Without this the model told a 2-point stab
    into a 20 HP dwarf as "deep into your ribs" -- the numbers right, the danger wrong,
    and the table unable to judge a fight from its narration.
    """
    if dealt <= 0:
        return "no harm — armor or luck turned it"
    if hp_after <= 0:
        return "taken down"
    share = dealt / max(1, max_hp)
    if share <= 0.15:
        return "a graze"
    if share <= 0.35:
        return "a solid hit"
    if share <= 0.6:
        return "a serious wound"
    return "a terrible wound"


def through_armor(total: int, armor: int, piercing: int = 0, ignores_armor: bool = False) -> int:
    """What actually comes off HP."""
    effective = 0 if ignores_armor else max(0, int(armor) - int(piercing))
    return max(0, int(total) - effective)
