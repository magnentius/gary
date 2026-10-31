"""The Dungeon World moves Gary knows: the basic moves, and the special moves the rules
trigger themselves (ARCHITECTURE.md §5).

Hand-written from the authors' text (Sagelt/Dungeon-World, text/Moves.xml), not parsed:
there are eight, and the parts a parser cannot get right -- which option exposes you to an
attack, how many questions a hit lets you ask -- are exactly the parts that matter. The
band text is the authors' own; options are lightly reworded to read as something a player
picks ("Take what you can get: -1d6 damage"), and Defend's spending list is folded into
its text because hold is not tracked yet.

A move is rolled 2d6 + a stat modifier and lands in a band: 10+ strong, 7-9 weak, 6- miss.
What each band means is fixed here. Where the rules give the *player* a choice, the band
carries it, and the player makes it (§5.1) -- the model is told what was chosen, never
asked to choose.

Dungeon World is CC-BY 3.0, by Sage LaTorra and Adam Koebel.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

#: Sentinel stats. ANY: the player's approach picks one of the six (Defy Danger). BOND:
#: rolled +Bond with the character helped -- how many bonds the roller has with them.
#: NOTHING: "just roll, +nothing" (Last Breath). CASTER: the caster's casting stat --
#: Wis for a Cleric, Int for a Wizard.
ANY = "any"
BOND = "bond"
NOTHING = "nothing"
CASTER = "caster"
#: Recruit rolls +1 for each thing made known; Order Hirelings rolls the hireling's loyalty.
OFFERS = "offers"
LOYALTY = "loyalty"

STRONG, WEAK, MISS = "strong", "weak", "miss"


def band_of(total: int) -> str:
    if total >= 10:
        return STRONG
    if total >= 7:
        return WEAK
    return MISS


@dataclass(frozen=True)
class Option:
    text: str
    # Taking this option means the enemy attacks you -- Gary applies it, not the model.
    enemy_attacks: bool = False
    # Taking this option changes the damage dice: "+1d6" or "-1d6".
    damage_mod: Optional[str] = None
    # Taking this option spends one ammo.
    spends_ammo: bool = False
    # Taking this option deals your damage, on a move whose result alone does not
    # (Backstab's "you deal your damage+1d6").
    deals_damage: bool = False
    # Taking this option heals the move's target by these dice (Arcane Art's 1d8).
    heals: Optional[str] = None
    # Cast a Spell, 7-9: "take -1 ongoing to cast a spell", or the spell is lost.
    casting_penalty: bool = False
    loses_spell: bool = False


@dataclass(frozen=True)
class Choice:
    """The player picks exactly ``count`` of ``options``."""

    count: int
    options: Tuple[Option, ...]


@dataclass(frozen=True)
class Band:
    text: str
    choice: Optional[Choice] = None
    # The rules say the enemy attacks you on this result, whatever anyone prefers.
    enemy_attacks: bool = False
    # "Deal your damage": the roller rolls their damage die against the target.
    deals_damage: bool = False
    # Heals the move's target by these dice (Lay on Hands' 1d8)...
    heals: Optional[str] = None
    # ...and moves that much damage onto the roller (Lay on Hands, 7-9).
    heal_transfers: bool = False
    # The spell named takes effect (Cast a Spell, on a hit).
    casts: bool = False


@dataclass(frozen=True)
class Move:
    key: str
    name: str
    trigger: str
    stat: str  # one of sheets.STATS, ANY, BOND, NOTHING or CASTER
    strong: Band
    weak: Band
    miss: Band
    # False for a move the rules trigger themselves -- Last Breath at 0 HP, Take Watch
    # when something approaches the camp -- so the model cannot call it by hand.
    callable: bool = True
    # The classes that have this move; empty means everyone (the basic moves).
    classes: Tuple[str, ...] = ()
    # An attack made at range: needs a near, far or thrown weapon, not a melee one.
    ranged: bool = False
    # Heals a character named as the target, rather than hurting a creature.
    heals_target: bool = False
    # Acts on another character named as the target (Aid or Interfere).
    helps_target: bool = False
    # The target names a form to take, not anyone in the scene (Shapeshifter).
    names_form: bool = False
    # The target names who or what is defended: a character, an item, a place (Defend).
    defends: bool = False
    # A move that names its own damage ("the target takes 1d10 damage") rolls that in place of
    # the class's die, and needs no weapon.
    damage_die: Optional[str] = None
    # An advanced move's own roll: only a character who has that advanced move may make it.
    requires: str = ""

    def band(self, name: str) -> Band:
        return {STRONG: self.strong, WEAK: self.weak, MISS: self.miss}[name]

    @property
    def attacks(self) -> bool:
        """Whether a hit can deal damage to someone -- so the move needs a target."""
        return any(
            b.deals_damage or any(o.deals_damage for o in (b.choice.options if b.choice else ()))
            for b in (self.strong, self.weak)
        )


#: Every basic move misses the same way: the GM makes a move.
_MISS = Band("The GM makes a move — as hard as they like.")

_DISCERN = (
    Option("What happened here recently?"),
    Option("What is about to happen?"),
    Option("What should I be on the lookout for?"),
    Option("What here is useful or valuable to me?"),
    Option("Who’s really in control here?"),
    Option("What here is not what it appears to be?"),
)

_AID = (Option("Help them: they take +1."), Option("Hinder them: they take -2."))

# Called Shot names its target before the roll in the book. Here it is picked after: the
# effects are the same at every result, so seeing the dice first buys nothing.
_CALLED = (
    Option("Head: they do nothing but stand and drool for a few moments."),
    Option("Arms: they drop anything they're holding."),
    Option("Legs: they're hobbled and slow moving."),
)

_BACKSTAB = (
    Option("You don't get into melee with them."),
    Option("You deal your damage+1d6.", deals_damage=True, damage_mod="+1d6"),
    Option("You create an advantage, +1 forward to you or an ally acting on it."),
    Option("Reduce their armor by 1 until they repair it."),
)

_BEND_BARS = (
    Option("It doesn't take a very long time."),
    Option("Nothing of value is damaged."),
    Option("It doesn't make an inordinate amount of noise."),
    Option("You can fix the thing again without a lot of effort."),
)

# Arcane Art chooses its effect before the roll in the book; picked after, for the same
# reason as Called Shot.
_ARCANE_ART = (
    Option("Heal 1d8 damage.", heals="1d8"),
    Option("+1d4 forward to damage."),
    Option("Their mind is shaken clear of one enchantment."),
    Option("Next time someone aids them successfully, it's +2 instead of +1."),
)

# The Cleric's and the Wizard's 7-9 differ only in their words ("your casting distances
# you from your deity", "the spell disturbs the fabric of reality"), so they share these.
_CAST = (
    Option("Draw unwelcome attention or put yourself in a spot. The GM will tell you how."),
    Option("Take -1 ongoing to cast a spell until you next commune or prepare spells.",
           casting_penalty=True),
    Option("The spell is lost: you cannot cast it again until you next commune or prepare.",
           loses_spell=True),
)

_CAROUSE = (
    Option("You befriend a useful NPC."),
    Option("You hear rumors of an opportunity."),
    Option("You gain useful information."),
    Option("You are not entangled, ensorcelled, or tricked."),
)

# Spending Defend's hold when an attack comes in: one point for one of these, or none.
# Held as a move of its own, so the choice is the defender's click like any other, and
# survives a restart with the attack's damage dice.
_SPEND = (
    Option("Halve its damage."),
    Option("Open the attacker up to an ally: +1 forward against it."),
    Option("Deal damage to the attacker equal to my level."),
    Option("Don't spend hold."),
)
_REDIRECT = Option("Redirect the attack to me.")


def _spend_band(options) -> Band:
    return Band("You stand in defense: spend 1 hold?", choice=Choice(1, options))


MOVES: Dict[str, Move] = {m.key: m for m in (
    Move(
        "hack_and_slash", "Hack and Slash", "attack an enemy in melee", "str",
        strong=Band(
            "You deal your damage to the enemy and avoid their attack. At your option, "
            "you may choose to do +1d6 damage but expose yourself to the enemy’s attack.",
            choice=Choice(1, (
                Option("Deal your damage and avoid their attack."),
                Option("Deal +1d6 damage, but expose yourself to their attack.",
                       enemy_attacks=True, damage_mod="+1d6"),
            )),
            deals_damage=True,
        ),
        weak=Band(
            "You deal your damage to the enemy and the enemy makes an attack against you.",
            enemy_attacks=True,
            deals_damage=True,
        ),
        miss=_MISS,
    ),
    Move(
        "volley", "Volley", "take aim and shoot at an enemy at range", "dex", ranged=True,
        strong=Band("You have a clear shot—deal your damage.", deals_damage=True),
        weak=Band(
            "Choose one (whichever you choose you deal your damage).",
            choice=Choice(1, (
                Option("Move to get the shot, placing yourself in danger of the GM’s choice."),
                Option("Take what you can get: -1d6 damage.", damage_mod="-1d6"),
                Option("Take several shots, reducing your ammo by one.", spends_ammo=True),
            )),
            deals_damage=True,
        ),
        miss=_MISS,
    ),
    Move(
        "defy_danger", "Defy Danger", "act despite an imminent threat or suffer a calamity",
        ANY,
        strong=Band("You do what you set out to, the threat doesn’t come to bear."),
        weak=Band(
            "You stumble, hesitate, or flinch: the GM will offer you a worse outcome, hard "
            "bargain, or ugly choice."
        ),
        miss=_MISS,
    ),
    Move(
        "defend", "Defend", "stand in defense of a person, item, or location under attack",
        "con", defends=True,
        strong=Band(
            "Hold 3. So long as you stand in defense, when you or the thing you defend is "
            "attacked you may spend hold, 1 for 1, to redirect the attack to yourself, halve "
            "its effect or damage, open the attacker up to an ally (+1 forward against it), "
            "or deal damage to the attacker equal to your level."
        ),
        weak=Band(
            "Hold 1. So long as you stand in defense, when you or the thing you defend is "
            "attacked you may spend it to redirect the attack to yourself, halve its effect "
            "or damage, open the attacker up to an ally (+1 forward against it), or deal "
            "damage to the attacker equal to your level."
        ),
        miss=_MISS,
    ),
    Move(
        "spout_lore", "Spout Lore", "consult your accumulated knowledge about something",
        "int",
        strong=Band(
            "The GM will tell you something interesting and useful about the subject "
            "relevant to your situation."
        ),
        weak=Band(
            "The GM will only tell you something interesting—it’s on you to make it useful."
        ),
        miss=_MISS,
    ),
    Move(
        "discern_realities", "Discern Realities", "closely study a situation or person",
        "wis",
        strong=Band(
            "Ask the GM 3 questions. Take +1 forward when acting on the answers.",
            choice=Choice(3, _DISCERN),
        ),
        weak=Band(
            "Ask the GM 1 question. Take +1 forward when acting on the answers.",
            choice=Choice(1, _DISCERN),
        ),
        miss=_MISS,
    ),
    Move(
        "parley", "Parley", "have leverage on a GM character and manipulate them", "cha",
        strong=Band("They ask you for something and do it if you make them a promise first."),
        weak=Band(
            "They ask you for something and do it if you make them a promise first — and "
            "they need some concrete assurance of your promise, right now."
        ),
        miss=_MISS,
    ),
    Move(
        "aid_or_interfere", "Aid or Interfere", "help or hinder someone", BOND, helps_target=True,
        strong=Band("They take +1 or -2, your choice.", choice=Choice(1, _AID)),
        weak=Band(
            "They take +1 or -2, your choice — and you expose yourself to danger, "
            "retribution, or cost.",
            choice=Choice(1, _AID),
        ),
        miss=_MISS,
    ),
    Move(
        "supply", "Supply",
        "go to buy something special, beyond what's usually available here, or non-mundane", "cha",
        strong=Band("You find what you're looking for at a fair price."),
        weak=Band("You'll have to pay more or settle for something similar."),
        miss=_MISS,
    ),
    Move(
        "carouse", "Carouse", "return triumphant and throw a big party", NOTHING,
        strong=Band("Choose 3.", choice=Choice(3, _CAROUSE)),
        weak=Band("Choose 1.", choice=Choice(1, _CAROUSE)),
        # Another miss that still chooses.
        miss=Band("You still choose one, but things get really out of hand.", choice=Choice(1, _CAROUSE)),
    ),
    Move(
        "recruit", "Recruit", "put out word that you're looking to hire help", OFFERS,
        strong=Band("You've got your pick of a number of skilled applicants, your choice who you "
                    "hire, no penalty for not taking them along."),
        weak=Band("You'll have to settle for someone close or turn them away."),
        miss=Band("Someone influential and ill-suited declares they'd like to come along (a "
                  "foolhardy youth, a loose-cannon, or a veiled enemy, for example): bring them and "
                  "take the consequences, or turn them away."),
    ),
    Move(
        "order_hirelings", "Order Hirelings",
        "order a hireling into something dangerous, degrading, or just flat-out crazy", LOYALTY,
        strong=Band("They stand firm and carry out the order."),
        weak=Band("They do it for now, but come back with serious demands later. Meet them or the "
                  "hireling quits on the worst terms."),
        miss=_MISS,
    ),
    Move(
        "outstanding_warrants", "Outstanding Warrants",
        "return to a civilized place in which you've caused trouble before", "cha",
        strong=Band("Word has spread of your deeds and everyone recognizes you."),
        # The complication is the GM's to choose, so it is the model's, not a button.
        weak=Band("Word has spread of your deeds and everyone recognizes you — and the GM chooses a "
                  "complication: the local constabulary has a warrant out for your arrest; someone "
                  "has put a price on your head; or someone important to you has been put in a bad "
                  "spot as a result of your actions."),
        miss=_MISS,
    ),
    Move(
        "last_breath", "Last Breath", "are dying", NOTHING,
        strong=Band("You've cheated death—you're in a bad spot but you're still alive."),
        weak=Band(
            "Death will offer you a bargain. Take it and stabilize or refuse and pass beyond "
            "the Black Gates into whatever fate awaits you.",
            choice=Choice(1, (
                Option("Take Death's bargain, and stabilize."),
                Option("Refuse, and pass beyond the Black Gates."),
            )),
        ),
        miss=Band(
            "Your fate is sealed. You're marked as Death's own and you'll cross the threshold "
            "soon. The GM will tell you when."
        ),
        callable=False,
    ),
    Move(
        "take_watch", "Take Watch", "are on watch and something approaches the camp", "wis",
        strong=Band(
            "You're able to wake the camp and prepare a response, the camp takes +1 forward."
        ),
        weak=Band(
            "You react just a moment too late; the camp is awake but hasn't had time to "
            "prepare. You have weapons and armor but little else."
        ),
        miss=Band("Whatever lurks outside the campfire's light has the drop on you."),
        callable=False,
    ),
    # -- class starting moves ------------------------------------------------------------
    Move(
        "turn_undead", "Turn Undead",
        "hold your holy symbol aloft and call on your deity for protection", "wis",
        strong=Band(
            "So long as you continue to pray and brandish your holy symbol, no undead may come "
            "within reach of you, and you momentarily daze intelligent undead and cause "
            "mindless undead to flee. Aggression breaks the effects and they are able to act "
            "as normal. Intelligent undead may still find ways to harry you from afar."
        ),
        weak=Band(
            "So long as you continue to pray and brandish your holy symbol, no undead may come "
            "within reach of you. Aggression breaks the effect."
        ),
        miss=_MISS, classes=("Cleric",),
    ),
    Move(
        "lay_on_hands", "Lay on Hands",
        "touch someone, skin to skin, and pray for their well-being", "cha",
        strong=Band("You heal 1d8 damage (or remove one disease).", heals="1d8"),
        weak=Band("They are healed, but the damage is transferred to you.",
                  heals="1d8", heal_transfers=True),
        miss=_MISS, classes=("Paladin",), heals_target=True,
    ),
    Move(
        "i_am_the_law", "I Am the Law",
        "give an NPC an order based on your divine authority", "cha",
        strong=Band(
            "They choose one: do what you say, back away cautiously then flee, or attack you. "
            "You also take +1 forward against them."
        ),
        weak=Band("They choose one: do what you say, back away cautiously then flee, or attack you."),
        miss=Band("They do as they please and you take -1 forward against them."),
        classes=("Paladin",),
    ),
    Move(
        "hunt_and_track", "Hunt and Track",
        "follow a trail of clues left behind by passing creatures", "wis",
        strong=Band(
            "You follow the creature's trail until there's a significant change in its "
            "direction or mode of travel, and you choose 1.",
            choice=Choice(1, (
                Option("Gain a useful bit of information about your quarry."),
                Option("Determine what caused the trail to end."),
            )),
        ),
        weak=Band(
            "You follow the creature's trail until there's a significant change in its "
            "direction or mode of travel."
        ),
        miss=_MISS, classes=("Ranger",),
    ),
    Move(
        "called_shot", "Called Shot",
        "attack a defenseless or surprised enemy at range, and name your target", "dex",
        strong=Band(
            "Your shot lands where you named, and you deal your damage.",
            choice=Choice(1, _CALLED), deals_damage=True,
        ),
        weak=Band("Your shot lands where you named.", choice=Choice(1, _CALLED)),
        miss=_MISS, classes=("Ranger",), ranged=True,
    ),
    Move(
        "trap_expert", "Trap Expert", "spend a moment to survey a dangerous area", "dex",
        strong=Band(
            "Hold 3. Spend your hold as you walk through the area to ask: Is there a trap here "
            "and if so, what activates it? What does the trap do when activated? What else is "
            "hidden here?"
        ),
        weak=Band(
            "Hold 1. Spend it as you walk through the area to ask: Is there a trap here and if "
            "so, what activates it? What does the trap do when activated? What else is hidden "
            "here?"
        ),
        miss=_MISS, classes=("Thief",),
    ),
    Move(
        "tricks_of_the_trade", "Tricks of the Trade", "pick locks or pockets or disable traps",
        "dex",
        strong=Band("You do it, no problem."),
        weak=Band("You still do it, but the GM will offer you two options between suspicion, "
                  "danger, or cost."),
        miss=_MISS, classes=("Thief",),
    ),
    Move(
        "backstab", "Backstab",
        "attack a surprised or defenseless enemy with a melee weapon", "dex",
        strong=Band("Choose two.", choice=Choice(2, _BACKSTAB)),
        weak=Band("Choose one.", choice=Choice(1, _BACKSTAB)),
        miss=_MISS, classes=("Thief",),
    ),
    Move(
        "bend_bars", "Bend Bars, Lift Gates",
        "use pure strength to destroy an inanimate obstacle", "str",
        strong=Band("Choose 3.", choice=Choice(3, _BEND_BARS)),
        weak=Band("Choose 2.", choice=Choice(2, _BEND_BARS)),
        miss=_MISS, classes=("Fighter",),
    ),
    Move(
        "arcane_art", "Arcane Art", "weave a performance into a basic spell for an ally", "cha",
        strong=Band("The ally gets the selected effect.", choice=Choice(1, _ARCANE_ART)),
        weak=Band(
            "Your spell still works, but you draw unwanted attention or your magic "
            "reverberates to other targets affecting them as well, GM's choice.",
            choice=Choice(1, _ARCANE_ART),
        ),
        miss=_MISS, classes=("Bard",), heals_target=True,
    ),
    # -- Undertake a Perilous Journey: one move per job, each opened by the Director for the
    # character who holds it, all rolled +Wis.
    Move(
        "journey_trailblazer", "Perilous Journey: Trailblazer", "blaze the trail through hostile territory", "wis",
        strong=Band("You reduce the time it takes to reach your destination (the GM will say by how much)."),
        weak=Band("The journey takes about as long as expected."),
        miss=_MISS, callable=False,
    ),
    Move(
        "journey_scout", "Perilous Journey: Scout", "scout ahead through hostile territory", "wis",
        strong=Band("You spot any trouble quick enough to let the party get the drop on it."),
        weak=Band("No one gets the drop on you, but you don't get the drop on them either."),
        miss=_MISS, callable=False,
    ),
    Move(
        "journey_quartermaster", "Perilous Journey: Quartermaster", "keep the party supplied through hostile territory", "wis",
        strong=Band("You reduce the number of rations required by one."),
        weak=Band("The normal number of rations are consumed."),
        miss=_MISS, callable=False,
    ),
    # The thing defended is attacked: redirect is one of the choices.
    Move(
        "defend_charge", "Defend", "hold Defend's hold and your charge is attacked", NOTHING,
        strong=_spend_band((_REDIRECT, *_SPEND)), weak=_spend_band((_REDIRECT, *_SPEND)),
        miss=_spend_band((_REDIRECT, *_SPEND)), callable=False,
    ),
    # The defender is attacked themselves: nothing to redirect.
    Move(
        "defend_self", "Defend", "hold Defend's hold and are attacked", NOTHING,
        strong=_spend_band(_SPEND), weak=_spend_band(_SPEND), miss=_spend_band(_SPEND),
        callable=False,
    ),
    Move(
        "shapeshifter", "Shapeshifter", "call upon the spirits to change your shape", "wis",
        strong=Band("Hold 3."),
        weak=Band("Hold 2."),
        # The one move whose miss still works: "hold 1 in addition to whatever the GM says".
        miss=Band("Hold 1, in addition to whatever the GM says."),
        classes=("Druid",), names_form=True,
    ),
    Move(
        "cast_a_spell", "Cast a Spell",
        "unleash a spell granted by your deity (Cleric, +Wis) or release a spell you've "
        "prepared (Wizard, +Int)", CASTER,
        strong=Band("The spell is successfully cast, and you keep it: you may cast it again.",
                    casts=True),
        weak=Band("The spell is cast, but choose one.", choice=Choice(1, _CAST), casts=True),
        miss=_MISS, classes=("Cleric", "Wizard"),
    ),
)}


# -- advanced moves with a roll of their own ------------------------------------------------
#
# Eleven advanced moves roll 2d6 themselves (Heirloom, Evil Eye, Counterspell...). They are
# read from the book's data rather than written out: the trigger, the stat, and each
# result's text, with Elemental Mastery's "choose two / choose one" as buttons. The few
# numbers they carry are wired by key in the Director (Metal Hurlant's 1d10 below,
# Communion of Whispers' -1 forward, Counterspell's staked spell).

_ROLL = re.compile(r"roll\s*\+\s*(str|dex|con|int|wis|cha)", re.IGNORECASE)
# The ✴ is optional: the compendium's Diplomat writes its results without one.
_RESULT = re.compile(r"(?:✴\s*|(?<=\. ))On an? (10\+|7\s*[–-]\s*9|miss|6-)[,:]?\s*", re.IGNORECASE)
_CHOOSE = re.compile(r"choose (one|two|three|[1-3])\b", re.IGNORECASE)
_COUNT = {"one": 1, "two": 2, "three": 3, "1": 1, "2": 2, "3": 3}

#: What the generated moves carry beyond their text.
_EXTRA = {
    "metal_hurlant": {"damage_die": "d10", "deals": True},
}


def move_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower().replace("\u2019", "").replace("'", "")).strip("_")


def _generated() -> Dict[str, Move]:
    path = Path(__file__).parent / "data" / "class_moves.json"
    book = json.loads(path.read_text(encoding="utf-8"))["classes"]
    extra = json.loads((Path(__file__).parent / "data" / "compendium.json").read_text(encoding="utf-8"))["classes"]
    out: Dict[str, Move] = {}
    lists = [moves["advanced"] for moves in book.values()] + [[c["starting"], *c["moves"]] for c in extra]
    for advanced_list in lists:
        for m in advanced_list:
            text = m["text"].replace("\n", " ")
            stat = _ROLL.search(text)
            if not stat or move_key(m["name"]) in out:
                continue
            key = move_key(m["name"])
            trigger = re.sub(r"^When\s+", "", text[:stat.start()].strip().rstrip(","))
            trigger = re.sub(r"\s+and$", "", re.sub(r"^you\s+", "", trigger))
            parts = _RESULT.split(text[stat.end():])
            bands: Dict[str, str] = {}
            for label, body in zip(parts[1::2], parts[2::2]):
                name = (STRONG if label.startswith("10") else MISS if label.lower() in ("miss", "6-")
                        else WEAK)
                bands[name] = body.strip()
            bullets = [b.strip() for b in text.split("•")[1:]]
            extra = _EXTRA.get(key, {})

            def band(name: str) -> Band:
                body = bands.get(name, "")
                if bullets:
                    body = body.split("•")[0].strip()
                choose = _CHOOSE.search(body) if name != MISS else None
                choice = Choice(_COUNT[choose[1].lower()], tuple(Option(b) for b in bullets)) \
                    if choose and bullets else None
                if name == MISS and not body:
                    return _MISS
                return Band(body or "The GM makes a move.", choice=choice,
                            deals_damage=bool(extra.get("deals")) and name != MISS)

            out[key] = Move(key, m["name"], trigger, stat[1].lower(), strong=band(STRONG), weak=band(WEAK),
                            miss=band(MISS), damage_die=extra.get("damage_die"), requires=m["name"])
    return out


ADVANCED_ROLLS: Dict[str, Move] = {k: m for k, m in _generated().items() if k not in MOVES}
MOVES.update(ADVANCED_ROLLS)
