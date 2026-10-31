"""Extract the Instant NPCs tables into src/gary/data/npcs.json.

Reads appendices/NPCs.xml from the authors' InDesign source (github.com/Sagelt/Dungeon-World):
two numbered lists of a hundred -- instincts, then knacks -- and a paragraph of names after the
Names heading. "When you need a quick NPC all you need is an instinct and some way to pursue
it. We call that a knack."

    python scripts/extract_dw_npcs.py ~/github/Dungeon-World
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "gary" / "data" / "npcs.json"

ATTRIBUTION = (
    "Dungeon World is the work of Sage LaTorra and Adam Koebel and is licensed under the "
    "Creative Commons Attribution 3.0 Unported License "
    "(http://creativecommons.org/licenses/by/3.0/). Extracted from "
    "https://github.com/Sagelt/Dungeon-World by scripts/extract_dw_npcs.py."
)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", fragment)).strip()


def extract(path: Path) -> dict:
    xml = path.read_text(encoding="utf-8")
    items = [_text(i) for i in re.findall(r"<li[^>]*>(.*?)</li>", xml, re.S)]
    if len(items) != 200:
        raise SystemExit(f"expected 200 instincts and knacks, read {len(items)}")
    names_at = xml.index("Names</h2>")
    names = _text(re.search(r"<p[^>]*>(.*?)</p>", xml[names_at:], re.S)[1])
    return {"instincts": items[:100], "knacks": items[100:],
            "names": [n.strip() for n in names.split(",") if n.strip()]}


def main(checkout: str) -> None:
    root = Path(checkout).expanduser()
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    tables = extract(root / "text" / "appendices" / "NPCs.xml")
    OUT.write_text(json.dumps({"attribution": ATTRIBUTION, "source_commit": commit, **tables},
                              indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {len(tables['instincts'])} instincts, {len(tables['knacks'])} knacks, "
          f"{len(tables['names'])} names to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
