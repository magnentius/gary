"""The Dungeon World basic moves (ARCHITECTURE.md §5).

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

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: Sentinel stats. ANY: the player's approach picks one of the six (Defy Danger). BOND:
#: rolled +Bond with the character helped, which nothing tracks yet, so it rolls +0.
ANY = "any"
BOND = "bond"

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


@dataclass(frozen=True)
class Move:
    key: str
    name: str
    trigger: str
    stat: str  # one of sheets.STATS, ANY, or BOND
    strong: Band
    weak: Band
    miss: Band

    def band(self, name: str) -> Band:
        return {STRONG: self.strong, WEAK: self.weak, MISS: self.miss}[name]

    @property
    def attacks(self) -> bool:
        """Whether a hit deals damage to someone -- so the move needs a target."""
        return self.strong.deals_damage or self.weak.deals_damage


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
        "volley", "Volley", "take aim and shoot at an enemy at range", "dex",
        strong=Band("You have a clear shot—deal your damage.", deals_damage=True),
        weak=Band(
            "Choose one (whichever you choose you deal your damage).",
            choice=Choice(1, (
                Option("Move to get the shot, placing yourself in danger of the GM’s choice."),
                Option("Take what you can get: -1d6 damage.", damage_mod="-1d6"),
                Option("Take several shots, reducing your ammo by one."),
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
        "con",
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
        "aid_or_interfere", "Aid or Interfere", "help or hinder someone", BOND,
        strong=Band("They take +1 or -2, your choice.", choice=Choice(1, _AID)),
        weak=Band(
            "They take +1 or -2, your choice — and you expose yourself to danger, "
            "retribution, or cost.",
            choice=Choice(1, _AID),
        ),
        miss=_MISS,
    ),
)}
