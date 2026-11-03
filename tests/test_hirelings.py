"""Hirelings: made by the book's rules, recruited and ordered with moves."""

from __future__ import annotations

import pytest

from gary import hirelings
from gary.class_moves import render
from gary.fsm import Director, SendMessage, UpdateSheet
from gary.sheets import SheetError

CHANNEL, ALICE = 555, 111
DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0, "hp": 20, "max_hp": 20,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
        "armor": 1, "damage": "d10"}
BRAM = {"name": "Bram", "skills": {"Protector": 3}, "loyalty": 1, "cost": "Money",
        "found_in": "town", "unpaid": False}


@pytest.fixture
def sheets():
    return {"Dorn": dict(DORN)}


@pytest.fixture
def director(registry, clock, dice, sheets):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn"}, sheet_for=sheets.get, monsters=lambda: {})


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def test_a_hireling_is_made_as_the_book_makes_one():
    h = hirelings.make("Bram", {"protector": 3}, 1, "money", "Town")
    assert h == BRAM
    with pytest.raises(SheetError, match="at most 6 points"):
        hirelings.make("Bram", {"Protector": 5}, 2, "Money", "town")
    with pytest.raises(SheetError, match="No hireling skill 'Cook'"):
        hirelings.make("Bram", {"Cook": 2}, 1, "Money", "town")
    with pytest.raises(SheetError, match="cost is one of"):
        hirelings.make("Bram", {"Warrior": 2}, 1, "Beer", "town")
    with pytest.raises(SheetError, match="where they were found"):
        hirelings.make("Bram", {"Warrior": 2}, 1, "Money", "hamlet")


def test_recruit_rolls_plus_one_for_each_offer(director, registry):
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="recruit",
                                      offers=["generous pay", "share", "share"]))
    assert ask == "**Dorn** — Recruit, roll +2 (+1 generous pay, +1 share)."
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="recruit",
                                 offers=["free beer"])
    assert "No Recruit offer 'free beer'" in note.payload["error"]


def test_a_hireling_works_only_for_an_adventurer_of_their_level_or_more(director, sheets):
    """"Generally hirelings only work for adventurers of equal or higher level than their highest skill." """
    _, result = director.hire(channel_id=CHANNEL, character="dorn", name="Bram", skills={"Protector": 3},
                              loyalty=1, cost="Money", found_in="town")
    assert "best skill is rank 3" in result["error"] and "level 1" in result["error"]


def test_hiring_paying_and_losing_one(director, sheets):
    sheets["Dorn"]["level"] = 3
    effects, result = director.hire(channel_id=CHANNEL, character="dorn", name="Bram",
                                    skills={"Protector": 3}, loyalty=1, cost="Money", found_in="town")
    assert result["ok"] and UpdateSheet(CHANNEL, "Dorn", {"hirelings": [BRAM]}) in effects
    sheets["Dorn"]["hirelings"] = [dict(BRAM)]
    assert "already employs Bram" in director.hire(channel_id=CHANNEL, character="Dorn", name="bram",
                                                   skills={"Warrior": 1}, loyalty=0, cost="Money",
                                                   found_in="town")[1]["error"]
    effects, result = director.update_hireling(channel_id=CHANNEL, character="Dorn", name="Bram",
                                               paid=False, reason="a week without coin")
    assert result["hireling"]["unpaid"] is True
    effects, result = director.update_hireling(channel_id=CHANNEL, character="Dorn", name="Bram",
                                               leaves=True, reason="fled the crypt")
    assert UpdateSheet(CHANNEL, "Dorn", {"hirelings": []}) in effects


def test_ordering_a_hireling_rolls_their_loyalty_less_one_unpaid(director, sheets, registry):
    sheets["Dorn"]["hirelings"] = [{**BRAM, "unpaid": True}]
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn",
                                      move="order_hirelings", target="bram"))
    assert ask == "**Dorn** — Order Hirelings: Bram, roll +Loyalty +0 (-1 unpaid)."
    registry.consume(list(registry.open_checks(CHANNEL))[0].check_id)
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn",
                                 move="order_hirelings", target="Sigrid")
    assert "no hireling named 'Sigrid'" in note.payload["error"]


def test_the_model_sees_the_hirelings_and_their_skills_rules():
    sheet = {**DORN, "hirelings": [dict(BRAM)]}
    assert hirelings.describe(sheet) == "Hirelings — Bram (Protector 3; loyalty +1; cost: Money)"
    block = render({"Dorn": sheet})
    assert "Hireling skills at this table" in block and "Protector: Sentry" in block
