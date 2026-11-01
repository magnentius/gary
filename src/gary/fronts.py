"""Fronts (Dungeon World, "Fronts"): the threats that move whether the party looks or not.

The book's recipe, which ``make`` checks:

    choose campaign front or adventure front
    create 2-3 dangers (at most 3: leave room for discovery), each of a type from the book's
        lists -- which gives its impulse, and its family's GM moves
    choose an impending doom for each, one of six kinds, given a concrete form
    add grim portents: 1-3 for an adventure front, 3-5 for the campaign front
    write 1-3 stakes questions
    list the general cast -- and a description, and any custom moves the danger suggests

Fronts are "built outside of active play": Gary opens a window for writing them after each
/end (and after the first session, the first ones), and refuses ``create_front``
otherwise. In play the model notes new threats, advances portents -- descriptively, as play
shows them, or prescriptively, as a hard move -- revises what is still to come, and moves a
danger between adventure and campaign fronts. Gary keeps them as journalled state -- every
change undoes -- shows the model each live danger's next portent and its family's GM moves
every turn, and shows the DM everything with /fronts. Nothing here decides anything: a front
is the GM's memory of what is coming, written down.

The danger types, their impulses and the GM moves are the authors' (CC BY 3.0).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .sheets import SheetError

MAX_FRONTS = 6
MAX_DANGERS = 3  # "limit yourself to 3 at most and leave room for discovery"
PORTENTS = {"adventure": (1, 3), "campaign": (3, 5)}  # fewest, most per danger
MAX_TEXT = 240
MAX_STAKES = 3
MAX_CAST = 8
MAX_MOVES = 3

#: "You'll make your campaign front and first adventure fronts after your first session."
SCOPES = ("campaign", "adventure")

#: Types of Dangers: family -> (type, impulse) ...
DANGER_TYPES: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "Ambitious Organizations": (
        ("Misguided Good", "to do what is “right” no matter the cost"),
        ("Thieves Guild", "to take by subterfuge"), ("Cult", "to infest from within"),
        ("Religious Organization", "to establish and follow doctrine"),
        ("Corrupt Government", "to maintain the status quo"), ("Cabal", "to absorb those in power, to grow")),
    "Planar Forces": (
        ("God", "to gather worshippers"), ("Demon Prince", "to open the gates of Hell"),
        ("Elemental Lord", "to tear down creation to its component parts"),
        ("Force of Chaos", "to destroy all semblance of order"), ("Choir of Angels", "to pass judgement"),
        ("Construct of Law", "to eliminate perceived disorder")),
    "Arcane Enemies": (
        ("Lord of the Undead", "to seek true immortality"), ("Power-mad Wizard", "to seek magical power"),
        ("Sentient Artifact", "to find a worthy wielder"), ("Ancient Curse", "to ensnare"),
        ("Chosen One", "to fulfill or resent their destiny"),
        ("Dragon", "to hoard gold and jewels, to protect the clutch")),
    "Hordes": (
        ("Wandering Barbarians", "to grow strong, to drive their enemies before them"),
        ("Humanoid Vermin", "to breed, to multiply and consume"),
        ("Underground Dwellers", "to defend the complex from outsiders"), ("Plague of the Undead", "to spread")),
    "Cursed Places": (
        ("Abandoned Tower", "to draw in the weak-willed"), ("Unholy Ground", "to spawn evil"),
        ("Elemental Vortex", "to grow, to tear apart reality"), ("Dark Portal", "to disgorge demons"),
        ("Shadowland", "to corrupt or consume the living"), ("Place of Power", "to be controlled or tamed")),
}

#: Each family's GM moves.
GM_MOVES: Dict[str, Tuple[str, ...]] = {
    "Ambitious Organizations": (
        "Attack someone by stealthy means (kidnapping, etc.)", "Attack someone directly (with a gang or single "
        "assailant)", "Absorb or buy out someone important (an ally, perhaps)", "Influence a powerful institution "
        "(change a law, manipulate doctrine)", "Establish a new rule (within the organization)",
        "Claim territory or resources", "Negotiate a deal", "Observe a potential foe in great detail"),
    "Planar Forces": (
        "Turn an organization (corrupt or infiltrate with influence)", "Give dreams of prophecy",
        "Lay a Curse on a foe", "Extract a promise in exchange for a boon", "Attack indirectly, through "
        "intermediaries", "Rarely, when the stars are right, attack directly", "Foster rivalries with other, "
        "similar powers", "Expose someone to a Truth, wanted or otherwise"),
    "Arcane Enemies": (
        "Learn forbidden knowledge", "Cast a spell over time and space", "Attack a foe with magic, directly or "
        "otherwise", "Spy on someone with a scrying spell", "Recruit a follower or toady",
        "Tempt someone with promises", "Demand a sacrifice"),
    "Hordes": (
        "Assault a bastion of civilization", "Embrace internal chaos", "Change direction suddenly",
        "Overwhelm a weaker force", "Perform a show of dominance", "Abandon an old home, find a new one",
        "Grow in size by breeding or conquest", "Appoint a champion", "Declare war and act upon that "
        "declaration without hesitation or deliberation"),
    "Cursed Places": (
        "Vomit forth a lesser monster", "Spread to an adjacent place", "Lure someone in",
        "Grow in intensity or depth", "Leave a lingering effect on an inhabitant or visitor",
        "Hide something from sight", "Offer power", "Dampen magic or increase its effects",
        "Confuse or obfuscate truth or direction", "Corrupt a natural law"),
}

#: Impending Doom: "Choose one of the types of impending dooms and give it a concrete form."
DOOMS: Dict[str, str] = {
    "Tyranny": "of the strong over the weak or the few over the many",
    "Pestilence": "the spread of sickness and disease, the end of wellness",
    "Destruction": "apocalypse, ruin and woe",
    "Usurpation": "the chain of order comes apart, someone rightful is displaced",
    "Impoverishment": "enslavement, the abandonment of goodness and right",
    "Rampant Chaos": "laws of reality, of society, or any order is dissolved",
}

_TYPES = {t.lower(): (family, t, impulse) for family, types in DANGER_TYPES.items() for t, impulse in types}


def type_names() -> List[str]:
    return [t for types in DANGER_TYPES.values() for t, _ in types]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:40]


def _text(value: Any, what: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise SheetError(f"Give {what}.")
    return text[:MAX_TEXT]


def _texts(values: Any, limit: int) -> List[str]:
    return [" ".join(str(v).split())[:MAX_TEXT] for v in values or () if str(v).strip()][:limit]


def _portents(values: Any, scope: str, reached: int = 0) -> List[str]:
    portents = [_text(p, "a portent") for p in values or () if str(p).strip()]
    low, high = PORTENTS[scope]
    if not max(low, reached) <= len(portents) <= max(high, reached + 1):
        raise SheetError(f"A danger on {'the campaign front' if scope == 'campaign' else 'an adventure front'} "
                         f"has {low} to {high} grim portents.")
    return portents


def _doom(d: Dict[str, Any]) -> Tuple[str, str]:
    kind = str(d.get("doom_type") or "").strip()
    found = next((k for k in DOOMS if k.lower() == kind.lower()), None)
    if found is None:
        raise SheetError("Choose the impending doom's kind: " + ", ".join(DOOMS) + ".")
    return found, _text(d.get("doom"), "the impending doom a concrete form")


def danger(d: Dict[str, Any], scope: str) -> Dict[str, Any]:
    """One danger, checked: a type from the book (its impulse unless one is given), portents
    and a doom."""
    found = _TYPES.get(str(d.get("type") or "").strip().lower())
    if found is None:
        raise SheetError("Give each danger a type from the book: " + ", ".join(type_names()) + ".")
    family, kind, impulse = found
    doom_type, doom = _doom(d)
    return {"name": _text(d.get("name"), "a name for each danger"), "type": kind, "family": family,
            "impulse": " ".join(str(d.get("impulse") or "").split())[:MAX_TEXT] or impulse,
            "portents": _portents(d.get("portents"), scope), "doom_type": doom_type, "doom": doom,
            "reached": 0, "ended": ""}


def make(name: str, dangers: Sequence[Dict[str, Any]], stakes: Sequence[str] = (), scope: str = "adventure",
         description: str = "", cast: Sequence[str] = (), moves: Sequence[str] = ()) -> Dict[str, Any]:
    """A front, checked against the book's recipe."""
    if scope not in SCOPES:
        raise SheetError("A front is a campaign front or an adventure front.")
    if not 1 <= len(list(dangers or ())) <= MAX_DANGERS:
        raise SheetError(f"A front has 1 to {MAX_DANGERS} dangers: leave room for discovery.")
    built = [danger(d, scope) for d in dangers]
    asked = _texts(stakes, MAX_STAKES + 1)
    if not 1 <= len(asked) <= MAX_STAKES:
        raise SheetError(f"Write 1 to {MAX_STAKES} stakes questions: what you want play to find out.")
    return {"name": _text(name, "a name"), "scope": scope, "dangers": built, "stakes": asked,
            "description": " ".join(str(description or "").split())[:MAX_TEXT * 2],
            "cast": _texts(cast, MAX_CAST), "moves": _texts(moves, MAX_MOVES)}


