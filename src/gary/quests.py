"""The Paladin's Quest (Dungeon World, Paladin starting moves).

"When you dedicate yourself to a mission through prayer and ritual cleansing, state what
you set out to do ... Then choose up to two boons ... The GM will then tell you what vow or
vows is required of you to maintain your blessing."

The player says it in play; the model records it with ``start_quest`` -- the mission, the
boons the player chose, and the vows it names as GM -- and ends it with ``end_quest`` when
the mission is done or a vow is broken. It lives on the Paladin's sheet as ``quest``.

While it lasts Gary applies what has a number: Smite and Holy Smite's damage, Holy
Protection and the Paladin's Divine Protection's armor (advanced.py), and the freedom from
hunger, thirst and sleep (no rations at camp or on a journey). The other boons -- a sense
of direction, invulnerability, divine authority, lie-piercing senses, a voice that
transcends language -- are the model's to honour, and it is shown them every turn.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .sheets import SheetError

MISSIONS = {
    "slay": "Slay {}, a great blight on the land",
    "defend": "Defend {} from the iniquities that beset them",
    "discover": "Discover the truth of {}",
}

#: key -> (the book's words, whether it names a blank)
BOONS = {
    "direction": ("An unwavering sense of direction to {}", True),
    "invulnerability": ("Invulnerability to {}", True),
    "authority": ("A mark of divine authority", False),
    "truth": ("Senses that pierce lies", False),
    "tongues": ("A voice that transcends language", False),
    "sustenance": ("A freedom from hunger, thirst, and sleep", False),
}

VOWS = {
    "honor": "Honor (forbidden: cowardly tactics and tricks)",
    "temperance": "Temperance (forbidden: gluttony in food, drink, and pleasure of the flesh)",
    "piety": "Piety (required: observance of daily holy services)",
    "valor": "Valor (forbidden: suffering an evil creature to live)",
    "truth": "Truth (forbidden: lies)",
    "hospitality": "Hospitality (required: comfort to those in need, no matter who they are)",
}

MAX_BOONS = 2
MAX_TEXT = 120


def _blank(text: Any, what: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        raise SheetError(f"Fill in the blank: {what}.")
    return text[:MAX_TEXT]


def make(mission: str, subject: str, boons: Sequence[Dict[str, Any]], vows: Sequence[str]) -> Dict[str, Any]:
    """A quest as the book shapes it, or SheetError saying what is missing."""
    kind = str(mission or "").strip().lower()
    if kind not in MISSIONS:
        raise SheetError(f"A quest is one of: {', '.join(MISSIONS)} (slay a blight, defend someone, "
                         "discover a truth).")
    chosen: List[Dict[str, str]] = []
    for boon in boons or ():
        key = str(boon.get("boon", "")).strip().lower()
        if key not in BOONS:
            raise SheetError(f"No boon {key!r}. Boons: {', '.join(BOONS)}.")
        if any(b["boon"] == key for b in chosen):
            raise SheetError(f"{key} is chosen twice.")
        words, blank = BOONS[key]
        detail = _blank(boon.get("detail"), f"what {key} is to or against") if blank else ""
        chosen.append({"boon": key, "detail": detail, "text": words.format(detail) if blank else words})
    if len(chosen) > MAX_BOONS:
        raise SheetError(f"Up to {MAX_BOONS} boons.")
    picked = []
    for vow in vows or ():
        key = str(vow).strip().lower().split(" ")[0]
        if key not in VOWS:
            raise SheetError(f"No vow {vow!r}. Vows: {', '.join(VOWS)}.")
        if key not in picked:
            picked.append(key)
    if not picked:
        raise SheetError("Name at least one vow the blessing requires -- that is the GM's part.")
    return {"mission": kind, "subject": _blank(subject, "who or what the quest is about"),
            "text": MISSIONS[kind].format(_blank(subject, "who or what the quest is about")),
            "boons": chosen, "vows": picked}


def on_quest(sheet: Optional[Dict[str, Any]]) -> bool:
    return bool((sheet or {}).get("quest"))


def has_boon(sheet: Optional[Dict[str, Any]], key: str) -> bool:
    return any(b["boon"] == key for b in ((sheet or {}).get("quest") or {}).get("boons") or ())


def describe(sheet: Dict[str, Any]) -> str:
    """One line for the sheet and the model; "" when not on a quest."""
    quest = sheet.get("quest")
    if not quest:
        return ""
    boons = "; ".join(b["text"] for b in quest["boons"]) or "none"
    vows = "; ".join(VOWS[v] for v in quest["vows"])
    return f"On a quest — {quest['text']}. Boons: {boons}. Vows: {vows}."
