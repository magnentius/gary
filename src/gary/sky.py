"""Daylight and the day's weather.

Dungeon World has no rules for either: they are fiction. But the model has to get them right
and keep them -- not narrate sunlight at nine on a midwinter night, and not have it pouring at
noon and dry an hour later. So Gary works out the light from the hour and the season and shows
it beside the clock, and keeps the day's weather once it is set (``set_weather``), on the scene,
until the next day. The Druid's Weather Weaver -- "When you are under open skies when the sun
rises the GM will ask you what the weather will be that day" -- is asked for at sunrise.

Sunrise and sunset are a temperate northern year's, by month: rough, as the light is.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

#: (sunrise, sunset) in hours, January to December.
_SUN = [(7.75, 16.5), (7.25, 17.25), (6.5, 18.0), (5.75, 18.75), (5.0, 19.5), (4.75, 20.0),
        (5.0, 19.75), (5.5, 19.0), (6.25, 18.0), (7.0, 17.0), (7.5, 16.25), (8.0, 16.0)]
TWILIGHT = 0.75  # hours either side of sunrise and sunset


def _now(state: Dict[str, Any]) -> Tuple[int, float, Optional[int]]:
    """(day index, hour of day, month 1-12 or None without a calendar)."""
    from .state.schema import _epoch

    minutes = int(state.get("world_minutes", 0))
    per_day = int((state.get("calendar") or {}).get("minutes_per_day", 1440))
    day, rem = divmod(minutes, per_day)
    epoch = _epoch(state)
    month = (epoch + timedelta(days=day)).month if epoch else None
    return day, rem / 60, month


def sun(state: Dict[str, Any]) -> Tuple[float, float]:
    _, _, month = _now(state)
    return _SUN[(month or 3) - 1]


def daylight(state: Dict[str, Any]) -> str:
    """dawn, daylight, dusk or night -- or, in a dungeon, the dark: no sun reaches it."""
    if (state.get("scene") or {}).get("dungeon"):
        return "underground: no light but what they carry"
    _, hour, _ = _now(state)
    rise, set_ = sun(state)
    if abs(hour - rise) <= TWILIGHT:
        return "dawn"
    if abs(hour - set_) <= TWILIGHT:
        return "dusk"
    return "daylight" if rise < hour < set_ else "night"


def today(state: Dict[str, Any]) -> int:
    return _now(state)[0]


def weather(state: Dict[str, Any]) -> str:
    """Today's weather, if it has been set today."""
    scene = state.get("scene") or {}
    return str(scene.get("weather") or "") if scene.get("weather_day") == today(state) else ""


def weavers(state: Dict[str, Any]) -> List[str]:
    from .advanced import has

    sheets = state.get("sheets") or {}
    return sorted(a for a in set((state.get("players") or {}).values())
                  if a in sheets and has(sheets[a], "Weather Weaver") and not sheets[a].get("dead"))


#: Past this, the party's want of sleep is worth the GM's notice.
AWAKE_HOURS = 24


def weariness(state: Dict[str, Any]) -> str:
    """A line when the party hasn't slept in over a day. The book has no rule for going without
    sleep -- healing needs "a few uninterrupted hours", and that is all -- so this is a fact for
    the GM to make something of, not a penalty."""
    rested = (state.get("scene") or {}).get("rested_at")
    if rested is None:
        return ""
    hours = (int(state.get("world_minutes", 0)) - int(rested)) // 60
    if hours <= AWAKE_HOURS:
        return ""
    return (f"- Rest: the party last slept {hours} hours ago. Weariness is yours to make something of -- "
            "stumbling, short tempers, a Defy Danger to stay alert, a debility if it goes on.")


def lines(state: Dict[str, Any]) -> List[str]:
    """For the live state: today's weather (or what to do about it), and weariness."""
    tired = [weariness(state)] if weariness(state) else []
    return _weather_lines(state) + tired


def _weather_lines(state: Dict[str, Any]) -> List[str]:
    known = weather(state)
    if known:
        return [f"- Weather today: {known}"]
    _, hour, _ = _now(state)
    rise, _ = sun(state)
    who = weavers(state)
    if who:
        if hour < rise:
            return [f"- Weather: not yet set. {' and '.join(who)} has Weather Weaver: at sunrise, if under open "
                    "skies, ask them what today's weather will be, then set_weather with their answer."]
        return [f"- Weather: not set today. If {' and '.join(who)} saw the sun rise under open skies, Weather "
                "Weaver lets them say what today's weather is -- ask, then set_weather. Otherwise set it yourself."]
    return ["- Weather: not set today. Call set_weather once for this day, fitting the season and place."]
