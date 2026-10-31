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