def danger_of(front: Dict[str, Any], name: str) -> Dict[str, Any]:
    folded = slug(name)
    for d in front["dangers"]:
        if slug(d["name"]) == folded:
            return d
    raise SheetError(f"No danger {name!r} in {front['name']}. Dangers: "
                     f"{', '.join(d['name'] for d in front['dangers']) or 'none'}.")


def revise(front: Dict[str, Any], danger_name: str = "", portents: Optional[Sequence[str]] = None,
           doom: str = "", doom_type: str = "", stakes: Optional[Sequence[str]] = None,
           cast: Sequence[str] = (), description: str = "", moves: Sequence[str] = ()) -> Dict[str, Any]:
    """"These often change in play... you can change them later." What is still to come for a
    danger -- the portents not yet reached, its doom -- or the front's stakes, cast (added to),
    description and custom moves. A portent that has come to pass stays."""
    out = {**front, "dangers": [dict(d) for d in front["dangers"]]}
    if danger_name:
        d = danger_of(out, danger_name)
        if portents is not None:
            reached = int(d["reached"])
            d["portents"] = d["portents"][:reached] + [_text(p, "a portent") for p in portents if str(p).strip()]
            if len(d["portents"]) <= reached:
                raise SheetError("Leave at least one portent still to come (end_danger ends a danger).")
        if doom or doom_type:
            d["doom_type"], d["doom"] = _doom({"doom_type": doom_type or d.get("doom_type", ""),
                                               "doom": doom or d.get("doom", "")})
    if stakes is not None:
        asked = _texts(stakes, MAX_STAKES + 1)
        if not 1 <= len(asked) <= MAX_STAKES:
            raise SheetError(f"Write 1 to {MAX_STAKES} stakes questions.")
        out["stakes"] = asked
    if cast:
        out["cast"] = (list(out.get("cast") or []) + _texts(cast, MAX_CAST))[:MAX_CAST]
    if description:
        out["description"] = " ".join(description.split())[:MAX_TEXT * 2]
    if moves:
        out["moves"] = (list(out.get("moves") or []) + _texts(moves, MAX_MOVES))[-MAX_MOVES:]
    return out


