"""Moves that answer a blow once it has landed: "when you take damage, you may...".

A creature's hit is applied at once, as always. If anyone at the table has a move that
answers it, the hit message carries a Reduce damage button; whoever presses it sees only
what they can do about that hit, and choosing gives the HP back. Nothing waits on it, and
ignoring it is taking the blow.

What answers a blow (the book's words, shortened):

- **Spell Defense** (Wizard): end an ongoing spell and "subtract its level from the damage".
- **Armor Mastery** / **Armored Perfection** (Fighter): "the damage is negated but you must
  reduce the armor value of your armor or shield (your choice) by 1" -- at 0 it is
  destroyed. Perfection also gives +1 forward against the attacker.
- **Bloody Aegis** (Paladin): "you take no damage but instead suffer a debility of your
  choice"; Indomitable adds +1 forward against whatever caused it.
- **Shed** (Druid): while shapeshifted, revert to your natural form to negate the damage.
- **Man's Best Friend** (Ranger): the companion takes it; its ferocity is 0 until a rest.
- **Divine Intervention** / **Divine Invincibility** (Cleric): spend Commune's hold when you
  or an ally takes damage -- the deity intervenes. The one answer that is not the
  victim's own.
- **Penitent** / **Martyr** (Cleric): "take +1d4 damage (ignoring armor)" for +1 forward to
  cast a spell -- more damage, not less, but the same moment.

Each hit is remembered on the victim's sheet (``last_hit``), so only the latest blow can be
answered, and only once; the pain is a separate choice from the reductions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from . import advanced, spells
from .sheets import DEBILITIES


@dataclass(frozen=True)
class Reaction:
    key: str  # what react() is told: "spell:0", "armor:3", "aegis:weak", "shed", "pet", "deity", "pain"
    label: str
    restores: int  # HP given back; 0 for the pain
    move: str


def options(presser: str, mine: Dict[str, Any], victim: str, theirs: Dict[str, Any]) -> List[Reaction]:
    """What ``presser`` (sheet ``mine``) can do about the last blow ``victim`` took."""
    hit = theirs.get("last_hit") or {}
    dealt = int(hit.get("dealt", 0))
    if mine.get("dead") or theirs.get("dead"):
        return []
    out: List[Reaction] = []
    own = presser == victim
    if dealt > 0 and not hit.get("reduced"):
        if own:
            out += _own_reductions(mine, dealt)
        if int(mine.get("intervention", 0)) > 0:
            deity = (mine.get("choices") or {}).get("deity") or "their deity"
            whose = "you" if own else victim
            out.append(Reaction("deity", f"Call on {deity} to spare {whose} (no damage)"[:80], dealt,
                                "Divine Intervention"))
    if (own and not hit.get("pain") and (advanced.has(mine, "Martyr") or advanced.has(mine, "Penitent"))
            and int(mine.get("hp", 0)) > 4):
        out.append(Reaction("pain", "Embrace the pain: +1d4 damage, +1 forward to cast", 0,
                            "Martyr" if advanced.has(mine, "Martyr") else "Penitent"))
    return out


def _own_reductions(sheet: Dict[str, Any], dealt: int) -> List[Reaction]:
    out: List[Reaction] = []
    if sheet.get("class") == "Wizard":
        for i, (spell, _) in enumerate(spells.running(sheet)):
            if spell.level >= 1:
                out.append(Reaction(f"spell:{i}", f"End {spell.name}: take {min(spell.level, dealt)} less",
                                    min(spell.level, dealt), "Spell Defense"))
    mastery = next((n for n in ("Armored Perfection", "Armor Mastery") if advanced.has(sheet, n)), None)
    if mastery:
        for i, item in enumerate(sheet.get("gear") or ()):
            value = int(item.get("armor", 0)) or int(item.get("shield", 0))
            if value > 0:
                fate = "destroyed" if value == 1 else "armor -1"
                out.append(Reaction(f"armor:{i}", f"Let {item['name']} take it ({fate})"[:80], dealt, mastery))
    if advanced.has(sheet, "Bloody Aegis"):
        have = set(sheet.get("debilities") or ())
        for name in DEBILITIES:
            if name not in have:
                out.append(Reaction(f"aegis:{name}", f"Grit your teeth: no damage, but {name.title()}",
                                    dealt, "Bloody Aegis"))
    if advanced.has(sheet, "Shed") and sheet.get("shape"):
        out.append(Reaction("shed", f"Shed the {sheet['shape'].get('form', 'form')} (no damage)"[:80], dealt, "Shed"))
    pet = sheet.get("companion")
    if advanced.has(sheet, "Man's Best Friend") and pet and not pet.get("spent") and int(pet.get("ferocity", 0)) > 0:
        out.append(Reaction("pet", f"{pet['name']} takes the blow (no damage)"[:80], dealt, "Man's Best Friend"))
    return out


def worn_down(item: Dict[str, Any]) -> Dict[str, Any]:
    """Armor Mastery: the item with 1 less armor (or shield), or {} once it reaches 0."""
    field = "armor" if int(item.get("armor", 0)) > 0 else "shield"
    value = int(item[field]) - 1
    if value <= 0:
        return {}
    tags = [("+" if t.startswith("+") else "") + f"{value} armor"
            if t.endswith(" armor") and t.split()[0].lstrip("+").isdigit() else t
            for t in item.get("tags") or ()]
    return {**item, field: value, "tags": tags}


def hold_from_commune(sheet: Dict[str, Any]) -> int:
    """Commune's hold for Divine Intervention: "you get 1 hold and lose any hold you already
    had" (2 with Divine Invincibility)."""
    if advanced.has(sheet, "Divine Invincibility"):
        return 2
    return 1 if advanced.has(sheet, "Divine Intervention") else 0
