"""Classes from playbooks outside the core book: the Barbarian.

The core eight are extracted from the book's XML by scripts; a playbook released on its own
(a two-page PDF) is transcribed by hand into ``data/playbooks.json`` and merged in wherever
the book's classes are loaded -- sheets.CLASSES, class_moves.TEXTS, moves -- so a class from
here is a class like any other. Kept in its own file so re-running the extractors cannot
wipe it.

The Barbarian is by Sage LaTorra and Adam Koebel, under CC BY 3.0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

_DATA = Path(__file__).parent / "data" / "playbooks.json"


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        return json.loads(_DATA.read_text(encoding="utf-8"))["classes"]
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"Playbook class data missing or unreadable at {_DATA} ({exc}).") from exc


BOOK = _load()

#: As classes.json has them: key -> {name, base_hp, damage, races}.
CLASSES: Dict[str, Dict[str, Any]] = {k: v["class"] for k, v in BOOK.items()}

#: As class_moves.json has them: key -> {race, starting, advanced, bonds, alignments, ...}.
MOVES: Dict[str, Dict[str, Any]] = {k: v["moves"] for k, v in BOOK.items()}

#: "Your Load is 8+Str."
LOAD: Dict[str, int] = {k: int(v["load"]) for k, v in BOOK.items()}


def choose_one(class_key: str) -> List[str]:
    """Starting moves the class picks one of ("Choose one of these to start with")."""
    return list(MOVES.get(class_key, {}).get("choose_one") or ())


def appetites() -> List[str]:
    """Herculean Appetites' list."""
    return list(MOVES["barbarian"]["appetites"])