def move(source: Dict[str, Any], target: Dict[str, Any], danger_name: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """"When a danger from an adventure front goes without resolution... move it to the campaign
    front"; "You can move dangers from the campaign fronts to an adventure front if they've come
    to bear, too." Its progress goes with it."""
    d = danger_of(source, danger_name)
    if len([x for x in target["dangers"] if not x.get("ended")]) >= MAX_DANGERS:
        raise SheetError(f"{target['name']} already has {MAX_DANGERS} dangers.")
    return ({**source, "dangers": [x for x in source["dangers"] if x is not d]},
            {**target, "dangers": [*target["dangers"], dict(d)]})


def render(fronts: Dict[str, Dict[str, Any]]) -> List[str]:
    """Live-state lines: each live danger's impulse, how far it has come and what is next, its
    front's custom moves, and the GM moves of each danger family in play (once each)."""
    lines: List[str] = []
    families: List[str] = []
    for front in fronts.values():
        live = [d for d in front["dangers"] if not d.get("ended")]
        if not live:
            continue
        lines.append(f"    - {front['name']}" + (" (campaign front)" if front.get("scope") == "campaign" else "") + ":")
        for d in live:
            done, total = int(d["reached"]), len(d["portents"])
            doom = f"{d.get('doom_type') + ': ' if d.get('doom_type') else ''}{d['doom']}"
            if done >= total:
                state = f"DOOM HAS COME: {doom}"
            else:
                state = f"portent {done}/{total}; next: {d['portents'][done]} (doom: {doom})"
            kind = f" ({d['type']})" if d.get("type") else ""
            lines.append(f"        - {d['name']}{kind} — wants {d['impulse']}; {state}")
            if d.get("family") and d["family"] not in families:
                families.append(d["family"])
        lines += [f"        - custom move: {m}" for m in front.get("moves") or ()]
    for family in families:
        lines.append(f"    - GM moves for {family}: " + "; ".join(GM_MOVES[family]) + ".")
    return lines


def describe(front: Dict[str, Any]) -> str:
    """The whole front, for the DM."""
    out = [f"**{front['name']}**" + (" (campaign front)" if front.get("scope") == "campaign" else "")]
    if front.get("description"):
        out.append(f"*{front['description']}*")
    for d in front["dangers"]:
        done = int(d["reached"])
        kind = f" ({d['type']})" if d.get("type") else ""
        out.append(f"• **{d['name']}**{kind} — impulse: {d['impulse']}"
                   + (f" — *ended: {d['ended']}*" if d.get("ended") else ""))
        out += [f"  {'☒' if i < done else '☐'} {p}" for i, p in enumerate(d["portents"])]
        out.append(f"  Impending doom: {d.get('doom_type') + ' — ' if d.get('doom_type') else ''}{d['doom']}")
    if front.get("cast"):
        out.append("Cast: " + " · ".join(front["cast"]))
    if front.get("moves"):
        out += [f"Custom move: {m}" for m in front["moves"]]
    if front.get("stakes"):
        out.append("Stakes: " + " · ".join(front["stakes"]))
    return "\n".join(out)
