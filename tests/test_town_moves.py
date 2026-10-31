"""Outstanding Warrants and Bolster."""

from __future__ import annotations

import asyncio

import pytest

from gary.fsm import Director, PromptModel, SendMessage, UpdateSheet
from gary.sheets import describe

CHANNEL, ALICE = 555, 111
NIM = {"name": "Nim", "class": "Thief", "race": "Halfling", "level": 1, "xp": 0, "hp": 12, "max_hp": 12,
       "stats": {"str": 8, "dex": 16, "con": 12, "int": 13, "wis": 9, "cha": 15}, "armor": 1, "damage": "d8"}


@pytest.fixture
def sheets():
    return {"Nim": dict(NIM)}


@pytest.fixture
def scene():
    return {}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Nim"}, sheet_for=sheets.get, monsters=lambda: scene)


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def test_outstanding_warrants_rolls_cha_and_a_7_9_brings_a_complication(director, dice):
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Nim",
                                      move="outstanding_warrants"))
    assert ask == "**Nim** — Outstanding Warrants, roll +Cha +1."
    dice.queue(3, 3)
    (payload,) = [e.payload for e in director.roll(channel_id=CHANNEL, user_id=ALICE)
                  if isinstance(e, PromptModel)]
    assert "the GM chooses a complication" in payload["what_it_means"]


def test_weeks_of_study_give_preparation(director, sheets):
    effects, result = director.bolster(channel_id=CHANNEL, character="nim", weeks=2, what="picking locks")
    assert result["preparation"] == 1 and result["days"] == 14
    assert "📚 **Nim** spends 2 weeks in picking locks — +1 preparation (1)." in texts(effects)
    sheets["Nim"]["preparation"] = 1
    _, result = director.bolster(channel_id=CHANNEL, character="Nim", weeks=5, what="the old texts")
    assert result["preparation"] == 4  # a month or longer: 3 more
    assert "preparation 1" in describe(sheets["Nim"])


def test_not_in_danger_and_not_without_saying_what(director, scene):
    scene["Rat 1"] = {"hp": 2}
    assert "still standing" in director.bolster(channel_id=CHANNEL, character="Nim", weeks=1,
                                                what="x")[1]["error"]
    scene.clear()
    assert "what" in director.bolster(channel_id=CHANNEL, character="Nim", weeks=1)[1]["error"]


def test_preparation_is_one_per_roll_and_only_when_flagged(director, sheets, registry):
    sheets["Nim"]["preparation"] = 2
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Nim", move="spout_lore"))
    assert "preparation" not in ask
    registry.consume(list(registry.open_checks(CHANNEL))[0].check_id)
    effects = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Nim", move="spout_lore",
                                 preparation=True)
    assert texts(effects)[-1] == "**Nim** — Spout Lore, roll +Int +2 (+1 preparation)."
    assert UpdateSheet(CHANNEL, "Nim", {"preparation": 1}) in effects


def test_bolster_moves_the_clock_by_the_weeks(clock):
    from gary.adapters import gemini_tools as T
    from gary.models import Mutation, MutationKind
    from gary.registry import PendingCheckRegistry
    from gary.state.backend import InMemoryStateBackend
    from gary.state.history import InMemoryHistoryBackend
    from gary.state.store import CampaignStore
    from tests.test_gemini import call_response, narrator, player, text_response

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.SHEET, "Nim", {"sheet": dict(NIM)})], reason="setup")
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={CHANNEL},
                        players={ALICE: "Nim"}, sheet_for=lambda a: (store.state().get("sheets") or {}).get(a))
    n = narrator((director, store, InMemoryHistoryBackend()),
                 call_response(T.BOLSTER, {"character": "Nim", "weeks": 1, "what": "climbing"}),
                 text_response("A week of practice."))
    asyncio.run(player(n, "I train"))
    assert store.state()["world_minutes"] == 7 * 1440
