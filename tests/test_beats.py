"""Upward and downward beats, read from the journal; a nudge only for a lopsided run."""

from __future__ import annotations

from gary.beats import beats, line


def outcome(band, move="hack_and_slash"):
    return {"kind": "move_outcome", "payload": {"band": band, "move": move}}


def sheet(before, after):
    return {"kind": "sheet", "payload": {"sheet": after}, "inverse": {"payload": {"sheet": before}}}


def test_each_kind_of_moment_reads_as_up_or_down():
    journal = [outcome("strong"), outcome("miss"), outcome("weak"), outcome("weak", "last_breath"),
               {"kind": "monster_hp", "payload": {"hp": 0}}, {"kind": "monster_hp", "payload": {"hp": 3}},
               {"kind": "monster_looted", "payload": {"looted": True}},
               sheet({"hp": 10}, {"hp": 6}), sheet({"hp": 6}, {"hp": 10}),
               sheet({"hp": 6, "debilities": []}, {"hp": 6, "debilities": ["weak"]}),
               {"kind": "scene", "payload": {"location": "x"}}]
    assert beats(journal) == ["up", "down", "down", "up", "up", "down", "up", "down"]


def test_a_reset_starts_the_run_again():
    assert beats([outcome("miss"), {"kind": "campaign_reset", "payload": {}}, outcome("strong")]) == ["up"]


def test_a_hard_run_asks_for_an_upward_beat_and_never_a_softer_roll():
    text = line(["up", "down", "down", "up", "down", "down", "down", "down"])
    assert "hard run" in text and "upward beat" in text and "Never soften a roll" in text


def test_a_long_good_run_asks_for_a_downward_beat():
    assert "downward beat" in line(["up"] * 6)


def test_a_mixed_or_short_run_says_nothing():
    assert line(["up", "down"] * 4) == ""
    assert line(["down"] * 4) == ""  # too few to call
