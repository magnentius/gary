"""Treasure by the book: a creature's damage die reads the table; Gary rolls the amounts,
the finder gets the coins and valuables, the model names the rest."""

from __future__ import annotations

import pytest

from gary import trade, treasure
from gary.adapters import gemini_tools as T
from gary.fsm import Director
from gary.models import Mutation, MutationKind
from gary.ports import ScriptedDice
from gary.registry import PendingCheckRegistry
from gary.state.backend import InMemoryStateBackend
from gary.state.history import InMemoryHistoryBackend
from gary.state.store import CampaignStore
from tests.test_gemini import CHANNEL, call_response, narrator, player, text_response


def first_three(items, k):
    return list(items[:k])


def scripted(*faces):
    dice = ScriptedDice(faces)
    return lambda spec: dice.roll(spec).total


def test_a_low_roll_is_a_few_coins():
    found = treasure.roll(scripted(1, 3, 4), "d6")  # d6: 1 -> 2d8 coins
    assert (found.roll, found.coins, found.entries) == (1, 7, ["A few coins: 7 coins"])


def test_a_hoarder_rolls_twice_and_keeps_the_higher():
    found = treasure.roll(scripted(2, 9, 3, 2, 1), "d10", ["hoarder"])  # 9: a chest, 3d6x100
    assert found.rolled == "d10 twice (hoarder): 2, 9 → 9"
    assert found.valuables == [{"name": "A chest of coins and other small valuables", "value": 600, "weight": 1}]


def test_a_lord_adds_a_d4_and_magic_brings_the_books_ideas():
    found = treasure.roll(scripted(8, 2), "d8", ["lord", "magical"], first_three)  # 8 + 2 = 10
    assert found.rolled == "d8: 8, +1d4 lord over others: 2 = 10"
    assert found.entries == ["A magical item or magical effect"]
    assert [i["name"] for i in found.ideas] == ["Argo-Thaan, Holy Avenger", "Arrows of Acheron",
                                                "Axe of the Conqueror-King"]
    assert "Also, as it is magical: some strange item, possibly magical." in found.for_gm


def test_roll_again_chains_and_a_hoard_is_huge():
    # 12 + 3 = 15: a spell, and roll again -- 3 + 1 = 4: a small item worth 2d10x10
    found = treasure.roll(scripted(12, 3, 3, 1, 5, 5), "b[2d12]", ["ancient"])
    assert found.entries[0] == "All the information needed to learn a new spell"
    assert found.entries[1] == ("and rolling again (d12: 3, +1d4 ancient: 1 = 4): "
                                "A small item (gem, art) of considerable value, worth 100 coins")
    # 12 + 4 + 2 = 18: a hoard -- 7 x 1000 coins, 5 x 10 gems at (3 + 3) x 100 each
    big = treasure.roll(scripted(12, 4, 2, 7, 5, 3, 3), "d12", ["lord", "ancient"])
    assert big.coins == 7000 and big.valuables == [{"name": "50 gems", "value": 50 * 600, "weight": 0}]


def test_the_magic_items_are_the_books():
    assert len(treasure.MAGIC_ITEMS) == 39
    bag = next(i for i in treasure.MAGIC_ITEMS if i["name"] == "Bag of Holding")
    assert bag["tags"] == ["0 weight"] and "always 0 weight" in bag["text"]


def test_a_found_valuable_sells_for_its_worth():
    sheet = {"name": "Dorn", "coins": 3, "gear": [{"name": "A very valuable small item", "value": 700,
                                                   "weight": 0, "tags": ["worth 700 coins"]}]}
    sold, name, paid = trade.sell(sheet, "a very valuable small item")
    assert (paid, sold["coins"], sold["gear"]) == (700, 703, [])


@pytest.fixture
def table(clock):
    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={CHANNEL},
                        dice=ScriptedDice())
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": {
        "name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "hp": 10, "max_hp": 10,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 0,
        "damage": "d10", "gear": [], "coins": 5, "rations": 0}})], reason="setup")
    store.apply([Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {
        "Orc 1": {"kind": "Orc Berserker", "hp": 0, "max_hp": 12, "armor": 0, "damage": "b[2d10]+2", "srd": True}}})],
        reason="setup")
    return director, store, InMemoryHistoryBackend()


async def test_searching_a_fallen_orc_pays_once_and_undoes(table):
    director, store, history = table
    director.dice.queue(3, 4, 6, 6, 2)  # d10: 3 -> several coins, 4d10: 4 + 6 + 6 + 2
    n = narrator(table, call_response(T.TREASURE, {"finder": "dorn", "creature": "orc 1"}),
                 call_response(T.TREASURE, {"finder": "Dorn", "creature": "Orc 1"}),
                 text_response("A greasy pouch of coins."))
    result = await player(n, "I search the orc")
    (line,) = [e.text for e in result.effects if getattr(e, "text", "").startswith("💰")]
    assert line.startswith("💰 **Dorn** searches Orc 1's belongings — d10: 3.")
    assert "• Several coins: 18 coins" in line
    assert store.state()["sheets"]["Dorn"]["coins"] == 23
    again = n.client.calls[2]["contents"][-1].parts[0].function_response.response
    assert again == {"ok": False, "error": "Orc 1's belongings have already been searched."}
    store.undo_last()
    assert store.state()["sheets"]["Dorn"]["coins"] == 5
    store.undo_last()
    assert not store.state()["monsters"]["Orc 1"]["looted"]
