"""From the guide: packs of attackers, damage without a roll, and how the GM plays."""

from __future__ import annotations

import pytest

from gary.context import DM_SYSTEM_INSTRUCTION
from gary.fsm import ChangeHp, Director, SendMessage
from gary.gear import Item

CHANNEL, ALICE = 555, 111
DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0, "hp": 25, "max_hp": 25,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
        "armor": 1, "damage": "d10", "gear": [Item("Long Sword", tags=("close", "+1 damage"), damage=1).to_dict()]}


@pytest.fixture
def scene():
    return {"Goblin 1": {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"},
            "Goblin 2": {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"},
            "Gnoll 1": {"kind": "Gnoll", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d8"}}


@pytest.fixture
def sheets():
    return {"Dorn": dict(DORN)}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn"}, sheet_for=sheets.get, monsters=lambda: scene)


def test_a_pack_rolls_the_highest_damage_plus_one_per_extra_attacker(director, dice):
    dice.queue(5)  # the gnoll's d8 once, +2 for the two goblins
    effects, result = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Dorn",
                                               also=["goblin 2", "Gnoll 1"])
    texts = [e.text for e in effects if isinstance(e, SendMessage)]
    assert texts[0] == "Goblin 1, Goblin 2 and Gnoll 1 attack together — Gnoll 1's d8, +2 for the 2 others."
    assert ChangeHp(CHANNEL, "character", "Dorn", 19) in effects  # 5 + 2 - armor 1
    assert result["attackers"] == ["Goblin 1", "Goblin 2", "Gnoll 1"]


def test_a_pack_member_must_be_standing(director, scene):
    scene["Goblin 2"]["hp"] = 0
    _, result = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Dorn",
                                         also=["Goblin 2"])
    assert "No standing creature named 'Goblin 2'" in result["error"]


def test_a_helpless_enemy_just_takes_the_damage(director, dice):
    dice.queue(5)  # d10 5 +1 sword = 6, armor 1
    effects, result = director.deal_damage(channel_id=CHANNEL, character="dorn", target="Gnoll 1",
                                           reason="the gnoll is pinned under the cart")
    assert ChangeHp(CHANNEL, "monster", "Gnoll 1", 1) in effects
    (line,) = [e.text for e in effects if isinstance(e, SendMessage)]
    assert line.startswith("🗡️ **Dorn** strikes with Long Sword — the gnoll is pinned under the cart. "
                           "No roll needed: d10+1: 5 +1 = 6")
    assert result["dealt"] == 5


def test_no_roll_still_needs_a_reason_and_a_weapon_that_reaches(director, sheets):
    assert "reason" in director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Gnoll 1")[1]["error"]
    _, result = director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Gnoll 1",
                                     ranged=True, reason="unaware")
    assert "near, far or thrown" in result["error"]


def test_the_gm_is_told_how_to_play():
    text = DM_SYSTEM_INSTRUCTION
    assert "Set up a threat, never its conclusion" in text
    assert "A 7-9 is a success with a cost" in text
    assert "deal_damage, not hack_and_slash" in text
    assert "Spread the spotlight" in text
    assert "Never speak the name of your move" in text
    assert "quest status" not in text
