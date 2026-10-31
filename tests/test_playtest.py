"""The headless playtest harness runs a scene end to end -- here with a scripted model, so it
costs nothing and keeps working as the code changes."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from tests.test_gemini import FakeClient, call_response, text_response

_SPEC = importlib.util.spec_from_file_location(
    "playtest", Path(__file__).resolve().parent.parent / "scripts" / "playtest.py")
playtest = importlib.util.module_from_spec(_SPEC)
sys.modules["playtest"] = playtest  # dataclasses look their module up while it loads
_SPEC.loader.exec_module(playtest)


def test_a_scene_plays_through_every_button_and_writes_a_transcript():
    scene = playtest.Scenario(
        "smoke", "A test world.", ("A road", "A goblin waits."),
        [("Dorn", "I attack the goblin!"), ("Thorin", "I pray for an hour.")],
        setup={"Dorn": {"coins": 50}},
    )
    client = FakeClient(
        call_response("add_monsters", {"monsters": [{"name": "Goblin"}]}),
        call_response("make_move", {"actor": "Dorn", "move": "hack_and_slash", "target": "Goblin 1"}),
        text_response("Steel rings."),          # the outcome, narrated
        call_response("prepare_spells", {"character": "Thorin", "reason": "a quiet road"}),
        text_response("An hour passes."),
    )
    report = asyncio.run(playtest.run(["smoke"], seed=3, client=client, scenarios={"smoke": scene}))
    text = playtest.render(report, ["smoke"], 3)

    assert "**Dorn (player):** I attack the goblin!" in text
    assert "🖱️ *Roll*" in text
    assert report.tool_calls["make_move"] == 1
    assert "## Final sheets" in text and "Dorn —" in text
    assert "🖱️ *Choose spells*" in text  # the goblin fell, so the prayer went through
    assert "50 coins" in text  # the scenario's setup reached the sheet
    assert report.flags == []


def test_model_played_characters_take_turns_until_their_goals_are_done():
    scene = playtest.Scenario(
        "smoke", "A test world.", ("A road", "Quiet."), [],
        goals={"Dorn": "Look around.", "Thorin": "Rest."},
    )
    gm = FakeClient(text_response("The road is empty. Thorin, what do you do?"),
                    text_response("You rest. Dorn, anything else?"))
    players = playtest.Players(FakeClient(
        text_response("I look up and down the road."),   # Dorn: the opening names nobody, so Dorn first
        text_response("I sit down on a stone."),          # Thorin, whom Gary turned to
        text_response("DONE"),                            # Dorn, turned to next, is done
        text_response("DONE"),                            # then Thorin, the only one left
    ), model="test-player")  # the fake routes the summary model's name elsewhere
    report = asyncio.run(playtest.run(["smoke"], seed=1, client=gm, scenarios={"smoke": scene},
                                      players=players))
    text = playtest.render(report, ["smoke"], 1)
    assert "**Dorn (player):** I look up and down the road." in text
    assert "**Thorin (player):** I sit down on a stone." in text
    assert "*Thorin's player is done.*" in text and "*Dorn's player is done.*" in text
    assert "Turn limit" not in text


def test_whoever_gary_turned_to_speaks_next():
    assert playtest._addressed(["Thorin, the blade falls. Dorn, the archer aims."], ["Dorn", "Thorin"], None) == "Dorn"
    assert playtest._addressed(["The wind howls."], ["Dorn", "Thorin"], "Dorn") == "Thorin"
