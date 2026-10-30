"""Debilities: -1 to a stat's rolls until cured."""

from __future__ import annotations

import pytest

from gary.fsm import Director, SendMessage, UpdateSheet
from gary.sheets import describe, stat_modifier

CHANNEL, ALICE = 555, 111
# STR 16 (+2), WIS 12 (+0)
DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
        "hp": 25, "max_hp": 25, "armor": 1, "damage": "d10"}


@pytest.fixture
def sheets():
    return {"Dorn": dict(DORN)}


@pytest.fixture
def director(registry, clock, dice, sheets):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn"}, sheet_for=sheets.get,
                    monsters=lambda: {"Ghoul 1": {"hp": 10, "armor": 1, "damage": "d6"}})


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)


def test_a_debility_takes_one_off_its_stat():
    assert stat_modifier({**DORN, "debilities": ["weak"]}, "str") == 1
    assert stat_modifier({**DORN, "debilities": ["weak"]}, "dex") == 0  # DEX 9, unchanged
    assert "Weak (-1 each)" in describe({**DORN, "debilities": ["weak"]})


def test_the_fiction_gives_it_and_the_roll_shows_it(director, sheets):
    effects, result = director.debility(channel_id=CHANNEL, character="dorn", debility="Weak",
                                        cause="the ghoul's claws numb his arm")
    assert result["debilities"] == ["weak"]
    assert "🩸 **Dorn** is Weak (-1 Strength) — the ghoul's claws numb his arm." in \
        [e.text for e in effects if isinstance(e, SendMessage)]
    apply(effects, sheets)
    (ask,) = [e.text for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn",
                                                  move="hack_and_slash", target="Ghoul 1")
              if isinstance(e, SendMessage)]
    assert ask == "**Dorn** — Hack and Slash at Ghoul 1, roll +Str +1 (-1 Weak)."


def test_no_stacking_no_unknowns_and_a_cause_is_needed(director, sheets):
    sheets["Dorn"]["debilities"] = ["weak"]
    assert "does not stack" in director.debility(channel_id=CHANNEL, character="Dorn", debility="weak",
                                                 cause="again")[1]["error"]
    assert "No debility" in director.debility(channel_id=CHANNEL, character="Dorn", debility="tired",
                                              cause="x")[1]["error"]
    assert "cause" in director.debility(channel_id=CHANNEL, character="Dorn", debility="sick")[1]["error"]


def test_curing_removes_it(director, sheets):
    sheets["Dorn"]["debilities"] = ["weak", "sick"]
    effects, result = director.debility(channel_id=CHANNEL, character="Dorn", debility="weak",
                                        cause="the healer's salve", cured=True)
    assert result["debilities"] == ["sick"]
    assert "is not shaky" in director.debility(channel_id=CHANNEL, character="Dorn", debility="shaky",
                                               cause="x", cured=True)[1]["error"]
