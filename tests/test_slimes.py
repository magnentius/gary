"""Blob: slime minions that act as characters, ooze form, and slime copies."""

from __future__ import annotations

import pytest

from gary import slimes
from gary.fsm import ChangeHp, Director, SendMessage, UpdateSheet
from gary.levelling import Advance
from gary.sheets import stat_modifier

CHANNEL, ALICE = 555, 111
STATS = {"str": 12, "dex": 12, "con": 13, "int": 16, "wis": 13, "cha": 16}  # Int +2, Wis +1, Cha +2


def blob(**more):
    return {"name": "Glorp", "class": "Wizard", "race": "Human", "level": 5, "xp": 12, "hp": 15, "max_hp": 15,
            "stats": STATS, "armor": 0, "damage": "d4", **more}


@pytest.fixture
def sheets():
    return {"Glorp": blob(advanced_moves=["Ambeoid Overlord"], minions=[slimes.make("Ooze")])}


@pytest.fixture
def scene():
    return {"Rat 1": {"kind": "Rat", "hp": 6, "max_hp": 6, "armor": 0, "damage": "d6"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice, players={ALICE: "Glorp"},
                    sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets, scene=None):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp):
            (sheets if e.kind == "character" else scene)[e.name]["hp"] = e.hp


def test_taking_ambeoid_overlord_gives_a_slime():
    a = Advance(blob(compendium_qualified=["Blob"], **{"class": "Fighter"}))  # no spellbook step
    a.choose_stat("con")
    a.choose_move("Ambeoid Overlord")
    (pet,) = a.result()["minions"]
    assert (pet["name"], pet["str"], pet["dex"], pet["con"], pet["hp"]) == ("Glorp's slime", 1, 0, 2, 4)


def test_a_slime_acts_with_its_own_stats_and_deals_d4(director, dice, sheets, scene):
    (ask,) = [e.text for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Glorp",
                                                 move="defy_danger", stat="con", minion="ooze")
              if isinstance(e, SendMessage)]
    assert "+Con +2" in ask and "Ooze acts" in ask
    for c in list(director.registry.open_checks(CHANNEL)):
        director.registry.cancel(c.check_id)
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Glorp", move="spout_lore", minion="Ooze")
    assert "a slime cannot make moves with it" in note.payload["error"]
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Glorp", move="hack_and_slash", target="Rat 1",
                       minion="Ooze")
    dice.queue(4, 3)  # 7 +1 Str = 8: a weak hit, and the rat bites back -- at the slime
    director.roll(channel_id=CHANNEL, user_id=ALICE)
    check = next(iter(director.registry.open_checks(CHANNEL)))
    dice.queue(3, 5)  # d4 3, then the rat's d6 5
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id)
    apply(effects, sheets, scene)
    assert scene["Rat 1"]["hp"] == 3 and sheets["Glorp"]["hp"] == 15
    assert sheets["Glorp"]["minions"][0]["hp"] == 0  # splattered, not dead
    effects, result = director.compendium_power(channel_id=CHANNEL, character="Glorp", power="rescoop", name="ooze")
    apply(effects, sheets)
    assert sheets["Glorp"]["minions"][0]["hp"] == 4


def test_ooze_form_and_copies_cost_the_mind(director, sheets):
    sheets["Glorp"]["advanced_moves"] += ["Scuzzy Metamorphosis", "Divide and Conquer"]
    apply(director.compendium_power(channel_id=CHANNEL, character="Glorp", power="ooze_form", on=True)[0], sheets)
    assert stat_modifier(sheets["Glorp"], "int") == 1 and stat_modifier(sheets["Glorp"], "str") == 0
    from gary import advanced

    assert advanced.armor_bonus(sheets["Glorp"], False)[0] == 1
    apply(director.compendium_power(channel_id=CHANNEL, character="Glorp", power="slime_copy", name="Glorp II")[0],
          sheets)
    assert stat_modifier(sheets["Glorp"], "cha") == 0  # +2, -1 ooze, -1 copy
    apply(director.compendium_power(channel_id=CHANNEL, character="Glorp", power="ooze_form", on=False)[0], sheets)
    apply(director.compendium_power(channel_id=CHANNEL, character="Glorp", power="slime_copy", name="Glorp III")[0],
          sheets)  # the second copy: Int +2 -1 copy is still +1, so it is allowed
    _, refused = director.compendium_power(channel_id=CHANNEL, character="Glorp", power="slime_copy", name="IV")
    assert "no Int, Wis or Cha of +1 or more" in refused["error"]


def test_slime_recruiter_keeps_more_and_shaper_reshapes(director, sheets):
    _, refused = director.compendium_power(channel_id=CHANNEL, character="Glorp", power="add_minion", name="Goo")
    assert "Slime Recruiter keeps more" in refused["error"]
    sheets["Glorp"]["advanced_moves"] += ["Slime Recruiter", "Slime Shaper"]
    apply(director.compendium_power(channel_id=CHANNEL, character="Glorp", power="add_minion", name="Goo")[0], sheets)
    assert [m["name"] for m in sheets["Glorp"]["minions"]] == ["Ooze", "Goo"]
    apply(director.compendium_power(channel_id=CHANNEL, character="Glorp", power="shape_minion", name="goo",
                                    stat="dex")[0], sheets)
    goo = slimes.find(sheets["Glorp"], "Goo")
    assert (goo["str"], goo["dex"], goo["con"]) == (1, 2, 1)
