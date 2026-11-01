"""Beyond 10th Level: retire, take on an apprentice, or change class."""

from __future__ import annotations

import pytest

from gary import beyond, levelling
from gary.context import render_live_state
from gary.fsm import Director
from gary.sheets import SheetError

STATS = {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}


def veteran(**more):
    return {"name": "Kael", "class": "Fighter", "race": "Dwarf", "level": 10, "xp": 18, "hp": 25, "max_hp": 25,
            "stats": STATS, "armor": 2, "damage": "d10", "advanced_moves": ["Merciless", "Iron Hide", "Blacksmith"],
            "choices": {"look": "Ancient", "look_0": "Hard Eyes", "look_own": "a scar"}, **more}


def test_ready_only_at_ten_with_the_xp_for_eleven():
    assert beyond.ready(veteran())
    assert not beyond.ready(veteran(xp=16))
    assert not beyond.ready(veteran(level=9))
    with pytest.raises(SheetError, match="go beyond it"):
        levelling.Advance(veteran(xp=3))


def test_retire_and_apprentice():
    b = beyond.Beyond(veteran())
    b.choose("retire")
    after = b.result()
    assert after["retired"] and after["xp"] == 1
    b = beyond.Beyond(veteran())
    b.choose("apprentice")
    assert b.result()["mentor"]


def test_changing_class_keeps_who_they_are_and_swaps_the_class_moves():
    b = beyond.Beyond(veteran(companion={"name": "Rex"}))
    b.choose("change")
    assert "fighter" not in b.class_options() and "barbarian" in b.class_options()
    b.choose_class("wizard")
    assert b.step() == "keep"
    with pytest.raises(SheetError, match="Keep up to 3"):
        b.choose_kept(["Merciless", "Iron Hide", "Blacksmith", "Nope"])
    b.choose_kept(["Iron Hide"])
    after = b.result()
    assert (after["class"], after["damage"], after["max_hp"], after["stats"]) == ("Wizard", "d4", 25, STATS)
    assert after["advanced_moves"] == [] and after["multiclass_moves"] == [{"class": "fighter", "name": "Iron Hide"}]
    assert after["choices"] == {"look_0": "Hard Eyes", "look_own": "a scar"}  # the look stays, the class's go
    assert "companion" not in after and after["former_classes"] == ["Fighter"]
    from gary import advanced

    assert advanced.has(after, "Iron Hide")  # a kept move still counts


def test_out_of_play_characters_make_no_moves_and_a_mentor_is_the_gms(registry, clock, dice):
    sheets = {"Kael": veteran(mentor=True)}
    director = Director(registry=registry, clock=clock, game_channels={555}, dice=dice, players={1: "Kael"},
                        sheet_for=sheets.get)
    effects = director.open_move(channel_id=555, user_id=1, actor="Kael", move="defy_danger", stat="str")
    assert not registry.open_checks(555) and "makes no moves" in str(effects[0].payload)
    shown = render_live_state({"players": {"1": "Kael"}, "sheets": sheets})
    assert "YOU play them now" in shown
    sheets["Kael"] = veteran(retired=True)
    assert "Kael" not in render_live_state({"players": {}, "sheets": sheets})
