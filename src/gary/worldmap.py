"""The campaign map (Dungeon World, "Steadings": "The Campaign Map", "Fronts on the Campaign Map").

"When the players leave the site of their first adventure for the safety of civilization it's time
to start drawing the campaign map": the site of the adventure, the nearest steading "within a day
or two", and "some terrain features" between -- a rocky pass, heavy woods, a road across open
ground. "Use pencil: this map will change."

Gary keeps it: places (steadings, dungeons, landmarks, terrain), routes between them in days -- the
book measures distance in rations, a day's supplies -- with the ground they cross, and roads that
lead off unexplored (blanks). Fronts appear only indirectly: "for each front add some feature to
the map that indicates the front's presence", named as the characters would -- a burning village, a
stream of refugees -- changed as the fronts change. The GM model sees the map every turn, so the
geography holds and a journey along a known route takes as long as it did before; the players see
it with /map.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

KINDS = ("steading", "dungeon", "landmark", "terrain")
MAX_PLACES = 80
MAX_SIGNS = 12
MAX_TEXT = 160


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:40]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_TEXT]


def empty() -> Dict[str, Any]:
    return {"places": {}, "order": [], "routes": [], "signs": []}


def _copy(world: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    w = world or empty()
    return {"places": {k: dict(v) for k, v in w["places"].items()}, "order": list(w["order"]),
            "routes": [dict(r) for r in w["routes"]], "signs": [dict(s) for s in w["signs"]]}


def place(world: Optional[Dict[str, Any]], name: str, kind: str = "landmark", note: str = "") -> Dict[str, Any]:
    """Add a place, or update what's known of it."""
    out = _copy(world)
    key = slug(name)
    if not key:
        raise ValueError("Name the place.")
    kind = kind if kind in KINDS else "landmark"
    if key not in out["places"]:
        if len(out["places"]) >= MAX_PLACES:
            raise ValueError(f"The map has {MAX_PLACES} places already.")
        out["places"][key] = {"name": _text(name), "kind": kind, "note": ""}
        out["order"].append(key)
    if note:
        out["places"][key]["note"] = _text(note)
    return out


def route(world: Optional[Dict[str, Any]], a: str, b: str, days: int = 0, terrain: str = "") -> Dict[str, Any]:
    """Join two places, ``days`` apart (0: not yet known) across ``terrain``. If ``b`` isn't a place,
    it's a road leading off unexplored -- a blank ("the north road")."""
    out = _copy(world)
    ka = slug(a)
    if ka not in out["places"]:
        raise ValueError(f"{a!r} isn't on the map: add it as a place first.")
    kb = slug(b) if slug(b) in out["places"] else ""
    label = "" if kb else _text(b)
    if not kb and not label:
        raise ValueError("Say where the route goes: a place on the map, or a road leading off ('the north road').")
    existing = next((r for r in out["routes"] if {r["a"], r["b"]} == {ka, kb} and kb), None) if kb else next(
        (r for r in out["routes"] if r["a"] == ka and not r["b"] and r["label"].lower() == label.lower()), None)
    if existing is None:
        existing = {"a": ka, "b": kb, "label": label, "days": 0, "terrain": ""}
        out["routes"].append(existing)
    if days:
        existing["days"] = max(1, min(int(days), 60))
    if terrain:
        existing["terrain"] = _text(terrain)
    if kb:  # a blank from a toward b is filled now
        out["routes"] = [r for r in out["routes"] if not (r["a"] == ka and not r["b"]
                                                          and r["label"].lower() == out["places"][kb]["name"].lower())]
    return out


def sign(world: Optional[Dict[str, Any]], text: str, near: str = "", remove: bool = False) -> Dict[str, Any]:
    """A sign of a front, named as the characters would ("smoke over Pigton"); or removed."""
    out = _copy(world)
    text = _text(text)
    if not text:
        raise ValueError("Say what the sign is, as the characters would see it.")
    out["signs"] = [s for s in out["signs"] if s["text"].lower() != text.lower()]
    if not remove:
        if len(out["signs"]) >= MAX_SIGNS:
            raise ValueError(f"At most {MAX_SIGNS} signs: remove one as its front changes.")
        out["signs"].append({"text": text, "near": slug(near) if slug(near) in out["places"] else ""})
    return out


def distance(world: Optional[Dict[str, Any]], a: str, b: str) -> int:
    """Days between two places along a known route, or 0."""
    if not world:
        return 0
    ka, kb = slug(a), slug(b)
    found = next((r for r in world["routes"] if r["b"] and {r["a"], r["b"]} == {ka, kb}), None)
    return int(found["days"]) if found else 0


def render(world: Optional[Dict[str, Any]]) -> str:
    if not world or not world["places"]:
        return ""
    places = world["places"]
    lines = []
    for key in world["order"]:
        p = places[key]
        out = []
        for r in world["routes"]:
            if key not in (r["a"], r["b"]):
                continue
            other = r["b"] if r["a"] == key else r["a"]
            far = f"{r['days']} day{'s' if r['days'] != 1 else ''}" if r["days"] else "distance unknown"
            ground = f", {r['terrain']}" if r["terrain"] else ""
            out.append(f"{places[other]['name']} ({far}{ground})" if other else f"{r['label']} (unexplored)")
        lines.append(f"• {p['name']} [{p['kind']}]" + (f": {p['note']}" if p["note"] else "")
                     + (f" — to {'; '.join(out)}" if out else ""))
    for s in world["signs"]:
        near = f" (near {places[s['near']]['name']})" if s["near"] in places else ""
        lines.append(f"⚠ {s['text']}{near}")
    return "\n".join(lines)
