"""Extract the compendium classes into src/gary/data/compendium.json.

Reads the backers' Player Compendium PDF (Backer-Classes-CC.pdf, CC-BY 3.0) through
pdftotext: each class is a section headed by its name, a paragraph saying what must happen
first ("When you drink the still-warm heart-blood of a dragon the next time you level up you
may choose this move:"), its starting move, and after "these count as class moves for you"
the moves that open once the starting move is taken. Each move is a ⃞ line with its name,
followed by its text to the next move or section.

    python scripts/extract_dw_compendium.py ~/path/to/Backer-Classes-CC.pdf

Divine Bond, the core book's own example (Advanced Delving), is added from the authors'
source, which has no follow-on moves.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "gary" / "data" / "compendium.json"
CLASSES = ("Awakened Mind", "Bearer", "Blob", "Dragonslayer", "Flux", "Landed Gentry", "WraithLander")

ATTRIBUTION = (
    "Dungeon World and the Player Compendium are the work of Sage LaTorra and Adam Koebel and "
    "are licensed under the Creative Commons Attribution 3.0 Unported License "
    "(http://creativecommons.org/licenses/by/3.0/). Compendium classes by Mattie Casper "
    "(Awakened Mind), Fred Hicks (Bearer), Joseph \"UserClone\" LeMay (Blob), Stras Acimovic "
    "(Dragonslayer), Jeffrey J.A. Fuller II (Flux), Markus Wagner (Landed Gentry) and MC3 "
    "(Wraithlander); Divine Bond from Dungeon World's Advanced Delving. Extracted by "
    "scripts/extract_dw_compendium.py."
)

DIVINE_BOND = {
    "name": "Divine Bond",
    "requirement": "When you enter the bodily presence of a god or their avatar the next time you gain a "
                   "level you can choose this move instead of a move from your class.",
    "starting": {"name": "Divine Bond", "text": (
        "When you write a new bond, instead of using the name of another character you can use the name "
        "of a deity you've had contact with. Anytime a bond with a deity applies to the current situation "
        "you can mark it off (as if it was resolved) to call on the deity's favor in a clear and decisive "
        "way that the GM describes. At the end of the session you then replace the marked off bond with a "
        "new one, with a deity or player character.")},
    "moves": [],
}


def _clean(text: str) -> str:
    lines = [ln for ln in text.splitlines() if not re.fullmatch(r"\s*\d+\s*", ln)]  # page numbers
    out = " ".join(" ".join(ln.split()) for ln in lines if ln.strip())
    return out.replace("­", "-").replace(" • ", "\n• ").replace("• ", "• ").strip()


def _moves(block: str):
    """[(name, text)] for every ⃞ move in a block of text."""
    parts = re.split(r"⃞", block)
    out = []
    for part in parts[1:]:
        first, _, rest = part.partition("\n")
        out.append((first.strip(), _clean(rest)))
    return out


def extract(text: str) -> list:
    body = text[text.index("Awakened Mind\nWhen you discover"):]
    starts = [re.search(rf"(^|\n){re.escape(name)}\n", body[1:] if i else body).start() + (1 if i else 0)
              for i, name in enumerate(CLASSES)]
    starts.append(body.index("Credits"))
    classes = []
    for name, a, b in zip(CLASSES, starts, starts[1:]):
        section = body[a:b]
        intro = section.split("⃞", 1)[0]
        requirement = re.sub(rf"^{re.escape(name)}\s*", "", _clean(intro))
        moves = _moves(section)
        (start_name, start_text), rest = moves[0], moves[1:]
        start_text = re.split(r"\s*If you (?:have|are|took)\b", start_text)[0].strip()
        classes.append({
            "name": "Wraithlander" if name == "WraithLander" else name,
            "requirement": requirement,
            "starting": {"name": start_name, "text": start_text},
            "moves": [{"name": n, "text": t} for n, t in rest],
        })
    return [*classes, DIVINE_BOND]


def main(pdf: str) -> None:
    text = subprocess.run(["pdftotext", "-layout", str(Path(pdf).expanduser()), "-"],
                          capture_output=True, text=True, check=True).stdout
    classes = extract(text)
    for c in classes:
        if not c["starting"]["name"] or not c["starting"]["text"]:
            raise SystemExit(f"{c['name']}: no starting move read")
    OUT.write_text(json.dumps({"attribution": ATTRIBUTION, "classes": classes}, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {len(classes)} compendium classes to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
