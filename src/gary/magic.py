"""Magic items on a character (Dungeon World, "Magic Items").

"Magic items are for you to make for your game... This list provides some ideas, but magic items
are ultimately for you to decide." A magic item is carried as gear -- so its weight counts toward
load, a weapon among them can be swung or thrown, armor and a shield protect -- with its text
attached (``magic``), which the GM is shown every turn under the character, so what it does is
honoured session after session rather than remembered from the conversation. The book's items
come with the book's text and tags; one the GM invents comes with the GM's words.

The numbers Gary applies are the ones the tags carry (weight, armor, shield, piercing, ammo,
ranges) and one the text names: Argo-Thaan's "a paladin wielding it increases their damage die
to d12". Everything else an item does is the GM's to narrate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_DATA = Path(__file__).parent / "data" / "magic_items.json"
BOOK: List[Dict[str, Any]] = json.loads(_DATA.read_text(encoding="utf-8"))["items"]

MAX_TEXT = 600  # the book's longest runs past this; the model needs the gist every turn
SHOWN = 280  # what the model is shown each turn, per item

#: The text's own numbers Gary applies: (item, class) -> damage die.
DAMAGE_DIE = {("argo-thaan, holy avenger", "Paladin"): "d12"}

_RANGES = ("hand", "close", "reach", "near", "far", "thrown")


def _fold(name: str) -> str:
    return " ".join(str(name or "").replace("’", "'").lower().split())


def book_item(name: str) -> Optional[Dict[str, Any]]:
    """The book's magic item by its name, or by the part before a comma ("Argo-Thaan")."""
    wanted = _fold(name)
    for item in BOOK:
        full = _fold(item["name"])
        if wanted and (wanted == full or wanted == full.split(",")[0].strip()):
            return item
    return None


def entry(name: str, text: str = "", tags: Sequence[str] = ()) -> Dict[str, Any]:
    """A gear entry for a magic item: the book's if it is one, else the GM's words and tags."""
    found = book_item(name)
    if found is not None:
        name, text, tags = found["name"].replace("’", "'"), found["text"], found["tags"]
    text = " ".join(str(text or "").split())
    if not text:
        raise ValueError(f"{name!r} isn't one of the book's magic items, so say what it does (text).")
    out: Dict[str, Any] = {"name": " ".join(str(name).split())[:80], "tags": [], "armor": 0, "shield": 0,
                           "damage": 0, "piercing": 0, "ammo": 0, "needs_ammo": False, "weight": 0, "rations": 0,
                           "magic": text[:MAX_TEXT], "book": found is not None}
    for raw in tags or ():
        tag = " ".join(str(raw).split())
        low = tag.lower()
        if m := re.fullmatch(r"(\d+) weight", low):
            out["weight"] = int(m[1])
        elif m := re.fullmatch(r"\+(\d+) armor", low):
            out["shield"] = int(m[1])  # "+1 armor": a shield's kind, on top of what is worn
        elif m := re.fullmatch(r"(\d+) armor", low):
            out["armor"] = int(m[1])
        elif m := re.fullmatch(r"(\d+) piercing", low):
            out["piercing"] = int(m[1])
        elif m := re.fullmatch(r"\+(\d+) damage", low):
            out["damage"] = int(m[1])
        elif m := re.fullmatch(r"(\d+) ammo", low):
            out["ammo"] = int(m[1])
        out["tags"].append(low if low in _RANGES else tag)  # ranges lowercase, as gear's are
    return out


def carried(sheet: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [g for g in sheet.get("gear") or () if g.get("magic")]


def damage_die(sheet: Dict[str, Any]) -> Optional[str]:
    """A die an item's text sets for this character (Argo-Thaan for a Paladin), or None."""
    for item in carried(sheet):
        die = DAMAGE_DIE.get((_fold(item["name"]), sheet.get("class", "")))
        if die:
            return die
    return None


def describe(sheet: Dict[str, Any]) -> str:
    """One line for the model and /sheet: each magic item and what it does."""
    items = carried(sheet)
    if not items:
        return ""
    parts = []
    for item in items:
        text = item["magic"] if len(item["magic"]) <= SHOWN else item["magic"][:SHOWN].rsplit(" ", 1)[0] + "…"
        parts.append(f"{item['name']} ({', '.join(item.get('tags') or []) or 'no tags'}): {text}")
    return "Magic items — " + " | ".join(parts)
