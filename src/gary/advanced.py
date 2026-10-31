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
    "Exterminatus": "the damage once you record the promise with swear_enemy",
    "Through Death's Eyes": "the -1 ongoing for the battle on a miss",
    "Indomitable": "the +1 forward whenever they suffer a debility",
    **{n: "the armor forward on every hack and slash" for n in ("Duelist's Parry", "Duelist's Block")},
    **{n: "the bonus on the next attack against that foe" for n in ("Scent of Blood", "Taste of Blood")},
    **{n: "the ally's bonus when make_move has ally" for n in ("Setup Strike", "Tandem Strike")},
    **{n: "it through lead_the_charge" for n in ("Charge!", "Ever Onward")},
    **{n: "the extra Arcane Art damage" for n in ("Vicious Cacophony", "Vicious Blast")},
    "Invigorate": "the +2 damage forward to whoever they heal",
    "Superior Warrior": "avoiding the counter-attack on a 12+ (the impression is yours)",
    "Smaug's Belly": "the 2 piercing when make_move has weak_point",
    **{n: "the off-hand damage when make_move has two_weapons" for n in ("Viper's Strike", "Viper's Fangs")},
    "Blot Out the Sun": "the extra targets and ammo when make_move has extra_targets",
    "Unnatural Ally": "the +2 ferocity and +1 instinct (the new training is yours)",
    "Half-Elven": "the other race's Ranger move",
    "Formshaper": "the armor or damage when Shapeshifter has form_boon",
    "Formcrafter": "the +1 and -1 when Shapeshifter has form_plus and form_minus",
    "Balance": "earning balance on damage; spend it with channel_balance",
    "Spell Augmentation": "it through spell_augmentation, onto the creature they last hit",
    "Tireless Blood": "ignoring the Sick debility's penalty",
    "Whosoever Holdeth": "bonds with 'my weapon' (the Bonds form offers it) and the boons through compendium_power",
    "Hunger": "the weapon bonds added to weapon damage",
    "Its Name Is Dread": "+bonds with the weapon when make_move has weapon_leverage",
    "Divine Bond": "deity bonds (the Bonds form offers the god) and marking one off through compendium_power",
    "Dovahkiin": "the Blood stat, its forward and the 1d4 through compendium_power",
    "Dragon's Breath": "it through compendium_power dragons_breath",
    "Fury": "the next attack's +1 piercing after dragon blood",
    "Wake the Dragon": "+Blood when make_move has dragon_nature",
    "Faraday's Law": "the +1d4 and the -1 to Bind while charged (compendium_power faraday)",
    "Velocitas Eradico": "Con, no ammo, no weapon when make_move has hurl",
    "Landed": "the stronghold steading through compendium_power found_stronghold",
    "Call the Banners": "no miss on Recruit at their stronghold",
    "From The Forbidden Land Returned": "the Arm's +2 piercing against the living (the other marks are yours)",
    **{n: "the note on each Trap Expert result (the hold is yours to keep)" for n in ("Cautious", "Extremely Cautious")},
    "Impervious Defender": "the +1 hold, and the 12+ (no hold: the attacker is stymied)",
    **{n: "the offer on a 10+ cast, as a note (doubling is yours)" for n in (
        "Empower", "Empowered Magic", "Greater Empower", "Greater Empowered Magic")},
    "Communion of Whispers": "the -1 forward on a miss",
    "Metal Hurlant": "the 1d10 damage",
    "Counterspell": "forgetting the staked spell on a 7-9",
    **{n: "the cleric spells: Commune (prepare_spells) and Cast a Spell at their cleric level"
       for n in ("Divine Favor", "God Amidst the Wastes")},
    **{n: "the chosen spell's lower level (lowered_spells)" for n in ("Chosen One", "Anointed", "Prodigy", "Master")},
    "Expanded Spellbook": "the spell, added to the spellbook",
    **{n: "it while start_quest has them on a quest" for n in ("Smite", "Holy Smite", "Holy Protection")},
    "Quest": "it once you record it with start_quest (and end it with end_quest): Smite and "
             "Holy Protection, and freedom from hunger at camp and on journeys",
    **{n: "the +1 forward" for n in ("Bamboozle", "Con", "A Little Help From My Friends")},
    # Answering a blow: the player presses Reduce damage on the hit (reactions.py).
    **{n: "it through the Reduce damage button on a hit" for n in (
        "Spell Defense", "Armor Mastery", "Armored Perfection", "Bloody Aegis", "Shed",
        "Man's Best Friend", "Divine Intervention", "Divine Invincibility", "Penitent")},
    "Martyr": "the pain and the forward through the Reduce damage button (adding their level to "
              "the spell is yours)",
    # Race moves, named as class_moves names them.
    "Elf (Ranger)": "it when perilous_journey has wilderness=true",
    "Human (Ranger)": "it when make_camp has dungeon_or_city=true",
    "Human (Fighter)": "it on a creature's hit, through the Reduce damage button",
    "Dwarf (Fighter)": "Con when make_move has race_move=true",
    "Halfling (Fighter)": "the +1 when make_move has race_move=true",
    "Human (Thief)": "the +1 when make_move has race_move=true",
    "Halfling (Thief)": "the +2 damage",
    "Halfling (Druid)": "the camp healing",
    "Elf (Wizard)": "the cantrip",
    "Dwarf (Cleric)": "the rote",
    "Human (Wizard)": "the borrowed spell",
    "Human (Cleric)": "the borrowed spell",
    "Elf (Fighter)": "the precise tag",
}


def applied(sheet: Dict[str, Any], name: str) -> str:
    """What Gary applies of this advanced move for this character, or ""."""
    if name == "Divine Protection":  # two moves share the name: the Cleric's, and the Paladin's on a quest
        return "the armor" if sheet.get("class") in ("Cleric", "Paladin") else ""
    return APPLIED.get(_same(name), "")


def _same(name: str) -> str:
    """The book writes "Man’s Best Friend" with a curly apostrophe; code writes it straight."""
    return name.replace("\u2019", "'")


def has(sheet: Dict[str, Any], name: str) -> bool:
    """An advanced move of their own, or one taken from another class (Multiclass)."""
    wanted = _same(name)
    return any(_same(n) == wanted for n in sheet.get("advanced_moves") or ()) or any(
        _same(m["name"]) == wanted for m in sheet.get("multiclass_moves") or ())


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
    if (sheet.get("shape") or {}).get("boon") == "armor":
        bonus.append((1, "Formshaper"))
    questing = bool(sheet.get("quest"))
    if questing and sheet.get("class") == "Paladin" and has(sheet, "Divine Protection"):
        bonus.append((2, "Divine Protection"))  # the Paladin's: "+2 armor while on a quest"
    elif questing and has(sheet, "Holy Protection"):
        bonus.append((1, "Holy Protection"))
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
    if (sheet.get("shape") or {}).get("boon") == "damage":
        out.append(("1d4", "Formshaper"))
    if weapon_tags or move in ("hack_and_slash", "volley"):
        from .bonds import weapon_bonds

        # Hunger: "Add your bonds with your weapon to any damage you deal with your weapon."
        if has(sheet, "Hunger") and weapon_bonds(sheet):
            out.append((str(weapon_bonds(sheet)), "Hunger"))
        # Faraday's Law: "When you stir the charge within a weapon you hold it does +1d4 damage ongoing."
        if sheet.get("faraday"):
            out.append(("1d4", "Faraday's Law"))
    if sheet.get("quest"):
        if has(sheet, "Holy Smite"):
            out.append(("1d8", "Holy Smite"))
        elif has(sheet, "Smite"):
            out.append(("1d4", "Smite"))
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
