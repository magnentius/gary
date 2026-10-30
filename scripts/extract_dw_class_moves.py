"""Extract every class's starting, race and advanced moves into
src/gary/data/class_moves.json.

Verbatim text from the authors' InDesign XML (github.com/Sagelt/Dungeon-World): each move
is an h3 (MoveName) inside the class's `racialmoves` or `startingmoves` div, followed by
paragraphs and list items until the next h3. Advanced moves are the same, in the
`moves` div: an Instructions paragraph ("When you gain a level from 6–10…") starts the
second tier, and a Requirement paragraph ("Requires: Chosen One", "Replaces: Empower")
says what a move needs or supersedes.

    python scripts/extract_dw_class_moves.py ~/github/Dungeon-World

Text only. Which of these Gary rolls, and what each result does, is hand-written in
class_moves.py, as the basic moves are in moves.py: the text is the book's, the rules
structure is ours to get right.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CLASSES = ("Bard", "Cleric", "Druid", "Fighter", "Paladin", "Ranger", "Thief", "Wizard")
OUT = Path(__file__).resolve().parent.parent / "src" / "gary" / "data" / "class_moves.json"

ATTRIBUTION = (
    "Dungeon World is the work of Sage LaTorra and Adam Koebel and is licensed under the "
    "Creative Commons Attribution 3.0 Unported License "
    "(http://creativecommons.org/licenses/by/3.0/). Extracted from "
    "https://github.com/Sagelt/Dungeon-World by scripts/extract_dw_class_moves.py."
)


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _moves(div) -> list:
    moves, cur = [], None
    for el in div.iter():
        if el.tag == "h3":
            cur = {"name": _text(el), "text": []}
            moves.append(cur)
        elif cur is not None and el.tag == "p":
            cur["text"].append(_text(el))
        elif cur is not None and el.tag == "li":
            cur["text"].append("• " + _text(el))
    return [{"name": m["name"], "text": "\n".join(t for t in m["text"] if t)} for m in moves]


AID = "{http://ns.adobe.com/AdobeInDesign/4.0/}"


def _advanced(div) -> list:
    moves, cur, tier = [], None, "2-5"
    for el in div.iter():
        style = el.get(AID + "pstyle")
        if el.tag == "p" and style == "Instructions" and "6" in _text(el):
            tier, cur = "6-10", None
        elif el.tag == "h3":
            cur = {"name": _text(el), "tier": tier, "requires": "", "replaces": "", "text": []}
            moves.append(cur)
        elif cur is not None and el.tag == "p" and style == "Requirement":
            kind, _, what = _text(el).partition(":")
            cur[kind.strip().lower()] = what.strip()
        elif cur is not None and el.tag == "p":
            cur["text"].append(_text(el))
        elif cur is not None and el.tag == "li":
            cur["text"].append("• " + _text(el))
    for m in moves:
        m["text"] = "\n".join(t for t in m["text"] if t)
    return moves


def extract(path: Path) -> dict:
    root = ET.parse(path).getroot()
    divs = {el.get("id"): el for el in root.iter() if el.get("id") in ("racialmoves", "startingmoves", "moves")}
    missing = {"racialmoves", "startingmoves", "moves"} - set(divs)
    if missing:
        raise SystemExit(f"{path.name}: no {', '.join(sorted(missing))}")
    races = {m["name"]: m["text"] for m in _moves(divs["racialmoves"])}
    # The race moves' div sits inside the starting moves' one, so they would come twice.
    starting = [m for m in _moves(divs["startingmoves"]) if m["name"] not in races]
    advanced = _advanced(divs["moves"])
    empty = [m["name"] for m in starting + advanced if not m["text"]] + [r for r, t in races.items() if not t]
    if empty:
        raise SystemExit(f"{path.name}: no text for {empty}")
    unknown = [m["name"] for m in advanced if set(m) - {"name", "tier", "requires", "replaces", "text"}]
    if unknown:
        raise SystemExit(f"{path.name}: unexpected requirement on {unknown}")
    return {"race": races, "starting": starting, "advanced": advanced}


def main(checkout: str) -> None:
    root = Path(checkout).expanduser()
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    data = {c.lower(): extract(root / "text" / f"{c}.xml") for c in CLASSES}
    OUT.write_text(json.dumps(
        {"attribution": ATTRIBUTION, "source_commit": commit, "classes": data},
        indent=1, ensure_ascii=False,
    ) + "\n")
    total = sum(len(c["starting"]) for c in data.values())
    print(f"wrote {total} starting, {sum(len(c['race']) for c in data.values())} race and "
          f"{sum(len(c['advanced']) for c in data.values())} advanced moves to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
