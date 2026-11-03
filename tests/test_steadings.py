"""Steadings: made by the book's templates, kept as state, and their tags applied."""

from __future__ import annotations

import pytest

from gary import steadings as S
from gary.adapters import gemini_tools as T
from gary.fsm import Director, SendMessage
from gary.models import Mutation, MutationKind
from gary.ports import ScriptedDice
from gary.registry import PendingCheckRegistry
from gary.sheets import SheetError
from gary.state.backend import InMemoryStateBackend
from gary.state.history import InMemoryHistoryBackend
from gary.state.store import CampaignStore
from tests.test_gemini import CHANNEL, call_response, narrator, player, text_response


def test_each_kind_starts_from_the_book_and_takes_an_option_and_a_problem():
    village = S.make("Pigton", "village", 3, 3, ["eels", "Castonshire", "Bucksburg", "the bog troll"])
    assert (village["prosperity"], village["population"], village["defenses"]) == ("moderate", "steady", "militia")
    assert S.has(village, "need", "adventurers") and S.has(village, "trade", "Bucksburg")
    town = S.make("Ennet Bend", "town", 1, 4, ["Pigton", "Curland"])
    assert S.has(town, "market") and town["prosperity"] == "wealthy" and town["population"] == "shrinking"
    keep = S.make("Irongate", "keep", 4, 2, ["Curland", "Varlosh"])
    assert not S.has(keep, "need") and keep["population"] == "shrinking"  # fertile land removed the need
    city = S.make("Torsea", "city", 5, 0, ["mages", "Seawall", "Irongate", "alchemy"])
    assert S.has(city, "arcane") and S.has(city, "need", "food") and city["population"] == "growing"


def test_every_your_choice_must_be_filled_and_the_error_says_which():
    with pytest.raises(SheetError, match=r"needs 4 details, in order: resource \(a good\); oath \(another "
                                         r"steading's name\); trade \(another steading's name\); blight"):
        S.make("Pigton", "village", 3, 3, ["eels"])
    assert S.blank_names("city", 4, 0)[0] == "guild (a kind of guild)"


def test_adjusting_steps_ranks_and_tags():
    village = S.make("Pigton", "village", 0, 0, ["eels", "Castonshire"])
    grown = S.adjust(village, ["+prosperity", "+prosperity", "+defenses"], [("market", "")], ["need"], "town")
    assert (grown["kind"], grown["prosperity"], grown["defenses"]) == ("town", "wealthy", "militia")  # Poor +2
    assert S.has(grown, "market") and not S.has(grown, "need")
    with pytest.raises(SheetError, match="no blight tag"):
        S.adjust(village, remove=["blight"])


def test_prosperity_decides_what_is_for_sale():
    village = S.make("Pigton", "village", 0, 0, ["eels", "Castonshire"])  # Poor, then -Defenses
    assert S.for_sale(village, "Dungeon rations", "gear", 3) == ""
    assert "bare necessities" in S.for_sale(village, "Long sword", "weapon", 15)
    dirt = S.adjust(village, ["-prosperity"])
    assert "Nothing is for sale" in S.for_sale(dirt, "Dungeon rations", "gear", 3)
    town = S.make("Ennet Bend", "town", 5, 4, ["Pigton", "Curland"])  # Moderate
    assert S.for_sale(town, "Long sword", "weapon", 15) == ""
    assert "not the finest" in S.for_sale(town, "Plate", "armor", 350)
    assert "poison" in S.for_sale(town, "Bloodweed", "poison", 12)
    assert S.for_sale(S.adjust(town, add=[("lawless", "")]), "Bloodweed", "poison", 12) == ""
    assert S.for_sale(None, "Plate", "armor", 350) == ""  # no steading: the GM decides


def test_resource_need_and_recruit_bonuses():
    village = S.make("Pigton", "village", 0, 0, ["arrows", "Castonshire"])
    assert S.price_factor(village, "Bundle of arrows") == (0.5, "plentiful here (Resource: arrows)")
    city = S.make("Torsea", "city", 5, 0, ["priests", "Seawall", "Irongate", "alchemy"])
    assert S.recruit_bonus(city, "adept") == (1, "Arcane") and S.recruit_bonus(city, "Priest") == (1, "Guild")
    assert S.recruit_bonus(village, "Adept") == (0, "")


