"""Ranger and Druid advanced moves: Smaug's Belly, Viper's Strike, Blot Out the Sun,
Unnatural Ally, Half-Elven, Formshaper, Formcrafter, Balance."""

from __future__ import annotations

import pytest

from gary import advanced
from gary.fsm import ChangeHp, Director, SendMessage, SpendAmmo, UpdateSheet
from gary.levelling import Advance

CHANNEL, ALICE, BOB = 555, 111, 222
STATS = {"str": 12, "dex": 16, "con": 13, "int": 9, "wis": 15, "cha": 8}
BOW = {"name": "Hunter's bow", "tags": ["near", "far"], "armor": 0, "shield": 0, "damage": 0, "piercing": 0,
       "ammo": 0, "needs_ammo": True, "weight": 1}
SWORD = {"name": "Short sword", "tags": ["close"], "armor": 0, "shield": 0, "damage": 0, "piercing": 0,
         "ammo": 0, "needs_ammo": False, "weight": 1}
DAGGER = {"name": "Dagger", "tags": ["hand"], "armor": 0, "shield": 0, "damage": 0, "piercing": 0,
          "ammo": 0, "needs_ammo": False, "weight": 1}


def ranger(**more):
    return {"name": "Rhea", "class": "Ranger", "race": "Elf", "level": 6, "xp": 0, "hp": 15, "max_hp": 15,
            "stats": STATS, "armor": 1, "damage": "d8", "gear": [BOW, SWORD, DAGGER], "ammo": 3, **more}


@pytest.fixture
def sheets():
    return {"Rhea": ranger(), "Moss": {**ranger(), "name": "Moss", "class": "Druid", "gear": [], "damage": "d6"}}


@pytest.fixture
def scene():
    return {n: {"kind": "Orc", "hp": 10, "max_hp": 10, "armor": 2, "damage": "d8"} for n in ("Orc 1", "Orc 2", "Orc 3")}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Rhea", BOB: "Moss"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets, scene=None):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp):
            (sheets if e.kind == "character" else scene)[e.name]["hp"] = e.hp


def land(director, dice, user, faces, damage_faces, **kw):
    effects = director.open_move(channel_id=CHANNEL, user_id=user, **kw)
    dice.queue(*faces)
    effects += director.roll(channel_id=CHANNEL, user_id=user)
    check = next(c for c in director.registry.open_checks(CHANNEL) if c.user_id == user)
    if check.picks == () and MOVE_PICK.get(kw["move"]) is not None:
        effects += director.choose(channel_id=CHANNEL, user_id=user, check_id=check.check_id,
                                   picks=[MOVE_PICK[kw["move"]]])
        check = next(c for c in director.registry.open_checks(CHANNEL) if c.user_id == user)
    dice.queue(*damage_faces)
    return effects + director.roll_damage(channel_id=CHANNEL, user_id=user, check_id=check.check_id)


MOVE_PICK = {"volley": None, "hack_and_slash": 0}  # a 10+ Hack and Slash asks for a pick first


def test_smaugs_belly_gives_arrows_two_piercing(director, dice, sheets, scene):
    sheets["Rhea"]["advanced_moves"] = ["Smaug’s Belly"]
    effects = land(director, dice, ALICE, (6, 6), (5,), actor="Rhea", move="volley", target="Orc 1", weak_point=True)
    assert any("5 damage" in e.text for e in effects if isinstance(e, SendMessage))  # armor 2 pierced


def test_vipers_strike_adds_the_off_hand(director, dice, sheets):
    sheets["Rhea"]["advanced_moves"] = ["Viper’s Strike"]
    effects = land(director, dice, ALICE, (6, 6), (4, 3), actor="Rhea", move="hack_and_slash", target="Orc 1",
                   two_weapons=True)
    assert any("+1d4 off-hand: 3" in e.text for e in effects if isinstance(e, SendMessage))


def test_two_weapons_need_two_weapons(director, sheets):
    sheets["Rhea"].update(advanced_moves=["Viper’s Strike"], gear=[SWORD])
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Rhea", move="hack_and_slash",
                                 target="Orc 1", two_weapons=True)
    assert "does not carry two hand or close weapons" in note.payload["error"]


def test_blot_out_the_sun_hits_extra_targets_for_ammo(director, dice, sheets, scene):
    sheets["Rhea"]["advanced_moves"] = ["Blot Out the Sun"]
    effects = land(director, dice, ALICE, (6, 6), (7,), actor="Rhea", move="volley", target="Orc 1",
                   extra_targets=["orc 2", "Orc 3"])
    apply(effects, sheets, scene)
    assert SpendAmmo(CHANNEL, "Rhea", 1) in effects  # 3 - 2 extra
    assert [scene[n]["hp"] for n in ("Orc 1", "Orc 2", "Orc 3")] == [5, 5, 5]


def test_unnatural_ally_and_half_elven(sheets):
    rhea = {**sheets["Rhea"], "level": 5, "xp": 12,
            "companion": {"name": "Ash", "ferocity": 2, "cunning": 1, "armor": 1, "instinct": 1}}
    a = Advance(rhea)
    a.choose_stat("dex")
    a.choose_move("Unnatural Ally")
    pet = a.result()["companion"]
    assert (pet["ferocity"], pet["instinct"], pet["monster"]) == (4, 2, True)
    from gary.fsm import _race_move_of

    assert _race_move_of({**rhea, "race": "Elf", "advanced_moves": ["Half-Elven"]}, "Human")
    assert not _race_move_of({**rhea, "race": "Elf"}, "Human")


def test_formshaper_and_formcrafter_ride_on_the_shape(director, dice, sheets):
    sheets["Moss"]["advanced_moves"] = ["Formshaper", "Formcrafter"]
    director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Moss", move="shapeshifter", target="bear",
                       form_boon="armor", form_plus="str", form_minus="int")
    dice.queue(6, 6)
    apply(director.roll(channel_id=CHANNEL, user_id=BOB), sheets)
    shape = sheets["Moss"]["shape"]
    assert (shape["boon"], shape["plus"], shape["minus"]) == ("armor", "str", "int")
    assert advanced.armor_bonus(sheets["Moss"], False) == (1, ["+1 Formshaper"])
    for c in list(director.registry.open_checks(CHANNEL)):
        director.registry.cancel(c.check_id)
    (ask,) = [e.text for e in director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Moss", move="spout_lore")
              if isinstance(e, SendMessage)]
    assert "-1 bear form" in ask


def test_balance_builds_on_damage_and_heals(director, dice, sheets, scene):
    sheets["Moss"].update(advanced_moves=["Balance"])
    del sheets["Moss"]["gear"]
    dice.queue(6)
    apply(director.deal_damage(channel_id=CHANNEL, character="Moss", target="Orc 1", reason="x")[0], sheets, scene)
    assert sheets["Moss"]["balance"] == 1
    sheets["Rhea"]["hp"] = 5
    dice.queue(3)
    effects, result = director.channel_balance(channel_id=CHANNEL, character="Moss", patient="Rhea", amount=1)
    apply(effects, sheets)
    assert sheets["Rhea"]["hp"] == 8 and sheets["Moss"]["balance"] == 0
