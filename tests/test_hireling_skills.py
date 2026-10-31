"""The hireling skills Gary runs: Priest, Warrior, Protector, Tracker, Minstrel."""

from __future__ import annotations

import pytest

from gary import trade
from gary.fsm import ChangeHp, Director, SendMessage, UpdateSheet

CHANNEL, ALICE = 555, 111


def hireling(name, **skills):
    return {"name": name, "skills": skills, "loyalty": 1, "cost": "Money", "found_in": "city", "unpaid": False}


DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0, "hp": 10, "max_hp": 20,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
        "armor": 1, "damage": "d10", "gear": [], "rations": 5}


@pytest.fixture
def sheets():
    return {"Dorn": dict(DORN, hirelings=[hireling("Bram", Warrior=3, Protector=2),
                                          hireling("Ysolde", Priest=2, Tracker=4, Minstrel=2)])}


@pytest.fixture
def scene():
    return {"Ogre 1": {"kind": "Ogre", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d10"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn"}, sheet_for=sheets.get, monsters=lambda: scene)


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def only_check(registry):
    (check,) = list(registry.open_checks(CHANNEL))
    return check


def test_a_warrior_adds_their_skill_and_takes_the_counter_attack(director, dice, registry, sheets):
    del sheets["Dorn"]["gear"]  # a sheet without gear is not weapon-gated
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn",
                                      move="hack_and_slash", target="Ogre 1", aide="bram"))
    assert "(+3 damage from Bram)" in ask
    dice.queue(3, 3)  # 6 +2 = 8, weak: damage, and the ogre hits back
    director.roll(channel_id=CHANNEL, user_id=ALICE)
    dice.queue(4)
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id)
    assert "d10: 4, +3 Bram = 7" in texts(effects)[0]
    assert "🛡️ Bram takes the brunt of Ogre 1's counter-attack." in texts(effects)
    assert not [e for e in effects if isinstance(e, ChangeHp) and e.kind == "character"]


def test_a_protector_helping_defy_danger_holds_a_10_to_a_7_9(director, dice):
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn",
                                      move="defy_danger", stat="dex", aide="Bram"))
    assert "(+1 Bram; a 10+ counts as 7–9)" in ask
    dice.queue(6, 5)
    (line,) = texts(director.roll(channel_id=CHANNEL, user_id=ALICE))[:1]
    assert "weak hit. (Bram's help: a 10+ counts as 7–9)" in line


def test_an_aide_must_have_the_skill_for_the_move(director):
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="defy_danger",
                                 stat="dex", aide="Ysolde")
    assert "Ysolde adds nothing to Defy Danger" in note.payload["error"]


def test_a_protector_standing_sentry_adds_armor_then_strains(director, dice, sheets):
    dice.queue(5)
    effects, result = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 1", character="Dorn",
                                               protector="Bram")
    assert result["armor"] == 3  # 1 + Bram's 2
    assert ChangeHp(CHANNEL, "character", "Dorn", 8) in effects  # 5 - 3
    (strain,) = [e for e in effects if isinstance(e, UpdateSheet)]
    assert strain.changes["hirelings"][0]["strain"] == 1


def test_a_priest_tends_the_camp_and_gives_first_aid(director, sheets, scene):
    effects, result = director.first_aid(channel_id=CHANNEL, hireling="ysolde", patient="Dorn")
    assert result["healed"] == 4 and "takes -1 forward" in " ".join(texts(effects))
    scene.clear()
    effects, result = director.make_camp(channel_id=CHANNEL, reason="a barn")
    assert result["party"]["Dorn"]["healed"] == 10  # 20 // 2 + 2, capped at missing HP


def test_a_tracker_guides_a_short_journey(director, sheets, scene):
    scene.clear()
    effects, result = director.perilous_journey(channel_id=CHANNEL, destination="the mill", days=3,
                                                quartermaster="Dorn")
    assert result["guided"] is True
    assert "the journey succeeds without a roll" in effects[0].text
    assert UpdateSheet(CHANNEL, "Dorn", {"rations": 3}) in effects  # 5 - 3 + the saved one


def test_a_minstrel_takes_their_skill_off_prices():
    sheet = {**DORN, "coins": 100, "gear": []}
    _, _, cost = trade.buy(sheet, "long sword", discount=2)  # book 15
    assert cost == 13
