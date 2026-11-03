"""Dungeon maps: "Draw maps, leave blanks" (Dungeon World, the GM's principles).

"Any time there's a new location described make sure it gets added to a map. When you draw a map
don't try to make it complete. Leave room for the unknown." A dungeon is built as it is explored,
by the GM's Dungeon Moves; Gary keeps the map. Each area the party enters goes on it with what
was established there, the way they came in, and its exits -- an exit not yet taken is a blank,
waiting. The GM model is shown the map every turn, so the layout holds, backtracking works and
the blanks get filled; the players see it with /map.

A dungeon may have a monster setting (the bestiary's Caverns, Depths, Undead...), so its
inhabitants hang together.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

MAX_AREAS = 60
MAX_TEXT = 160
SETTINGS = ("Caverns", "Depths", "Experiments", "Folk", "Hordes", "Planes", "Swamp", "Undead", "Woods")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:40]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_TEXT]


def new(name: str, setting: str = "") -> Dict[str, Any]:
    setting = next((s for s in SETTINGS if s.lower() == str(setting or "").strip().lower()), "")
    return {"name": _text(name), "setting": setting, "areas": {}, "order": []}


def enter(dungeon: Dict[str, Any], area: str, summary: str = "", came_from: str = "", via: str = "",
          exits: Sequence[str] = ()) -> Dict[str, Any]:
    """The party enters ``area``: it goes on the map (or is revisited), joined to where they came
    from by the exit they took, and its exits are added as blanks. Returns the new record."""
    out = {**dungeon, "areas": {k: {**v, "exits": [dict(e) for e in v["exits"]]} for k, v in dungeon["areas"].items()},
           "order": list(dungeon["order"])}
    key = slug(area)
    if not key:
        raise ValueError("Name the area.")
    if key not in out["areas"]:
        if len(out["areas"]) >= MAX_AREAS:
            raise ValueError(f"{dungeon['name']} has {MAX_AREAS} areas mapped already.")
        out["areas"][key] = {"name": _text(area), "summary": "", "exits": []}
        out["order"].append(key)
    here = out["areas"][key]
    if summary:
        here["summary"] = _text(summary)
    for label in exits or ():
        label = _text(label)
        if label and not any(e["label"].lower() == label.lower() for e in here["exits"]):
            here["exits"].append({"label": label, "to": ""})
    before = slug(came_from)
    if before and before != key and before in out["areas"]:
        back = out["areas"][before]
        taken = _text(via) or f"to {here['name']}"
        exit_ = next((e for e in back["exits"] if e["label"].lower() == taken.lower()), None) or next(
            (e for e in back["exits"] if e["to"] == key), None) or next(
            (e for e in back["exits"] if taken.lower() in e["label"].lower() and not e["to"]), None)
        if exit_ is None:
            back["exits"].append({"label": taken, "to": key})
        else:
            exit_["to"] = key
        if not any(e["to"] == before for e in here["exits"]):
            here["exits"].append({"label": f"back to {back['name']}", "to": before})
    out["here"] = key
    return out


def render(dungeon: Dict[str, Any], for_players: bool = False) -> str:
    """The map as lines: each area, what is known of it, and its exits -- the blanks marked."""
    areas = dungeon["areas"]
    head = f"{dungeon['name']}" + (f" ({dungeon['setting']})" if dungeon.get("setting") and not for_players else "")
    lines = [head]
    for key in dungeon["order"]:
        area = areas[key]
        exits = []
        for e in area["exits"]:
            if e["to"] and e["to"] in areas:
                exits.append(f"{e['label']} → {areas[e['to']]['name']}")
            else:
                exits.append(f"{e['label']} (unexplored)")
        mark = " ← here" if dungeon.get("here") == key else ""
        lines.append(f"• {area['name']}{mark}" + (f": {area['summary']}" if area.get("summary") else "")
                     + (f" — exits: {'; '.join(exits)}" if exits else ""))
    return "\n".join(lines)


def blanks(dungeon: Dict[str, Any]) -> int:
    return sum(1 for a in dungeon["areas"].values() for e in a["exits"] if not e["to"])


def creatures(setting: str) -> List[str]:
    """The bestiary's creatures of a monster setting, by name."""
    from .monsters import BESTIARY

    return sorted(str(m.get("name") or k).title() for k, m in BESTIARY.items()
                  if str(m.get("setting") or "").lower() == str(setting or "").lower())


def catalog() -> str:
    """Every monster setting with its creatures, one line each: for populating fronts."""
    return "\n".join(f"{s}: {', '.join(creatures(s))}" for s in SETTINGS)


def current(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    key = slug((state.get("scene") or {}).get("dungeon") or "")
    return (state.get("dungeons") or {}).get(key) if key else None
