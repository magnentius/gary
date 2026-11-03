"""Dungeon maps: every area on the map with its exits, the blanks kept, the layout consistent."""

from __future__ import annotations

from gary import dungeons
from gary.adapters import gemini_tools as T
from gary.context import render_live_state
from tests.test_gemini import call_response, narrator, player, text_response
from tests.test_gemini import rig as _rig

rig = _rig


def test_areas_join_by_the_exit_taken_and_untaken_exits_are_blanks():
    d = dungeons.new("The Barrow", "Undead")
    d = dungeons.enter(d, "Entry hall", "cold, bones underfoot", exits=["iron door north", "narrow stair down"])
    d = dungeons.enter(d, "Crypt", "rows of niches", came_from="Entry hall", via="iron door north",
                       exits=["a crawlway"])
    hall = d["areas"]["entry_hall"]
    assert {e["label"]: e["to"] for e in hall["exits"]} == {"iron door north": "crypt", "narrow stair down": ""}
    assert any(e["to"] == "entry_hall" for e in d["areas"]["crypt"]["exits"])  # the way back
    assert dungeons.blanks(d) == 2 and d["here"] == "crypt"
    shown = dungeons.render(d)
    assert "• Entry hall: cold, bones underfoot — exits: iron door north → Crypt; narrow stair down (unexplored)" in shown
    assert "• Crypt ← here" in shown and "(Undead)" in shown
    assert "(Undead)" not in dungeons.render(d, for_players=True)


def test_backtracking_revisits_an_area_without_duplicating_it():
    d = dungeons.new("The Barrow")
    d = dungeons.enter(d, "Entry hall", exits=["iron door"])
    d = dungeons.enter(d, "Crypt", came_from="Entry hall", via="iron door")
    d = dungeons.enter(d, "Entry hall", came_from="Crypt", via="back to Entry hall")
    assert d["order"] == ["entry_hall", "crypt"] and d["here"] == "entry_hall"
    assert len(d["areas"]["crypt"]["exits"]) == 1


async def test_setting_scenes_in_a_dungeon_draws_its_map(rig):
    director, store, history = rig
    n = narrator(rig,
                 call_response(T.SET_SCENE, {"location": "Entry hall", "summary": "cold", "dungeon": "The Barrow",
                                             "setting": "Undead", "exits": ["iron door north", "stair down"]}),
                 call_response(T.SET_SCENE, {"location": "Crypt", "dungeon": "The Barrow", "via": "iron door north",
                                             "exits": ["a crawlway"]}),
                 text_response("."))
    await player(n, "we go in")
    record = store.state()["dungeons"]["the_barrow"]
    assert record["order"] == ["entry_hall", "crypt"] and record["setting"] == "Undead"
    shown = render_live_state(store.state())
    assert "- Dungeon map" in shown and "iron door north → Crypt" in shown
    assert "Creatures of the Undead, first:" in shown and "Ghoul" in shown
    store.undo_last()  # the scene; then the map change
    store.undo_last()
    assert store.state()["dungeons"]["the_barrow"]["order"] == ["entry_hall"]


def test_creatures_of_a_setting_come_from_the_bestiary():
    assert "Ghoul" in dungeons.creatures("Undead") and "Goblin" not in dungeons.creatures("Undead")
