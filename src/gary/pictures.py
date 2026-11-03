"""Pictures (/picture): an image of something the table knows, in one consistent style.

Subjects are only what the players already know -- the seated characters, the people they
have met or heard of (npcs.known), the creatures in the scene, the current location and the
steadings on record -- so a picture never shows what the GM is keeping back. Each prompt is
built from what Gary has recorded (a character's look, an NPC's public line, a creature's tags
and qualities, a steading's tags), and every one is sent with the same style reference image
(data/style_reference.jpg) and style words, so a dragon and an innkeeper look like they belong
to the same campaign.

The first picture of each subject is kept and shown again when asked; "redraw" makes a new one.
This module is the words; adapters/pictures.py talks to the image model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STYLE_REFERENCE = Path(__file__).parent / "data" / "style_reference.jpg"

#: The style, in words, beside the reference image that carries most of it.
STYLE = ("painterly digital fantasy illustration with visible, textured brushwork; a muted, earthy palette of "
         "browns, greens and deep shadow; dramatic soft lighting; gritty, weathered realism, like concept art "
         "for a dark fantasy game")

KINDS = ("character", "npc", "creature", "place", "item")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:60] or "subject"


def subjects(state: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(kind, name) for everything the table can ask to see, characters first."""
    from .beyond import out_of_play
    from .npcs import known

    sheets = state.get("sheets") or {}
    seated = sorted({a for a in (state.get("players") or {}).values() if a in sheets
                     and not sheets[a].get("dead") and not out_of_play(sheets[a])})
    out: List[Tuple[str, str]] = [("character", a) for a in seated]
    out += [("npc", str(n["name"])) for n in known(state)]
    for a in seated:  # magic items the party carries
        for g in sheets[a].get("gear") or ():
            if g.get("magic") and ("item", g["name"]) not in out:
                out.append(("item", str(g["name"])))
    kinds = []
    for record in (state.get("monsters") or {}).values():
        kind = str(record.get("kind") or "")
        if kind and int(record.get("hp", 0)) > 0 and kind not in kinds:
            kinds.append(kind)
    out += [("creature", k) for k in kinds]
    location = str((state.get("scene") or {}).get("location") or "")
    if location:
        out.append(("place", location))
    for s in (state.get("steadings") or {}).values():
        if s.get("name") and ("place", s["name"]) not in out:
            out.append(("place", str(s["name"])))
    return out


def find(state: Dict[str, Any], wanted: str) -> Optional[Tuple[str, str]]:
    folded = " ".join(str(wanted or "").split()).lower()
    every = subjects(state)
    return (next((s for s in every if s[1].lower() == folded), None)
            or next((s for s in every if folded and folded in s[1].lower()), None))


def prompt(state: Dict[str, Any], kind: str, name: str) -> str:
    """What to draw, from what is recorded. The style goes with every one."""
    if kind == "character":
        from .choices import look_of

        sheet = (state.get("sheets") or {}).get(name) or {}
        look = look_of(sheet)
        what = (f"A head-and-shoulders character portrait of {name}, a {sheet.get('race', '')} "
                f"{sheet.get('class', 'adventurer')}" + (f": {look}" if look else "") + "."
                + (f" As pictured before: {sheet['portrait']}." if sheet.get("portrait") else ""))
    elif kind == "npc":
        npc = next((n for n in (state.get("npcs") or {}).values() if str(n.get("name")) == name), {})
        public = str(npc.get("public") or "").strip()
        what = (f"A head-and-shoulders portrait of {name}" + (f", {public}" if public else "") + "."
                + (f" As pictured before: {npc['portrait']}." if npc.get("portrait") else ""))
    elif kind == "item":
        item = next((g for s in (state.get("sheets") or {}).values() for g in s.get("gear") or ()
                     if g.get("magic") and g.get("name") == name), {})
        what = (f"A magic item, {name}, shown alone on a plain surface, with an air of power: "
                f"{str(item.get('magic', ''))[:300]}. Only the item: no people.")
    elif kind == "creature":
        from .monsters import lookup

        record = lookup(name) or next((r for r in (state.get("monsters") or {}).values()
                                       if r.get("kind") == name), {})
        details = [", ".join(str(t) for t in record.get("tags") or [] if str(t)[:1].isupper())]
        if record.get("qualities"):
            details.append("qualities: " + ", ".join(record["qualities"]))
        if record.get("attack"):
            details.append(f"attacks with its {str(record['attack']).lower()}")
        what = (f"A fantasy creature, the {name}, shown whole in its surroundings, menacing"
                + ("; " + "; ".join(d for d in details if d) if any(details) else "") + ". Only the creature: no "
                "people or adventurers in the picture.")
    else:
        from .steadings import describe as describe_steading

        scene = state.get("scene") or {}
        steading = next((s for s in (state.get("steadings") or {}).values() if s.get("name") == name), None)
        if steading is not None:
            what = f"A wide view of {name}, a fantasy settlement: {describe_steading(steading)}."
        else:
            summary = str(scene.get("summary") or "") if scene.get("location") == name else ""
            what = f"A wide view of {name}, a fantasy location" + (f": {summary}" if summary else "") + "."
    if kind == "place":
        # The party's looks come only from their own portraits: a stranger standing in for Gorm
        # in a landscape would contradict his.
        what += " No people or characters in it: the place alone."
    # No premise: put in, it drew the adventure's MacGuffin into every picture.
    return (f"{what} The reference image shows the art style ONLY: match its brushwork, palette and lighting -- "
            f"{STYLE} -- but copy nothing of its subject: not the person, face, hair, clothing, tattoos, jewellery "
            "or pose. Draw only what is described here. No text, lettering, borders or watermarks.")


#: Asked of a fresh portrait, so the story and later pictures match what was drawn.
DESCRIBE = ("Describe the person in this portrait in one line, under 30 words, for someone who has to recognise "
            "them later: hair, face, marks, clothing, anything they wear or carry. Plain words, no style or "
            "painting terms, no name.")

#: The second reference on a redraw.
SAME_SUBJECT = ("The second image is this same {kind} as drawn before. Keep WHO or WHAT it is the same -- face, "
                "build, features, colours, what it wears and carries -- but make a genuinely new picture: a clearly "
                "different pose, angle, expression and background, as if seen on another day. It must not look like a "
                "copy of the second image.")


def is_person(kind: str) -> bool:
    return kind in ("character", "npc")
