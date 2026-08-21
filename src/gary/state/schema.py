"""Campaign state shape (ARCHITECTURE.md §7).

Plain dicts behind typed accessors rather than a rigid object graph: campaign state
accretes odd fields over a long game, and a schema that fights that costs more than it
saves. What *is* pinned down is the version number and the authority boundary -- the
fields Avrae owns during an encounter are marked and are not written here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

#: How far back the campaign calendar sits from the real one. The Gregorian calendar with
#: a millennium taken off it beats an invented one: everybody already knows that August is
#: harvest and January is dark, so the model gets season, day length and weather for free
#: and the table needs no lore to read a date.
YEARS_BEHIND = 1000

#: Used when no epoch is supplied. `empty_state()` has to stay a pure function -- it is
#: what `_replay` rebuilds a reset campaign with, and an epoch that came from
#: `date.today()` there would make replay depend on the day it ran. The live epoch is set
#: once, when the campaign is created, and then lives in the snapshot.
DEFAULT_EPOCH_DATE = "1000-01-01"

#: The full-moon names, in Gregorian month order. The calendar underneath is still the
#: real one -- these only change what a month is called, so month lengths, leap years and
#: seasons stay correct and free. They also carry the season in the name itself, which the
#: bare Gregorian months do not: Snow Moon says February without anyone knowing it is.
MONTH_NAMES = (
    "Wolf Moon", "Snow Moon", "Worm Moon", "Pink Moon",
    "Flower Moon", "Strawberry Moon", "Buck Moon", "Sturgeon Moon",
    "Harvest Moon", "Hunter's Moon", "Beaver Moon", "Cold Moon",
)

#: Accepted on input as well as the full name, because "4 Harvest 1026" is what people
#: type when the screen says "4 Harvest Moon 1026".
_MONTH_LOOKUP = {name.lower(): i + 1 for i, name in enumerate(MONTH_NAMES)}
_MONTH_LOOKUP.update({name.lower().replace(" moon", ""): i + 1 for i, name in enumerate(MONTH_NAMES)})
#: September is the one with two names in common use.
_MONTH_LOOKUP.update({"corn moon": 9, "corn": 9})


def default_epoch_date(today: Optional[date] = None) -> str:
    """Today, a millennium ago, as ISO. Frozen into the campaign at creation.

    Anchored once rather than tracked: a clock that followed the real date would jump
    weeks between sessions, and §3.1 hands the world clock to Avrae mid-encounter on the
    assumption that nothing else moves it.
    """
    today = today or date.today()
    try:
        return today.replace(year=today.year - YEARS_BEHIND).isoformat()
    except ValueError:
        # 29 February, and the target year is not a leap year.
        return today.replace(year=today.year - YEARS_BEHIND, day=28).isoformat()

# In-game minutes per real combat round (ARCHITECTURE.md §3.1).
SECONDS_PER_ROUND = 6


def empty_state(epoch_date: Optional[str] = None) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        # Absolute in-world clock, in minutes since campaign epoch. Minute granularity
        # is deliberate: finer resolution invites conflict with Avrae's round timing.
        "world_minutes": 0,
        # `epoch_date` is day 1. Time is stored as minutes since it, never as a date, so
        # the clock stays a single integer that can only move forward.
        "calendar": {
            "epoch_date": epoch_date or DEFAULT_EPOCH_DATE,
            "minutes_per_day": 1440,
        },
        # The standing frame: premise, tone, region. Rendered every turn, unlike a
        # note, which only surfaces when its subject is mentioned — and the premise is
        # needed most at the start of a scene, before anyone has said the magic word.
        "setting": "",
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


def _epoch(state: Dict[str, Any]) -> Optional[date]:
    raw = str(state.get("calendar", {}).get("epoch_date", "") or "")
    try:
        year, month, day = (int(part) for part in raw.split("-"))
        return date(year, month, day)
    except (ValueError, TypeError):
        # A campaign written before the calendar existed, or a hand-edited snapshot.
        # Falling back to "Day N" is better than refusing to render the time at all.
        return None


def format_world_date(day: date) -> str:
    """`21 Sturgeon Moon 1026`. Built by hand, not strftime: the month names are not the
    locale's, and %Y is not portable below year 1000 -- zero-padding a year is exactly the
    kind of thing that renders as `0026` on one platform and `26` on another."""
    return f"{day.day} {MONTH_NAMES[day.month - 1]} {day.year}"


def format_world_time(state: Dict[str, Any]) -> str:
    minutes = int(state.get("world_minutes", 0))
    per_day = int(state.get("calendar", {}).get("minutes_per_day", 1440))
    day, rem = divmod(minutes, per_day)
    hour, minute = divmod(rem, 60)

    epoch = _epoch(state)
    if epoch is None:
        return f"Day {day + 1}, {hour:02d}:{minute:02d}"
    return f"{format_world_date(epoch + timedelta(days=day))}, {hour:02d}:{minute:02d}"


def parse_world_time(text: str, state: Dict[str, Any]) -> int:
    """Absolute world-minute for a time, a date, or a day number. Inverse of
    format_world_time.

    The clock can only ever be *advanced* (``_h_time`` adds a delta), so setting a time
    means computing the gap to it. A fresh campaign opens at midnight on its epoch date,
    which is almost never the intended hour for a first scene.

    Four forms, because all four get typed: ``18:30`` (today, in world terms),
    ``4 Harvest Moon 1026 06:00`` (what the screen shows, typed back), ``1026-09-04
    06:00`` (the same date, unambiguously), and ``day 2 06:00`` (the original form, kept
    working -- it is what every campaign written before the calendar used).
    """
    per_day = int(state.get("calendar", {}).get("minutes_per_day", 1440))
    raw = text.strip().lower()
    day = int(state.get("world_minutes", 0)) // per_day + 1

    # "4 Harvest Moon 1026 06:00" -- the form the screen invites you to type back.
    named = re.match(r"^(\d{1,2})\s+([a-z'\s]+?)\s+(\d{1,4})[\s,]+(\d{1,2}):(\d{2})$", raw)
    if named and named.group(2).strip() in _MONTH_LOOKUP:
        raw = "{}-{:02d}-{:02d} {}:{}".format(
            named.group(3),
            _MONTH_LOOKUP[named.group(2).strip()],
            int(named.group(1)),
            named.group(4),
            named.group(5),
        )

    dated = re.match(r"^(\d{1,4})-(\d{1,2})-(\d{1,2})[\s,]+(\d{1,2}):(\d{2})$", raw)
    if dated:
        epoch = _epoch(state)
        if epoch is None:
            raise ValueError("this campaign has no calendar; use 'HH:MM' or 'day N HH:MM'")
        try:
            target = date(int(dated.group(1)), int(dated.group(2)), int(dated.group(3)))
        except ValueError as exc:
            raise ValueError(f"{text!r} is not a real date ({exc})")
        hour, minute = int(dated.group(4)), int(dated.group(5))
        if hour > 23 or minute > 59:
            raise ValueError(f"{text!r} is not a valid time")
        if target < epoch:
            raise ValueError(
                f"the campaign begins on {format_world_date(epoch)}; "
                f"{format_world_date(target)} is before it"
            )
        return (target - epoch).days * per_day + hour * 60 + minute

    match = re.match(r"^(?:day\s+(\d{1,4})\s+)?(\d{1,2}):(\d{2})$", raw)
    if not match:
        raise ValueError(
            "expected a time, a date or a day number — e.g. '18:30', "
            "'4 Harvest Moon 1026 06:00', '1026-09-04 06:00' or 'day 2 06:00'"
        )
    if match.group(1):
        day = int(match.group(1))
    hour, minute = int(match.group(2)), int(match.group(3))
    if hour > 23 or minute > 59 or day < 1:
        raise ValueError(f"{text!r} is not a valid time")
    return (day - 1) * per_day + hour * 60 + minute


def party_member(state: Dict[str, Any], actor: str) -> Optional[Dict[str, Any]]:
    return state.get("party", {}).get(actor)
