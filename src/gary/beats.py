"""Upward and downward beats: the rhythm of good moments and setbacks, read from the journal.

From Mike Shea's "Upward and Downward Beats" (Sly Flourish, 2022), after Robin Laws's
*Hamlet's Hit Points*: players stay engaged when good moments and setbacks alternate -- too
many ups feel dull, too many downs hopeless. Dungeon World already makes every miss a GM move
(a downward beat); the GM's soft moves ("offer an opportunity"), and being a fan of the
characters, are the upward ones. What it lacks is a view of the run. Gary reads the recent
beats from what it has journalled and, only when the run is lopsided, tells the GM which way
to lean next -- in what it introduces, never in how a roll comes out.

    up:   a strong hit; a creature brought down; treasure taken; HP regained
    down: a miss; Last Breath; HP lost; a debility taken
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

WINDOW = 8  # the beats looked at
LOPSIDED = 5  # of the last LOOK, this many one way is a run worth leaning against
LOOK = 6


def _sheet_change(entry: Dict[str, Any]) -> List[str]:
    after = (entry.get("payload") or {}).get("sheet") or {}
    before = ((entry.get("inverse") or {}).get("payload") or {}).get("sheet") or {}
    if not after or not before:
        return []
    out: List[str] = []
    hp_now, hp_was = after.get("hp"), before.get("hp")
    if isinstance(hp_now, int) and isinstance(hp_was, int) and hp_now != hp_was:
        out.append("up" if hp_now > hp_was else "down")
    if len(after.get("debilities") or ()) > len(before.get("debilities") or ()):
        out.append("down")
    return out


def beats(journal: Iterable[Dict[str, Any]]) -> List[str]:
    """"up" and "down", oldest first, since the campaign last began."""
    out: List[str] = []
    for entry in journal:
        kind = entry.get("kind")
        payload = entry.get("payload") or {}
        if kind == "campaign_reset":
            out = []
        elif kind == "move_outcome":
            if payload.get("move") == "last_breath":
                out.append("down")
            elif payload.get("band") == "strong":
                out.append("up")
            elif payload.get("band") == "miss":
                out.append("down")
        elif kind == "monster_hp" and payload.get("hp") == 0:
            out.append("up")
        elif kind == "monster_looted":
            out.append("up")
        elif kind == "sheet":
            out += _sheet_change(entry)
    return out


def line(run: List[str]) -> str:
    """One line for the GM when the recent run leans hard one way; "" otherwise."""
    recent = run[-WINDOW:]
    last = recent[-LOOK:]
    if len(last) < LOOK:
        return ""
    downs, ups = last.count("down"), last.count("up")
    shown = ", ".join(recent)
    if downs >= LOPSIDED:
        return (f"BEATS (recent good moments and setbacks, oldest first): {shown}. The table has had a hard run: let "
                "the next thing you introduce be an upward beat -- a friendly face, a find, a safe place to breathe, "
                "a weaker foe, an opportunity. Never soften a roll: the dice still decide.")
    if ups >= LOPSIDED:
        return (f"BEATS (recent good moments and setbacks, oldest first): {shown}. Things have gone their way for a "
                "while: let the next thing you introduce be a downward beat -- a stronger foe, an ambush, a cost, a "
                "dead end, a threat to something they care about. Never rig a roll: the dice still decide.")
    return ""
