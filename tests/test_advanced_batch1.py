"""Advanced moves that set up the next blow: armor forward, marked foes, an ally's strike."""

from __future__ import annotations

import pytest

from gary.fsm import ChangeHp, Director, SendMessage, UpdateSheet

CHANNEL, ALICE, BOB = 555, 111, 222
STATS = {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 16}


def sheet(name, cls, moves=(), **more):
    return {"name": name, "class": cls, "race": "Dwarf", "level": 6, "xp": 0, "hp": 20, "max_hp": 20,
            "stats": STATS, "armor": 0, "damage": "d8", "advanced_moves": list(moves), **more}


@pytest.fixture
def sheets():
    return {"Dorn": sheet("Dorn", "Fighter"), "Anna": sheet("Anna", "Paladin")}


@pytest.fixture
def scene():
    return {"Ogre 1": {"kind": "Ogre", "hp": 30, "max_hp": 30, "armor": 0, "damage": "d10"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn", BOB: "Anna"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp) and e.kind == "character":
            sheets[e.name]["hp"] = e.hp


def hack(director, dice, sheets, user, faces, damage_faces=(), **kw):
    actor = "Dorn" if user == ALICE else "Anna"
    director.open_move(channel_id=CHANNEL, user_id=user, actor=actor, move="hack_and_slash", target="Ogre 1", **kw)
    dice.queue(*faces)
    effects = director.roll(channel_id=CHANNEL, user_id=user)
    apply(effects, sheets)
    check = next((c for c in director.registry.open_checks(CHANNEL) if c.user_id == user), None)
    if check is not None and damage_faces:
        dice.queue(*damage_faces)
        more = director.roll_damage(channel_id=CHANNEL, user_id=user, check_id=check.check_id)
        apply(more, sheets)
        effects += more
    return effects


def test_duelists_parry_braces_against_the_next_blow(director, dice, sheets):
    sheets["Dorn"].update({"class": "Bard"}, advanced_moves=["Duelist’s Parry"])
    hack(director, dice, sheets, ALICE, (1, 1))  # even a miss
    assert sheets["Dorn"]["armor_forward"] == [{"amount": 1, "reason": "Duelist's Parry"}]
    dice.queue(5)
    effects, result = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 1", character="Dorn")
    apply(effects, sheets)
    assert result["armor"] == 1 and sheets["Dorn"]["hp"] == 16 and sheets["Dorn"]["armor_forward"] == []


def test_scent_of_blood_marks_the_foe_for_the_next_attack(director, dice, sheets):
    sheets["Dorn"]["advanced_moves"] = ["Scent of Blood"]
    hack(director, dice, sheets, ALICE, (1, 1))
    assert sheets["Dorn"]["marks"] == [{"target": "Ogre 1", "damage": "1d4", "reason": "Scent of Blood"}]
    dice.queue(4, 3)  # d8 4 +1d4 3
    effects, result = director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Ogre 1", reason="x")
    apply(effects, sheets)
    assert result["rolled"] == 7
    assert any("+1d4 Scent of Blood: 3" in e.text for e in effects if isinstance(e, SendMessage))
    assert sheets["Dorn"]["marks"] == []  # spent


def test_tandem_strike_sets_up_an_ally(director, dice, sheets):
    sheets["Anna"]["advanced_moves"] = ["Tandem Strike"]
    hack(director, dice, sheets, BOB, (1, 1), ally="dorn")
    assert sheets["Dorn"]["marks"][0]["reason"] == "Anna's Tandem Strike"
    assert sheets["Dorn"]["forwards"][0]["reason"] == "against Ogre 1 (Anna's Tandem Strike)"


def test_ever_onward_leads_the_charge(director, sheets):
    sheets["Anna"]["advanced_moves"] = ["Ever Onward"]
    effects, result = director.lead_the_charge(channel_id=CHANNEL, character="Anna", led=["Dorn"])
    apply(effects, sheets)
    assert sheets["Dorn"]["forwards"][0]["amount"] == 1 and sheets["Dorn"]["armor_forward"][0]["amount"] == 2
    _, refused = director.lead_the_charge(channel_id=CHANNEL, character="Dorn", led=["Anna"])
    assert "neither Charge! nor Ever Onward" in refused["error"]


def test_superior_warrior_avoids_the_counter_attack_on_12(director, dice, sheets):
    sheets["Dorn"]["advanced_moves"] = ["Superior Warrior"]
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="hack_and_slash", target="Ogre 1")
    dice.queue(6, 6)  # 12 +2 = 14
    director.roll(channel_id=CHANNEL, user_id=ALICE)
    check = next(iter(director.registry.open_checks(CHANNEL)))
    effects = director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id, picks=[1])  # +1d6, exposed
    check = next(iter(director.registry.open_checks(CHANNEL)))
    dice.queue(4, 3)
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id)
    assert not [e for e in effects if isinstance(e, ChangeHp) and e.kind == "character"]


def test_invigorate_gives_the_healed_plus_two_damage(director, dice, sheets):
    sheets["Anna"].update(advanced_moves=["Invigorate"])
    director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Anna", move="lay_on_hands", target="Dorn")
    dice.queue(6, 6, 5)
    effects = director.roll(channel_id=CHANNEL, user_id=BOB)
    apply(effects, sheets)
    assert sheets["Dorn"]["forwards"] == [{"damage": "2", "reason": "Anna's Invigorate"}]


def test_indomitable_answers_any_debility(director, sheets):
    sheets["Anna"]["advanced_moves"] = ["Indomitable"]
    effects, _ = director.debility(channel_id=CHANNEL, character="Anna", debility="weak", cause="the ghoul's touch")
    apply(effects, sheets)
    assert sheets["Anna"]["forwards"][0]["reason"] == "against the ghoul's touch (Indomitable)"


def test_vicious_cacophony_adds_a_d4_to_arcane_arts_damage(director, dice, sheets):
    sheets["Anna"].update({"class": "Bard"}, advanced_moves=["Vicious Cacophony"])
    director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Anna", move="arcane_art", target="Dorn")
    dice.queue(6, 6)
    director.roll(channel_id=CHANNEL, user_id=BOB)
    check = next(iter(director.registry.open_checks(CHANNEL)))
    apply(director.choose(channel_id=CHANNEL, user_id=BOB, check_id=check.check_id, picks=[1]), sheets)
    assert sheets["Dorn"]["forwards"] == [{"damage": "2d4", "reason": "Arcane Art"}]
