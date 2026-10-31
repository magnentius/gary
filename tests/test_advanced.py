"""The advanced moves Gary applies to its own numbers."""

from __future__ import annotations

import pytest

from gary import advanced, spells
from gary.fsm import ChangeHp, Director, SendMessage

CHANNEL, ALICE = 555, 111

DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 3, "xp": 0, "hp": 20, "max_hp": 20,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
        "armor": 1, "damage": "d10"}


@pytest.fixture
def sheets():
    return {"Dorn": dict(DORN)}


@pytest.fixture
def scene():
    return {"Ogre 1": {"kind": "Ogre", "hp": 30, "max_hp": 30, "armor": 0, "damage": "d10"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn"}, sheet_for=sheets.get, monsters=lambda: scene)


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def test_armor_moves_stack_and_the_stronger_replaces_the_weaker():
    sheet = dict(DORN, advanced_moves=["Iron Hide", "Steel Hide", "Serious Underdog"])
    assert advanced.armor_bonus(sheet, outnumbered=False) == (3, ["+2 Steel Hide", "+1 Serious Underdog"])
    assert advanced.armor_bonus(sheet, outnumbered=True)[0] == 4
    assert advanced.armor_bonus(dict(DORN, advanced_moves=["Underdog"]), outnumbered=False)[0] == 0


def test_divine_protection_needs_no_armor_worn():
    cleric = dict(DORN, **{"class": "Cleric"}, advanced_moves=["Divine Protection"])
    assert advanced.armor_bonus(dict(cleric, armor=0), False)[0] == 2
    assert advanced.armor_bonus(cleric, False)[0] == 0


def test_iron_hide_softens_a_monster_blow(director, dice, sheets):
    sheets["Dorn"]["advanced_moves"] = ["Iron Hide"]
    dice.queue(6)
    effects, result = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 1", character="Dorn")
    assert ChangeHp(CHANNEL, "character", "Dorn", 16) in effects  # 6 - (1 worn + 1 Iron Hide)


def test_merciless_adds_a_d4_to_dealt_damage(director, dice, sheets, scene):
    sheets["Dorn"]["advanced_moves"] = ["Merciless"]
    dice.queue(5, 3)
    effects, result = director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Ogre 1",
                                           reason="a shield bash")
    assert any("+1d4 Merciless: 3 = 8" in t for t in texts(effects))


def test_staunch_defender_holds_even_on_a_miss(director, dice, sheets):
    sheets["Dorn"]["advanced_moves"] = ["Staunch Defender"]
    sheets["Dorn"]["class"] = "Paladin"
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="defend", target="the gate")
    dice.queue(1, 2)
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    assert "🛡️ **Dorn** stands in defense of the gate — hold 1." in texts(effects)


def test_serenity_ignores_the_first_ongoing_penalty():
    cleric = {"name": "Thorin", "class": "Cleric", "race": "Human", "level": 3, "cast_penalty": 0,
              "spells": ["Bless"], "ongoing": [{"spell": "Bless", "target": "Dorn"}]}
    assert spells.casting_modifier(cleric)[0] == -1
    assert spells.casting_modifier(dict(cleric, advanced_moves=["Serenity"]))[0] == 0


def test_first_aid_makes_cure_light_wounds_a_rote():
    cleric = {"name": "Thorin", "class": "Cleric", "race": "Human", "level": 3,
              "spells": [], "advanced_moves": ["First Aid"]}
    rote = next(s for s in spells.free_spells(cleric) if s.name == "Cure Light Wounds")
    assert rote.free and "Cure Light Wounds" not in [s.name for s in spells.preparable(cleric)]


def test_seeing_red_adds_one_to_discern_in_combat(director):
    director._sheet_for("Dorn")["advanced_moves"] = ["Seeing Red"]
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn",
                                      move="discern_realities"))
    assert "+1 Seeing Red" in ask


def test_a_druids_dangerous_form_grows_its_die():
    druid = {"shape": {"form": "bear"}, "advanced_moves": ["Red of Tooth and Claw"]}
    assert advanced.shaped_die(druid, "d6") == "d8"
    assert advanced.shaped_die({**druid, "shape": None}, "d6") == "d6"


def test_healers_add_their_moves_only_when_healing_others():
    paladin = {"level": 4, "advanced_moves": ["Hospitaller", "Devoted Healer"]}
    assert advanced.healing_dice(paladin, "lay_on_hands", "", other=True) == [
        ("1d8", "Hospitaller"), ("4", "Devoted Healer")]
    assert advanced.healing_dice(paladin, "lay_on_hands", "", other=False) == []


def test_the_model_is_told_what_gary_already_applies():
    from gary import class_moves

    sheet = dict(DORN, advanced_moves=["Merciless", "Heirloom"])
    text = class_moves.render({"Dorn": sheet})
    assert "Merciless (advanced): When you deal damage, deal +1d4 damage. [Gary applies the extra damage" in text
    assert "Heirloom (advanced)" in text and "Heirloom" not in advanced.APPLIED
    assert advanced.applied({"class": "Paladin"}, "Divine Protection") == ""
