"""Instant NPCs (Dungeon World, Appendix 4): "When you need a quick NPC all you need is an
instinct and some way to pursue it. We call that a knack ... Combine the two and you have an
NPC who has something they want and a way to try to get it."

Gary rolls the d100 tables -- with its own dice, like everything else -- and draws a name
the campaign has not used yet; the model makes the NPC from them (upsert_npc).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

_DATA = json.loads((Path(__file__).parent / "data" / "npcs.json").read_text(encoding="utf-8"))
INSTINCTS = _DATA["instincts"]
KNACKS = _DATA["knacks"]
#: The Appendix's names, and the class chapters' lists by race (data/names.json) that it lacks.
_BOOK = json.loads((Path(__file__).parent / "data" / "names.json").read_text(encoding="utf-8"))["names"]
NAMES = list(dict.fromkeys([*_DATA["names"], *(n for race in sorted(_BOOK) for n in _BOOK[race])]))


def roll(die: Callable[[str], int], used: Iterable[str] = ()) -> Dict[str, object]:
    """An instinct and a knack off the d100 tables, and a name nobody here has yet."""
    instinct, knack = die("1d100"), die("1d100")
    taken = {n.lower() for n in used}
    free = [n for n in NAMES if n.lower() not in taken] or NAMES
    name = free[(die(f"1d{len(free)}") - 1)] if len(free) > 1 else free[0]
    return {"name": name, "instinct": INSTINCTS[instinct - 1], "knack": KNACKS[knack - 1],
            "rolled": {"instinct": instinct, "knack": knack}}


def used_names(state: Dict[str, Any]) -> List[str]:
    """Everyone named in this campaign so far: its NPCs and its characters."""
    return [str(n.get("name") or k) for k, n in (state.get("npcs") or {}).items()] + list(state.get("sheets") or {})


def suggest(state: Dict[str, Any], count: int = 6) -> List[str]:
    """A handful of names this campaign hasn't used, for the GM to draw on: the model, left to
    itself, names everyone Vane and Pip. The same handful until the campaign moves on (its time
    or its NPCs change), so a turn's context doesn't shift under it."""
    import random

    taken = {w.lower() for n in used_names(state) for w in str(n).split()}
    free = [n for n in NAMES if n.lower() not in taken]
    seed = f"{state.get('world_minutes', 0)}:{len(state.get('npcs') or {})}"
    return random.Random(seed).sample(free, min(count, len(free)))


# -- who the party knows (/npcs) ------------------------------------------------------------

#: How a disposition (-5 hostile to +5 devoted) reads to the players.
def feeling(disposition: Any) -> str:
    if not isinstance(disposition, int):
        return ""
    return ("hostile" if disposition <= -3 else "wary" if disposition < 0 else "neutral" if disposition == 0
            else "friendly" if disposition <= 2 else "devoted")


def known(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The people the party has met or heard of, by name. Never the GM's notes: those hold
    secrets and instincts, and the imported setting's people stay hidden until met."""
    people = [n for n in (state.get("npcs") or {}).values() if n.get("known") and str(n.get("name") or "").strip()]
    return sorted(people, key=lambda n: str(n["name"]).lower())


def line(npc: Dict[str, Any]) -> str:
    how = feeling(npc.get("disposition"))
    out = f"**{npc['name']}**" + (f" ({how})" if how else "")
    return out + (f" — {npc['public']}" if npc.get("public") else "")


def roster(state: Dict[str, Any], name: Optional[str] = None, limit: int = 1900) -> str:
    people = known(state)
    if name:
        folded = " ".join(name.split()).lower()
        hit = [n for n in people if str(n["name"]).lower() == folded] or \
              [n for n in people if folded in str(n["name"]).lower()]
        return "\n".join(line(n) for n in hit[:5]) if hit else f"The party doesn't know anyone called {name}."
    if not people:
        return "The party hasn't met anyone by name yet."
    text, shown = f"**People you know** ({len(people)})", 0
    for npc in people:
        nxt = "\n- " + line(npc)
        if len(text) + len(nxt) > limit - 60:
            break
        text, shown = text + nxt, shown + 1
    if shown < len(people):
        text += f"\n…and {len(people) - shown} more: `/npcs name:` for one."
    return text
