#!/usr/bin/env python3
"""Check captured Avrae output against the derived fixtures (ARCHITECTURE.md §14.1).

Run after ``capture_avrae_fixtures.py``. Answers the only question that matters: **does
the parser read what Avrae actually sends?**

    python scripts/verify_avrae_fixtures.py

Reports, in order of how much they should worry you:

1. **UNKNOWN captures** — a real format the parser cannot classify. Always a bug.
2. **Structural disagreements** — a capture whose container (embed vs. plain content)
   differs from the derived fixture covering the same case. This is the class that hid
   the initiative tracker.
3. **Coverage gaps** — commands from the capture checklist with no capture, so you know
   what still needs running.

Exits non-zero if anything in 1 or 2 is found, so it can gate a commit.

Needs no Discord connection and no token; it reads files on disk.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from gary.parser.avrae import ParseStatus, parse_embed  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "avrae"
CAPTURED = FIXTURES / "captured"

# Rough signatures for matching a capture to the case it exercises. Deliberately loose:
# this is a triage aid, not a classifier.
SIGNATURES = [
    ("init", re.compile(r"```md|current initiative", re.IGNORECASE)),
    ("error", re.compile(r"^error:", re.IGNORECASE)),
    ("attack", re.compile(r"attacks? with|to hit", re.IGNORECASE)),
    ("save", re.compile(r"makes an? .*save", re.IGNORECASE)),
    ("check", re.compile(r"makes an? .*check", re.IGNORECASE)),
    ("help", re.compile(r"help", re.IGNORECASE)),
]


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def full_text(payload: dict) -> str:
    parts = [payload.get("content") or ""]
    for embed in payload.get("embeds") or []:
        parts += [embed.get("title") or "", embed.get("description") or ""]
        for field in embed.get("fields") or []:
            parts.append(f"{field.get('name', '')} {field.get('value', '')}")
    return "\n".join(p for p in parts if p)


def classify(payload: dict) -> str:
    text = full_text(payload)
    for name, pattern in SIGNATURES:
        if pattern.search(text):
            return name
    return "other"


def container(payload: dict) -> str:
    """Which container the message actually used. The distinction that bit us."""
    has_embed = bool(payload.get("embeds"))
    has_content = bool((payload.get("content") or "").strip())
    if has_embed and has_content:
        return "both"
    if has_embed:
        return "embed"
    if has_content:
        return "content"
    return "empty"


def main() -> int:
    if not CAPTURED.exists() or not list(CAPTURED.glob("*.json")):
        print(f"No captures in {CAPTURED}.")
        print("Run: python scripts/capture_avrae_fixtures.py --channel <id>")
        return 1

    derived: Dict[str, str] = {}
    for path in sorted(FIXTURES.glob("*.json")):
        payload = load(path)["message"]
        derived.setdefault(classify(payload), container(payload))

    captures = sorted(CAPTURED.glob("*.json"))
    unknown: List[str] = []
    mismatches: List[str] = []
    seen: Dict[str, int] = {}

    print(f"{len(captures)} capture(s) in {CAPTURED.relative_to(REPO)}\n")
    print(f"{'file':34} {'case':8} {'container':10} {'parsed as':12} detail")
    print("-" * 92)

    for path in captures:
        payload = load(path)["message"]
        case, box = classify(payload), container(payload)
        seen[case] = seen.get(case, 0) + 1
        parsed = parse_embed(payload)

        detail = ""
        if parsed.roll:
            detail = f"total={parsed.roll.total} nat={parsed.roll.natural} actor={parsed.roll.actor!r}"
        elif parsed.combatants:
            dying = [c.name for c in parsed.combatants if c.is_dying]
            detail = f"{len(parsed.combatants)} combatants, dying={dying}"
        elif parsed.reason:
            detail = parsed.reason

        print(f"{path.name:34} {case:8} {box:10} {parsed.status.value:12} {detail}")

        if parsed.status is ParseStatus.UNKNOWN:
            unknown.append(path.name)
        expected_box = derived.get(case)
        if expected_box and expected_box != box:
            mismatches.append(
                f"{path.name}: {case} arrived as {box}, derived fixture says {expected_box}"
            )

    print()
    ok = True

    if unknown:
        ok = False
        print(f"❌ {len(unknown)} capture(s) the parser could not classify:")
        for name in unknown:
            print(f"     {name}")
        print("   Each is a real format the parser does not handle. Fix the parser.\n")

    if mismatches:
        ok = False
        print(f"❌ {len(mismatches)} structural disagreement(s):")
        for line in mismatches:
            print(f"     {line}")
        print("   Either Avrae changed or the derivation was wrong. Understand which.\n")

    missing = [c for c in ("check", "save", "attack", "init", "error", "help") if c not in seen]
    if missing:
        print(f"⚠️  no capture yet for: {', '.join(missing)}")
        print("   Re-run the capture and exercise those commands.\n")

    if ok and not missing:
        print("✅ every capture parsed, and containers match the derived fixtures.")
        print("   Move the captures into tests/fixtures/avrae/, fill in each `expect`")
        print("   from what Discord shows, and delete the xfail in tests/test_parser.py.")
    elif ok:
        print("✅ everything captured so far parsed, and containers match.")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
