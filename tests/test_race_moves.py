"""Race moves that change a number Gary runs."""

from __future__ import annotations

import pytest

from gary.fsm import ChangeHp, Director, ReactPrompt, SendMessage, UpdateSheet

CHANNEL, ALICE, BOB = 555, 111, 222
STATS = {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 7}  # Con +1, Cha -2


def sheet(name, cls, race, **more):
    return {"name": name, "class": cls, "race": race, "level": 2, "xp": 0, "hp": 20, "max_hp": 20,
            "stats": STATS, "armor": 0, "damage": "d8", **more}


@pytest.fixture
def sheets():
    return {"Dorn": sheet("Dorn", "Fighter", "Dwarf"), "Ash": sheet("Ash", "Ranger", "Elf")}


@pytest.fixture
def scene():
    return {}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn", BOB: "Ash"}, sheet_for=sheets.get, monsters=lambda: scene)


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp) and e.kind == "character":
            sheets[e.name]["hp"] = e.hp


def test_a_dwarf_fighter_parleys_with_con_over_a_drink(director):
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="parley",
                                      race_move=True))
    assert "+Con +1" in ask


def test_a_race_move_that_does_not_apply_is_refused(director):
    (note,) = director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Ash", move="parley", race_move=True)
    assert "No race move of Ash's (Elf Ranger) applies to Parley" in note.payload["error"]


def test_a_halfling_fighter_and_a_human_thief_take_plus_one(director, sheets):
    sheets["Dorn"]["race"] = "Halfling"
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="defy_danger",
                                      stat="dex", race_move=True))
    assert "+1 Halfling" in ask
    sheets["Ash"].update({"class": "Thief", "race": "Human"})
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Ash", move="spout_lore",
                                      race_move=True))
    assert "+1 Human" in ask


def test_an_elf_ranger_succeeds_at_any_wilderness_job(director, sheets):
    sheets["Ash"].update(gear=[], rations=4)
    effects, result = director.perilous_journey(channel_id=CHANNEL, destination="the Fangs", days=2,
                                                quartermaster="Ash", scout="Dorn", wilderness=True)
    assert result["succeeded_as_10_plus"] == {"quartermaster": "Ash"}
    assert "🌲 **Ash** is at home in the wild: as quartermaster, a 10+ without a roll." in texts(effects)
    assert UpdateSheet(CHANNEL, "Ash", {"rations": 3}) in effects  # 4 - 2 + the saved one
    assert any("Dorn" in t and "roll" in t for t in texts(effects))  # the scout still rolls
    _, result = director.perilous_journey(channel_id=CHANNEL, destination="the mill", days=1, scout="Ash")
    assert "succeeded_as_10_plus" not in result  # roads, not wilderness


def test_a_human_ranger_eats_nothing_camping_in_a_city(director, sheets):
    sheets["Ash"].update(race="Human", gear=[], rations=3)
    effects, result = director.make_camp(channel_id=CHANNEL, reason="a rented loft", dungeon_or_city=True)
    assert result["party"]["Ash"]["ate"] is True and "rations_left" not in result["party"]["Ash"]
    effects, result = director.make_camp(channel_id=CHANNEL, reason="a hollow oak")
    assert result["party"]["Ash"]["rations_left"] == 2


def test_a_human_fighter_rerolls_a_blow_once_per_battle(director, dice, sheets, scene):
    sheets["Dorn"]["race"] = "Human"
    scene["Ogre 1"] = {"kind": "Ogre", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d10"}
    dice.queue(9)
    effects, _ = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 1", character="Ash")
    apply(effects, sheets)
    (hid,) = [e.prompt.hit_id for e in effects if isinstance(e, SendMessage) and isinstance(e.prompt, ReactPrompt)]
    (reroll,) = director.reactions(user_id=ALICE, hit_id=hid)[0]  # Dorn, for his friend
    assert reroll.key == "reroll"
    dice.queue(2)
    effects = director.react(channel_id=CHANNEL, user_id=ALICE, hit_id=hid, key="reroll")
    apply(effects, sheets)
    assert sheets["Ash"]["hp"] == 18
    assert "🎲 **Dorn** rerolls Ogre 1's damage (d10: 2) — now **2 damage** — **Ash** HP 11 → 18." in texts(effects)
    dice.queue(5)
    effects, _ = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 1", character="Ash")
    assert not [e for e in effects if isinstance(e, SendMessage) and e.prompt]  # spent this battle
    scene["Ogre 1"]["hp"] = 0
    scene["Ogre 2"] = {"kind": "Ogre", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d10"}
    dice.queue(5)
    effects, _ = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 2", character="Ash")
    assert [e for e in effects if isinstance(e, SendMessage) and e.prompt]  # a new battle
