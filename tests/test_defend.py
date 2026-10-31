"""Defend: hold, and spending it when an attack comes in."""

from __future__ import annotations

import pytest

from gary.fsm import ChangeHp, ChoicePrompt, Director, PromptModel, SendMessage, UpdateSheet
from gary.sheets import describe

CHANNEL, ALICE, BOB = 555, 111, 222

# CON 15 (+1)
DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 2, "xp": 0,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
        "hp": 25, "max_hp": 25, "armor": 1, "damage": "d10"}
EVA = {"name": "Eva", "class": "Wizard", "race": "Human", "level": 1, "xp": 0,
       "stats": {"str": 8, "dex": 12, "con": 13, "int": 16, "wis": 15, "cha": 9},
       "hp": 10, "max_hp": 17, "armor": 0, "damage": "d4"}


@pytest.fixture
def sheets():
    return {"Dorn": dict(DORN), "Eva": dict(EVA)}


@pytest.fixture
def scene():
    return {"Orc 1": {"kind": "Orc", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d8"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn", BOB: "Eva"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets, scene):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp):
            (sheets if e.kind == "character" else scene)[e.name]["hp"] = e.hp


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def only_check(registry):
    (check,) = list(registry.open_checks(CHANNEL))
    return check


def defend(director, dice, sheets, scene, faces=(5, 5), charge="eva"):
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="defend", target=charge)
    dice.queue(*faces)
    apply(director.roll(channel_id=CHANNEL, user_id=ALICE), sheets, scene)


def test_defending_holds_3_or_1_on_the_charge(director, dice, sheets, scene):
    defend(director, dice, sheets, scene)
    assert sheets["Dorn"]["defending"] == {"charge": "Eva", "hold": 3}
    assert "defending Eva (hold 3)" in describe(sheets["Dorn"])
    defend(director, dice, sheets, scene, faces=(3, 3))  # 6 +1 = 7
    assert sheets["Dorn"]["defending"]["hold"] == 1


def test_defend_needs_a_charge(director):
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="defend")
    assert "needs who or what is defended" in note.payload["error"]


def test_an_attack_on_the_charge_waits_for_the_defender(director, dice, sheets, scene, registry):
    defend(director, dice, sheets, scene)
    dice.queue(7)
    effects, result = director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Eva")
    assert result["held"] is True and not [e for e in effects if isinstance(e, ChangeHp)]
    (ask,) = effects
    assert isinstance(ask.prompt, ChoicePrompt) and ask.prompt.options[0] == "Redirect the attack to me."
    check = only_check(registry)
    assert (check.user_id, check.move) == (ALICE, "defend_charge")
    # Only Dorn's player can spend it.
    assert director.choose(channel_id=CHANNEL, user_id=BOB, check_id=check.check_id, picks=[0]) == []


def test_redirecting_takes_the_blow_onto_the_defender(director, dice, sheets, scene, registry):
    defend(director, dice, sheets, scene)
    dice.queue(7)
    director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Eva")
    effects = director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id, picks=[0])
    assert ChangeHp(CHANNEL, "character", "Dorn", 19) in effects  # 7 - armor 1
    assert not [e for e in effects if isinstance(e, ChangeHp) and e.name == "Eva"]
    assert UpdateSheet(CHANNEL, "Dorn", {"defending": {"charge": "Eva", "hold": 2}}) in effects
    (event,) = [e.payload for e in effects if isinstance(e, PromptModel)]
    assert event["defender_chose"] == "Redirect the attack to me." and event["hold_left"] == 2


def test_halving_and_striking_back(director, dice, sheets, scene, registry):
    defend(director, dice, sheets, scene)
    dice.queue(7)
    director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Eva")
    effects = director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id, picks=[1])
    assert ChangeHp(CHANNEL, "character", "Eva", 7) in effects  # 7 // 2 = 3
    apply(effects, sheets, scene)
    dice.queue(4)
    director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Eva")
    effects = director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id, picks=[3])
    assert ChangeHp(CHANNEL, "monster", "Orc 1", 5) in effects  # level 2 - armor 1
    assert ChangeHp(CHANNEL, "character", "Eva", 3) in effects  # the blow still lands


def test_the_last_hold_ends_the_defense(director, dice, sheets, scene, registry):
    defend(director, dice, sheets, scene, faces=(3, 3))  # hold 1
    dice.queue(2)
    director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Dorn")
    check = only_check(registry)
    assert check.move == "defend_self"  # nothing to redirect
    effects = director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id, picks=[0])
    assert UpdateSheet(CHANNEL, "Dorn", {"defending": None}) in effects
    apply(effects, sheets, scene)
    dice.queue(3)
    effects, result = director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Eva")
    assert "held" not in result  # no hold, no pause


def test_silence_spends_nothing(director, dice, sheets, scene, registry, clock):
    defend(director, dice, sheets, scene)
    dice.queue(5)
    director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Eva")
    clock.advance(500)
    effects = director.sweep()
    assert "**Dorn** didn't spend hold in time." in texts(effects)
    assert ChangeHp(CHANNEL, "character", "Eva", 5) in effects
    assert not [e for e in effects if isinstance(e, UpdateSheet) and "last_hit" not in e.changes]


def test_camp_ends_a_defense(director, dice, sheets, scene):
    defend(director, dice, sheets, scene)
    scene.clear()
    effects, _ = director.make_camp(channel_id=CHANNEL, reason="a barred room")
    assert any(isinstance(e, UpdateSheet) and e.name == "Dorn" and e.changes.get("defending") is None
               and "defending" in e.changes for e in effects)
