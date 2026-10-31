"""An adventure's own end-of-session questions, and the book's Instant NPCs."""

from __future__ import annotations

import asyncio

from gary import npcs
from gary.adapters.session import QUESTIONS, EndOfSession
from gary.ports import ScriptedDice


def die_from(*faces):
    dice = ScriptedDice(faces)
    return lambda spec: dice.roll(spec).total


def test_instant_npcs_roll_the_books_tables_and_skip_used_names():
    assert len(npcs.INSTINCTS) == len(npcs.KNACKS) == 100 and npcs.NAMES[0] == "Finbar"
    npc = npcs.roll(die_from(1, 100, 1), used=["Finbar"])
    assert (npc["instinct"], npc["knack"]) == ("To avenge", "The wind at his back and a spring in his step")
    assert npc["name"] == "Hywn"  # Finbar is taken; the first free name


def test_the_end_of_session_form_asks_the_adventures_questions():
    async def run():
        view = EndOfSession(lambda *a: None, {}, questions=["Did we learn something about the Cult of the Scaled God?",
                                                           "Did we rescue a captured villager?"])
        assert "Did we rescue a captured villager?" in view.prompt()
        labels = [b.label for b in view.children]
        assert labels[:2] == ["⬜ Did we learn something about the Cult of the Scaled God",
                              "⬜ Did we rescue a captured villager"]
        plain = EndOfSession(lambda *a: None, {})
        assert plain.questions == list(QUESTIONS) and plain.children[0].label == "⬜ Learned something important"

    asyncio.run(run())


def test_the_dm_sets_shows_and_resets_them():
    from gary.adapters.discord_bot import GaryClient
    from gary.models import MutationKind
    from tests.test_integration import _dm_client, _seeded_state

    state, sent = _seeded_state(), []
    Msg, dispatch, client = _dm_client(state, sent)

    def apply(mutations, reason=""):
        for m in mutations:
            assert m.kind is MutationKind.SESSION_QUESTIONS
            state["session_questions"] = m.payload["questions"]
        return type("Diff", (), {"ok": True, "rejected": ()})()

    client.store.apply = apply
    client._session_questions = GaryClient._session_questions.__get__(client)
    asyncio.run(dispatch(Msg("?dm questions")))
    assert sent[-1].startswith("End of session asks the book's questions:")
    asyncio.run(dispatch(Msg("?dm questions Did we find the Scaled God's temple? | Did we save Secor?")))
    assert state["session_questions"] == ["Did we find the Scaled God's temple?", "Did we save Secor?"]
    assert sent[-1].startswith("📜 **End of session** now asks, for this adventure:\n• Did we find")
    asyncio.run(dispatch(Msg("?dm questions a | b | c | d")))
    assert sent[-1] == "One to three questions, separated by |, each under 200 characters."
    asyncio.run(dispatch(Msg("?dm questions default")))
    assert state["session_questions"] == [] and "book's questions again" in sent[-1]


def test_the_session_questions_undo():
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=lambda: 0.0)
    store.apply([Mutation(MutationKind.SESSION_QUESTIONS, "", {"questions": ["Did we win?"]})], reason="set")
    store.undo_last()
    assert store.state().get("session_questions") == []
