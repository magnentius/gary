"""Extract the Cleric and Wizard spell lists into src/gary/data/spells.json.

Verbatim text from the authors' InDesign XML (github.com/Sagelt/Dungeon-World): each spell
is a SpellName paragraph -- the name in a span, then a tab and its tags ("Level 1
Ongoing", "Rote", "Cantrip Ongoing", "Level 3 Evocation") -- followed by paragraphs and
list items until the next spell or level heading.

    python scripts/extract_dw_spells.py ~/github/Dungeon-World

Text and tags only. What a spell does to the numbers Gary tracks -- the 1d8 Cure Light
Wounds heals, Fireball ignoring armor -- is hand-written in spells.py, as the moves are:
the text is the book's, the rules structure is ours to get right.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

AID = "{http://ns.adobe.com/AdobeInDesign/4.0/}"
OUT = Path(__file__).resolve().parent.parent / "src" / "gary" / "data" / "spells.json"
SOURCES = {"cleric": "Cleric_Spells.xml", "wizard": "Wizard_Spells.xml"}

ATTRIBUTION = (
    "Dungeon World is the work of Sage LaTorra and Adam Koebel and is licensed under the "
    "Creative Commons Attribution 3.0 Unported License "
    "(http://creativecommons.org/licenses/by/3.0/). Extracted from "
    "https://github.com/Sagelt/Dungeon-World by scripts/extract_dw_spells.py."
)


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _spell(el) -> dict:
    span = el.find("span")
    name = (span.text or "").strip()
    # The tags follow the name, some in <em> ("Level 1\t<em>Ongoing</em>").
    tags = _text(el)[len(name):].split()
    if tags[:1] == ["Level"]:
        level, rest = int(tags[1]), tags[2:]
    elif tags[:1] in (["Rote"], ["Cantrip"]):
        level, rest = 0, tags[1:]
    else:
        raise SystemExit(f"{name}: no level in tags {tags}")
    return {"name": name, "level": level, "ongoing": "Ongoing" in rest,
            "school": next((t for t in rest if t != "Ongoing"), ""), "text": []}


def extract(path: Path) -> list:
    spells, cur = [], None
    for el in ET.parse(path).getroot().iter():
        style = el.get(AID + "pstyle")
        if el.tag in ("h1", "h2"):
            cur = None  # a level's introduction is not part of the spell before it
        elif el.tag == "p" and style == "SpellName":
            cur = _spell(el)
            spells.append(cur)
        elif cur is not None and el.tag == "p":
            cur["text"].append(_text(el))
        elif cur is not None and el.tag == "li":
            cur["text"].append("• " + _text(el))
    for s in spells:
        s["text"] = "\n".join(t for t in s["text"] if t)
    empty = [s["name"] for s in spells if not s["text"]]
    if empty:
        raise SystemExit(f"{path.name}: no text for {empty}")
    return spells


def main(checkout: str) -> None:
    root = Path(checkout).expanduser()
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    data = {cls: extract(root / "text" / f) for cls, f in SOURCES.items()}
    OUT.write_text(json.dumps(
        {"attribution": ATTRIBUTION, "source_commit": commit, "classes": data},
        indent=1, ensure_ascii=False,
    ) + "\n")
    print(", ".join(f"{len(v)} {k} spells" for k, v in data.items()) + f" written to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
