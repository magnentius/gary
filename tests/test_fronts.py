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

DRAGON = {"name": "The Dragon", "type": "Dragon",
          "portents": ["She burns the naval patrol", "She turns the lizardmen", "Her hatchlings feed"],
          "doom_type": "Destruction", "doom": "the coast burns"}
STAKES = ["Will the lizardmen side with her?"]


def front_args(**more):
    return {"name": "The Dragon of the Spear", "scope": "adventure", "dangers": [DRAGON], "stakes": STAKES, **more}


def test_a_front_follows_the_books_recipe():
    front = fronts.make("The Dragon of the Spear", [DRAGON], STAKES)
    d = front["dangers"][0]
    assert d["reached"] == 0 and d["family"] == "Arcane Enemies"
    assert d["impulse"] == "to hoard gold and jewels, to protect the clutch"  # the type's, by default
    with pytest.raises(SheetError, match="1 to 3 grim portents"):
        fronts.make("X", [{**DRAGON, "portents": ["a", "b", "c", "d"]}], STAKES)
    with pytest.raises(SheetError, match="3 to 5 grim portents"):
        fronts.make("X", [{**DRAGON, "portents": ["a", "b"]}], STAKES, scope="campaign")
    with pytest.raises(SheetError, match="type from the book"):
        fronts.make("X", [{**DRAGON, "type": "Wyrm"}], STAKES)
    with pytest.raises(SheetError, match="impending doom's kind"):
        fronts.make("X", [{**DRAGON, "doom_type": "Fire"}], STAKES)
    with pytest.raises(SheetError, match="1 to 3 dangers"):
        fronts.make("X", [DRAGON] * 4, STAKES)
    with pytest.raises(SheetError, match="stakes questions"):
        fronts.make("X", [DRAGON], [])
    with pytest.raises(SheetError, match="campaign front or an adventure front"):
        fronts.make("X", [DRAGON], STAKES, scope="dungeon")


def test_the_model_sees_whats_next_and_the_familys_gm_moves():
    front = fronts.make("The Dragon of the Spear", [DRAGON], STAKES, moves=["When you meet her gaze, roll+WIS..."])
    front["dangers"][0]["reached"] = 1
    lines = "\n".join(fronts.render({"dragon": front}))
    assert ("The Dragon (Dragon) — wants to hoard gold and jewels, to protect the clutch; portent 1/3; next: She "
            "turns the lizardmen (doom: Destruction: the coast burns)") in lines
    assert "GM moves for Arcane Enemies: Learn forbidden knowledge;" in lines
    assert "custom move: When you meet her gaze" in lines
    shown = fronts.describe(front)
    assert "☒ She burns the naval patrol" in shown and "Impending doom: Destruction — the coast burns" in shown


def test_revising_keeps_what_has_come_to_pass_and_moving_carries_progress():
    front = fronts.make("The Dragon of the Spear", [DRAGON], STAKES)
    front["dangers"][0]["reached"] = 1
    revised = fronts.revise(front, "the dragon", ["She takes the lighthouse"], doom_type="Tyranny",
                            doom="she rules the coast", cast=["Vael, her herald"])
    d = revised["dangers"][0]
    assert d["portents"] == ["She burns the naval patrol", "She takes the lighthouse"]
    assert (d["doom_type"], revised["cast"]) == ("Tyranny", ["Vael, her herald"])
    campaign = fronts.make("The Old Wyrms", [{**DRAGON, "name": "The Mother", "portents": ["a", "b", "c"]}],
                           STAKES, scope="campaign")
    source, target = fronts.move(revised, campaign, "The Dragon")
    assert source["dangers"] == [] and target["dangers"][-1]["reached"] == 1


def _open_work(store):
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "done", "front_work": True})], reason="t")