def test_market_helps_supply_and_arcane_helps_recruit_an_adept(registry, clock, dice):
    town = S.make("Ennet Bend", "town", 1, 0, ["Pigton", "Curland", "grain", "Mayhill"])
    town = S.adjust(town, add=[("arcane", "")])
    sheet = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "hp": 10, "max_hp": 10,
             "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 0,
             "damage": "d10", "coins": 50}
    director = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice, players={1: "Dorn"},
                        sheet_for={"Dorn": sheet}.get, steading=lambda: town)
    texts = [e.text for e in director.open_move(channel_id=CHANNEL, user_id=1, actor="Dorn", move="supply",
                                                target="a silvered blade") if isinstance(e, SendMessage)]
    assert any("+1 Market (Ennet Bend)" in t for t in texts)
    registry.cancel(next(iter(registry.open_checks(CHANNEL))).check_id)
    texts = [e.text for e in director.open_move(channel_id=CHANNEL, user_id=1, actor="Dorn", move="recruit",
                                                target="Adept", offers=["generous pay"]) if isinstance(e, SendMessage)]
    assert any("+1 Arcane (Ennet Bend)" in t for t in texts)


@pytest.fixture
def table(clock):
    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={CHANNEL},
                        dice=ScriptedDice(), steading=lambda: S.here(store.state()))
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": {
        "name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "hp": 10, "max_hp": 10,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 0,
        "damage": "d10", "gear": [], "coins": 100, "rations": 0, "ammo": 0}})], reason="setup")
    return director, store, InMemoryHistoryBackend()


async def test_the_model_makes_a_steading_puts_the_party_there_and_the_shop_follows(table):
    director, store, history = table
    n = narrator(table,
                 call_response(T.CREATE_STEADING, {"name": "Pigton", "kind": "village", "option": 0, "problem": 0,
                                                   "details": ["eels", "Castonshire"]}),
                 call_response(T.SET_SCENE, {"location": "Pigton's mud street", "steading": "pigton"}),
                 call_response(T.BUY, {"actor": "Dorn", "item": "long sword"}),
                 call_response(T.BUY, {"actor": "Dorn", "item": "dungeon rations"}),
                 text_response("A muddy village."))
    await player(n, "we walk into the village and look for a smith")
    assert store.state()["steadings"]["pigton"]["prosperity"] == "poor"
    assert store.state()["scene"]["steading"] == "pigton"
    answers = [p.function_response.response for c in n.client.calls[1:] for p in c["contents"][-1].parts
               if p.function_response]
    assert "Only the bare necessities are for sale in Pigton (Poor)" in answers[2]["error"]
    assert answers[3]["ok"] and [g["name"] for g in store.state()["sheets"]["Dorn"]["gear"]] == ["Dungeon Rations"]
    from gary.context import render_live_state

    assert "Pigton — village: Poor, Steady, None; Resource (eels), Oath (Castonshire), Safe, Need (food) ← the party is here" \
        in render_live_state(store.state())


async def test_a_scene_cannot_name_a_steading_nobody_made(table):
    director, store, history = table
    n = narrator(table, call_response(T.SET_SCENE, {"location": "x", "steading": "Atlantis"}), text_response("."))
    await player(n, "...")
    answer = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    assert "No steading 'Atlantis' yet" in answer["error"]


async def test_trade_with_no_steading_on_the_scene_reminds_the_model_to_make_one(table):
    """3.8-flash shopped a whole scene in a town it never recorded, so no tag applied."""
    from gary.fsm import NO_STEADING

    director, store, history = table
    n = narrator(table, call_response(T.BUY, {"actor": "Dorn", "item": "dungeon rations"}),
                 call_response(T.SELL, {"actor": "Dorn", "item": "dungeon rations"}), text_response("."))
    await player(n, "I buy rations in the market, then change my mind")
    bought = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    sold = n.client.calls[2]["contents"][-1].parts[0].function_response.response
    assert bought["ok"] and bought["note"] == NO_STEADING
    assert sold["ok"] and sold["note"] == NO_STEADING


