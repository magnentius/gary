"""Extract the Dungeon World magic items into src/gary/data/magic_items.json.

Reads Equipment.xml from the authors' InDesign source (github.com/Sagelt/Dungeon-World):
each magic item is a MagicItem paragraph -- name, a tab, then a Tags span -- followed by
the paragraphs describing it, up to the next item or heading.

    python scripts/extract_dw_magic_items.py ~/github/Dungeon-World

The book offers these as ideas: "magic items are ultimately for you to decide". Gary shows
the GM a few when a treasure roll turns up something magical; what an item does stays
the GM's to narrate.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

AID = "{http://ns.adobe.com/AdobeInDesign/4.0/}"
OUT = Path(__file__).resolve().parent.parent / "src" / "gary" / "data" / "magic_items.json"

ATTRIBUTION = (
    "Dungeon World is the work of Sage LaTorra and Adam Koebel and is licensed under the "
    "Creative Commons Attribution 3.0 Unported License "
    "(http://creativecommons.org/licenses/by/3.0/). Extracted from "
    "https://github.com/Sagelt/Dungeon-World by scripts/extract_dw_magic_items.py."
)


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def extract(path: Path) -> list:
    items, current = [], None
    for el in ET.parse(path).getroot().iter("p"):
        style = el.get(AID + "pstyle", "")
        if style == "MagicItem":
            tags = el.find("span")
            tag_text = _text(tags) if tags is not None else ""
            name = _text(el)
            if tag_text and name.endswith(tag_text):
                name = name[: -len(tag_text)].strip()
            current = {"name": name, "tags": [t.strip() for t in tag_text.split(",") if t.strip()],
                       "text": ""}
            items.append(current)
        elif current is not None and style.startswith("h"):
            current = None
        elif current is not None:
            current["text"] = (current["text"] + " " + _text(el)).strip()
    return items


def main(checkout: str) -> None:
    root = Path(checkout).expanduser()
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    items = extract(root / "text" / "Equipment.xml")
    if not items or any(not i["text"] for i in items):
        raise SystemExit(f"an item without text: {[i['name'] for i in items if not i['text']]}")
    OUT.write_text(json.dumps(
        {"attribution": ATTRIBUTION, "source_commit": commit, "items": items},
        indent=1, ensure_ascii=False,
    ) + "\n")
    print(f"wrote {len(items)} items to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
