"""Conversation history backends (ARCHITECTURE.md §8, §11.1)."""

from __future__ import annotations

import pytest

from drelm.state.history import (
    FileHistoryBackend,
    FirestoreHistoryBackend,
    HistoryBackend,
    InMemoryHistoryBackend,
    trim,
)

from .conftest import FakeFirestoreClient

CHANNEL = 555


@pytest.fixture(params=["file", "memory", "firestore"])
def history_factory(request, tmp_path):
    if request.param == "file":
        root = tmp_path / "history"
        return lambda: FileHistoryBackend(root)
    if request.param == "firestore":
        client = FakeFirestoreClient()
        return lambda: FirestoreHistoryBackend(client, "test-campaign")
    shared = InMemoryHistoryBackend()
    return lambda: shared


TURNS = [
    {"role": "user", "parts": [{"text": "I open the door"}]},
    {"role": "model", "parts": [{"text": "It swings wide."}]},
]


def test_empty_history_is_not_an_error(history_factory):
    assert history_factory().load(CHANNEL) == ([], "")


def test_turns_round_trip(history_factory):
    history_factory().save_turns(CHANNEL, TURNS)
    turns, summary = history_factory().load(CHANNEL)
    assert turns == TURNS
    assert summary == ""


def test_channels_are_isolated(history_factory):
    backend = history_factory()
    backend.save_turns(CHANNEL, TURNS)
    assert history_factory().load(999) == ([], "")


def test_saving_turns_preserves_the_summary(history_factory):
    """The race this split exists to prevent (ARCHITECTURE.md §8)."""
    backend = history_factory()
    backend.save_summary(CHANNEL, "The party reached the city.")
    backend.save_turns(CHANNEL, TURNS)

    turns, summary = history_factory().load(CHANNEL)
    assert summary == "The party reached the city."
    assert turns == TURNS


def test_saving_a_summary_preserves_the_turns(history_factory):
    backend = history_factory()
    backend.save_turns(CHANNEL, TURNS)
    backend.save_summary(CHANNEL, "Later notes.")

    turns, summary = history_factory().load(CHANNEL)
    assert turns == TURNS
    assert summary == "Later notes."


def test_stored_turns_are_a_copy(history_factory):
    backend = history_factory()
    turns = [{"role": "user", "parts": [{"text": "original"}]}]
    backend.save_turns(CHANNEL, turns)
    turns[0]["parts"][0]["text"] = "mutated"

    loaded, _ = history_factory().load(CHANNEL)
    assert loaded[0]["parts"][0]["text"] == "original"


def test_backends_satisfy_the_protocol(tmp_path):
    assert isinstance(InMemoryHistoryBackend(), HistoryBackend)
    assert isinstance(FileHistoryBackend(tmp_path / "h"), HistoryBackend)
    assert isinstance(FirestoreHistoryBackend(FakeFirestoreClient(), "c"), HistoryBackend)


def test_corrupt_history_file_does_not_block_boot(tmp_path):
    """Starting with no memory of last week beats not starting at all."""
    root = tmp_path / "history"
    root.mkdir()
    (root / f"history_{CHANNEL}.json").write_text("{ truncated")
    assert FileHistoryBackend(root).load(CHANNEL) == ([], "")


# -- trimming -------------------------------------------------------------------------------


def test_short_history_is_untouched():
    kept, overflow = trim(TURNS, window=30)
    assert kept == TURNS and overflow == []


def test_window_keeps_the_newest_turns():
    turns = [{"role": "user", "parts": [{"text": f"t{i}"}]} for i in range(10)]
    kept, overflow = trim(turns, window=3)
    assert [t["parts"][0]["text"] for t in kept] == ["t7", "t8", "t9"]
    assert len(overflow) == 7


def test_window_never_starts_on_an_orphaned_function_response():
    """Gemini rejects a function_response with no matching call in the conversation."""
    turns = [
        {"role": "user", "parts": [{"text": "I pick the lock"}]},
        {"role": "model", "parts": [{"function_call": {"name": "open_check", "args": {}}}]},
        {"role": "user", "parts": [{"function_response": {"name": "open_check", "response": {}}}]},
        {"role": "model", "parts": [{"text": "The lock gives."}]},
    ]
    kept, overflow = trim(turns, window=2)

    assert "function_response" not in str(kept[0])
    assert len(kept) + len(overflow) == len(turns)


def test_trim_tolerates_a_window_of_all_function_responses():
    turns = [
        {"role": "user", "parts": [{"function_response": {"name": "open_check", "response": {}}}]}
        for _ in range(4)
    ]
    kept, overflow = trim(turns, window=2)
    assert kept == [] and len(overflow) == 4
