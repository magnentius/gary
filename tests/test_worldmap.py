"""The campaign map: places, routes in days, blanks, and the fronts' signs."""

from __future__ import annotations

import pytest

from gary import worldmap
from gary.adapters import gemini_tools as T
from gary.context import render_live_state
from tests.test_gemini import call_response, narrator, player, text_response
from tests.test_gemini import rig as _rig

rig = _rig


def test_places_routes_blanks_and_signs():
    w = worldmap.place(None, "Fenwick", "steading", "a walled market town")
    w = worldmap.place(w, "The Barrow", "dungeon")
    w = worldmap.route(w, "Fenwick", "The Barrow", 2, "the open moor")
    w = worldmap.route(w, "Fenwick", "the north road")
    w = worldmap.sign(w, "smoke over Pigton", near="Fenwick")
    shown = worldmap.render(w)
    assert "• Fenwick [steading]: a walled market town — to The Barrow (2 days, the open moor); the north road (unexplored)" in shown
    assert "⚠ smoke over Pigton (near Fenwick)" in shown
    assert worldmap.distance(w, "the barrow", "Fenwick") == 2
    assert "smoke" not in worldmap.render(worldmap.sign(w, "smoke over Pigton", remove=True))
    with pytest.raises(ValueError, match="isn't on the map"):
        worldmap.route(w, "Atlantis", "Fenwick", 3)


def test_a_road_once_travelled_fills_its_blank():
    w = worldmap.place(None, "Fenwick", "steading")
    w = worldmap.route(w, "Fenwick", "Pigton")  # not a place yet: a road leading off
    w = worldmap.place(w, "Pigton", "steading")
    w = worldmap.route(w, "Fenwick", "Pigton", 1)
    assert [r for r in w["routes"] if not r["b"]] == []


async def test_steadings_and_dungeons_go_on_the_map_and_a_known_route_sets_a_journey(rig):
    director, store, history = rig
    n = narrator(rig,
                 call_response(T.CREATE_STEADING, {"name": "Pigton", "kind": "village", "option": 0, "problem": 0,
                                                   "details": ["eels", "Castonshire"]}),
                 call_response(T.SET_SCENE, {"location": "Barrow mouth", "dungeon": "The Barrow"}),
                 call_response(T.WORLD_MAP, {"action": "route", "from_place": "Pigton", "to": "The Barrow",
                                             "days": 3, "terrain": "the moor"}),
                 text_response("."))
    await player(n, "we set out")
    places = store.state()["world"]["places"]
    assert places["pigton"]["kind"] == "steading" and places["the_barrow"]["kind"] == "dungeon"
    assert "Pigton [steading]: a village — to The Barrow (3 days, the moor)" in render_live_state(store.state())


async def test_a_journey_on_a_known_route_takes_the_maps_days(clock, known=True):
    """The model asked for five days; the map says two, so two it is -- the same trip never changes length."""
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
    world = worldmap.route(worldmap.place(worldmap.place(None, "Fenwick", "steading"), "The Barrow", "dungeon"),
                           "Fenwick", "The Barrow", 2, "the moor")
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": {
        "name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0, "hp": 20, "max_hp": 20,
        "armor": 0, "damage": "d10", "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
        "gear": [], "rations": 10}}),
        *([Mutation(MutationKind.WORLD, "", {"world": world})] if known else []),
        Mutation(MutationKind.SCENE, "", {"location": "Fenwick, the gate", "steading": ""})], reason="setup")
    store.apply([Mutation(MutationKind.SCENE, "", {"location": "Fenwick"})], reason="at Fenwick")
    n = narrator((director, store, InMemoryHistoryBackend()),
                 call_response(T.PERILOUS_JOURNEY, {"destination": "The Barrow", "days": 5, "trailblazer": "Dorn"}),
                 text_response("."))
    await player(n, "we set out for the barrow")
    answer = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    if known:
        assert answer["days"] == 2 and "2 days" in answer["note"]
    else:  # no route yet: the journey draws one, the days it took
        assert answer["days"] == 5 and worldmap.distance(store.state()["world"], "Fenwick", "The Barrow") == 5


async def test_a_journey_on_no_known_route_draws_it(clock):
    await test_a_journey_on_a_known_route_takes_the_maps_days(clock, known=False)


def test_a_place_has_a_monster_setting_and_the_gm_sees_its_creatures_there():
    w = worldmap.place(None, "The Greyfen", "terrain", "black water and reeds", setting="swamp")
    assert w["places"]["the_greyfen"]["setting"] == "Swamp"
    assert "[terrain, Swamp]" in worldmap.render(w) and "Swamp" not in worldmap.render(w, for_players=True)
    assert worldmap.place(w, "The Greyfen", setting="Moonbase")["places"]["the_greyfen"]["setting"] == "Swamp"
    shown = render_live_state({"world": w, "scene": {"location": "Deep in the Greyfen, a drowned causeway"}})
    assert "Creatures of The Greyfen (Swamp), first:" in shown
    edge = worldmap.place(None, "The Edge of the Greyfen", "terrain", setting="Swamp")  # as seen live
    edge = worldmap.place(edge, "Fenwick Woods", "terrain", setting="Woods")
    assert worldmap.here(edge, {"location": "The Greyfen - western margin"})["setting"] == "Swamp"
    assert worldmap.here(edge, {"location": "A clearing in the woods"}) is None  # "woods" alone is no name


def test_the_front_work_is_given_the_monster_settings():
    from gary import opening

    w = worldmap.place(None, "Fenwick Woods", "terrain", setting="Woods")
    for event in (opening.fronts_event((), w), opening.review_event(world=w)):
        assert "Undead:" in event["monster_settings"] and event["places_by_setting"] == ["Fenwick Woods: Woods"]
        assert "monster settings" in event["instruction"]
