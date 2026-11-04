"""Captives: a creature that surrenders or is bound stays in the scene but is out of the fight.

Live, in the delve playtest: a goblin chained to a rack counted as "still standing", so the party
couldn't make camp until the GM removed him from a scene he was still in.
"""

from __future__ import annotations

import pytest

from gary.fsm import ConditionMonster, Director, SendMessage, UpdateSheet, battle_over, fighting

CHANNEL, ALICE = 555, 111
DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0, "hp": 25, "max_hp": 25,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 1, "damage": "d10",
        "gear": [], "ongoing": [{"spell": "Bless", "target": "Dorn"}]}


@pytest.fixture
def scene():
    return {"Snig": {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"}}


@pytest.fixture
def director(registry, clock, dice, scene):
    sheets = {"Dorn": dict(DORN)}
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn"}, sheet_for=sheets.get, monsters=lambda: scene)


def _apply(scene, effects):
    for e in effects:
        if isinstance(e, ConditionMonster):
            scene[e.name] = {**scene[e.name], "conditions": list(e.conditions)}


def test_a_captive_stays_but_does_not_stop_the_party_resting(director, scene):
    assert "Snig" in director.unsettled(CHANNEL, "make camp")
    effects, result = director.take_captive(channel_id=CHANNEL, creature="snig", reason="chained to a tool rack")
    assert result == {"ok": True, "captive": "Snig"}
    assert "⛓️ **Snig** is a captive — chained to a tool rack." in [e.text for e in effects if isinstance(e, SendMessage)]
    _apply(scene, effects)
    assert "Snig" in scene and not fighting(scene["Snig"])
    assert director.unsettled(CHANNEL, "make camp") == ""


def test_the_last_foe_taken_captive_ends_the_battle(director, scene):
    effects, _ = director.take_captive(channel_id=CHANNEL, creature="Snig", reason="he throws down his knife")
    assert UpdateSheet(CHANNEL, "Dorn", {"ongoing": []}) in effects  # Bless: "so long as battle continues"
    _apply(scene, effects)
    assert battle_over({"monsters": scene, "sheets": {}}, CHANNEL) == []  # nothing left to end


def test_a_captive_cannot_attack_and_slipping_free_ends_it(director, scene):
    _apply(scene, director.take_captive(channel_id=CHANNEL, creature="Snig", reason="tied up")[0])
    _, refused = director.monster_attacks(channel_id=CHANNEL, monster="Snig", character="Dorn")
    assert "captive" in refused["error"]
    effects, freed = director.free_creature(channel_id=CHANNEL, creature="Snig", reason="he gnaws through the rope")
    assert freed["ok"] and ConditionMonster(CHANNEL, "Snig", ()) in effects


def test_taking_captive_needs_a_creature_and_a_reason(director):
    assert "No creature" in director.take_captive(channel_id=CHANNEL, creature="Ogre", reason="x")[1]["error"]
    assert "reason" in director.take_captive(channel_id=CHANNEL, creature="Snig")[1]["error"]
