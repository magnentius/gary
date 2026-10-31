"""Multiclass Dabbler, Initiate and Master: "Get one move from another class. Treat your
level as one lower for choosing the move."

Each grants one move, chosen on the level-up form after the advanced move itself: a class,
then a move from it -- a starting move, or an advanced move whose tier the character's
level less one reaches. They are kept on the sheet as ``multiclass_moves`` ({class, name})
and work like the character's own: shown to the model in the book's words, counted by the
advanced moves Gary applies, and a move Gary rolls (Backstab, Called Shot) may be made.

Left out: the spellcasting core (Deity, Commune, Cast a Spell, Spellbook, Prepare Spells),
which grants nothing without a caster's spell lists -- Divine Favor and God Amidst the
Wastes are how a non-caster gets spells -- and the Ranger's companion moves, which Gary
builds only for Rangers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .class_moves import TEXTS

MOVES = ("Multiclass Dabbler", "Multiclass Initiate", "Multiclass Master")

EXCLUDED = {"Deity", "Commune", "Cast a Spell", "Cast a Spell (Int)", "Spellbook", "Prepare Spells",
            "Animal Companion", "Command"}


def classes(sheet: Dict[str, Any]) -> List[str]:
    """Every class but their own, as the book's data names them ("thief")."""
    own = str(sheet.get("class", "")).lower()
    return [c for c in TEXTS if c != own]


def taken(sheet: Dict[str, Any]) -> List[str]:
    return [m["name"] for m in sheet.get("multiclass_moves") or ()]


def options(sheet: Dict[str, Any], cls: str, level: int) -> List[Dict[str, Any]]:
    """The moves of ``cls`` someone reaching ``level`` may take: "treat your level as one
    lower" -- the 2-5 list from an effective level 2, the 6-10 list from 6."""
    effective = level - 1
    have = set(taken(sheet)) | set(sheet.get("advanced_moves") or ())
    book = TEXTS.get(cls, {})
    out = [{**m, "tier": "starting"} for m in book.get("starting", ())
           if m["name"] not in EXCLUDED and m["name"] not in have]
    for m in book.get("advanced", ()):
        if m["name"] in have or m["name"] in MOVES:
            continue
        if (m["tier"] == "2-5" and effective < 2) or (m["tier"] == "6-10" and effective < 6):
            continue
        if m.get("requires") and m["requires"] not in have:
            continue
        out.append(m)
    return out


def find(sheet: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """A multiclass move this character has, with its class."""
    return next((m for m in sheet.get("multiclass_moves") or () if m["name"] == name), None)


def text_of(entry: Dict[str, Any]) -> str:
    book = TEXTS.get(entry["class"], {})
    return next((m["text"] for m in [*book.get("starting", ()), *book.get("advanced", ())]
                 if m["name"] == entry["name"]), "")


def grants_key(sheet: Dict[str, Any], key: str) -> bool:
    """Does a multiclass move let them make the move Gary rolls as ``key``?"""
    from .class_moves import _KINDS

    return any(_KINDS.get(m["class"], {}).get(m["name"]) == key for m in sheet.get("multiclass_moves") or ())