async def test_create_advance_and_undo(table):
    director, store, history = table
    _open_work(store)
    n = narrator(table,
                 call_response(T.CREATE_FRONT, front_args()),
                 call_response(T.ADVANCE_PORTENT, {"front": "the dragon of the spear", "danger": "the dragon",
                                                   "reason": "the party lingered in town a week"}),
                 text_response("Smoke on the horizon."))
    result = await player(n, "we rest a week")
    danger = store.state()["fronts"]["the_dragon_of_the_spear"]["dangers"][0]
    assert danger["reached"] == 1
    assert not [e for e in result.effects if getattr(e, "text", "")]  # nothing told to the table
    fr = n.client.calls[2]["contents"][-1].parts
    answers = [p.function_response.response for p in fr if p.function_response]
    assert answers[-1]["portent"] == "She burns the naval patrol" and answers[-1]["next"] == "She turns the lizardmen"
    assert "next: She turns the lizardmen" in render_live_state(store.state())
    store.undo_last()
    assert store.state()["fronts"]["the_dragon_of_the_spear"]["dangers"][0]["reached"] == 0


async def test_the_last_portent_brings_the_doom_and_resolves_the_danger(table):
    director, store, history = table
    front = fronts.make("Cult", [{**DRAGON, "name": "Cultists", "type": "Cult", "portents": ["a", "b"]}], STAKES)
    front["dangers"][0]["reached"] = 1
    store.apply([Mutation(MutationKind.FRONT, "cult", {"front": front})], reason="setup")
    n = narrator(table, call_response(T.ADVANCE_PORTENT, {"front": "Cult", "danger": "cultists", "reason": "x"}),
                 text_response("It is done."))
    await player(n, "...")
    fr = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    assert "impending doom comes to pass: the coast burns" in fr["doom"]
    assert store.state()["fronts"]["cult"]["dangers"][0]["ended"].startswith("its doom came to pass")


async def test_fronts_are_written_only_between_sessions(table):
    """"Be especially sure to read the rules for fronts, but don't create any yet" -- and "Fronts
    are built outside of active play"."""
    director, store, history = table
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "playing"})], reason="t")
    n = narrator(table, call_response(T.CREATE_FRONT, front_args()), text_response("."))
    await player(n, "a dragon!")
    refused = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    assert "first session" in refused["error"] and "kind=threat" in refused["error"]

    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "done"})], reason="t")
    n = narrator(table, call_response(T.CREATE_FRONT, front_args()), text_response("."))
    await player(n, "later, in play")
    refused = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    assert "between sessions, not in play" in refused["error"]
    assert not store.state().get("fronts")

    _open_work(store)
    campaign = front_args(scope="campaign", dangers=[{**DRAGON, "portents": ["a", "b", "c"]}])
    n = narrator(table, call_response(T.CREATE_FRONT, campaign),
                 call_response(T.CREATE_FRONT, {**campaign, "name": "Another"}), text_response("."))
    await player(n, "between sessions")
    front = store.state()["fronts"]["the_dragon_of_the_spear"]
    assert front["scope"] == "campaign" and "(campaign front)" in fronts.describe(front)
    second = n.client.calls[2]["contents"][-1].parts[0].function_response.response
    assert "already a campaign front" in second["error"]


async def test_in_play_a_front_is_revised_and_a_danger_moved(table):
    director, store, history = table
    adventure = fronts.make("The Dragon of the Spear", [DRAGON], STAKES)
    campaign = fronts.make("The Old Wyrms", [{**DRAGON, "name": "The Mother", "portents": ["a", "b", "c"]}],
                           STAKES, scope="campaign")
    store.apply([Mutation(MutationKind.FRONT, "the_dragon_of_the_spear", {"front": adventure}),
                 Mutation(MutationKind.FRONT, "the_old_wyrms", {"front": campaign})], reason="setup")
    n = narrator(table,
                 call_response(T.REVISE_FRONT, {"front": "The Dragon of the Spear", "danger": "The Dragon",
                                                "portents": ["She flees north"], "reason": "the party wounded her"}),
                 call_response(T.MOVE_DANGER, {"front": "The Dragon of the Spear", "danger": "The Dragon",
                                               "to_front": "The Old Wyrms", "reason": "she escaped"}),
                 text_response("."))
    await player(n, "she flies off")
    state = store.state()["fronts"]
    assert state["the_dragon_of_the_spear"]["dangers"] == []
    assert state["the_old_wyrms"]["dangers"][-1]["portents"] == ["She flees north"]
    store.undo_last()  # undo is one change at a time: half undone, the danger is in both, not lost
    assert len(store.state()["fronts"]["the_dragon_of_the_spear"]["dangers"]) == 1
    store.undo_last()
    assert [d["name"] for d in store.state()["fronts"]["the_old_wyrms"]["dangers"]] == ["The Mother"]
