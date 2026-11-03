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

from typing import Any, Dict, List, Sequence, Tuple

from .sheets import SheetError

MAX_CONDITIONS = 4
MAX_TEXT = 200


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_TEXT]


def start_ritual(goal: str, conditions: Sequence[str]) -> Dict[str, Any]:
    goal = _text(goal)
    if not goal:
        raise SheetError("Say what the ritual is to achieve.")
    named = [_text(c) for c in conditions or () if _text(c)]
    if not 1 <= len(named) <= MAX_CONDITIONS:
        raise SheetError("The GM gives one to four conditions: days, weeks or months; first you must...; help "
                         "from...; a lot of money; a lesser version; danger from...; disenchanting something.")
    return {"goal": goal, "conditions": [{"text": c, "met": False} for c in named]}


def meet(ritual: Dict[str, Any], condition: str) -> Dict[str, Any]:
    """Mark a condition met: by its number (1-4) or a word from it."""
    conditions = [dict(c) for c in ritual["conditions"]]
    wanted = str(condition or "").strip().lower()
    index = int(wanted) - 1 if wanted.isdigit() else next(
        (i for i, c in enumerate(conditions) if wanted and wanted in c["text"].lower()), -1)
    if not 0 <= index < len(conditions):
        raise SheetError("Name the condition met, by its number or a word from it: "
                         + "; ".join(f"{i + 1}. {c['text']}" for i, c in enumerate(conditions)))
    conditions[index]["met"] = True
    return {**ritual, "conditions": conditions}


def done(ritual: Dict[str, Any]) -> bool:
    return all(c["met"] for c in ritual["conditions"])


def describe(sheet: Dict[str, Any]) -> str:
    ritual = sheet.get("ritual")
    if not ritual:
        return ""
    marks = "; ".join(f"{'☒' if c['met'] else '☐'} {c['text']}" for c in ritual["conditions"])
    return f"Ritual under way: {ritual['goal']} — {marks}"


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
