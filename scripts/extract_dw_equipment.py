"""Extract the Dungeon World equipment list into src/gary/data/equipment.json.

Reads Equipment.xml from the authors' InDesign source (github.com/Sagelt/Dungeon-World):
each item is an Item paragraph -- name, a tab, then a Tags span with its tags, price and
weight -- optionally followed by an ItemDescription saying what it does.

    python scripts/extract_dw_equipment.py ~/github/Dungeon-World

Weapons, armor, dungeon gear and poisons: the things a character carries. Services,
meals and transport are prices for things done, not items, and are left out. A line that
names several items ("Short Sword, Axe, Warhammer, Mace") becomes one entry per name.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

AID = "{http://ns.adobe.com/AdobeInDesign/4.0/}"
OUT = Path(__file__).resolve().parent.parent / "src" / "gary" / "data" / "equipment.json"
SECTIONS = {"Weapon List": "weapon", "Armor List": "armor", "Dungeon Gear": "gear", "Poisons": "poison"}

ATTRIBUTION = (
    "Dungeon World is the work of Sage LaTorra and Adam Koebel and is licensed under the "
    "Creative Commons Attribution 3.0 Unported License "
    "(http://creativecommons.org/licenses/by/3.0/). Extracted from "
    "https://github.com/Sagelt/Dungeon-World by scripts/extract_dw_equipment.py."
)


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _stats(kind: str, tags: list) -> dict:
    """The numbers the rules use, read off the book's tags."""
    out = {"armor": 0, "shield": 0, "damage": 0, "piercing": 0, "ammo": 0,
           "needs_ammo": False, "weight": 0, "rations": 0, "price": None}
    kept = []
    for tag in tags:
        low = tag.lower()
        if m := re.fullmatch(r"(\d+) coins?", low):
            out["price"] = int(m[1])
            continue
        if m := re.fullmatch(r"(\d+) weight", low):
            out["weight"] = int(m[1])
            continue
        if m := re.fullmatch(r"\+(\d+) armor", low):
            out["shield"] = int(m[1])
        elif m := re.fullmatch(r"(\d+) armor", low):
            out["armor"] = int(m[1])
        elif m := re.fullmatch(r"\+(\d+) damage", low):
            out["damage"] = int(m[1])
        elif m := re.fullmatch(r"(\d+) piercing", low):
            out["piercing"] = int(m[1])
        elif m := re.fullmatch(r"(\d+) ammo", low):
            out["ammo"] = int(m[1])
        kept.append(tag)
    return out, kept


def extract(path: Path) -> dict:
    items, section, last = {}, None, None
    for el in ET.parse(path).getroot().iter():
        if el.tag in ("h1", "h2", "h3"):
            section = SECTIONS.get(_text(el))
            continue
        style = el.get(AID + "pstyle")
        if section and el.tag == "p" and style == "Item":
            span = el.find("span")
            names = (el.text or "").strip()
            raw_tags = [t.strip() for t in _text(span).split(",")] if span is not None else []
            stats, tags = _stats(section, raw_tags)
            is_ration = any(t.lower() == "ration" for t in tags)
            uses = next((int(m[1]) for t in tags if (m := re.fullmatch(r"(\d+) uses?", t.lower()))), 0)
            if is_ration:
                stats["rations"] = uses or 1
            if section == "weapon":
                # Bows and crossbows fire nothing without arrows; a thrown dagger is its own.
                stats["needs_ammo"] = "bow" in names.lower()
            last = []
            for name in (n.strip() for n in names.split(",")):
                entry = {"name": name, "kind": section, "tags": tags, **stats, "description": ""}
                items[name.lower()] = entry
                last.append(entry)
        elif section and el.tag == "p" and style == "ItemDescription" and last:
            for entry in last:
                entry["description"] = _text(el)
    return items


def main(checkout: str) -> None:
    root = Path(checkout).expanduser()
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    items = extract(root / "text" / "Equipment.xml")
    unpriced = [i["name"] for i in items.values() if i["price"] is None]
    if unpriced:
        raise SystemExit(f"no price read for: {unpriced}")
    OUT.write_text(json.dumps(
        {"attribution": ATTRIBUTION, "source_commit": commit, "items": items},
        indent=1, ensure_ascii=False,
    ) + "\n")
    print(f"wrote {len(items)} items to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
