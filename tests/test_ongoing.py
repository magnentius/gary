"""+1 ongoing: to every move roll, or one move, until the battle ends or the GM ends it."""

from __future__ import annotations

import pytest

from gary.fsm import Director, SendMessage, UpdateSheet

CHANNEL, ALICE = 555, 111
STATS = {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}


@pytest.fixture
def sheets():
    return {"Dorn": {"name": "Dorn", "class": "Fighter", "race": "Dwarf", "level": 6, "xp": 0, "hp": 20,
                     "max_hp": 20, "stats": STATS, "armor": 0, "damage": "d10"}}


@pytest.fixture
def scene():
    return {"Orc 1": {"kind": "Orc", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d8"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)


def ask(director, move="spout_lore", **kw):
    texts = [e.text for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move=move, **kw)
             if isinstance(e, SendMessage)]
    for c in list(director.registry.open_checks(CHANNEL)):
        director.registry.cancel(c.check_id)
    return texts[0]


def test_an_ongoing_bonus_counts_on_every_roll_for_the_battle(director, sheets, scene):
    effects, result = director.give_ongoing(channel_id=CHANNEL, character="dorn", amount=-1,
                                            reason="the orc's war cry rattles him")
    assert result["ok"]
    apply(effects, sheets)
    assert "-1 ongoing (the orc's war cry rattles him)" in ask(director)
    assert "-1 ongoing" in ask(director, "parley")
    scene["Orc 1"]["hp"] = 0  # the battle is over
    assert "ongoing" not in ask(director)


def test_one_that_lasts_until_ended_can_be_narrow(director, sheets):
    effects, _ = director.give_ongoing(channel_id=CHANNEL, character="Dorn", amount=1, reason="the saint's blessing",
                                       until="ended", move="parley")
    apply(effects, sheets)
    assert "+1 ongoing" in ask(director, "parley") and "ongoing" not in ask(director)
    effects, result = director.give_ongoing(channel_id=CHANNEL, character="Dorn", reason="saint", end=True)
    apply(effects, sheets)
    assert result["ended"] == ["the saint's blessing"] and sheets["Dorn"]["ongoing_mods"] == []


def test_a_battle_bonus_needs_a_battle(director, scene):
    scene.clear()
    _, result = director.give_ongoing(channel_id=CHANNEL, character="Dorn", amount=1, reason="x")
    assert "No battle is going on" in result["error"]


def test_through_deaths_eyes_miss_is_minus_one_for_the_battle(director, sheets, dice):
    sheets["Dorn"]["advanced_moves"] = ["Through Death’s Eyes"]
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="through_deaths_eyes")
    dice.queue(1, 2)
    apply(director.roll(channel_id=CHANNEL, user_id=ALICE), sheets)
    assert sheets["Dorn"]["ongoing_mods"][0]["reason"] == "saw their own death"
    assert "-1 ongoing (saw their own death)" in ask(director, "hack_and_slash", target="Orc 1")


def test_camp_clears_the_battles_ongoing(director, sheets, scene):
    sheets["Dorn"]["ongoing_mods"] = [{"amount": -1, "reason": "x", "until": "battle", "battle": ["Orc 1"]},
                                      {"amount": 1, "reason": "y", "until": "ended", "move": ""}]
    scene.clear()
    effects, _ = director.make_camp(channel_id=CHANNEL, reason="a ruined tower")
    apply(effects, sheets)
    assert [o["reason"] for o in sheets["Dorn"]["ongoing_mods"]] == ["y"]
