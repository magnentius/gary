"""Encumbrance: weight against load, -1 past it, and a choice past load+2."""

from __future__ import annotations

import pytest

from gary.fsm import Director, OverloadPrompt, PromptModel, RecordOutcome, RollPrompt, SendMessage, UpdateSheet
from gary.gear import Item
from gary.sheets import carried, describe, encumbrance, load

CHANNEL, ALICE = 555, 111


def rock(n):
    return Item(f"Rock {n}", weight=1).to_dict()


def wizard(weight):
    # Wizard load 7 + Str -1 (STR 8) = 6
    return {"name": "Eva", "class": "Wizard", "race": "Human", "level": 1, "xp": 0, "hp": 10, "max_hp": 10,
            "stats": {"str": 8, "dex": 12, "con": 12, "int": 16, "wis": 12, "cha": 12},
            "armor": 0, "damage": "d4", "gear": [rock(i) for i in range(weight)], "coins": 0}


@pytest.fixture
def sheets():
    return {"Eva": wizard(6)}


@pytest.fixture
def director(registry, clock, dice, sheets):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Eva"}, sheet_for=sheets.get, monsters=lambda: {})


def ask(director):
    return [e for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva",
                                          move="spout_lore") if isinstance(e, SendMessage)][-1]


def test_load_is_the_class_base_plus_str_and_coin_weighs():
    assert load(wizard(0)) == 6
    assert carried({**wizard(3), "coins": 250}) == 5
    assert [encumbrance(wizard(n)) for n in (6, 7, 8, 9)] == ["fine", "burdened", "burdened", "overloaded"]
    assert "weight 7/6 (burdened, -1)" in describe(wizard(7))
    assert encumbrance({"name": "Old", "class": "Wizard"}) == "fine"  # no gear, nothing carried


def test_at_load_no_cost(director):
    assert isinstance(ask(director).prompt, RollPrompt)
    assert "encumbered" not in ask(director).text


def test_one_or_two_over_costs_one(director, sheets, registry):
    sheets["Eva"] = wizard(8)
    sent = ask(director)
    assert sent.text == "**Eva** — Spout Lore, roll +Int +1 (-1 encumbered)."
    assert list(registry.open_checks(CHANNEL))[0].modifier == 1


def test_more_than_two_over_asks_drop_or_fail(director, sheets, registry, dice):
    sheets["Eva"] = wizard(9)
    sent = ask(director)
    assert isinstance(sent.prompt, OverloadPrompt) and len(sent.prompt.items) == 9
    assert "drop something and roll, or fail" in sent.text


def test_dropping_an_item_rolls_at_minus_one(director, sheets, registry, dice):
    sheets["Eva"] = wizard(9)
    ask(director)
    check = list(registry.open_checks(CHANNEL))[0]
    dice.queue(4, 4)
    effects = director.drop_and_roll(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id, index=0)
    assert [e.text for e in effects if isinstance(e, SendMessage)][0] == "**Eva** drops Rock 0."
    (update,) = [e for e in effects if isinstance(e, UpdateSheet)]
    assert len(update.changes["gear"]) == 8
    assert "4 + 4 +1 = 9" in [e.text for e in effects if isinstance(e, SendMessage)][1]


def test_failing_is_a_miss_with_no_dice_and_no_xp(director, sheets, registry):
    sheets["Eva"] = wizard(9)
    ask(director)
    check = list(registry.open_checks(CHANNEL))[0]
    effects = director.fail_overloaded(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id)
    (record,) = [e for e in effects if isinstance(e, RecordOutcome)]
    assert record.outcome.band == "miss"
    assert not [e for e in effects if isinstance(e, UpdateSheet)]  # no XP
    (payload,) = [e.payload for e in effects if isinstance(e, PromptModel)]
    assert "chose to fail" in payload["note"]
    assert list(registry.open_checks(CHANNEL)) == []


def test_someone_else_cannot_choose(director, sheets, registry):
    sheets["Eva"] = wizard(9)
    ask(director)
    check = list(registry.open_checks(CHANNEL))[0]
    assert director.fail_overloaded(channel_id=CHANNEL, user_id=999, check_id=check.check_id) == []


def test_the_buttons():
    import asyncio

    from gary.adapters.discord_bot import overload_view

    async def run():
        view = overload_view(OverloadPrompt("01ABC", ((0, "Rock"), (3, "Plate"))))
        return [c.custom_id for c in view.children]

    assert asyncio.run(run()) == ["gary:drop:01ABC:0", "gary:drop:01ABC:3", "gary:fail:01ABC"]
