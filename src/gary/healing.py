"""Healing items: what using one does (Dungeon World, "Dungeon Gear").

From the book's own descriptions:
- **Healing potion.** Whoever drinks it heals 10 (or removes a debility -- debilities are
  not tracked, so 10 HP). Yours to drink, or to pour down a fallen friend's throat. Gone
  once drunk.
- **Bandages.** A few minutes binding *someone else's* wounds: heal them 4, spend a use.
- **Poultices and herbs.** Carefully treating anyone's wounds: heal them 7, spend a use.

Bandages and poultices are *slow* -- minutes, not a moment -- so not while creatures are
still standing. A potion is drunk in a breath, fight or no fight.

Pure: takes sheets, returns new ones; the caller journals them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .gear import item_for


@dataclass(frozen=True)
class Remedy:
    heals: int
    target: str  # "other" (not on yourself) | "any"
    slow: bool
    consumed: bool  # gone after one use, rather than spending one of several


REMEDIES: Dict[str, Remedy] = {
    "Healing Potion": Remedy(heals=10, target="any", slow=False, consumed=True),
    "Bandages": Remedy(heals=4, target="other", slow=True, consumed=False),
    "Poultices and Herbs": Remedy(heals=7, target="any", slow=True, consumed=False),
}


class HealingError(ValueError):
    """Refused. The message goes back to the model as is."""


def _uses(item: Dict[str, Any]) -> int:
    """Uses left: the counted field once one has been spent, else the book's "N uses"."""
    if "uses" in item:
        return int(item["uses"])
    return next((int(m[1]) for t in item.get("tags", ()) if (m := re.fullmatch(r"(\d+) uses?", t))), 1)


def use(
    user: Dict[str, Any], item_name: str, target: Optional[Dict[str, Any]], fighting: bool
) -> Tuple[Dict[str, Any], Dict[str, Any], int, str]:
    """Use a healing item. Returns (user's new sheet, target's new sheet, HP healed, the
    remedy's name). ``target`` is None for "on themselves"; ``fighting`` is whether
    creatures are still standing."""
    wanted, known = item_for(item_name)
    remedy = REMEDIES.get(wanted.name) if known else None
    if remedy is None:
        healers = ", ".join(r.lower() for r in REMEDIES)
        raise HealingError(f"{wanted.name} does not heal. What does: {healers}.")

    gear = list(user.get("gear") or [])
    index = next((i for i, g in enumerate(gear) if g["name"].lower() == wanted.name.lower()), None)
    if index is None:
        raise HealingError(f"{user['name']} has no {wanted.name.lower()}.")

    on_self = target is None or target["name"] == user["name"]
    if remedy.target == "other" and on_self:
        raise HealingError(f"{wanted.name} are for binding someone else's wounds, not your own.")
    if remedy.slow and fighting:
        raise HealingError(f"{wanted.name} take minutes, not a moment: not while creatures "
                           "are still standing.")

    patient = user if on_self else target
    if patient.get("dead"):
        raise HealingError(f"{patient['name']} is dead.")
    healed = max(0, min(remedy.heals, int(patient["max_hp"]) - int(patient["hp"])))

    item = dict(gear[index])
    left = _uses(item) - 1
    if remedy.consumed or left <= 0:
        gear.pop(index)
    else:
        gear[index] = {**item, "uses": left}
    new_user = {**user, "gear": gear}
    new_patient = {**patient, "hp": int(patient["hp"]) + healed}
    if on_self:
        new_user = {**new_user, "hp": new_patient["hp"]}
        new_patient = new_user
    return new_user, new_patient, healed, wanted.name
