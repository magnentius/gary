"""Monster morale (ARCHITECTURE.md §5.4).

Avrae has no morale rule, so every creature it tracks fights to the death. That is not
how anything with a survival instinct behaves, and it is not how the fiction reads: a
bandit who took the job for coin does not die for it, and a wolf that has watched the
rest of its pack drop does not close for one more bite.

**Morale is computed, not chosen**, for the reason `resolver.py` is arithmetic (§5.2). A
model asked "would the goblin run now?" answers plausibly and inconsistently, and the
answer would drift with whatever it just narrated. Here it is a table and a comparison,
so the same fight always breaks at the same place and the journal explains why.

**Nothing here decides anything at the table.** A broken creature produces a suggestion —
narration plus the command the DM may run. Gary cannot run Avrae commands and does
not pretend to; a DM who ignores the suggestion has a creature that fights on, and that is
a legitimate outcome rather than a desync.

Two inputs, as asked for:

* **Creature type**, from the SRD data (§3). Mindless things never break: an undead has
  no fear, a construct no instinct, an ooze nothing to change its mind with. Beasts break
  soonest, because an animal has no cause. This is why `extract_srd_monsters.py` had to be
  fixed first -- 36 creatures were typed "or Small Humanoid", and humanoids are the whole
  point of a morale rule.
The tracker words and Avrae's combatant naming are both read from Avrae's source at
``avrae@12b146fb`` rather than guessed -- see `STATUS_PRESSURE` and
`parser.avrae.combatant_prefix`.

* **Disposition**, the -5..+5 already carried on named NPCs. What matters is *conviction*
  rather than friendliness: |disposition|. A devoted ally holds the line for the party and
  a creature that hates them fights out of hatred; it is the mercenary in the middle, who
  feels nothing either way, that leaves first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

#: Base steadiness by SRD creature type. ``None`` never breaks at all.
#:
#: Keyed on the bare type; a parenthesised sub-type ("Fiend (Devil)") is looked up whole
#: first, so a devil can be steadier than a demon without listing every demon.
NERVE_BY_TYPE: Mapping[str, Optional[int]] = {
    # Nothing to frighten. These fight until destroyed, which is what Avrae already does.
    "Construct": None,
    "Ooze": None,
    "Plant": None,
    "Undead": None,
    "Swarm of Tiny Beasts": None,
    "Swarm of Tiny Undead": None,
    # Bound, proud, or fanatical: they break only when nearly destroyed.
    "Celestial": 5,
    "Fiend (Devil)": 5,
    "Dragon": 5,
    "Dragon (Chromatic)": 5,
    "Dragon (Metallic)": 5,
    "Monstrosity (Titan)": 5,
    # Dangerous and willful, but self-interested.
    "Aberration": 4,
    "Elemental": 4,
    "Elemental (Genie)": 4,
    "Fiend": 4,
    "Fiend (Demon)": 4,
    "Giant": 4,
    # Cunning enough to know when a fight is lost.
    "Fey": 3,
    "Fey (Goblinoid)": 2,
    "Monstrosity": 3,
    "Monstrosity (Lycanthrope)": 3,
    "Humanoid": 3,
    # An animal has no cause worth dying for.
    "Beast": 1,
    "Beast (Dinosaur)": 2,
}

#: Fallback for a creature whose type is known but unlisted -- homebrew, or an SRD type
#: added later. Mid-scale, so an unlisted creature behaves like a person rather than
#: like a wolf or a devil.
DEFAULT_NERVE = 3

#: Avrae's word for a monster's health when it hides the numbers, mapped to how hard that
#: is pressing on the creature.
#:
#: Read from Avrae's source at ``avrae@12b146fb``, ``cogs5e/initiative/combatant.py``,
#: rather than inferred from the two words live capture happened to show. That mattered:
#: ``Critical`` exists and had been left out, so a creature one hit from death was read as
#: merely bloodied and held one band longer than it should have.
STATUS_PRESSURE: Mapping[str, int] = {
    "healthy": 0,    # ratio >= 1
    "injured": 1,    # 0.5 < ratio < 1
    "bloodied": 3,   # 0.15 < ratio <= 0.5
    "critical": 4,   # 0 < ratio <= 0.15
}

#: Avrae's words for a combatant that is already gone. Not pressure -- a corpse has no
#: morale -- and listed so they are refused deliberately rather than by falling off the
#: end of the table. ``Very Dead`` is what Avrae writes when max HP is somehow <= 0.
DEAD_WORDS = frozenset({"dead", "very dead"})

#: Pressure from watching its own side go down, per fallen ally, and in total.
PRESSURE_PER_FALLEN_ALLY = 1
MAX_ALLY_PRESSURE = 3

#: The last one standing has nobody left to hold the line with.
PRESSURE_ALONE = 2

#: Facing more of them than there are of you. Counted separately from watching allies
#: fall, because a creature that was outnumbered from the first round never had allies to
#: lose and would otherwise feel no pressure at all until it was nearly dead.
PRESSURE_OUTNUMBERED = 1


@dataclass(frozen=True)
class MoraleCall:
    """Why a creature did or did not break. Journalled, so it has to explain itself."""

    combatant: str
    broke: bool
    nerve: int
    pressure: int
    creature_type: str
    reasons: tuple = ()

    def explain(self) -> str:
        why = ", ".join(self.reasons) if self.reasons else "no pressure"
        return (
            f"{self.combatant} ({self.creature_type}): "
            f"pressure {self.pressure} vs nerve {self.nerve} — {why}"
        )


def base_type(creature_type: str) -> str:
    """"Fiend (Devil)" -> "Fiend". The whole string is tried first by `nerve`."""
    return str(creature_type or "").split("(")[0].strip()


def nerve(creature_type: str, disposition: int = 0) -> Optional[int]:
    """How much pressure this creature absorbs before it runs. ``None`` never runs.

    Conviction is ``abs(disposition)``: a devoted ally and a creature that loathes the
    party both have a reason to stay, and the indifferent mercenary between them does
    not. It is worth at most +1, and only for a strong feeling: the type spread is 1..5,
    so a larger bonus let a devoted wolf outlast an indifferent soldier, which is the
    tail wagging the dog. A devoted wolf is still a wolf.
    """
    if creature_type is None or not str(creature_type).strip():
        return None  # Unknown creature: never guess a rout. See `assess`.

    key = str(creature_type).strip()
    if key in NERVE_BY_TYPE:
        base = NERVE_BY_TYPE[key]
    else:
        stripped = base_type(key)
        base = NERVE_BY_TYPE.get(stripped, DEFAULT_NERVE) if stripped else DEFAULT_NERVE

    if base is None:
        return None
    return base + min(abs(int(disposition)) // 3, 1)


def pressure_of_status(status: str = "", hp_current: Optional[int] = None,
                       hp_max: Optional[int] = None) -> Optional[int]:
    """How hard its own wounds are pressing.

    Avrae shows numbers for PCs and a word for monsters (§6.4), so both are read. A word
    that is not one of the confirmed three returns ``None``, which suppresses morale
    entirely rather than treating an unrecognised state as unhurt -- an unknown word is
    most likely a format change, and a rout suggested off a misread tracker is worse than
    no rout at all.
    """
    if hp_current is not None and hp_max is not None and hp_max > 0:
        # The same bands Avrae uses for the word, so a creature under morale reads the
        # same whether the DM is showing numbers or not.
        ratio = hp_current / hp_max
        if ratio >= 1:
            return 0
        if ratio > 0.5:
            return 1
        if ratio > 0.15:
            return 3
        if ratio > 0:
            return 4
        return None  # dead

    word = str(status or "").strip().lower()
    if not word or word in DEAD_WORDS:
        return None
    return STATUS_PRESSURE.get(word)


def assess(
    combatant: str,
    creature_type: str,
    *,
    disposition: int = 0,
    status: str = "",
    hp_current: Optional[int] = None,
    hp_max: Optional[int] = None,
    allies_down: int = 0,
    allies_standing: int = 0,
    foes_standing: int = 0,
) -> Optional[MoraleCall]:
    """Does this creature break? ``None`` means "not enough is known to say".

    ``None`` and a ``MoraleCall`` with ``broke=False`` are different answers and the
    caller must not conflate them: the first is Gary declining to guess, the second is
    Gary saying the creature is holding. Only the second is worth journalling.
    """
    steadiness = nerve(creature_type, disposition)
    if steadiness is None:
        return None  # Mindless, or a creature we cannot identify.

    own = pressure_of_status(status, hp_current, hp_max)
    if own is None:
        return None  # Unreadable health; say nothing.

    reasons = []
    total = own
    if own:
        reasons.append(f"{'bloodied' if own >= 3 else 'injured'}")

    fallen = min(int(allies_down) * PRESSURE_PER_FALLEN_ALLY, MAX_ALLY_PRESSURE)
    if fallen:
        total += fallen
        reasons.append(f"{int(allies_down)} of its side down")

    if allies_standing <= 0 and int(allies_down) > 0:
        total += PRESSURE_ALONE
        reasons.append("last one standing")

    own_side = int(allies_standing) + 1  # itself
    if int(foes_standing) > own_side:
        total += PRESSURE_OUTNUMBERED
        reasons.append(f"outnumbered {int(foes_standing)} to {own_side}")

    return MoraleCall(
        combatant=combatant,
        broke=total > steadiness,
        nerve=steadiness,
        pressure=total,
        creature_type=str(creature_type),
        reasons=tuple(reasons),
    )


def routed(calls: Sequence[MoraleCall]) -> bool:
    """Whether the whole side is done, which is what turns removals into `!init end`.

    Requires at least one call and every one of them broken. A side holding one unbreakable
    construct is not routed, which falls out of `assess` returning ``None`` for it: the
    caller passes only the creatures it could assess, so an unassessable one leaves the
    list short of the roster and the caller checks that itself.
    """
    calls = list(calls)
    return bool(calls) and all(c.broke for c in calls)
