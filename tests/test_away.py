"""/away and /back, and a split party recorded on the scene."""

from __future__ import annotations

from types import SimpleNamespace

from gary.adapters import gemini_tools as T
from gary.adapters.discord_bot import GaryClient
from gary.context import render_live_state
from gary.models import Mutation, MutationKind
from tests.test_gemini import CHANNEL, call_response, narrator, player, text_response
from tests.test_gemini import rig as _rig

rig = _rig


def sheet(name, cls="Thief"):
    return {"name": name, "class": cls, "race": "Human", "level": 1, "xp": 0, "hp": 12, "max_hp": 12, "armor": 0,
            "damage": "d8", "stats": {"str": 9, "dex": 16, "con": 12, "int": 10, "wis": 13, "cha": 8}, "gear": []}


def seat(store, director):
    store.apply([Mutation(MutationKind.SHEET, n, {"sheet": sheet(n)}) for n in ("Slymin", "Dorn")]
                + [Mutation(MutationKind.PLAYER_CHARACTER, str(u), {"actor": n}) for u, n in ((11, "Slymin"), (12, "Dorn"))]
                + [Mutation(MutationKind.SCENE, "", {"location": "Blackwater Quay"})], reason="setup")
    director.set_player_character(11, "Slymin")
    director.set_player_character(12, "Dorn")


async def test_away_and_back(rig):
    director, store, _ = rig
    seat(store, director)
    sent = []
    client = SimpleNamespace(store=store, director=director,
                             sender=SimpleNamespace(send=lambda c, t: _append(sent, t)))
    msg = SimpleNamespace(author=SimpleNamespace(id=12))
    await GaryClient._away(client, msg, CHANNEL, "away", "")
    assert store.state()["sheets"]["Dorn"]["away"] and "Dorn** is away" in sent[-1]
    assert "Dorn" in render_live_state(store.state()) and "AWAY: their player isn't at the table" in render_live_state(store.state())
    await GaryClient._away(client, msg, CHANNEL, "away", "dorn")
    assert "already away" in sent[-1]
    await GaryClient._away(client, msg, CHANNEL, "back", "")
    assert not store.state()["sheets"]["Dorn"]["away"] and "AWAY" not in render_live_state(store.state())


async def _append(sent, text):
    sent.append(text)


async def test_an_away_character_makes_no_moves_and_a_split_is_recorded(rig):
    director, store, _ = rig
    seat(store, director)
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": {**sheet("Dorn"), "away": True}})], reason="away")
    n = narrator(rig,
                 call_response(T.MAKE_MOVE, {"actor": "Dorn", "move": "defy_danger", "stat": "dex"}),
                 call_response(T.SET_SCENE, {"location": "The Drowned Gull", "apart": ["Dorn: the docks"]}),
                 call_response(T.SET_SCENE, {"location": "The Drowned Gull", "apart": ["Slymin: the cliff path"]}),
                 text_response("."))
    await player(n, "I go")
    answers = [c["contents"][-1].parts[0].function_response.response for c in n.client.calls[1:4]]
    assert "Dorn is away" in answers[0]["error"]
    assert "Dorn is away" in answers[1]["error"]
    assert answers[2]["ok"]
    shown = render_live_state(store.state())
    assert "The party is split. NOT at this location: Slymin (the cliff path)" in shown
    n = narrator(rig, call_response(T.SET_SCENE, {"location": "The Drowned Gull", "summary": "Rain."}),
                 call_response(T.SET_SCENE, {"location": "The Drowned Gull", "apart": []}), text_response("."))
    await player(n, "I wait")
    assert store.state()["scene"]["apart"] == {}
