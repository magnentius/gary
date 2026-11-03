"""The spotlight tally: who has had the recent moments, so the quiet ones get the next."""

from __future__ import annotations

from google.genai import types

from gary.context import moments_in, spotlight


def test_a_lopsided_table_is_named_with_who_to_turn_to():
    moments = ["Gorm"] * 7 + ["Thorin", "Gorm", "Thorin"]
    line = spotlight(moments, ["Gorm", "Thorin", "Liriel"])
    assert line.startswith("SPOTLIGHT (the last 10 moments") and "Gorm 8, Thorin 2, Liriel 0." in line
    assert "Turn to Liriel next" in line


def test_an_even_table_or_too_few_moments_says_less_or_nothing():
    even = spotlight(["Gorm", "Thorin"] * 3, ["Gorm", "Thorin"])
    assert "Gorm 3, Thorin 3" in even and "Turn to" not in even
    assert spotlight(["Gorm", "Thorin"], ["Gorm", "Thorin"]) == ""      # too few
    assert spotlight(["Gorm"] * 9, ["Gorm"]) == ""                      # one character: nothing to spread
    assert "Ash" not in spotlight(["Ash"] * 5 + ["Gorm"] * 5, ["Gorm", "Thorin"])  # only who is seated


def test_moments_come_from_player_messages_and_moves():
    turns = [
        types.Content(role="user", parts=[types.Part.from_text(text="Player John (playing Gorm) says: I charge!")]),
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
            name="make_move", args={"actor": "Gorm", "move": "hack_and_slash"}))]),
        types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
            name="make_move", args={"actor": "Thorin", "move": "defend"}))]),
        types.Content(role="user", parts=[types.Part.from_text(text="RULES ENGINE: {}")]),
    ]
    assert moments_in(turns) == ["Gorm", "Thorin"]  # Gorm's message and his move are one moment
