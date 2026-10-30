"""Bonds (Dungeon World, "Bonds"): who a character is tied to, and how much that helps.

Each class has a handful of bond lines with a blank for a companion ("___ has insulted my
deity; I do not trust them"). A character fills in at least one with another character
at the table. On the sheet they are ``bonds``: ``[{"line": ..., "with": ...}]``, the line
as the book has it, blank and all, and the companion's name.

Aid or Interfere rolls +Bond: the number of bonds the roller has with the character they
help or hinder. Bond lines are the book's, extracted by scripts/extract_dw_class_moves.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .class_moves import TEXTS
from .registry import normalize_actor
from .sheets import SheetError

BLANK = "___"


def lines_for(sheet: Dict[str, Any]) -> List[str]:
    """The class's bond lines, each with its blank."""
    return list(TEXTS.get(str(sheet.get("class", "")).lower(), {}).get("bonds", ()))


def written(bond: Dict[str, str]) -> str:
    """The line with the companion's name in it."""
    return bond["line"].replace(BLANK, bond["with"], 1)


def bond_with(sheet: Dict[str, Any], other: str) -> int:
    """+Bond: how many bonds this character has with ``other``."""
    folded = normalize_actor(other)
    return sum(1 for b in sheet.get("bonds") or () if normalize_actor(b["with"]) == folded)


def describe(sheet: Dict[str, Any]) -> str:
    """One line for the player and the model. "" for no bonds."""
    bonds = sheet.get("bonds") or []
    return "Bonds — " + " · ".join(written(b) for b in bonds) if bonds else ""


def add(sheet: Dict[str, Any], line: str, companion: str, companions: Sequence[str]) -> Dict[str, Any]:
    """The sheet with one more bond. ``companions`` are the other characters at the table.

    Each of the class's lines is written once: the book lists them to be filled in, not
    copied out.
    """
    if line not in lines_for(sheet):
        raise SheetError("That is not one of your class's bonds.")
    if companion not in companions:
        raise SheetError(f"{companion} is not a companion at this table.")
    bonds = list(sheet.get("bonds") or [])
    if any(b["line"] == line for b in bonds):
        raise SheetError("You have written that bond already. Remove it to rewrite it.")
    return {**sheet, "bonds": [*bonds, {"line": line, "with": companion}]}


def remove(sheet: Dict[str, Any], index: int) -> Dict[str, Any]:
    bonds = list(sheet.get("bonds") or [])
    if not 0 <= index < len(bonds):
        raise SheetError("There is no such bond.")
    del bonds[index]
    return {**sheet, "bonds": bonds}


def resolve(sheet: Dict[str, Any], line: str) -> Dict[str, Any]:
    """The sheet without the bond on this line: resolved at End of Session, to be
    rewritten. Unchanged if it is no longer there."""
    return {**sheet, "bonds": [b for b in sheet.get("bonds") or () if b["line"] != line]}
