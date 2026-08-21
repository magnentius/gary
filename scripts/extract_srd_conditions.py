"""Extract the conditions and their rules from the SRD 5.2.1 glossary.

    python scripts/extract_srd_conditions.py SRD_CC_v5.2.1.pdf src/gary/data/conditions.json

Conditions are the one part of the rules Gary has to reason about rather than merely
name: Avrae tracks a condition once told and never infers its consequences, so knowing
that Prone means "Advantage if the attacker is within 5 feet, otherwise Disadvantage" is
what lets the right modifier be rendered.

The glossary tags them, which makes extraction structural rather than guesswork:

    Prone [Condition]
    While you have the Prone condition, ...

An entry ends at the next glossary heading — a short unpunctuated line in title case.

SRD 5.2.1 is CC-BY-4.0; the attribution rides in the generated file.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_TAGGED = re.compile(r"^(?P<name>[A-Z][A-Za-z ]+)\s*\[Condition\]\s*$")
# A glossary heading: short, title-case, no sentence punctuation.
_HEADING = re.compile(r"^[A-Z][A-Za-z][A-Za-z' ]{1,38}(\s*\[[A-Za-z ]+\])?$")

LICENCE = (
    "Conditions from the System Reference Document 5.2.1, (c) Wizards of the Coast LLC, "
    "licensed under CC BY 4.0. https://creativecommons.org/licenses/by/4.0/legalcode"
)


def _dehyphenate(text: str) -> str:
    """PDF line breaks split words as "op -\\ntions" and "at-\\ntack"."""
    text = re.sub(r"(\w)\s*-\s*\n\s*(\w)", r"\1\2", text)
    return re.sub(r"\s*\n\s*", " ", text).strip()


def extract(pdf_path: Path) -> dict:
    from pypdf import PdfReader

    lines: list[str] = []
    for page in PdfReader(str(pdf_path)).pages:
        lines.extend((page.extract_text() or "").splitlines())

    conditions: dict[str, dict] = {}
    for i, line in enumerate(lines):
        match = _TAGGED.match(line.strip())
        if not match:
            continue
        body: list[str] = []
        for follow in lines[i + 1 :]:
            stripped = follow.strip()
            if not stripped:
                continue
            # Stop at the next glossary heading, but not at the entry's own sub-headings,
            # which are bold run-ins ending in a period ("Restricted Movement.").
            if _HEADING.match(stripped) and not stripped.endswith("."):
                break
            body.append(follow)
            if len(body) > 40:  # runaway guard
                break
        name = match.group("name").strip()
        conditions[name.lower()] = {"name": name, "rules": _dehyphenate("\n".join(body))}
    return {"_licence": LICENCE, "conditions": conditions}


def main() -> None:
    pdf = Path(sys.argv[1] if len(sys.argv) > 1 else "SRD_CC_v5.2.1.pdf")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "src/gary/data/conditions.json")
    data = extract(pdf)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"{len(data['conditions'])} conditions -> {out}")
    for name, c in sorted(data["conditions"].items()):
        print(f"  {c['name']:<14} {len(c['rules']):>4} chars  {c['rules'][:64]}...")


if __name__ == "__main__":
    main()