def test_supply_with_no_steading_tells_the_model_to_make_one(registry, clock, dice):
    from gary.fsm import NO_STEADING

    sheet = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "hp": 10, "max_hp": 10,
             "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 0,
             "damage": "d10", "coins": 50}
    for town, expected in ((None, True), (S.make("Ennet Bend", "town", 1, 0, ["Pigton", "Curland", "grain", "Mayhill"]), False)):
        director = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                            players={1: "Dorn"}, sheet_for={"Dorn": sheet}.get, steading=lambda t=town: t)
        director.open_move(channel_id=CHANNEL, user_id=1, actor="Dorn", move="supply", target="rope")
        check = next(iter(registry.open_checks(CHANNEL)))
        from gary.models import Roll

        notes = director._advanced_notes(check, Roll(spec="2d6", faces=(4, 4)))
        assert (NO_STEADING in notes) is expected
        registry.cancel(check.check_id)


async def test_a_steading_made_with_none_on_the_scene_puts_the_party_there(table):
    """Made and shopped in one round, before set_scene: the purchase still gets the town."""
    director, store, history = table
    n = narrator(table,
                 _parallel(call_response(T.CREATE_STEADING, {"name": "Pigton", "kind": "village", "option": 0,
                                                             "problem": 0, "details": ["eels", "Castonshire"]}),
                           call_response(T.BUY, {"actor": "Dorn", "item": "long sword"})),
                 call_response(T.CREATE_STEADING, {"name": "Curland", "kind": "village", "option": 0,
                                                   "problem": 0, "details": ["wool", "Castonshire"]}),
                 text_response("."))
    await player(n, "we walk into the village and look for a smith")
    answers = [p.function_response.response for p in n.client.calls[1]["contents"][-1].parts]
    assert "party is now in Pigton" in answers[0]["note"]
    assert "Only the bare necessities are for sale in Pigton" in answers[1]["error"]
    # A second one, made while they stand in the first, is somewhere else.
    assert store.state()["scene"]["steading"] == "Pigton"
    later = n.client.calls[2]["contents"][-1].parts[0].function_response.response
    assert later["note"].startswith("Recorded. When the party is there")


def _parallel(*responses):
    from tests.test_gemini import _Response

    return _Response([part for r in responses for part in r.candidates[0].content.parts])


async def test_settling_into_a_steading_shows_the_table_its_downtime(table):
    director, store, history = table
    town = S.make("Ennet Bend", "town", 1, 0, ["Pigton", "Curland", "grain", "Mayhill"])
    store.apply([Mutation(MutationKind.STEADING, "ennet_bend", {"steading": town})], reason="setup")
    n = narrator(table,
                 call_response(T.SET_SCENE, {"location": "Ennet Bend, the square", "steading": "Ennet Bend"}),
                 call_response(T.SET_SCENE, {"location": "Ennet Bend, the inn", "steading": "Ennet Bend"}),
                 text_response("."))
    result = await player(n, "we walk into town")
    shown = [e.text for e in result.effects if getattr(e, "text", "")]
    assert len(shown) == 1  # once, on arriving -- not again for the inn in the same town
    menu = shown[0]
    assert menu.startswith("🏘️ **Ennet Bend** — some downtime.") and "Recover" in menu and "Carouse" in menu
    assert "+1 here (Market)" in menu  # this town's Market
    answer = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    assert "post" not in answer and "downtime" in answer["note"]


async def test_no_downtime_menu_in_a_fight(table):
    director, store, history = table
    town = S.make("Ennet Bend", "town", 1, 0, ["Pigton", "Curland", "grain", "Mayhill"])
    store.apply([Mutation(MutationKind.STEADING, "ennet_bend", {"steading": town}),
                 Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {"Bandit 1": {
                     "kind": "Bandit", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d6"}}})], reason="setup")
    n = narrator(table, call_response(T.SET_SCENE, {"location": "Ennet Bend gate", "steading": "Ennet Bend"}),
                 text_response("."))
    result = await player(n, "we run for the gate")
    assert not [e for e in result.effects if getattr(e, "text", "").startswith("🏘️")]


async def test_a_steading_made_on_arrival_shows_its_downtime_too(table):
    director, store, history = table
    n = narrator(table, call_response(T.CREATE_STEADING, {"name": "Pigton", "kind": "village", "option": 0,
                                                          "problem": 0, "details": ["eels", "Castonshire"]}),
                 text_response("A muddy village."))
    result = await player(n, "we reach the village")
    assert any(getattr(e, "text", "").startswith("🏘️ **Pigton**") for e in result.effects)
