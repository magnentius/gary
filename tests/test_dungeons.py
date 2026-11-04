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


def _delve():
    """The delve playtest's first three areas, as Gemini set them."""
    d = dungeons.new("Hollowdeep Mine", "Caverns")
    d = dungeons.enter(d, "Upper Adit", "dwarf-cut gallery", exits=["steep incline downward into the dark",
                                                                    "adit mouth out to the hillside"])
    return dungeons.enter(d, "Ore-Cart Turnout", came_from="Upper Adit", via="steep incline downward into the dark",
                          exits=["steep incline back up to the Upper Adit", "half-flooded drift"])


def test_an_exit_that_names_a_mapped_area_leads_there_and_isnt_doubled():
    """Live: 'steep incline back up to the Upper Adit' stayed a blank beside Gary's own 'back to Upper Adit'."""
    turnout = _delve()["areas"]["ore_cart_turnout"]
    assert [(e["label"], e["to"]) for e in turnout["exits"]] == [
        ("steep incline back up to the Upper Adit", "upper_adit"), ("half-flooded drift", "")]


def test_listing_exits_again_replaces_the_blanks():
    """Live: each re-description of the vault door added another unexplored exit."""
    d = _delve()
    d = dungeons.enter(d, "Ore-Cart Turnout", exits=["the incline up to the Upper Adit", "the flooded drift",
                                                     "a collapsed tunnel"])
    exits = {e["label"]: e["to"] for e in d["areas"]["ore_cart_turnout"]["exits"]}
    assert exits == {"steep incline back up to the Upper Adit": "upper_adit", "the flooded drift": "",
                     "a collapsed tunnel": ""}


def test_the_longest_name_wins():
    d = _delve()
    d = dungeons.enter(d, "Upper Adit Archway", came_from="Ore-Cart Turnout", via="half-flooded drift")
    d = dungeons.enter(d, "Vault", exits=["the slab door back to the Upper Adit Archway"])
    assert d["areas"]["vault"]["exits"][0]["to"] == "upper_adit_archway"


def test_backtracking_into_a_new_name_is_refused_with_the_known_one():
    """Live: retreating up the incline made 'Upper Adit Archway', a second Upper Adit."""
    d = _delve()
    try:
        dungeons.enter(d, "Upper Adit Archway", came_from="Ore-Cart Turnout", via="steep incline back up")
    except ValueError as exc:
        assert "leads to Upper Adit, already on the map" in str(exc) and "set_scene location 'Upper Adit'" in str(exc)
    else:
        raise AssertionError("not refused")
    back = dungeons.enter(d, "Upper Adit", came_from="Ore-Cart Turnout", via="steep incline back up")
    assert back["order"] == ["upper_adit", "ore_cart_turnout"] and back["here"] == "upper_adit"


async def test_set_scene_refuses_a_new_name_for_a_known_room_and_says_which(rig):
    director, store, history = rig
    n = narrator(rig,
                 call_response(T.SET_SCENE, {"location": "Upper Adit", "dungeon": "Hollowdeep Mine",
                                             "exits": ["steep incline down"]}),
                 call_response(T.SET_SCENE, {"location": "Ore-Cart Turnout", "dungeon": "Hollowdeep Mine",
                                             "via": "steep incline down"}),
                 call_response(T.SET_SCENE, {"location": "Upper Adit Archway", "dungeon": "Hollowdeep Mine",
                                             "via": "back to Upper Adit"}),
                 text_response("."))
    await player(n, "we retreat up the incline")
    record = store.state()["dungeons"]["hollowdeep_mine"]
    assert record["order"] == ["upper_adit", "ore_cart_turnout"]
    assert store.state()["scene"]["location"] == "Ore-Cart Turnout"


def test_the_scene_summary_describes_the_place_not_the_moment():
    (decl,) = [d for d in T.DECLARATIONS if d.name == T.SET_SCENE]
    assert "Not who is doing what this moment" in decl.parameters.properties["summary"].description


def test_the_maps_own_arrow_copied_into_a_label_is_not_doubled():
    """Live: 'iron service ladder up → The Assayer's Strongroom' rendered '→ X → X'."""
    d = dungeons.new("Hollowdeep Mine")
    d = dungeons.enter(d, "The Assayer's Strongroom", exits=["a circular iron hatch"])
    d = dungeons.enter(d, "The Lower Sump", came_from="The Assayer's Strongroom", via="a circular iron hatch",
                       exits=["iron service ladder up → The Assayer's Strongroom", "flooded tunnel south"])
    assert [(e["label"], e["to"]) for e in d["areas"]["the_lower_sump"]["exits"]] == [
        ("iron service ladder up", "the_assayer_s_strongroom"), ("flooded tunnel south", "")]
    assert "→ The Assayer's Strongroom → " not in dungeons.render(d)


def test_the_way_back_loosely_put_is_the_way_back():
    """Live: 'up the incline back to the adit entrance' stayed a blank beside 'back to Adit Entry'."""
    d = dungeons.new("Hollowdeep Mine")
    d = dungeons.enter(d, "Adit Entry", exits=["down the adit following the cart-tracks"])
    d = dungeons.enter(d, "The First Winch Chamber", came_from="Adit Entry", via="down the adit following the cart-tracks",
                       exits=["up the incline back to the adit entrance", "a low side tunnel"])
    assert [(e["label"], e["to"]) for e in d["areas"]["the_first_winch_chamber"]["exits"]] == [
        ("up the incline back to the adit entrance", "adit_entry"), ("a low side tunnel", "")]


def test_the_exit_taken_in_other_words_is_the_one_listed():
    """Live: through 'the circular iron hatch', while the room listed 'a circular iron trapdoor'."""
    d = dungeons.new("Hollowdeep Mine")
    d = dungeons.enter(d, "Strongroom", exits=["back through the oak door", "a circular iron trapdoor in the corner"])
    d = dungeons.enter(d, "Lower Sump", came_from="Strongroom", via="the circular iron hatch")
    assert {e["label"]: e["to"] for e in d["areas"]["strongroom"]["exits"]} == {
        "back through the oak door": "", "a circular iron trapdoor in the corner": "lower_sump"}


def test_a_tie_or_a_single_shared_word_is_not_a_match():
    d = dungeons.new("Mine")
    d = dungeons.enter(d, "Hall", exits=["an iron door north", "an iron door south"])
    d = dungeons.enter(d, "Crypt", came_from="Hall", via="the iron door")
    exits = {e["label"]: e["to"] for e in d["areas"]["hall"]["exits"]}
    assert exits["an iron door north"] == exits["an iron door south"] == "" and exits["the iron door"] == "crypt"
