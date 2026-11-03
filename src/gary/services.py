"""Paying for things that aren't equipment: the book's Services, Meals, Transport, Land and
Buildings, Bribes, and Gifts and Finery lists (data/services.json).

Some prices are "14-Charisma coins": the payer's Charisma modifier comes off. The book prices a
week at an inn; a night is a seventh of that, rounded, at least 1 coin -- Gary's arithmetic, not
the book's. A Minstrel hireling's A Hero's Welcome takes their skill off every price in town, as
it does for goods. What is bought to keep -- a horse, a cottage, finery -- goes on the sheet as a
possession (a mount's load is noted, not added to the rider's); the rest is simply paid.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DATA = Path(__file__).parent / "data" / "services.json"
LIST: List[Dict[str, Any]] = json.loads(_DATA.read_text(encoding="utf-8"))["services"]

_INN_WEEK = {"peasant": "A week's stay at a peasant inn", "civilized": "A week's stay at a civilized inn",
             "fanciest": "A week's stay at the fanciest inn in town"}


_ARTICLE = re.compile(r"^(a|an|the)\s+")


def _fold(text: str) -> str:
    return " ".join(str(text or "").replace("’", "'").lower().split())


def find(what: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """(the listed service, whether it's one night of an inn week)."""
    wanted = _fold(what)
    inn = re.search(r"\binns?\b", wanted) is not None  # not "dinner"
    night = inn and "night" in wanted
    if inn:  # "a night at the inn": a civilized one unless it says peasant or fancy
        kind = "peasant" if "peasant" in wanted else "fanciest" if "fanc" in wanted else "civilized"
        return next(s for s in LIST if s["name"] == _INN_WEEK[kind]), night
    exact = next((s for s in LIST if _fold(s["name"]) == wanted), None)
    if exact:
        return exact, False
    bare = _ARTICLE.sub("", wanted)
    hits = [s for s in LIST if bare and (bare == _ARTICLE.sub("", _fold(s["name"])) or bare in _fold(s["name"]))]
    return (hits[0] if len(hits) == 1 else None), False


def price(service: Dict[str, Any], cha: int, night: bool = False, discount: int = 0) -> int:
    base = service["price"] - (cha if service["minus_cha"] else 0)
    if night:
        base = math.ceil(base / 7)
    return max(1, base - int(discount or 0))


def options(category: str = "") -> List[str]:
    return [s["name"] for s in LIST if not category or s["category"].lower() == category.lower()]
