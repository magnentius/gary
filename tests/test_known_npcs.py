"""/npcs: the people the party knows, and nothing the GM keeps to themselves."""

from __future__ import annotations

from gary import npcs
from gary.adapters import gemini_tools as T
from tests.test_gemini import CHANNEL, call_response, narrator, player, text_response
from tests.test_gemini import rig as _rig

rig = _rig  # the narrator test fixture, shared


async def _import(n):
    return await n.narrate(channel_id=CHANNEL, kind="import_document",
                           payload={"filename": "setting.md", "text": "...", "part": 1, "total": 1})


async def test_met_in_play_is_known_imported_and_secret_are_not(rig):
    _, store, _ = rig
    n = narrator(rig,
                 call_response(T.UPSERT_NPC, {"npc_id": "vex", "name": "Lady Vex", "note": "the cult's patron"}),
                 text_response("Recorded."),
                 call_response(T.UPSERT_NPC, {"npc_id": "bartleby", "name": "Bartleby", "disposition": -1,
                                              "public": "apothecary by the temple", "note": "skims the tithe"}),
                 call_response(T.UPSERT_NPC, {"npc_id": "hand", "name": "The Grey Hand", "secret": True}),
                 text_response("Bartleby scowls."))
    await _import(n)
    await player(n, "I go into the apothecary")
    assert [p["name"] for p in npcs.known(store.state())] == ["Bartleby"]
    text = npcs.roster(store.state())
    assert "**Bartleby** (wary) — apothecary by the temple" in text
    assert "skims" not in text and "Vex" not in text and "Grey Hand" not in text


async def test_meeting_someone_from_the_import_reveals_them_for_good(rig):
    _, store, _ = rig
    n = narrator(rig,
                 call_response(T.UPSERT_NPC, {"npc_id": "vex", "name": "Lady Vex"}), text_response("."),
                 call_response(T.UPSERT_NPC, {"npc_id": "vex", "public": "a noblewoman at the ball"}),
                 call_response(T.UPSERT_NPC, {"npc_id": "vex", "secret": True}),
                 text_response("."))
    await _import(n)
    assert npcs.known(store.state()) == []
    await player(n, "who is she?")
    assert npcs.roster(store.state(), "vex") == "**Lady Vex** — a noblewoman at the ball"


def test_roster_empty_unknown_and_long():
    assert npcs.roster({}) == "The party hasn't met anyone by name yet."
    assert "doesn't know anyone called Mira" in npcs.roster({}, "Mira")
    state = {"npcs": {f"n{i}": {"name": f"Person {i:03d}", "known": True, "public": "x" * 80}
                      for i in range(60)}}
    text = npcs.roster(state)
    assert len(text) <= 1900 and "more: `/npcs name:`" in text


def test_feeling_words():
    assert [npcs.feeling(d) for d in (-5, -1, 0, 2, 4, None)] == \
        ["hostile", "wary", "neutral", "friendly", "devoted", ""]
