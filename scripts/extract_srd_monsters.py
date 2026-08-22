"""Extract monster names and challenge ratings from the SRD 5.2.1 PDF.

    python scripts/extract_srd_monsters.py SRD_CC_v5.2.1.pdf src/gary/data/monsters.json

Only names, size, type and CR — the inputs the Lazy Encounter Benchmark needs (§3). Not
statblocks: Avrae owns those, and duplicating them here would be a second source of truth
for numbers Avrae already rolls against.

The SRD is CC-BY-4.0, so this is redistributable with attribution; the attribution rides
in the generated file.

Statblocks are regular enough to parse structurally rather than by guesswork:

    Azer Sentinel                      <- name
    Medium Elemental, Lawful Neutral   <- size, type, alignment
    ...
    CR 2 (XP 450; PB +2)               <- the anchor

The CR line is the anchor and the two lines are found by walking back from it, so a
statblock with unexpected fields in between still parses.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SIZES = ("Tiny", "Small", "Medium", "Large", "Huge", "Gargantuan")

_CR = re.compile(r"^CR\s+(\d+(?:/\d+)?)\s*\(XP", re.IGNORECASE)
# Some statblocks offer a choice of size -- "Medium or Small Humanoid, Neutral" -- and
# the alternative has to be consumed here or it lands in the type. It did: 36 of 327
# creatures were typed "or Small Humanoid", and they were disproportionately the
# humanoids, which is exactly the group anything keyed on creature type cares about.
_SIZE_LINE = re.compile(
    rf"^({'|'.join(SIZES)})(?:\s+or\s+(?:{'|'.join(SIZES)}))?\s+([^,]+),",
    re.IGNORECASE,
)

LICENCE = (
    "Monster names and challenge ratings from the System Reference Document 5.2.1, "
    "(c) Wizards of the Coast LLC, licensed under CC BY 4.0. "
    "https://creativecommons.org/licenses/by/4.0/legalcode"
)


def cr_to_float(raw: str) -> float:
    if "/" in raw:
        num, _, den = raw.partition("/")
        return round(int(num) / int(den), 4)
    return float(int(raw))


def extract(pdf_path: Path) -> dict:
    from pypdf import PdfReader

    lines: list[str] = []
    for page in PdfReader(str(pdf_path)).pages:
        lines.extend((page.extract_text() or "").splitlines())

    monsters: dict[str, dict] = {}
    for i, line in enumerate(lines):
        match = _CR.match(line.strip())
        if not match:
            continue
        # Walk back for the size/type line; the name is immediately above it.
        # Wide enough for the longest statblock header. A tight window silently drops
        # the monsters with the most fields -- which are the high-CR ones, the exact
        # entries a difficulty benchmark most needs.
        for back in range(1, 25):
            if i - back < 1:
                break
            size_match = _SIZE_LINE.match(lines[i - back].strip())
            if not size_match:
                continue
            name = lines[i - back - 1].strip()
            if not name or name.isdigit() or "System Reference" in name:
                break
            monsters[name] = {
                "name": name,
                "size": size_match.group(1).title(),
                "type": size_match.group(2).strip(),
                "cr": cr_to_float(match.group(1)),
            }
            break
    return {"_licence": LICENCE, "monsters": monsters}


def main() -> None:
    pdf = Path(sys.argv[1] if len(sys.argv) > 1 else "SRD_CC_v5.2.1.pdf")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "src/gary/data/monsters.json")
    data = extract(pdf)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    crs = sorted({m["cr"] for m in data["monsters"].values()})
    print(f"{len(data['monsters'])} monsters -> {out}")
    print(f"CR range: {crs[0]} .. {crs[-1]}")


if __name__ == "__main__":
    main()
