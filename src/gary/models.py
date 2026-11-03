"""Core domain types.

Every type here is frozen. The system's central invariant (ARCHITECTURE.md §5) is that a
move and its modifier are fixed *before* the player rolls; immutability is how that is
enforced at the type level rather than by convention.

Nothing in this module imports Discord or Gemini.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Tuple

_log = logging.getLogger("gary.models")

# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford, no I/L/O/U


def new_id(now: Optional[float] = None) -> str:
    """A short, time-sortable, dependency-free identifier.

    Lexicographic order matches creation order, which makes the journal (state/store.py)
    readable and lets "the most recent check" be answered without a timestamp field.
    """
    ms = int((time.time() if now is None else now) * 1000)
    out = []
    for _ in range(10):
        ms, rem = divmod(ms, 32)
        out.append(_B32[rem])
    stamp = "".join(reversed(out))
    rand = "".join(_B32[b % 32] for b in os.urandom(8))
    return stamp + rand


# --------------------------------------------------------------------------------------
# Moves
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingCheck:
    """A move that has been triggered and is awaiting the player's roll.

    The move, the stat and its modifier are fixed when the model calls the move, before
    anyone rolls, and are never revisited. What each result means is fixed by the move
    itself (moves.py), so nothing the model writes afterwards can change the outcome.

    ``faces`` is empty until the roll. A move whose result still needs the player -- a
    choice to make, damage to roll -- is held here after the dice, with ``picks`` filled
    in once they choose. A restart at any point between loses none of it.

    ``target`` is the creature an attack is aimed at, fixed before the dice like the rest.
    ``spell`` is the spell being cast, for Cast a Spell -- fixed as early, so the model
    cannot pick a bigger one once the dice are good.
    """

    check_id: str
    channel_id: int
    user_id: int
    actor: str  # character name, as the model and the table know them
    move: str  # key into moves.MOVES
    stat: str  # the stat rolled, or moves.BOND
    modifier: int  # the stat's modifier from the sheet, fixed at declaration
    stakes: str
    dice: str  # what Gary rolls when the player rolls, e.g. "2d6"
    created_at: float
    expires_at: float
    faces: tuple = ()
    picks: tuple = ()
    target: str = ""
    spell: str = ""
    companion: str = ""  # the animal companion working with them (Command), by name
    aide: str = ""  # a hireling helping with this move (Man-at-arms, Intervene), by name
    ally: str = ""  # Setup Strike, Tandem Strike: the ally whose next attack gets the bonus
    # What the situation adds, for moves that depend on it: "weak_point" (Smaug's Belly),
    # "two_weapons" (Viper's Strike), "spread" (Blot Out the Sun), "boon:armor",
    # "plus:dex", "minus:int" (Formshaper, Formcrafter).
    flags: Tuple[str, ...] = ()

    @property
    def rolled(self) -> bool:
        return bool(self.faces)

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_dict(self) -> dict:
        """Serialised for durable storage.

        Pending moves must outlive the process: a modifier committed before a roll is
        worthless if a restart between the request and the dice loses it.
        """
        return {
            "check_id": self.check_id,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "actor": self.actor,
            "move": self.move,
            "stat": self.stat,
            "modifier": self.modifier,
            "stakes": self.stakes,
            "dice": self.dice,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "faces": list(self.faces),
            "picks": list(self.picks),
            "target": self.target,
            "spell": self.spell,
            "companion": self.companion,
            "aide": self.aide,
            "ally": self.ally,
            "flags": list(self.flags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PendingCheck":
        """Raises ValueError for a check saved before moves replaced d20 checks.

        Those carry a DC and no move, so there is nothing to resolve them as. They lapse
        after minutes anyway; the loaders skip them (``restorable``) rather than refuse
        to boot.
        """
        if "move" not in d:
            raise ValueError("a d20 check from before moves; it cannot be resolved as a move")
        return cls(
            check_id=d["check_id"],
            channel_id=int(d["channel_id"]),
            user_id=int(d["user_id"]),
            actor=d["actor"],
            move=d["move"],
            stat=d["stat"],
            modifier=int(d["modifier"]),
            stakes=d.get("stakes", ""),
            dice=d.get("dice", "2d6"),
            created_at=float(d["created_at"]),
            expires_at=float(d["expires_at"]),
            faces=tuple(int(f) for f in d.get("faces", ())),
            picks=tuple(int(p) for p in d.get("picks", ())),
            target=d.get("target", ""),
            spell=d.get("spell", ""),
            companion=d.get("companion", ""),
            aide=d.get("aide", ""),
            ally=d.get("ally", ""),
            flags=tuple(d.get("flags") or ()),
        )


def restorable(dicts) -> list:
    """Every saved check that can still be resolved, skipping any that cannot.

    One unreadable entry must not cost every other player their pending roll, or stop a
    restart outright.
    """
    out = []
    for d in dicts:
        try:
            out.append(PendingCheck.from_dict(d))
        except (KeyError, TypeError, ValueError) as exc:
            _log.warning("skipping saved check %s: %s", d.get("check_id", "?"), exc)
    return out


class CancelReason(str, Enum):
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    MANUAL = "manual"


@dataclass(frozen=True)
class Roll:
    """Dice Gary rolled, kept face by face.

    The faces are kept rather than only the total because the table is shown them: Gary
    is both referee and dice, and a roll nobody can inspect is one nobody has reason to
    trust.
    """

    spec: str  # what was asked for, e.g. "1d20" or "2d6"
    faces: tuple  # each die as it landed, in order
    modifier: int = 0

    @property
    def total(self) -> int:
        return sum(self.faces) + self.modifier

    @property
    def natural(self) -> Optional[int]:
        """The bare face of a single die, or None when there is more than one."""
        return self.faces[0] if len(self.faces) == 1 else None

    def render(self) -> str:
        """`4 + 5 +1 = 10`, or `17` for a single unmodified die."""
        dice = " + ".join(str(f) for f in self.faces)
        if self.modifier:
            dice += f" {self.modifier:+d}"
        if len(self.faces) == 1 and not self.modifier:
            return dice
        return f"{dice} = {self.total}"


@dataclass(frozen=True)
class MoveOutcome:
    """What a move came to: the band the dice landed in, and what it means.

    Handed to the model as a structured tool result, never as prose. The model narrates
    this; it does not evaluate it.
    """

    check_id: str
    actor: str
    move: str  # key into moves.MOVES
    stat: str
    modifier: int
    faces: tuple
    total: int
    band: str  # moves.STRONG | WEAK | MISS
    text: str  # the band's rules text, verbatim
    chosen: tuple = ()  # the options the player picked, as text
    enemy_attacks: bool = False  # the rules say the enemy attacks: not the model's call
    auto_chosen: bool = False  # the player never chose, so Gary took the first options
    # What the roller's damage did, and what the enemy's attack back did: see
    # fsm.Director for the fields. None when there was none.
    damage: Optional[dict] = None
    enemy_attack: Optional[dict] = None
    healing: Optional[dict] = None  # a healing move's roll and what it healed
    spell: Optional[dict] = None  # Cast a Spell: the spell, and what became of it
    shape: Optional[dict] = None  # Shapeshifter: the form taken and the hold
    notes: tuple = ()  # what an advanced move adds that Gary does not track (Cautious's hold)
    dice: str = "2d6"  # what was rolled: 1d6+1d8 for a Barbarian chasing an appetite


# --------------------------------------------------------------------------------------
# Campaign state mutations
# --------------------------------------------------------------------------------------


class MutationKind(str, Enum):
    GOLD = "gold"
    ITEM_ADD = "item_add"
    ITEM_REMOVE = "item_remove"
    QUEST = "quest"
    NPC_RELATION = "npc_relation"
    EFFECT_ADD = "effect_add"
    EFFECT_REMOVE = "effect_remove"
    NOTE = "note"
    SCENE = "scene"
    TIME = "time"
    PLAYER_CHARACTER = "player_character"
    SHEET = "sheet"
    MONSTERS_ADD = "monsters_add"
    MONSTERS_REMOVE = "monsters_remove"
    MONSTER_HP = "monster_hp"
    SETTING = "setting"
    FRONT = "front"
    MONSTER_LOOTED = "monster_looted"
    MONSTER_AFFLICT = "monster_afflict"
    STEADING = "steading"
    SESSION_QUESTIONS = "session_questions"
    OPENING = "opening"  # /begin: where the table is in starting the adventure
    DUNGEON = "dungeon"  # a dungeon's map: its areas, how they connect, the blanks
    CALENDAR = "calendar"


@dataclass(frozen=True)
class Mutation:
    """A single reversible change to campaign state.

    ``inverse`` is computed by the store at apply time, not supplied by the caller --
    the model must not be able to describe its own undo.
    """

    kind: MutationKind
    target: str  # character name, quest id, npc id, or "" for world-level
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AppliedMutation:
    mutation: Mutation
    inverse: Optional[Mutation]
    before: Any = None
    after: Any = None


@dataclass(frozen=True)
class StateDiff:
    """Returned to the model so it narrates what actually happened, not what it asked for."""

    entries: tuple = ()
    rejected: tuple = ()

    @property
    def applied_count(self) -> int:
        return len(self.entries)

    @property
    def ok(self) -> bool:
        return not self.rejected
