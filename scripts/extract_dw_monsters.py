"""Extract the Dungeon World monsters into src/gary/data/monsters.json.

Reads the authors' InDesign XML (github.com/Sagelt/Dungeon-World, text/monster_settings),
where every field has its own paragraph style -- MonsterName, MonsterStats,
MonsterQualities, MonsterDescription -- so nothing here guesses where a field ends.

    python scripts/extract_dw_monsters.py ~/github/Dungeon-World

Known quirks of the source, handled rather than hand-fixed:
- Twelve monsters have no stats line at all: Folk of the Realm non-combatants, and things
  like the Tarrasque that are not meant to be fought. They are kept, with hp/armor/damage
  null, so Gary can say "not something you fight with a sword" instead of "unknown".
- The Treant's instinct sits in a NoIndent paragraph after its verse, not in its
  description.
- A few stats lines lack a space ("w[2d8]damage").
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

AID = "{http://ns.adobe.com/AdobeInDesign/4.0/}"
OUT = Path(__file__).resolve().parent.parent / "src" / "gary" / "data" / "monsters.json"

ATTRIBUTION = (
    "Dungeon World is the work of Sage LaTorra and Adam Koebel and is licensed under the "
    "Creative Commons Attribution 3.0 Unported License "
    "(http://creativecommons.org/licenses/by/3.0/). Extracted from "
    "https://github.com/Sagelt/Dungeon-World by scripts/extract_dw_monsters.py."
)

# "Bite (d8+1 damage 1 piercing)" -> attack, damage expression, damage tags
_ATTACK = re.compile(r"^(?P<attack>.+?)\s*\((?P<damage>[bw]?\[?\d*d\d+(?:[+-]\d+)?\]?(?:[+-]\d+)?)\s*damage,?\s*(?P<tags>[^)]*)\)$")


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _style(el) -> str:
    return el.get(AID + "pstyle") or ""


def _split_tags(text: str) -> list:
    return [t.strip() for t in text.split(",") if t.strip()]


def extract_setting(path: Path) -> list:
    setting = path.stem
    monsters, cur = [], None
    for el in ET.parse(path).getroot().iter():
        style = _style(el)
        if el.tag == "p" and style == "MonsterName":
            span = el.find("span")
            cur = {
                "name": (el.text or "").strip(),
                "setting": setting,
                "tags": _split_tags(_text(span)) if span is not None else [],
                "attack": None, "damage": None, "piercing": 0, "ignores_armor": False,
                "hp": None, "armor": None, "attack_tags": [], "qualities": [],
                "instinct": None, "moves": [],
            }
            monsters.append(cur)
        elif cur is None:
            continue
        elif el.tag == "p" and style == "MonsterStats":
            span = el.find("span")
            if span is not None and not (el.text or "").strip():
                cur["attack_tags"] = _split_tags(_text(span))
                continue
            for part in (p.strip() for p in "".join(el.itertext()).split("\t")):
                if not part:
                    continue
                if m := re.fullmatch(r"(\d+) HP", part):
                    cur["hp"] = int(m[1])
                elif m := re.fullmatch(r"(\d+) Armor", part):
                    cur["armor"] = int(m[1])
                elif m := _ATTACK.match(part):
                    cur["attack"], cur["damage"] = m["attack"], m["damage"]
                    tags = m["tags"].lower()
                    if p := re.search(r"(\d+)\s*piercing", tags):
                        cur["piercing"] = int(p[1])
                    cur["ignores_armor"] = "ignores armor" in tags
                else:
                    raise SystemExit(f"{setting}/{cur['name']}: unread stat {part!r}")
        elif el.tag == "p" and style == "MonsterQualities":
            cur["qualities"] = _split_tags(_text(el).replace("Special Qualities:", ""))
        elif el.tag == "p" and style in ("MonsterDescription", "NoIndent"):
            if m := re.search(r"Instinct\s*:\s*(.+)$", _text(el)):
                cur["instinct"] = m[1]
        elif el.tag == "li":
            cur["moves"].append(_text(el))
    return monsters


def main(checkout: str) -> None:
    root = Path(checkout).expanduser()
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
    ).stdout.strip()
    monsters = [
        m for path in sorted((root / "text" / "monster_settings").glob("*.xml"))
        for m in extract_setting(path)
    ]

    names = [m["name"].lower() for m in monsters]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SystemExit(f"duplicate monster names: {dupes}")
    fightable = [m for m in monsters if m["hp"] is not None]
    broken = [m["name"] for m in fightable if m["armor"] is None or m["damage"] is None]
    if broken:
        raise SystemExit(f"stats line incomplete: {broken}")
    if any(m["instinct"] is None for m in monsters):
        raise SystemExit(f"no instinct: {[m['name'] for m in monsters if m['instinct'] is None]}")

    OUT.write_text(json.dumps(
        {"attribution": ATTRIBUTION, "source_commit": commit,
         "monsters": {m["name"].lower(): m for m in monsters}},
        indent=1, ensure_ascii=False,
    ) + "\n")
    print(f"wrote {len(monsters)} monsters ({len(fightable)} with stats) to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
