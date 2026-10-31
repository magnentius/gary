"""Instant NPCs (Dungeon World, Appendix 4): "When you need a quick NPC all you need is an
instinct and some way to pursue it. We call that a knack ... Combine the two and you have an
NPC who has something they want and a way to try to get it."

Gary rolls the d100 tables -- with its own dice, like everything else -- and draws a name
the campaign has not used yet; the model makes the NPC from them (upsert_npc).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Iterable

_DATA = json.loads((Path(__file__).parent / "data" / "npcs.json").read_text(encoding="utf-8"))
INSTINCTS = _DATA["instincts"]
KNACKS = _DATA["knacks"]
NAMES = _DATA["names"]


def roll(die: Callable[[str], int], used: Iterable[str] = ()) -> Dict[str, object]:
    """An instinct and a knack off the d100 tables, and a name nobody here has yet."""
    instinct, knack = die("1d100"), die("1d100")
    taken = {n.lower() for n in used}
    free = [n for n in NAMES if n.lower() not in taken] or NAMES
    name = free[(die(f"1d{len(free)}") - 1)] if len(free) > 1 else free[0]
    return {"name": name, "instinct": INSTINCTS[instinct - 1], "knack": KNACKS[knack - 1],
            "rolled": {"instinct": instinct, "knack": knack}}
