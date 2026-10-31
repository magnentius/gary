"""Blob (Player Compendium): slime minions, ooze form and slime copies.

Ambeoid Overlord: "A piece of the thing that engulfed you is now yours to command. Treat it
as your character: you say what it does and it triggers basic moves as a character would.
Slime, ooze, or pudding characters have +1 STR, +0 DEX, +2 Con, and no Int, Wis, or CHA (they
cannot make moves with those stats). They have d4 damage and 4 HP but if they reach 0 HP they
splatter and become inert instead of dying -- scoop it together again and it'll be fine. They
do not gain XP."

Minions live on their owner's sheet as ``minions``; a move made by one rolls its stat and
deals its d4. Divide and Conquer's copies are minions too, flagged ``copy``: they "use your
stats" and cost the owner -1 Int, Wis and Cha each (``penalty``). Scuzzy Metamorphosis'
ooze form is the sheet's ``ooze``: +1 armor, -1 ongoing to Int, Wis and Cha.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .sheets import SheetError

BODY = ("str", "dex", "con")
MIND = ("int", "wis", "cha")
MINION_HP = 4
MINION_DAMAGE = "d4"


def make(name: str) -> Dict[str, Any]:
    name = " ".join(str(name or "").split())[:40]
    if not name:
        raise SheetError("Name the slime.")
    return {"name": name, "str": 1, "dex": 0, "con": 2, "hp": MINION_HP, "max_hp": MINION_HP}


def make_copy(owner: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Divide and Conquer: "You can only create more copies so long as one of your Int, Wis,
    or Cha is +1 or greater"."""
    from .sheets import stat_modifier

    if max(stat_modifier(owner, s) for s in MIND) < 1:
        raise SheetError(f"{owner['name']} has no Int, Wis or Cha of +1 or more left to divide.")
    copy = make(name or f"{owner['name']}'s copy")
    copy.update(copy=True, hp=int(owner["max_hp"]), max_hp=int(owner["max_hp"]))
    return copy


def find(sheet: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    folded = " ".join(str(name or "").split()).lower()
    return next((m for m in sheet.get("minions") or () if m["name"].lower() == folded), None)


def limit(sheet: Dict[str, Any]) -> int:
    """How many slime minions they command: one, or 2+Cha with Slime Recruiter."""
    from .sheets import stat_modifier

    names = set(sheet.get("advanced_moves") or ())
    return max(1, 2 + stat_modifier(sheet, "cha")) if "Slime Recruiter" in names else 1


def modifier(minion: Dict[str, Any], owner: Dict[str, Any], stat: str) -> int:
    """What a minion rolls: its own Str, Dex or Con; a copy rolls its owner's."""
    from .sheets import stat_modifier

    if stat in MIND:
        raise SheetError(f"{minion['name']} has no {stat.upper()}: a slime cannot make moves with it.")
    return stat_modifier(owner, stat) if minion.get("copy") else int(minion[stat])


def penalty(sheet: Dict[str, Any], stat: str) -> int:
    """What Blob takes off the owner's Int, Wis or Cha: -1 in ooze form, -1 for each copy."""
    if stat not in MIND:
        return 0
    copies = sum(1 for m in sheet.get("minions") or () if m.get("copy"))
    return copies + (1 if sheet.get("ooze") else 0)


def describe(sheet: Dict[str, Any]) -> str:
    parts = []
    for m in sheet.get("minions") or ():
        state = "inert" if m["hp"] <= 0 else f"HP {m['hp']}/{m['max_hp']}"
        stats = "a copy, using their stats" if m.get("copy") else f"STR {m['str']:+d} DEX {m['dex']:+d} CON {m['con']:+d}"
        parts.append(f"{m['name']} ({stats}, {state})")
    line = "Slime minions — " + "; ".join(parts) if parts else ""
    if sheet.get("ooze"):
        line = (line + " · " if line else "") + "in ooze form (+1 armor, -1 Int/Wis/Cha)"
    return line
