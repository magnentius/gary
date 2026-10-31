"""Fronts: written and advanced by the model, kept as journalled state, shown to the model
each turn and to the DM on request."""

from __future__ import annotations

import pytest

from gary import fronts
from gary.adapters import gemini_tools as T
from gary.context import render_live_state
from gary.models import Mutation, MutationKind
from gary.sheets import SheetError
from gary.fsm import Director
from gary.registry import PendingCheckRegistry
from gary.state.backend import InMemoryStateBackend
from gary.state.history import InMemoryHistoryBackend
from gary.state.store import CampaignStore
from tests.test_gemini import CHANNEL, call_response, narrator, player, text_response


@pytest.fixture
def table(clock):
    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={CHANNEL})
    return director, store, InMemoryHistoryBackend()

DRAGON = {"name": "The Dragon", "impulse": "to defend her young",
          "portents": ["She burns the naval patrol", "She turns the lizardmen", "Her hatchlings feed"],
          "doom": "Destruction"}


def test_a_front_is_checked():
    front = fronts.make("The Dragon of the Spear", [DRAGON], ["Will the lizardmen side with her?"])
    assert front["dangers"][0]["reached"] == 0
    with pytest.raises(SheetError, match="2 to 5 grim portents"):
        fronts.make("X", [{**DRAGON, "portents": ["only one"]}])
    with pytest.raises(SheetError, match="impulse"):
        fronts.make("X", [{**DRAGON, "impulse": ""}])
    with pytest.raises(SheetError, match="1 to 4 dangers"):
        fronts.make("X", [])


def test_the_model_sees_whats_next():
    front = fronts.make("The Dragon of the Spear", [DRAGON])
    front["dangers"][0]["reached"] = 1
    lines = "\n".join(fronts.render({"dragon": front}))
    assert "The Dragon — wants to defend her young; portent 1/3; next: She turns the lizardmen" in lines
    assert "☒ She burns the naval patrol" in fronts.describe(front)


async def test_create_advance_and_undo(table):
    director, store, history = table
    n = narrator(table,
                 call_response(T.CREATE_FRONT, {"name": "The Dragon of the Spear", "dangers": [DRAGON]}),
                 call_response(T.ADVANCE_PORTENT, {"front": "the dragon of the spear", "danger": "the dragon",
                                                   "reason": "the party lingered in town a week"}),
                 text_response("Smoke on the horizon."))
    result = await player(n, "we rest a week")
    danger = store.state()["fronts"]["the_dragon_of_the_spear"]["dangers"][0]
    assert danger["reached"] == 1
    assert not [e for e in result.effects if getattr(e, "text", "")]  # nothing told to the table
    fr = n.client.calls[2]["contents"][-1].parts
    answers = [p.function_response.response for p in fr if p.function_response]
    assert answers[-1] == {"ok": True, "portent": "She burns the naval patrol", "reached": 1, "of": 3,
                           "next": "She turns the lizardmen"}
    assert "next: She turns the lizardmen" in render_live_state(store.state())
    store.undo_last()
    assert store.state()["fronts"]["the_dragon_of_the_spear"]["dangers"][0]["reached"] == 0


async def test_the_last_portent_brings_the_doom(table):
    director, store, history = table
    front = fronts.make("Cult", [{**DRAGON, "name": "Cultists", "portents": ["a", "b"]}])
    front["dangers"][0]["reached"] = 1
    store.apply([Mutation(MutationKind.FRONT, "cult", {"front": front})], reason="setup")
    n = narrator(table, call_response(T.ADVANCE_PORTENT, {"front": "Cult", "danger": "cultists", "reason": "x"}),
                 text_response("It is done."))
    await player(n, "...")
    fr = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    assert "impending doom comes to pass: Destruction" in fr["doom"]
