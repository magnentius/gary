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


def test_a_long_stretch_without_sleep_is_shown_but_not_punished():
    rested = at("03-20", 10, scene={"rested_at": 0})
    assert sky.weariness(rested) == ""
    tired = {**rested, "world_minutes": 30 * 60}
    assert "last slept 30 hours ago" in sky.weariness(tired)
    assert "last slept 30 hours ago" in render_live_state(tired)
    assert sky.weariness(at("03-20", 40)) == ""  # never camped yet: nothing to measure from


async def test_making_camp_marks_when_they_slept(clock):
    from gary.fsm import Director
    from gary.models import Mutation, MutationKind
    from gary.registry import PendingCheckRegistry
    from gary.state.backend import InMemoryStateBackend
    from gary.state.history import InMemoryHistoryBackend
    from gary.state.store import CampaignStore
    from tests.test_gemini import CHANNEL

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={CHANNEL},
                        players={1: "Dorn"}, sheet_for=lambda a: (store.state().get("sheets") or {}).get(a))
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": {
        "name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0, "hp": 5, "max_hp": 20, "armor": 0, "damage": "d10",
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "gear": [], "rations": 2}}),
                 Mutation(MutationKind.TIME, "", {"minutes": 600})], reason="setup")
    rig = (director, store, InMemoryHistoryBackend())
    n = narrator(rig, call_response(T.MAKE_CAMP, {"reason": "a dry cave, the entrance hidden"}), text_response("."))
    await player(n, "we camp")
    assert store.state()["scene"]["rested_at"] == store.state()["world_minutes"] == 600 + 480
