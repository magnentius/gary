"""Recover: days of rest heal fully and clear debilities, the player choosing which."""

from __future__ import annotations

import asyncio

import pytest

from gary.fsm import CurePrompt, Director, SendMessage, UpdateSheet

CHANNEL, ALICE, BOB = 555, 111, 222


def sheet(name, hp, debilities=()):
    return {"name": name, "class": "Fighter", "race": "Human", "level": 1, "hp": hp, "max_hp": 20,
            "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
            "armor": 1, "damage": "d10", "debilities": list(debilities)}


@pytest.fixture
def sheets():
    return {"Dorn": sheet("Dorn", 4, ["weak", "sick"]), "Eva": sheet("Eva", 15)}


@pytest.fixture
def scene():
    return {}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn", BOB: "Eva"}, sheet_for=sheets.get, monsters=lambda: scene)


def test_a_day_heals_everyone_fully(director):
    effects, result = director.recover(channel_id=CHANNEL, days=1, reason="the Gilded Goose")
    assert UpdateSheet(CHANNEL, "Dorn", {"hp": 20}) in effects
    assert UpdateSheet(CHANNEL, "Eva", {"hp": 20}) in effects
    assert "Weak and Sick need longer" in effects[0].text


def test_three_days_clear_one_debility_of_their_choice(director, sheets):
    effects, result = director.recover(channel_id=CHANNEL, days=3, reason="the temple")
    assert result["party"]["Dorn"]["choosing"] == 1
    (ask,) = [e for e in effects if isinstance(e, SendMessage) and e.prompt]
    assert ask.prompt == CurePrompt(ALICE, ("weak", "sick"))
    sheets["Dorn"].update(cures_owed=1, hp=20)
    assert director.cure(channel_id=CHANNEL, user_id=BOB, debility="weak") == []  # not Eva's
    effects = director.cure(channel_id=CHANNEL, user_id=ALICE, debility="weak")
    assert UpdateSheet(CHANNEL, "Dorn", {"debilities": ["sick"], "cures_owed": 0}) in effects
    sheets["Dorn"].update(debilities=["sick"], cures_owed=0)
    assert director.cure(channel_id=CHANNEL, user_id=ALICE, debility="sick") == []  # none owed


def test_a_healer_clears_one_every_two_days_and_enough_clears_all(director):
    effects, result = director.recover(channel_id=CHANNEL, days=4, healer=True, reason="the abbey",
                                       characters=["dorn"])
    assert result["party"] == {"Dorn": {"hp": 20, "cured": ["weak", "sick"]}}
    assert UpdateSheet(CHANNEL, "Dorn", {"hp": 20, "debilities": [], "cures_owed": 0}) in effects


def test_not_in_danger_not_without_a_reason_not_for_a_year(director, scene):
    scene["Wolf 1"] = {"hp": 5}
    assert "Wolf 1 still standing" in director.recover(channel_id=CHANNEL, days=1, reason="x")[1]["error"]
    scene.clear()
    assert "reason" in director.recover(channel_id=CHANNEL, days=1)[1]["error"]
    assert "1 to 30 days" in director.recover(channel_id=CHANNEL, days=300, reason="x")[1]["error"]


async def _gemini_recover(clock):
    from gary.adapters import gemini_tools as T
    from gary.models import Mutation, MutationKind
    from gary.registry import PendingCheckRegistry
    from gary.state.backend import InMemoryStateBackend
    from gary.state.history import InMemoryHistoryBackend
    from gary.state.store import CampaignStore
    from tests.test_gemini import call_response, narrator, player, text_response

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.SHEET, "Kethra", {"sheet": sheet("Kethra", 3)})], reason="setup")
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={CHANNEL},
                        players={111: "Kethra"},
                        sheet_for=lambda a: (store.state().get("sheets") or {}).get(a))
    n = narrator((director, store, InMemoryHistoryBackend()),
                 call_response(T.RECOVER, {"days": 2, "reason": "the Drowned Rat inn"}),
                 text_response("Two quiet days."))
    await player(n, "we rest at the inn")
    return store


def test_the_clock_moves_the_days(clock):
    store = asyncio.run(_gemini_recover(clock))
    assert store.state()["world_minutes"] == 2 * 1440
