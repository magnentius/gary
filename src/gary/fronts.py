"""Fronts (Dungeon World, "Fronts"): the threats that move whether the party looks or not.

A front groups dangers. Each danger has an impulse -- what it wants -- a short list of
grim portents, escalating steps it takes if nobody stops it, and an impending doom, what
comes to pass when the last portent does. A front also asks stakes questions: what the
GM wants play to find out.

The model writes fronts as threats take shape and ticks a portent off when the fiction
warrants it, onscreen or off; it is shown each danger's next portent every turn, so it
can foreshadow it. Gary keeps them as journalled state -- every change undoes -- and the
DM sees them with ``/fronts``. Nothing here decides anything: a front is the GM's
memory of what is coming, written down.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

from .sheets import SheetError

MAX_FRONTS = 6
MAX_DANGERS = 4
PORTENTS = (2, 5)  # fewest, most per danger
MAX_TEXT = 240


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:40]


def _text(value: Any, what: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise SheetError(f"A front needs {what}.")
    return text[:MAX_TEXT]


def make(name: str, dangers: Sequence[Dict[str, Any]], stakes: Sequence[str] = ()) -> Dict[str, Any]:
    """A front, checked: 1-4 dangers, each with an impulse, 2-5 grim portents and a doom."""
    built = []
    for d in list(dangers or ())[:MAX_DANGERS + 1]:
        portents = [_text(p, "a portent") for p in d.get("portents") or () if str(p).strip()]
        if not PORTENTS[0] <= len(portents) <= PORTENTS[1]:
            raise SheetError(f"Each danger has {PORTENTS[0]} to {PORTENTS[1]} grim portents.")
        built.append({"name": _text(d.get("name"), "a name for each danger"),
                      "impulse": _text(d.get("impulse"), "an impulse for each danger"),
                      "portents": portents, "doom": _text(d.get("doom"), "an impending doom for each danger"),
                      "reached": 0, "ended": ""})
    if not 1 <= len(built) <= MAX_DANGERS:
        raise SheetError(f"A front has 1 to {MAX_DANGERS} dangers.")
    return {"name": _text(name, "a name"), "dangers": built,
            "stakes": [_text(q, "a question") for q in stakes or () if str(q).strip()][:5]}


def danger_of(front: Dict[str, Any], name: str) -> Dict[str, Any]:
    folded = slug(name)
    for d in front["dangers"]:
        if slug(d["name"]) == folded:
            return d
    raise SheetError(f"No danger {name!r} in {front['name']}. Dangers: "
                     f"{', '.join(d['name'] for d in front['dangers'])}.")


def render(fronts: Dict[str, Dict[str, Any]]) -> List[str]:
    """Live-state lines: each live danger's impulse, how far it has come, and what is next."""
    lines: List[str] = []
    for front in fronts.values():
        live = [d for d in front["dangers"] if not d.get("ended")]
        if not live:
            continue
        lines.append(f"    - {front['name']}:")
        for d in live:
            done, total = int(d["reached"]), len(d["portents"])
            if done >= total:
                state = f"DOOM HAS COME: {d['doom']}"
            else:
                state = f"portent {done}/{total}; next: {d['portents'][done]} (doom: {d['doom']})"
            lines.append(f"        - {d['name']} — wants {d['impulse']}; {state}")
    return lines


def describe(front: Dict[str, Any]) -> str:
    """The whole front, for the DM."""
    out = [f"**{front['name']}**"]
    for d in front["dangers"]:
        done = int(d["reached"])
        out.append(f"• **{d['name']}** — impulse: {d['impulse']}"
                   + (f" — *ended: {d['ended']}*" if d.get("ended") else ""))
        out += [f"  {'☒' if i < done else '☐'} {p}" for i, p in enumerate(d["portents"])]
        out.append(f"  Impending doom: {d['doom']}")
    if front.get("stakes"):
        out.append("Stakes: " + " · ".join(front["stakes"]))
    return "\n".join(out)
