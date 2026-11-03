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
