"""Hirelings (Dungeon World, "Hirelings"; the Recruit and Order Hirelings moves).

A hireling is a name, a cost, a loyalty score and one or more skills with a rank. They
are kept on their employer's sheet as ``hirelings``, so hiring, paying and losing one is
a journalled sheet change that undoes like any other.

Gary checks a hireling against the book's rules for making one -- where they were found
sets the points shared between loyalty and skills -- and rolls Order Hirelings +loyalty
(-1 while their cost goes unpaid). What each skill does is shown to the model to honour;
Gary applies the five that touch a number it runs -- Priest (camp healing, first aid),
Warrior (damage, taking the counter-attack), Protector (Intervene on Defy Danger, Sentry
armor), Tracker (guided journeys), Minstrel (prices). Burglar and Adept stay with the
model.

The skill rules are the book's, lightly shortened; there are seven.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .registry import normalize_actor
from .sheets import MAX_NAME_CHARS, SheetError

SKILLS: Dict[str, str] = {
    "Adept": "Arcane Assistance — when an adept aids in casting a spell of lower level than their "
             "skill, its range, duration or potency grows (the GM says how, before the casting); "
             "any bad effects of the casting fall on the adept first.",
    "Burglar": "Experimental Trap Disarming — leading the way, they spot traps almost in time: a "
               "sprung trap hits the burglar fully, the party gets +skill against it and adds the "
               "skill to armor against it. Given a camp nearby, they disarm it by morning.",
    "Minstrel": "A Hero's Welcome — in a place of food, drink or entertainment you are treated as "
                "friends by everyone present; subtract their skill from all prices in town.",
    "Priest": "Ministry — making camp with a priest, whoever heals heals +skill HP. First Aid — when "
              "a priest staunches your wounds, heal 2×skill HP and take -1 forward.",
    "Protector": "Sentry — standing between you and an attack, add their skill to your armor against "
                 "it, then their skill drops 1 until they heal. Intervene — helping you Defy Danger, "
                 "you may take +1, but a 10+ counts as a 7-9.",
    "Tracker": "Track — given a camp to study a trail, they follow it to the next major change. Guide "
               "— leading the way, a Perilous Journey shorter (in rations) than their skill succeeds "
               "automatically.",
    "Warrior": "Man-at-arms — when they aid you, add their skill to the damage you deal; if your attack "
               "draws a counter-attack, they take the brunt of it.",
}

COSTS = ("The Thrill of Victory", "Money", "Uncovered Knowledge", "Fame and Glory", "Debauchery",
         "Good Accomplished")

#: Where a hireling was found -> the most points shared between loyalty and skills.
FOUND = {"village": 5, "town": 6, "keep": 8, "city": 10}

#: "Starting loyalty higher than 2 is unusual, as is starting loyalty below 0."
LOYALTY_RANGE = (-1, 3)

#: Recruit: what a player may make known, each +1.
OFFERS = {"generous pay": "that your pay is generous",
          "purpose": "what you're setting out to do",
          "share": "that they'll get a share of whatever you find",
          "reputation": "a useful reputation around these parts"}


def make(name: str, skills: Dict[str, int], loyalty: int, cost: str, found_in: str) -> Dict[str, Any]:
    """A hireling as the book makes one, or SheetError saying what is wrong."""
    name = " ".join(str(name or "").split())
    if not name or len(name) > MAX_NAME_CHARS:
        raise SheetError(f"A hireling needs a name of 1 to {MAX_NAME_CHARS} characters.")
    where = str(found_in or "").strip().lower()
    if where not in FOUND:
        raise SheetError(f"Say where they were found: {', '.join(FOUND)}.")
    picked: Dict[str, int] = {}
    for skill, rank in (skills or {}).items():
        match = next((s for s in SKILLS if s.lower() == str(skill).strip().lower()), None)
        if match is None:
            raise SheetError(f"No hireling skill {skill!r}. Skills: {', '.join(SKILLS)}.")
        if int(rank) < 1:
            raise SheetError(f"{match} needs a rank of 1 or more.")
        picked[match] = int(rank)
    if not picked:
        raise SheetError("A hireling has at least one skill.")
    loyalty = int(loyalty)
    if not LOYALTY_RANGE[0] <= loyalty <= LOYALTY_RANGE[1]:
        raise SheetError(f"Starting loyalty runs {LOYALTY_RANGE[0]} to {LOYALTY_RANGE[1]}.")
    points = max(loyalty, 0) + sum(picked.values())
    if points > FOUND[where]:
        raise SheetError(f"A {where} hireling has at most {FOUND[where]} points between loyalty and "
                         f"skills; that is {points}.")
    match = next((c for c in COSTS if c.lower() == str(cost or "").strip().lower()), None)
    if match is None:
        raise SheetError(f"Their cost is one of: {', '.join(COSTS)}.")
    return {"name": name, "skills": picked, "loyalty": loyalty, "cost": match, "found_in": where,
            "unpaid": False}


def find(sheet: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    folded = normalize_actor(name)
    return next((h for h in sheet.get("hirelings") or () if normalize_actor(h["name"]) == folded), None)


def loyalty_of(hireling: Dict[str, Any]) -> int:
    """What Order Hirelings rolls: loyalty, -1 ongoing while their cost goes unpaid."""
    return int(hireling["loyalty"]) - (1 if hireling.get("unpaid") else 0)


def skill(hireling: Dict[str, Any], name: str) -> int:
    """A hireling's rank in a skill, less any strain from standing sentry (Protector)."""
    rank = int((hireling.get("skills") or {}).get(name, 0))
    return max(0, rank - int(hireling.get("strain", 0))) if name == "Protector" else rank


def best(sheets: Sequence[Dict[str, Any]], name: str) -> int:
    """The highest rank in this skill among every hireling at the table."""
    return max((skill(h, name) for s in sheets for h in s.get("hirelings") or ()), default=0)


def label(h: Dict[str, Any]) -> str:
    skills = ", ".join(f"{s} {r}" + (f" (strained to {skill(h, s)})" if s == "Protector" and h.get("strain") else "")
                       for s, r in h["skills"].items())
    return (f"{h['name']} ({skills}; loyalty {h['loyalty']:+d}; cost: {h['cost']}"
            + (", UNPAID" if h.get("unpaid") else "") + ")")


def describe(sheet: Dict[str, Any]) -> str:
    """One line for the player and the model. "" without hirelings."""
    hired = sheet.get("hirelings") or []
    return "Hirelings — " + "; ".join(label(h) for h in hired) if hired else ""


def skill_rules(sheets: Sequence[Dict[str, Any]]) -> List[str]:
    """The rules of every skill some hireling at the table has, for the model."""
    have = {s for sheet in sheets for h in sheet.get("hirelings") or () for s in h["skills"]}
    return [f"{s}: {SKILLS[s]}" for s in SKILLS if s in have]
