"""Extract the Dungeon World classes into src/gary/data/classes.json.

Reads the authors' own source -- the InDesign XML in github.com/Sagelt/Dungeon-World --
not a conversion of it: the XML marks base damage with an id and races with a paragraph
style, where the Markdown and HTML copies have lost both.

    python scripts/extract_dw_classes.py ~/github/Dungeon-World

What a sheet needs: name, base HP, damage die, the races the class offers, and its looks
(one list per line of "Choose one for each": eyes, hair, clothes, body).
Gary has no other source for these, so the extraction fails loudly rather than writing a
class with a field missing.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

AID = "{http://ns.adobe.com/AdobeInDesign/4.0/}"
CLASSES = ("Bard", "Cleric", "Druid", "Fighter", "Paladin", "Ranger", "Thief", "Wizard")
OUT = Path(__file__).resolve().parent.parent / "src" / "gary" / "data" / "classes.json"

ATTRIBUTION = (
    "Dungeon World is the work of Sage LaTorra and Adam Koebel and is licensed under the "
    "Creative Commons Attribution 3.0 Unported License "
    "(http://creativecommons.org/licenses/by/3.0/). Extracted from "
    "https://github.com/Sagelt/Dungeon-World by scripts/extract_dw_classes.py."
)


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def extract(path: Path) -> dict:
    root = ET.parse(path).getroot()
    every = list(root.iter())

    hp = next(
        (m for el in every if (m := re.search(r"maximum HP is (\d+)\s*\+\s*Constitution", _text(el)))),
        None,
    )
    damage = next((el for el in every if el.get("id") == "Damage"), None)
    racial = next((el for el in every if el.get("id") == "racialmoves"), None)
    races = (
        [_text(h) for h in racial.iter() if h.get(AID + "pstyle") == "MoveName"]
        if racial is not None else []
    )

    look = next((el for el in every if el.get("id") == "look"), None)
    looks = [] if look is None else [
        [o.strip() for o in re.split(r",\s*(?:or\s+)?|\s+or\s+", _text(p)) if o.strip()]
        for p in look.iter("p") if not _text(p).lower().startswith("choose")]

    # `is None`, not truthiness: an Element with no children is falsy, which is exactly
    # what the damage span is.
    missing = [n for n, v in (("base HP", hp), ("damage", damage)) if v is None]
    if not races:
        missing.append("races")
    if not looks or any(len(options) < 2 for options in looks):
        missing.append("looks")
    if missing:
        raise SystemExit(f"{path.name}: could not find {', '.join(missing)}")

    return {
        "name": path.stem,
        "base_hp": int(hp[1]),
        "damage": _text(damage),
        "races": races,
        "looks": looks,
    }


def main(checkout: str) -> None:
    text = Path(checkout).expanduser() / "text"
    commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    classes = {name.lower(): extract(text / f"{name}.xml") for name in CLASSES}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"attribution": ATTRIBUTION, "source_commit": commit, "classes": classes},
        indent=2, ensure_ascii=False,
    ) + "\n")
    print(f"wrote {len(classes)} classes to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
