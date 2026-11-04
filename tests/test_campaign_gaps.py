"""What the two-session campaign playtest found: creatures made once and forgotten, steadings on
the map with no record, and the same few names in every game."""

from __future__ import annotations

import pytest

from gary import monsters, npcs, opening
from gary.adapters import gemini_tools as T
from gary.context import render_live_state
from gary.models import Mutation, MutationKind
from tests.test_gemini import call_response, narrator, player, text_response
from tests.test_gemini import rig as _rig

rig = _rig

GUARD = {"name": "Salt Mine Guard", "hp": 6, "armor": 2, "damage": "d8", "instinct": "To enforce Vane's order",
         "moves": ["Sound the alarm horn"]}


def _result(n, call: int):
    return n.client.calls[call]["contents"][-1].parts[0].function_response.response


# -- creatures made once are remembered --------------------------------------------------------

def test_a_creature_made_this_campaign_is_added_again_by_name():
    made = {"salt mine guard": monsters.kind_record(monsters.stats_for(GUARD))}
    added, errors = monsters.prepare([{"name": "salt mine guard", "count": 2}], [], made)
    assert errors == [] and sorted(added) == ["Salt Mine Guard 1", "Salt Mine Guard 2"]
    assert added["Salt Mine Guard 1"]["armor"] == 2 and added["Salt Mine Guard 1"]["moves"] == ["Sound the alarm horn"]
    # Not made yet: still refused, as the book asks for a recipe or numbers.
    assert "not in the Dungeon World bestiary" in monsters.prepare([{"name": "Pit Mastiff"}], [], made)[1][0]
    # Numbers given again win, and become what's remembered.
    again, _ = monsters.prepare([{**GUARD, "hp": 10}], [], made)
    assert again["Salt Mine Guard 1"]["hp"] == 10


async def test_add_monsters_remembers_what_it_made_and_says_so(rig):
    """Live: three refusals for 'Salt Mine Guard' after Gemini had made one with stats."""
    director, store, _ = rig
    n = narrator(rig, call_response(T.ADD_MONSTERS, {"monsters": [GUARD]}),
                 call_response(T.REMOVE_MONSTERS, {}),
                 call_response(T.ADD_MONSTERS, {"monsters": [{"name": "Salt Mine Guard", "count": 2}]}),
                 text_response("."))
    await player(n, "more guards come")
    assert _result(n, 3)["ok"] is True
    assert sorted(store.state()["monsters"]) == ["Salt Mine Guard 2", "Salt Mine Guard 3"]
    assert store.state()["homebrew"]["salt mine guard"]["instinct"] == "To enforce Vane's order"
    assert "Creatures made this campaign (add_monsters by name alone): Salt Mine Guard" in \
        render_live_state(store.state())


def test_the_bestiary_is_undone_with_its_change(rig):
    _, store, _ = rig
    store.apply([Mutation(MutationKind.HOMEBREW, "", {"homebrew": {"x": {"kind": "X"}}})], reason="t")
    store.undo_last()
    assert not store.state().get("homebrew")


# -- a steading on the map gets its record ------------------------------------------------------

async def test_placing_a_steading_without_a_record_asks_for_one(rig):
    """Live: Brinewell went on the map as a steading, never got a record, and so never changed."""
    _, store, _ = rig
    n = narrator(rig, call_response(T.WORLD_MAP, {"action": "place", "name": "Brinewell", "kind": "steading"}),
                 call_response(T.WORLD_MAP, {"action": "place", "name": "The Old Mine", "kind": "dungeon"}),
                 text_response("."))
    await player(n, "we look at the map")
    assert "create_steading" in _result(n, 1)["next"]
    assert "next" not in _result(n, 2)


def test_the_fronts_after_session_one_ask_for_every_steadings_record():
    assert "create_steading for any that has none" in opening.fronts_event()["instruction"]


# -- names ------------------------------------------------------------------------------------

def test_fresh_names_leave_out_those_already_used_and_hold_still():
    state = {"setting": "x", "sheets": {"Dorn": {}}, "npcs": {"vane": {"name": "Captain Ajax Vane"}},
             "world_minutes": 90}
    names = npcs.suggest(state)
    assert len(names) == 6 and "Dorn" not in names and "Ajax" not in names
    assert npcs.suggest(state) == names  # the same turn, the same handful
    assert npcs.suggest({**state, "world_minutes": 600}) != names


def test_the_name_pool_takes_in_the_class_chapters_lists():
    assert {"Ozruk", "Hawke", "Sparrow", "Throndir"} <= set(npcs.NAMES)
    assert len(npcs.NAMES) == len(set(npcs.NAMES))


@pytest.mark.parametrize("state, shown", [({}, False), ({"setting": "Low fantasy"}, True)])
def test_names_are_offered_once_a_game_is_under_way(state, shown):
    assert ("Fresh names for anyone new:" in render_live_state(state)) is shown
