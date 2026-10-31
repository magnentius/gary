"""The Paladin's Quest: recorded by the model, applied by Gary while it lasts."""

from __future__ import annotations

import pytest

from gary import advanced, quests, sheets as sheet_lib
from gary.fsm import Director, UpdateSheet
from gary.sheets import SheetError

CHANNEL, ALICE = 555, 111

ANNA = {"name": "Anna", "class": "Paladin", "race": "Human", "level": 3, "xp": 0, "hp": 20, "max_hp": 20,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 0,
        "damage": "d10", "gear": [], "rations": 3}


@pytest.fixture
def sheets():
    return {"Anna": dict(ANNA)}


@pytest.fixture
def director(registry, clock, dice, sheets):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Anna"}, sheet_for=sheets.get, monsters=lambda: {})


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)


def test_a_quest_is_shaped_like_the_book():
    quest = quests.make("slay", "the Wyrm of Hollowmere",
                        [{"boon": "invulnerability", "detail": "fire"}, {"boon": "sustenance"}], ["valor", "Truth"])
    assert quest["text"] == "Slay the Wyrm of Hollowmere, a great blight on the land"
    assert [b["text"] for b in quest["boons"]] == ["Invulnerability to fire",
                                                    "A freedom from hunger, thirst, and sleep"]
    with pytest.raises(SheetError, match="Up to 2 boons"):
        quests.make("slay", "x", [{"boon": "truth"}, {"boon": "tongues"}, {"boon": "authority"}], ["honor"])
    with pytest.raises(SheetError, match="Fill in the blank"):
        quests.make("slay", "x", [{"boon": "direction"}], ["honor"])
    with pytest.raises(SheetError, match="at least one vow"):
        quests.make("defend", "the village", [], [])


def test_start_and_end_a_quest(director, sheets):
    effects, result = director.start_quest(channel_id=CHANNEL, character="anna", mission="defend",
                                           subject="the orphans of Fenwick", boons=[{"boon": "truth"}],
                                           vows=["hospitality"])
    assert result["ok"]
    apply(effects, sheets)
    assert effects[0].text.startswith("⚜️ **Anna** sets out on a quest: Defend the orphans of Fenwick")
    assert "On a quest — Defend the orphans of Fenwick" in sheet_lib.describe(sheets["Anna"])
    _, again = director.start_quest(channel_id=CHANNEL, character="Anna", mission="slay", subject="x", vows=["honor"])
    assert "already on a quest" in again["error"]
    effects, result = director.end_quest(channel_id=CHANNEL, character="Anna", how="broken", reason="she lied")
    apply(effects, sheets)
    assert sheets["Anna"]["quest"] is None
    assert "breaks a vow — the blessing leaves them" in effects[0].text


def test_only_a_paladin_quests(director, sheets):
    sheets["Anna"]["class"] = "Fighter"
    _, result = director.start_quest(channel_id=CHANNEL, character="Anna", mission="slay", subject="x",
                                     vows=["honor"])
    assert "Quest is a Paladin's move" in result["error"]


def test_smite_and_protection_only_count_on_a_quest():
    paladin = dict(ANNA, advanced_moves=["Smite", "Holy Protection"])
    assert advanced.damage_dice(paladin, "hack_and_slash", ()) == []
    assert advanced.armor_bonus(paladin, False)[0] == 0
    paladin["quest"] = quests.make("slay", "the wyrm", [], ["valor"])
    assert advanced.damage_dice(paladin, "hack_and_slash", ()) == [("1d4", "Smite")]
    assert advanced.armor_bonus(paladin, False) == (1, ["+1 Holy Protection"])


def test_freedom_from_hunger_skips_rations(director, sheets):
    sheets["Anna"]["quest"] = quests.make("discover", "the drowned bell", [{"boon": "sustenance"}], ["piety"])
    _, result = director.make_camp(channel_id=CHANNEL, reason="a chapel")
    assert "rations_left" not in result["party"]["Anna"]
