"""Cover, stun, Cloudkill, Consume Unlife, Dwarven Hardtack, and poisons anyone can use."""

from __future__ import annotations

import pytest

from gary import trade
from gary.fsm import AfflictMonster, ChangeHp, Director, SendMessage, UpdateSheet

CHANNEL, ALICE = 555, 111
STATS = {"str": 16, "dex": 12, "con": 15, "int": 8, "wis": 16, "cha": 13}


@pytest.fixture
def sheets():
    return {"Dorn": {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 9, "xp": 0, "hp": 10,
                     "max_hp": 20, "stats": STATS, "armor": 1, "damage": "d10"}}


@pytest.fixture
def scene():
    return {"Orc 1": {"kind": "Orc", "hp": 10, "max_hp": 10, "armor": 1, "damage": "d8"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets, scene):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp):
            (sheets if e.kind == "character" else scene)[e.name]["hp"] = e.hp
        elif isinstance(e, AfflictMonster):
            scene[e.name]["afflictions"] = list(e.afflictions)


def test_cover_adds_armor_against_a_creatures_blow(director, dice):
    dice.queue(5)
    effects, result = director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Dorn", cover=2)
    assert result["armor"] == 3 and ChangeHp(CHANNEL, "character", "Dorn", 8) in effects


def test_a_stunned_character_can_only_defy_danger(director, sheets, scene):
    apply(director.stun(channel_id=CHANNEL, character="Dorn", reason="a club to the temple")[0], sheets, scene)
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="hack_and_slash",
                                 target="Orc 1")
    assert "Dorn is stunned (a club to the temple)" in note.payload["error"]
    assert any(isinstance(e, SendMessage) for e in director.open_move(
        channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="defy_danger", stat="con"))
    apply(director.stun(channel_id=CHANNEL, character="Dorn", end=True)[0], sheets, scene)
    assert sheets["Dorn"]["stunned"] is None


def test_cloudkill_adds_a_separate_d6_to_every_wound(director, dice, sheets, scene):
    sheets["Eva"] = {"name": "Eva", "class": "Wizard", "race": "Human", "level": 9, "hp": 20, "max_hp": 20,
                     "stats": STATS, "armor": 0, "damage": "d4", "spellbook": ["Cloudkill"],
                     "ongoing": [{"spell": "Cloudkill", "target": "the hall"}], "spells": [], "lost": []}
    director.players[222] = "Eva"
    dice.queue(5, 4)  # d10 5 - armor 1 = 4, + Cloudkill 4
    effects, result = director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Orc 1", reason="x")
    assert result["dealt"] == 8 and any("+4 Cloudkill" in e.text for e in effects if isinstance(e, SendMessage))


def test_consume_unlife_destroys_and_heals_by_what_was_left(director, dice, sheets, scene):
    sheets["Dorn"].update({"class": "Cleric"}, spells=["Consume Unlife"], lost=[], cast_penalty=0)
    scene["Ghoul 1"] = {"kind": "Ghoul", "hp": 7, "max_hp": 10, "armor": 1, "damage": "d6"}
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="cast_a_spell",
                       spell="Consume Unlife", target="Ghoul 1")
    dice.queue(6, 6)
    apply(director.roll(channel_id=CHANNEL, user_id=ALICE), sheets, scene)
    assert scene["Ghoul 1"]["hp"] == 0 and sheets["Dorn"]["hp"] == 17


def test_dwarven_hardtack_feeds_only_dwarves():
    human = {"name": "Dorn", "race": "Human", "gear": [], "rations": 0, "coins": 0, "ammo": 0}
    fed, _ = trade.add_items(human, ["dwarven hardtack"])
    assert fed["rations"] == 0
    dwarf, _ = trade.add_items({**human, "race": "Dwarf"}, ["dwarven hardtack"])
    assert dwarf["rations"] == 7


def test_anyone_can_use_a_carried_poison_and_serpents_tears_sticks(director, dice, sheets, scene):
    sheets["Dorn"]["gear"] = [{"name": "Serpent’s Tears", "tags": ["Dangerous", "touch"], "weight": 0}]
    effects, result = director.use_poison(channel_id=CHANNEL, character="Dorn", target="Orc 1",
                                          poison="serpent's tears")
    apply(effects, sheets, scene)
    assert result["ok"] and "Dangerous" in result["note"] and sheets["Dorn"]["gear"] == []
    assert scene["Orc 1"]["afflictions"] == ["Serpent’s Tears"]
    del sheets["Dorn"]["gear"]
    dice.queue(2, 7)  # rolled twice, the better kept
    effects, result = director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Orc 1", reason="x")
    assert result["rolled"] == 7


def test_bloodweed_weakens_the_poisoned_creatures_blows(director, dice, scene):
    scene["Orc 1"]["afflictions"] = ["Bloodweed"]
    dice.queue(6, 2)  # d8 6 - d4 2 = 4, armor 1
    effects, result = director.monster_attacks(channel_id=CHANNEL, monster="Orc 1", character="Dorn")
    assert result["dealt"] == 3


async def test_antitoxin_is_drunk_and_spent(clock):
    from gary.adapters import gemini_tools as T
    from gary.models import Mutation, MutationKind
    from gary.ports import ScriptedDice
    from gary.registry import PendingCheckRegistry
    from gary.state.backend import InMemoryStateBackend
    from gary.state.history import InMemoryHistoryBackend
    from gary.state.store import CampaignStore
    from tests.test_gemini import CHANNEL as ROOM, call_response, narrator, player, text_response

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={ROOM},
                        dice=ScriptedDice())
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": {
        "name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "hp": 10, "max_hp": 10, "stats": STATS,
        "armor": 0, "damage": "d10", "gear": [{"name": "Antitoxin", "tags": [], "weight": 0}], "coins": 0,
        "rations": 0, "ammo": 0}})], reason="setup")
    table = (director, store, InMemoryHistoryBackend())
    n = narrator(table, call_response(T.USE_ITEM, {"actor": "Dorn", "item": "antitoxin"}), text_response("Better."))
    await player(n, "I drink the antitoxin")
    assert store.state()["sheets"]["Dorn"]["gear"] == []
    answer = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    assert "cured of it" in answer["note"]  # no poison on the sheet: whatever was narrated

    sheet = store.state()["sheets"]["Dorn"]
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": {
        **sheet, "gear": [{"name": "Antitoxin", "tags": [], "weight": 0}],
        "afflictions": ["Bloodweed", "Serpent\u2019s Tears"]}})], reason="poisoned")
    n = narrator(table, call_response(T.USE_ITEM, {"actor": "Dorn", "item": "antitoxin", "cure": "serpent's tears"}),
                 text_response("Better."))
    await player(n, "I drink the antitoxin for the tears")
    assert store.state()["sheets"]["Dorn"]["afflictions"] == ["Bloodweed"]
