"""Campaign state shape (ARCHITECTURE.md §7).

Plain dicts behind typed accessors rather than a rigid object graph: campaign state
accretes odd fields over a long game, and a schema that fights that costs more than it
saves. What *is* pinned down is the version number and the authority boundary -- the
fields Avrae owns during an encounter are marked and are not written here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

# In-game minutes per real combat round (ARCHITECTURE.md §3.1).
SECONDS_PER_ROUND = 6


def empty_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        # Absolute in-world clock, in minutes since campaign epoch. Minute granularity
        # is deliberate: finer resolution invites conflict with Avrae's round timing.
        "world_minutes": 0,
        "calendar": {"epoch_label": "1st of Hammer, 1492 DR", "minutes_per_day": 1440},
        "scene": {"location": "", "summary": ""},
        "party": {},  # actor -> {"gold": int, "inventory": [str], "notes": [str]}
        "effects": [],  # out-of-combat only; see EffectRecord
        "quests": {},  # quest_id -> {"title": str, "status": str, "notes": [str]}
        "npcs": {},  # npc_id -> {"name": str, "disposition": int, "notes": [str]}
        # discord user id (as str -- JSON object keys are strings) -> Avrae character
        # name. Correlation is name-based in practice (§14 (7)), so this is what lets
        # open_check name an actor Avrae will echo back.
        "players": {},
        "notes": [],  # narrative memory, feeds context retrieval
        "encounter": None,  # non-None while Avrae owns combat state
    }


@dataclass(frozen=True)
class EffectRecord:
    """An out-of-combat effect with a wall-clock expiry.

    Effects inside initiative are Avrae's, tracked in rounds, and are never represented
    here -- that dual ownership is what made v1 of the design incoherent.
    """

    effect_id: str
    actor: str
    name: str
    expires_at_minute: int
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "actor": self.actor,
            "name": self.name,
            "expires_at_minute": self.expires_at_minute,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EffectRecord":
        return cls(
            effect_id=d["effect_id"],
            actor=d["actor"],
            name=d["name"],
            expires_at_minute=int(d["expires_at_minute"]),
            source=d.get("source", ""),
        )


@dataclass(frozen=True)
class Encounter:
    """Marks the window in which Avrae, not the state server, owns effects and time."""

    encounter_id: str
    started_at_minute: int
    frozen_effects: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "encounter_id": self.encounter_id,
            "started_at_minute": self.started_at_minute,
            "frozen_effects": self.frozen_effects,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Encounter":
        return cls(
            encounter_id=d["encounter_id"],
            started_at_minute=int(d["started_at_minute"]),
            frozen_effects=list(d.get("frozen_effects", [])),
        )


def format_world_time(state: Dict[str, Any]) -> str:
    minutes = int(state.get("world_minutes", 0))
    per_day = int(state.get("calendar", {}).get("minutes_per_day", 1440))
    day, rem = divmod(minutes, per_day)
    hour, minute = divmod(rem, 60)
    return f"Day {day + 1}, {hour:02d}:{minute:02d}"


def parse_world_time(text: str, state: Dict[str, Any]) -> int:
    """Absolute world-minute for "HH:MM" or "day N HH:MM". Inverse of format_world_time.

    The clock can only ever be *advanced* (``_h_time`` adds a delta), so setting a time
    means computing the gap to it. A fresh campaign therefore opens at Day 1, 00:00 —
    midnight — which is almost never the intended hour for a first scene.
    """
    per_day = int(state.get("calendar", {}).get("minutes_per_day", 1440))
    raw = text.strip().lower()
    day = int(state.get("world_minutes", 0)) // per_day + 1

    match = re.match(r"^(?:day\s+(\d{1,4})\s+)?(\d{1,2}):(\d{2})$", raw)
    if not match:
        raise ValueError("expected 'HH:MM' or 'day N HH:MM', e.g. '18:30' or 'day 2 06:00'")
    if match.group(1):
        day = int(match.group(1))
    hour, minute = int(match.group(2)), int(match.group(3))
    if hour > 23 or minute > 59 or day < 1:
        raise ValueError(f"{text!r} is not a valid time")
    return (day - 1) * per_day + hour * 60 + minute


def party_member(state: Dict[str, Any], actor: str) -> Optional[Dict[str, Any]]:
    return state.get("party", {}).get(actor)
