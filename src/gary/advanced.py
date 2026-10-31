"""The advanced moves Gary applies, where they touch a number it already runs.

Most advanced moves are fiction with a number attached -- "+1 when acting on the
answers", "while on a quest" -- or need a choice Gary does not record, and those stay
text for the model to honour (class_moves.render). The ones here change armor, damage,
healing, a roll, a hold or a spell's cost in a way Gary can see for itself. Each is
looked up by name on the sheet's ``advanced_moves``; a move that replaces another is
written so only the stronger one counts.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple


#: What Gary applies itself, per advanced move; the model is told not to apply it again. A
#: move only partly here (A Safer Place's forward, Con's question) keeps the rest with the model.
APPLIED: Dict[str, str] = {
    **{n: "the armor" for n in ("Iron Hide", "Steel Hide", "Barkskin", "Divine Armor", "Arcane Ward",
                                "Arcane Armor", "Underdog", "Serious Underdog")},
    **{n: "the extra damage" for n in ("Merciless", "Bloodthirsty", "Cheap Shot", "Dirty Fighter")},
    **{n: "the damage die" for n in ("Red of Tooth and Claw", "Blood and Thunder")},
    **{n: "the extra healing" for n in ("Healing Song", "Healing Chorus", "Hospitaller",
                                         "Perfect Hospitaller", "Devoted Healer")},
    **{n: "the ignored penalties" for n in ("Serenity", "Providence")},
    **{n: "the rote" for n in ("First Aid", "Greater First Aid")},
    **{n: "the +1" for n in ("Seeing Red", "Voice of Authority", "Divine Authority", "A Safe Place",
                             "A Safer Place", "The Scales of Life and Death")},
    **{n: "the +1 hold" for n in ("Staunch Defender", "Impervious Defender")},
    "Embracing No Form": "the extra hold",
    **{n: "the +1 forward" for n in ("Bamboozle", "Con", "A Little Help From My Friends")},
}


def applied(sheet: Dict[str, Any], name: str) -> str:
    """What Gary applies of this advanced move for this character, or ""."""
    if name == "Divine Protection":  # two moves share the name; only the Cleric's is armor
        return "the armor" if sheet.get("class") == "Cleric" else ""
    return APPLIED.get(name, "")


def has(sheet: Dict[str, Any], name: str) -> bool:
    return name in (sheet.get("advanced_moves") or ())


def armor_bonus(sheet: Dict[str, Any], outnumbered: bool) -> Tuple[int, List[str]]:
    """Armor from advanced moves, and what each is, for a blow landing now."""
    from . import spells

    bonus: List[Tuple[int, str]] = []
    if has(sheet, "Steel Hide"):
        bonus.append((2, "Steel Hide"))
    elif has(sheet, "Iron Hide"):
        bonus.append((1, "Iron Hide"))
    if has(sheet, "Barkskin"):
        bonus.append((1, "Barkskin"))
    worn = int(sheet.get("armor", 0))
    if worn == 0 and has(sheet, "Divine Armor"):
        bonus.append((3, "Divine Armor"))
    elif worn == 0 and has(sheet, "Divine Protection") and sheet.get("class") == "Cleric":
        bonus.append((2, "Divine Protection"))
    holding = any(s.level >= 1 for s in spells.castable(sheet)) if spells.caster_class(sheet) else False
    if holding and has(sheet, "Arcane Armor"):
        bonus.append((4, "Arcane Armor"))
    elif holding and has(sheet, "Arcane Ward"):
        bonus.append((2, "Arcane Ward"))
    if has(sheet, "Serious Underdog"):
        bonus.append((2 if outnumbered else 1, "Serious Underdog"))
    elif has(sheet, "Underdog") and outnumbered:
        bonus.append((1, "Underdog"))
    return sum(b for b, _ in bonus), [f"+{b} {n}" for b, n in bonus]


def damage_dice(sheet: Dict[str, Any], move: str, weapon_tags: Sequence[str]) -> List[Tuple[str, str]]:
    """Extra damage dice from advanced moves for this attack: [(dice, why)]."""
    out: List[Tuple[str, str]] = []
    if has(sheet, "Bloodthirsty"):
        out.append(("1d8", "Bloodthirsty"))
    elif has(sheet, "Merciless"):
        out.append(("1d4", "Merciless"))
    precise_or_hand = "precise" in weapon_tags or "hand" in weapon_tags
    if move == "backstab" and precise_or_hand:
        if has(sheet, "Dirty Fighter"):
            out.append(("1d8", "Dirty Fighter"))
        elif has(sheet, "Cheap Shot"):
            out.append(("1d6", "Cheap Shot"))
    elif has(sheet, "Dirty Fighter") and precise_or_hand:
        out.append(("1d4", "Dirty Fighter"))
    return out


def shaped_die(sheet: Dict[str, Any], die: str) -> str:
    """A Druid's damage die in a dangerous animal form."""
    if not sheet.get("shape"):
        return die
    if has(sheet, "Blood and Thunder"):
        return "d10"
    if has(sheet, "Red of Tooth and Claw"):
        return "d8"
    return die


def healing_dice(healer: Dict[str, Any], move: str, spell: str, other: bool) -> List[Tuple[str, str]]:
    """Extra healing from the healer's advanced moves: [(dice or a number, why)]."""
    out: List[Tuple[str, str]] = []
    if move == "arcane_art":
        if has(healer, "Healing Chorus"):
            out.append(("2d8", "Healing Chorus"))
        elif has(healer, "Healing Song"):
            out.append(("1d8", "Healing Song"))
    if other and (move == "lay_on_hands" or spell):
        if has(healer, "Perfect Hospitaller"):
            out.append(("2d8", "Perfect Hospitaller"))
        elif has(healer, "Hospitaller"):
            out.append(("1d8", "Hospitaller"))
    if other and has(healer, "Devoted Healer"):
        out.append((str(int(healer.get("level", 1))), "Devoted Healer"))
    return out


def ongoing_relief(sheet: Dict[str, Any]) -> int:
    """How many ongoing-spell penalties the caster ignores (Serenity, Providence)."""
    return 2 if has(sheet, "Providence") else 1 if has(sheet, "Serenity") else 0


def rote_spells(sheet: Dict[str, Any]) -> List[str]:
    """Cure spells made rotes (First Aid, Greater First Aid)."""
    out = []
    if has(sheet, "First Aid") and sheet.get("class") == "Cleric":
        out.append("Cure Light Wounds")
    if has(sheet, "Greater First Aid"):
        out.append("Cure Moderate Wounds")
    return out
