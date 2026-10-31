"""Spell Augmentation, Cautious and Impervious Defender."""

from __future__ import annotations

import pytest

from gary.fsm import ChangeHp, Director, PromptModel, SendMessage, UpdateSheet

CHANNEL, ALICE = 555, 111
STATS = {"str": 10, "dex": 16, "con": 13, "int": 16, "wis": 12, "cha": 9}


@pytest.fixture
def sheets():
    return {"Eva": {"name": "Eva", "class": "Wizard", "race": "Human", "level": 6, "xp": 0, "hp": 15, "max_hp": 15,
                    "stats": STATS, "armor": 0, "damage": "d4", "spellbook": ["Invisibility"],
                    "spells": ["Invisibility"], "ongoing": [{"spell": "Invisibility", "target": "Eva"}],
                    "lost": [], "cast_penalty": 0, "advanced_moves": ["Spell Augmentation"]}}


@pytest.fixture
def scene():
    return {"Orc 1": {"kind": "Orc", "hp": 10, "max_hp": 10, "armor": 0, "damage": "d8"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Eva"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets, scene):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp):
            (sheets if e.kind == "character" else scene)[e.name]["hp"] = e.hp


def test_spell_augmentation_shunts_a_spell_into_the_creature_just_hit(director, dice, sheets, scene):
    _, refused = director.spell_augmentation(channel_id=CHANNEL, character="Eva", spell="Invisibility")
    assert "has not just dealt damage" in refused["error"]
    dice.queue(3)
    apply(director.deal_damage(channel_id=CHANNEL, character="Eva", target="Orc 1", reason="x")[0], sheets, scene)
    assert sheets["Eva"]["last_struck"] == "Orc 1"
    effects, result = director.spell_augmentation(channel_id=CHANNEL, character="Eva", spell="invisibility")
    apply(effects, sheets, scene)
    assert result["dealt"] == 1 and scene["Orc 1"]["hp"] == 6 and sheets["Eva"]["ongoing"] == []


def test_cautious_notes_the_extra_hold_even_on_a_miss(director, dice, sheets):
    sheets["Eva"].update({"class": "Thief"}, advanced_moves=["Cautious"])
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="trap_expert")
    dice.queue(1, 1)
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    (told,) = [e.payload for e in effects if isinstance(e, PromptModel)]
    assert told["also"] == ["Cautious: +1 hold, even on a 6-."]


def test_impervious_defender_12_stymies_instead_of_holding(director, dice, sheets):
    sheets["Eva"].update({"class": "Paladin"}, advanced_moves=["Impervious Defender"])
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="defend", target="the gate")
    dice.queue(6, 6)
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    assert any("the nearest attacker is stymied" in e.text for e in effects if isinstance(e, SendMessage))
    assert not [e for e in effects if isinstance(e, UpdateSheet) and "defending" in e.changes]


def test_empowered_magic_offers_its_trade_on_a_10(director, dice, sheets):
    sheets["Eva"]["advanced_moves"] = ["Empowered Magic"]
    sheets["Eva"]["ongoing"] = []
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="cast_a_spell", spell="Invisibility",
                       target="Eva")
    dice.queue(5, 5)
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    (told,) = [e.payload for e in effects if isinstance(e, PromptModel)]
    assert told["also"][0].startswith("Empowered Magic: they may take one of the 7-9 costs to also have "
                                      "effects maximized or targets doubled")
