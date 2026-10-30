"""Core domain types.

Every type here is frozen. The system's central invariant (ARCHITECTURE.md §5) is that a
check's DC and consequences are fixed *before* the player rolls; immutability is how
that is enforced at the type level rather than by convention.

Nothing in this module imports Discord or Gemini.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

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
# Checks
# --------------------------------------------------------------------------------------


class CheckKind(str, Enum):
    CHECK = "check"
    SAVE = "save"


@dataclass(frozen=True)
class PendingCheck:
    """A check that has been declared and is awaiting the player's roll.

    ``dc``, ``on_success`` and ``on_failure`` are set by the model at declaration time
    and are never revisited. The model is not consulted again about whether the roll
    succeeded -- see resolver.resolve.
    """

    check_id: str
    channel_id: int
    user_id: int
    actor: str  # character name, as the model and the table know them
    kind: CheckKind
    key: str  # canonical snake_case skill/save key, or an attack name
    dc: int
    on_success: str
    on_failure: str
    stakes: str
    dice: str  # what Gary rolls when the player rolls, e.g. "1d20"
    created_at: float
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_dict(self) -> dict:
        """Serialised for durable storage.

        Pending checks must outlive the process: a DC committed before a roll is
        worthless if a restart between the command and the dice loses it.
        """
        return {
            "check_id": self.check_id,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "actor": self.actor,
            "kind": self.kind.value,
            "key": self.key,
            "dc": self.dc,
            "on_success": self.on_success,
            "on_failure": self.on_failure,
            "stakes": self.stakes,
            "dice": self.dice,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PendingCheck":
        return cls(
            check_id=d["check_id"],
            channel_id=int(d["channel_id"]),
            user_id=int(d["user_id"]),
            actor=d["actor"],
            kind=CheckKind(d["kind"]),
            key=d["key"],
            dc=int(d["dc"]),
            on_success=d["on_success"],
            on_failure=d["on_failure"],
            stakes=d.get("stakes", ""),
            # Checks persisted before Avrae was removed carry "command" instead. Any
            # still pending after the upgrade were d20 checks, so they roll as one.
            dice=d.get("dice", "1d20"),
            created_at=float(d["created_at"]),
            expires_at=float(d["expires_at"]),
        )


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
class CheckOutcome:
    """The result of comparing a roll against a persisted DC.

    Handed to the model as a structured tool result, never as prose. The model narrates
    this; it does not evaluate it.
    """

    check_id: str
    actor: str
    kind: CheckKind
    key: str
    total: int
    dc: int
    margin: int
    succeeded: bool
    consequence: str  # the pre-written on_success/on_failure line, chosen by arithmetic
    natural: Optional[int] = None
    faces: tuple = ()

    @property
    def critical_success(self) -> bool:
        return self.natural == 20

    @property
    def critical_failure(self) -> bool:
        return self.natural == 1


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
    SETTING = "setting"
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
