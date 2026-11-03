"""`/moves`: what your character can do, in the book's words.

"Players looking for direction will look to their character sheet." On paper the moves are
printed there; in Gary they were only in the GM's view. This gathers a character's own moves --
race move, class starting moves (only the one of a Barbarian's pair they chose), advanced moves
(compendium ones included), and any taken from another class -- with their text, and lists the
basic and special moves everyone has, one line each.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Everyone's moves, in the book's order, with when they come up.
BASIC = ("hack_and_slash", "volley", "defy_danger", "defend", "spout_lore", "discern_realities", "parley",
         "aid_or_interfere")
SPECIAL = ("last_breath", "supply", "carouse", "recruit", "outstanding_warrants")
#: Special moves Gary runs without a roll of yours, said in a line.
OTHER = (("Make Camp", "when you settle in to rest: eat a ration, and after a few uninterrupted hours heal half your HP"),
         ("Take Watch (+Wis)", "when you're on watch and something approaches the camp"),
         ("Undertake a Perilous Journey", "when you travel through hostile territory: trailblazer, scout and quartermaster each roll +Wis"),
         ("Recover", "when you do nothing but rest in comfort and safety: a day heals you fully, three days shed a debility"),
         ("Level Up", "when you have downtime and XP equal to your level+7: the button on /sheet"),
         ("End of Session", "when the session ends: /end"))

_STAT = {"str": "+Str", "dex": "+Dex", "con": "+Con", "int": "+Int", "wis": "+Wis", "cha": "+Cha",
         "any": "+the stat your approach uses", "bond": "+Bond", "nothing": "no stat", "offers": "+what you offer",
         "loyalty": "+Loyalty"}

LIMIT = 1900  # under Discord's 2,000 a message


def own(sheet: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(name, text) for every move this character has of their own, race first."""
    from .class_moves import moves_of
    from .levelling import advanced_moves
    from .multiclass import text_of

    chosen = (sheet.get("choices") or {}).get("start", "")
    out = [(m["name"], m["text"]) for m in moves_of(str(sheet.get("class", "")), str(sheet.get("race", "")), chosen)]
    out += [(m["name"], m["text"]) for m in advanced_moves(sheet)]
    out += [(f"{m['name']} (from the {m['class'].title()})", text_of(m)) for m in sheet.get("multiclass_moves") or ()]
    return out


def basic() -> List[Tuple[str, str]]:
    from .moves import MOVES

    lines = [(MOVES[k].name, f"when you {MOVES[k].trigger} ({_STAT.get(MOVES[k].stat, MOVES[k].stat)})")
             for k in (*BASIC, *SPECIAL) if k in MOVES]
    return lines + list(OTHER)


def find(sheet: Dict[str, Any], wanted: str) -> Optional[Tuple[str, str]]:
    """One move by name: theirs first, then everyone's (with the rules Gary runs for it)."""
    from .moves import MOVES

    folded = " ".join(str(wanted or "").replace("’", "'").lower().split())
    mine = own(sheet)
    for name, text in mine:
        if name.replace("’", "'").lower() == folded:
            return name, text
    for name, text in mine:
        if folded and folded in name.replace("’", "'").lower():
            return name, text
    for key, move in MOVES.items():
        if folded in (key.replace("_", " "), move.name.lower()):
            bands = [f"10+: {move.strong.text}", f"7-9: {move.weak.text}", f"6-: {move.miss.text}"]
            return move.name, f"When you {move.trigger}, roll {_STAT.get(move.stat, move.stat)}. " + " ".join(bands)
    return None


def pages(sheet: Dict[str, Any]) -> List[str]:
    """The whole list, split into messages under Discord's limit."""
    blocks = [f"**{sheet['name']}'s moves**"]
    blocks += [f"**{name}** — {' '.join(text.split())}" for name, text in own(sheet)]
    blocks.append("**Everyone's moves** — `/moves move:` shows any of them in full")
    blocks += [f"• **{name}** — {text}" for name, text in basic()]
    out: List[str] = []
    current = ""
    for block in blocks:
        if len(block) > LIMIT:
            block = block[:LIMIT - 1] + "…"
        if current and len(current) + 1 + len(block) > LIMIT:
            out.append(current)
            current = block
        else:
            current = f"{current}\n{block}" if current else block
    if current:
        out.append(current)
    return out
