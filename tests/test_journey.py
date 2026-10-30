"""Undertake a Perilous Journey: three jobs, +Wis each, rations for the road."""

from __future__ import annotations

import pytest

from gary.fsm import Director, PromptModel, RollPrompt, SendMessage, UpdateSheet

CHANNEL, ALICE, BOB = 555, 111, 222


def sheet(name, wis=13, rations=5, cls="Fighter"):
    return {"name": name, "class": cls, "race": "Human", "level": 1, "xp": 0, "hp": 20, "max_hp": 20,
            "stats": {"str": 12, "dex": 12, "con": 12, "int": 12, "wis": wis, "cha": 12},
            "armor": 0, "damage": "d8", "gear": [], "rations": rations}


@pytest.fixture
def sheets():
    return {"Dorn": sheet("Dorn", rations=2), "Eva": sheet("Eva", wis=16)}


@pytest.fixture
def director(registry, clock, dice, sheets):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn", BOB: "Eva"}, sheet_for=sheets.get, monsters=lambda: {})


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)


def test_setting_out_spends_rations_and_opens_each_jobs_roll(director, registry, sheets):
    effects, result = director.perilous_journey(channel_id=CHANNEL, destination="the Black Fen", days=3,
                                                trailblazer="dorn", quartermaster="Eva")
    assert result["jobs"] == {"trailblazer": "Dorn", "quartermaster": "Eva"}
    assert result["unfilled_jobs_count_as_6"] == ["scout"]
    assert result["rations"]["Dorn"] == {"rations_used": 2, "rations_left": 0, "short": 1}
    assert UpdateSheet(CHANNEL, "Eva", {"rations": 2}) in effects
    text = effects[0].text
    assert "sets out for the Black Fen" in text and "Nobody is scout: that counts as a 6." in text
    asks = [e.text for e in effects if isinstance(e, SendMessage) and isinstance(e.prompt, RollPrompt)]
    assert asks == ["**Dorn** — Perilous Journey: Trailblazer, roll +Wis +1.",
                    "**Eva** — Perilous Journey: Quartermaster, roll +Wis +2."]
    assert {c.user_id for c in registry.open_checks(CHANNEL)} == {ALICE, BOB}


def test_one_job_each(director):
    _, result = director.perilous_journey(channel_id=CHANNEL, destination="x", days=1,
                                          trailblazer="Dorn", scout="Dorn")
    assert "cannot have two" in result["error"]
    _, result = director.perilous_journey(channel_id=CHANNEL, destination="x", days=1, scout="Gandalf")
    assert "not travelling with the party" in result["error"]


def test_a_quartermasters_10_saves_a_ration(director, dice, sheets):
    effects, _ = director.perilous_journey(channel_id=CHANNEL, destination="the coast", days=2,
                                           quartermaster="Eva")
    apply(effects, sheets)
    dice.queue(4, 4)  # +2: 10
    effects = director.roll(channel_id=CHANNEL, user_id=BOB)
    assert UpdateSheet(CHANNEL, "Eva", {"rations": 4}) in effects
    (event,) = [e for e in effects if isinstance(e, PromptModel)]
    assert event.kind == "rules_event" and "reduce the number of rations" in event.payload["what_it_means"]


def test_a_druid_packs_no_rations(director, sheets):
    sheets["Eva"]["class"] = "Druid"
    effects, result = director.perilous_journey(channel_id=CHANNEL, destination="x", days=2)
    assert "Eva" not in result["rations"]
