"""Daylight from the hour and the season; the day's weather, kept; Weather Weaver at sunrise."""

from __future__ import annotations

from gary import sky
from gary.adapters import gemini_tools as T
from gary.context import render_live_state
from tests.test_gemini import call_response, narrator, player, text_response
from tests.test_gemini import rig as _rig

rig = _rig


def at(month_day: str, hour: float, **more):
    return {"calendar": {"epoch_date": f"1026-{month_day}"}, "world_minutes": int(hour * 60), **more}


def test_the_light_follows_the_hour_and_the_season():
    assert sky.daylight(at("12-21", 9)) == "daylight" and sky.daylight(at("12-21", 8)) == "dawn"
    assert sky.daylight(at("12-21", 7)) == "night"       # a midwinter morning is still dark
    assert sky.daylight(at("06-21", 7)) == "daylight"    # a midsummer one is not
    assert sky.daylight(at("12-21", 16)) == "dusk"
    assert sky.daylight(at("06-21", 21)) == "night" and sky.daylight(at("06-21", 20)) == "dusk"
    assert "(night)" in render_live_state(at("12-21", 23))


def test_the_days_weather_is_kept_until_the_next_day():
    state = at("01-10", 10, scene={"weather": "hard frost, a white sky", "weather_day": 0})
    assert sky.weather(state) == "hard frost, a white sky"
    assert "- Weather today: hard frost, a white sky" in render_live_state(state)
    tomorrow = {**state, "world_minutes": 24 * 60 + 600}
    assert sky.weather(tomorrow) == "" and "Call set_weather once" in render_live_state(tomorrow)


def test_weather_weaver_is_asked_at_sunrise():
    druid = {"name": "Liriel", "class": "Druid", "advanced_moves": ["Weather Weaver"]}
    before = at("03-20", 4, players={"1": "Liriel"}, sheets={"Liriel": druid})
    assert "at sunrise, if under open skies, ask them" in "\n".join(sky.lines(before))
    after = {**before, "world_minutes": 9 * 60}
    assert "Weather Weaver lets them say" in "\n".join(sky.lines(after))


async def test_set_weather_records_it_for_today(rig):
    director, store, history = rig
    n = narrator(rig, call_response(T.SET_WEATHER, {"weather": "a cold drizzle off the marsh"}), text_response("."))
    await player(n, "we set out")
    assert sky.weather(store.state()) == "a cold drizzle off the marsh"
