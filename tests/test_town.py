"""Carouse and Supply."""

from __future__ import annotations

import pytest

from gary.fsm import ChoicePrompt, Director, PromptModel, SendMessage, UpdateSheet

CHANNEL, ALICE = 555, 111
THORIN = {"name": "Thorin", "class": "Cleric", "race": "Dwarf", "level": 1, "xp": 0, "hp": 20, "max_hp": 20,
          "stats": {"str": 12, "dex": 9, "con": 13, "int": 12, "wis": 16, "cha": 15},
          "armor": 1, "damage": "d6", "coins": 350}


@pytest.fixture
def sheets():
    return {"Thorin": dict(THORIN)}


@pytest.fixture
def director(registry, clock, dice, sheets):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Thorin"}, sheet_for=sheets.get, monsters=lambda: {})


def carouse(director, coins):
    return director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin", move="carouse", coins=coins)


def only_check(registry):
    (check,) = list(registry.open_checks(CHANNEL))
    return check


def test_each_hundred_past_the_first_is_plus_one(director, registry):
    (ask,) = [e.text for e in carouse(director, 350) if isinstance(e, SendMessage)]
    assert ask == "**Thorin** — Carouse with 300 coins, roll +2 (+1 for each 100 past the first)."
    assert only_check(registry).modifier == 2


def test_a_party_needs_the_coin(director):
    (note,) = carouse(director, 50)
    assert "at least 100 coins" in note.payload["error"]
    (note,) = carouse(director, 500)
    assert "has 350 coins; that party costs 500" in note.payload["error"]


def test_the_coin_goes_and_the_player_chooses(director, registry, dice):
    carouse(director, 200)
    dice.queue(5, 4)  # +1: 10
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    (ask,) = [e for e in effects if isinstance(e, SendMessage) and isinstance(e.prompt, ChoicePrompt)]
    assert ask.prompt.count == 3
    effects = director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id,
                              picks=[0, 1, 2])
    assert UpdateSheet(CHANNEL, "Thorin", {"coins": 150}) in effects


def test_even_a_miss_chooses_one(director, registry, dice):
    carouse(director, 100)
    dice.queue(1, 2)
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    (ask,) = [e for e in effects if isinstance(e, SendMessage) and isinstance(e.prompt, ChoicePrompt)]
    assert ask.prompt.count == 1 and "out of hand" in ask.text


def test_supply_rolls_cha(director, registry, dice):
    (ask,) = [e.text for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin",
                                                  move="supply") if isinstance(e, SendMessage)]
    assert ask == "**Thorin** — Supply, roll +Cha +1."
    dice.queue(3, 3)
    (payload,) = [e.payload for e in director.roll(channel_id=CHANNEL, user_id=ALICE)
                  if isinstance(e, PromptModel)]
    assert "pay more or settle" in payload["what_it_means"]
