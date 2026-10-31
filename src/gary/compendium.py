"""Compendium classes: "a class only available to higher level characters who meet specific
requirements" (Dungeon World, Advanced Delving) -- Divine Bond from the core book and the
seven of the backers' Player Compendium (CC-BY 3.0).

Each has a requirement met in the fiction ("When you drink the still-warm heart-blood of a
dragon"), a starting move that can then be taken "the next time you level up" instead of a
class move, and moves that "count as class moves" once the starting move is taken. The model
records the requirement met (``qualify_compendium``); the level-up form offers the starting
move at the next level, and the follow-on moves at every level after. Taken moves go on the
sheet's ``advanced_moves`` like any other, so everything that reads advanced moves reads them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def _load() -> List[Dict[str, Any]]:
    path = Path(__file__).parent / "data" / "compendium.json"
    return json.loads(path.read_text(encoding="utf-8"))["classes"]


CLASSES: Dict[str, Dict[str, Any]] = {c["name"]: c for c in _load()}

#: Starting moves that ask for a choice as they are taken, and the choices.
MARKS = ("Eye", "Tongue", "Arm", "Legs")
MOVE_CHOICES: Dict[str, tuple] = {"From The Forbidden Land Returned": MARKS, "Branded": MARKS}


def find_class(name: str) -> Optional[Dict[str, Any]]:
    folded = " ".join(str(name or "").split()).lower()
    return next((c for c in CLASSES.values() if c["name"].lower() == folded), None)


def move(name: str) -> Optional[Dict[str, Any]]:
    """A compendium move by name, with its class: {"name", "text", "class", "starting"}."""
    for c in CLASSES.values():
        if c["starting"]["name"] == name:
            return {**c["starting"], "class": c["name"], "starting": True}
        for m in c["moves"]:
            if m["name"] == name:
                return {**m, "class": c["name"], "starting": False}
    return None


def qualified(sheet: Dict[str, Any]) -> List[str]:
    """The compendium classes whose requirement this character has met, not yet taken."""
    taken = set(sheet.get("advanced_moves") or ())
    return [n for n in sheet.get("compendium_qualified") or ()
            if n in CLASSES and CLASSES[n]["starting"]["name"] not in taken]


def options(sheet: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Compendium moves this character may take at a level-up: the starting move of a class
    they qualify for, and the follow-on moves of a class they have started."""
    taken = set(sheet.get("advanced_moves") or ())
    out = [{**CLASSES[n]["starting"], "tier": "compendium", "compendium": n} for n in qualified(sheet)]
    for c in CLASSES.values():
        if c["starting"]["name"] in taken:
            out += [{**m, "tier": "compendium", "compendium": c["name"]} for m in c["moves"] if m["name"] not in taken]
    return out


def describe_requirements() -> str:
    """Every class and what qualifies a character for it, for the model."""
    return "; ".join(f"{c['name']}: {c['requirement'].split(' the next time')[0].rstrip(',')}"
                     for c in CLASSES.values())
