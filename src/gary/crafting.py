"""Making things: the Wizard's Ritual kept on the sheet, and the Fighter's Blacksmith graft.

Dungeon World has no crafting system; it has moves. Ritual: "When you draw on a place of power to
create a magical effect, tell the GM what you're trying to achieve. Ritual effects are always
possible, but the GM will give you one to four of the following conditions" -- days or months,
first you must..., help from..., a lot of money, a lesser version, danger from..., disenchanting
something. The conditions are the GM's, and they can take weeks of play, so the ritual goes on the
Wizard's sheet with its conditions and which are met, shown every turn until it is finished.

Blacksmith (Fighter, 6-10): "When you have access to a forge you can graft the magical powers of a
weapon onto your signature weapon. This process destroys the magical weapon. Your signature
weapon gains the magical powers of the destroyed weapon." The magic weapon's text, piercing and
reach move onto the signature weapon, and the magic weapon is gone.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .sheets import SheetError

MAX_CONDITIONS = 4
MAX_TEXT = 200


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_TEXT]


_UNIT = {"day": 1440, "week": 7 * 1440, "month": 30 * 1440, "year": 365 * 1440}
_NUMBER = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a few": 3, "several": 4, "a couple of": 2}


def duration(text: str) -> int:
    """Minutes a condition takes, if it is a span of time ("it will take three weeks", "a month"):
    0 when it isn't one. A bare plural ("weeks") is two of them."""
    low = " ".join(str(text or "").lower().split())
    m = re.search(r"\b(\d+|a couple of|a few|several|an|a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
                  r"twelve)\s+(day|week|month|year)s?\b", low)
    if m:
        count = int(m[1]) if m[1].isdigit() else _NUMBER[m[1]]
        return count * _UNIT[m[2]]
    m = re.search(r"\b(day|week|month|year)s\b", low)
    return 2 * _UNIT[m[1]] if m else 0


def start_ritual(goal: str, conditions: Sequence[str], now: int = 0, where: str = "",
                 when: Optional[Callable[[int], str]] = None) -> Dict[str, Any]:
    """The ritual begun now, at ``where``: a condition that is a span of time is met by the clock
    itself once that much time has passed (``due``, shown as the date ``when`` gives)."""
    goal = _text(goal)
    if not goal:
        raise SheetError("Say what the ritual is to achieve.")
    named = [_text(c) for c in conditions or () if _text(c)]
    if not 1 <= len(named) <= MAX_CONDITIONS:
        raise SheetError("The GM gives one to four conditions: days, weeks or months; first you must...; help "
                         "from...; a lot of money; a lesser version; danger from...; disenchanting something.")
    out = []
    for c in named:
        entry: Dict[str, Any] = {"text": c, "met": False}
        span = duration(c)
        if span:
            entry["due"] = int(now) + span
            if when is not None:
                entry["due_label"] = when(entry["due"])
        out.append(entry)
    return {"goal": goal, "conditions": out, "started": int(now), **({"where": _text(where)} if where else {})}


def met(condition: Dict[str, Any], now: Optional[int] = None) -> bool:
    """Met by play, or -- a span of time -- by the clock."""
    return bool(condition.get("met")) or (now is not None and "due" in condition and int(now) >= condition["due"])


def left(condition: Dict[str, Any], now: int) -> str:
    """How long a time condition still has to run, in words."""
    minutes = max(0, int(condition["due"]) - int(now))
    days = -(-minutes // 1440)
    return f"{days} day{'s' if days != 1 else ''} to go" if days else "done"


def meet(ritual: Dict[str, Any], condition: str, now: Optional[int] = None) -> Dict[str, Any]:
    """Mark a condition met: by its number (1-4) or a word from it. A span of time is the clock's to
    meet, not the GM's: refused until it has passed."""
    conditions = [dict(c) for c in ritual["conditions"]]
    wanted = str(condition or "").strip().lower()
    index = int(wanted) - 1 if wanted.isdigit() else next(
        (i for i, c in enumerate(conditions) if wanted and wanted in c["text"].lower()), -1)
    if not 0 <= index < len(conditions):
        raise SheetError("Name the condition met, by its number or a word from it: "
                         + "; ".join(f"{i + 1}. {c['text']}" for i, c in enumerate(conditions)))
    chosen = conditions[index]
    if "due" in chosen and now is not None and not met(chosen, now):
        raise SheetError(f"{chosen['text']}: that's the clock's to meet — {left(chosen, now)}"
                         + (f" (until {chosen['due_label']})" if chosen.get("due_label") else "") + ".")
    chosen["met"] = True
    return {**ritual, "conditions": conditions}


def done(ritual: Dict[str, Any], now: Optional[int] = None) -> bool:
    return all(met(c, now) for c in ritual["conditions"])


def describe(sheet: Dict[str, Any]) -> str:
    ritual = sheet.get("ritual")
    if not ritual:
        return ""
    marks = "; ".join(f"{'☒' if c['met'] else '☐'} {c['text']}"
                      + (f" (time: until {c['due_label']})" if c.get("due_label") and not c["met"] else "")
                      for c in ritual["conditions"])
    at = f" at {ritual['where']}" if ritual.get("where") else ""
    return f"Ritual under way{at}: {ritual['goal']} — {marks}"


def graft(sheet: Dict[str, Any], weapon: str) -> Tuple[List[Dict[str, Any]], str, str]:
    """(new gear, the signature weapon's name, the destroyed weapon's name)."""
    gear = [dict(g) for g in sheet.get("gear") or []]
    signature = next((i for i, g in enumerate(gear) if str(g.get("name", "")).startswith("Signature ")), None)
    if signature is None:
        raise SheetError(f"{sheet['name']} has no signature weapon to graft onto.")
    wanted = " ".join(str(weapon or "").split()).lower()
    source = next((i for i, g in enumerate(gear) if i != signature and g.get("magic")
                   and (str(g["name"]).lower() == wanted or (wanted and wanted in str(g["name"]).lower()))), None)
    if source is None:
        raise SheetError(f"{sheet['name']} carries no magic weapon called {weapon!r}.")
    magic = gear[source]
    ranges = ("hand", "close", "reach", "near", "far", "thrown")
    if not any(t in ranges for t in magic.get("tags") or ()):
        raise SheetError(f"{magic['name']} isn't a weapon: Blacksmith grafts a weapon's powers.")
    sig = gear[signature]
    sig["magic"] = (f"{sig['magic']} " if sig.get("magic") else "") + f"Grafted from {magic['name']}: {magic['magic']}"
    sig["piercing"] = max(int(sig.get("piercing", 0)), int(magic.get("piercing", 0)))
    sig["tags"] = list(dict.fromkeys([*sig.get("tags", []),
                                      *(t for t in magic.get("tags") or () if t in ranges)]))
    name = sig["name"]
    del gear[source]
    return gear, name, magic["name"]
