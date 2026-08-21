"""Between-session prep (ARCHITECTURE.md §2.1).

This is the whole substance of the MCP server. It is tested here because the MCP SDK
needs Python 3.10+ and the binding cannot be imported in this environment — so everything
with behaviour worth trusting was deliberately kept out of the binding.
"""

from __future__ import annotations

import pytest

from gary.models import Mutation, MutationKind
from gary.prep import CampaignPrep, ReadOnly
from gary.state.backend import InMemoryStateBackend
from gary.state.history import InMemoryHistoryBackend
from gary.state.store import CampaignStore

CHANNEL = 555


@pytest.fixture
def store(clock):
    return CampaignStore(backend=InMemoryStateBackend(), clock=clock)


@pytest.fixture
def history():
    return InMemoryHistoryBackend()


@pytest.fixture
def prep(store, history):
    return CampaignPrep(store, history_backend=history, writable=True)


@pytest.fixture
def readonly(store, history):
    return CampaignPrep(store, history_backend=history, writable=False)


# -- reads ------------------------------------------------------------------------------------


def test_overview_of_an_empty_campaign_is_still_useful(prep):
    text = prep.overview()
    assert "Campaign overview" in text
    assert "World time" in text


def test_overview_reports_party_effects_quests_and_npcs(prep, store):
    store.apply(
        [
            Mutation(MutationKind.GOLD, "Kethra", {"delta": 42}),
            Mutation(MutationKind.ITEM_ADD, "Kethra", {"items": ["rope", "torch"]}),
            Mutation(MutationKind.SCENE, "", {"location": "The Drowned Rat"}),
        ],
        reason="setup",
    )
    prep.upsert_quest("q1", title="Find the sigil", status="open")
    prep.upsert_npc("varo", name="Varo", disposition=2)

    text = prep.overview()
    assert "Kethra" in text and "42gp" in text and "rope" in text
    assert "The Drowned Rat" in text
    assert "Find the sigil" in text
    assert "Varo" in text


def test_overview_announces_read_only_mode(readonly):
    """A prep client must not silently look like it can write."""
    assert "Read-only" in readonly.overview()
    assert "Read-only" not in CampaignPrep(readonly._store, writable=True).overview()


def test_overview_flags_active_combat(prep, store):
    store.begin_encounter()
    assert "Combat is active" in prep.overview()


def test_journal_is_the_audit_trail(prep, store):
    """Trace an odd campaign change back to the message that caused it."""
    store.apply(
        [Mutation(MutationKind.GOLD, "Kethra", {"delta": 300})],
        reason="model granted treasure",
        source_message_id=98765,
    )
    entry = prep.journal()[-1]
    assert entry["reason"] == "model granted treasure"
    assert entry["source_message_id"] == 98765


def test_notes_can_be_filtered_by_subject(prep):
    prep.record_note("npc", "Varo", "Owes the party a favour.")
    prep.record_note("npc", "Serrin", "Wants them dead.")

    assert len(prep.notes()) == 2
    assert [n["subject"] for n in prep.notes("varo")] == ["Varo"]


def test_last_session_returns_summary_and_recent_turns(prep, history):
    history.save_turns(CHANNEL, [
        {"role": "user", "parts": [{"text": "I open the door"}]},
        {"role": "model", "parts": [{"text": "It swings wide."}]},
    ])
    history.save_summary(CHANNEL, "The party reached the city.")

    session = prep.last_session(CHANNEL)
    assert session["summary"] == "The party reached the city."
    assert session["turns"] == 2
    assert session["recent"][-1]["text"] == "It swings wide."


def test_last_session_without_a_history_backend(store):
    assert CampaignPrep(store, writable=True).last_session(CHANNEL)["turns"] == 0


# -- writes -----------------------------------------------------------------------------------


def test_upsert_npc_creates_then_updates(prep):
    prep.upsert_npc("varo", name="Varo Enn", disposition=1)
    assert prep.npcs()["varo"]["name"] == "Varo Enn"

    prep.upsert_npc("varo", disposition=-3)
    assert prep.npcs()["varo"]["disposition"] == -3
    assert prep.npcs()["varo"]["name"] == "Varo Enn"  # preserved


def test_npc_notes_accumulate(prep):
    prep.upsert_npc("varo", name="Varo", note="Owes a favour.")
    prep.upsert_npc("varo", note="Lied about the sigil.")
    assert prep.npcs()["varo"]["notes"] == ["Owes a favour.", "Lied about the sigil."]


def test_quest_status_can_be_closed(prep):
    prep.upsert_quest("q1", title="Find the sigil", status="open")
    prep.upsert_quest("q1", status="done")
    assert prep.quests()["q1"]["status"] == "done"
    assert prep.quests()["q1"]["title"] == "Find the sigil"


def test_recorded_notes_reach_the_model_context(prep, store):
    """§8 layer 3: this is how foreshadowing is seeded before a session."""
    from gary.context import render_memory

    prep.record_note("foreshadow", "Varo", "Will betray them at the bridge.")
    rendered = render_memory(store.state()["notes"], "", ["Varo"])
    assert "betray them at the bridge" in rendered


def test_set_scene_moves_the_party(prep):
    prep.set_scene("The Bridge", "Fog, and something waiting.")
    scene = prep.state()["scene"]
    assert scene["location"] == "The Bridge"
    assert scene["summary"] == "Fog, and something waiting."


def test_prep_writes_bypass_injection_bounds(prep):
    """Bounds guard a model steered by player text (§9.1); this is a human editing."""
    result = prep._apply(
        [Mutation(MutationKind.GOLD, "Kethra", {"delta": 50_000})], reason="treasure hoard"
    )
    assert result["ok"] is True
    assert prep.state()["party"]["Kethra"]["gold"] == 50_000


def test_undo_reverses_a_bad_model_mutation(prep, store):
    store.apply([Mutation(MutationKind.GOLD, "Kethra", {"delta": 100})], reason="loot")
    store.apply([Mutation(MutationKind.GOLD, "Kethra", {"delta": 400})], reason="hallucinated")
    assert prep.state()["party"]["Kethra"]["gold"] == 500

    result = prep.undo_last()
    assert result["ok"] is True
    assert result["reversed"]["reason"] == "hallucinated"
    assert prep.state()["party"]["Kethra"]["gold"] == 100


def test_undo_with_nothing_to_undo(prep):
    assert prep.undo_last()["ok"] is False


# -- read-only enforcement ----------------------------------------------------------------------


def test_reads_work_without_the_lock(readonly, store):
    store.apply([Mutation(MutationKind.GOLD, "Kethra", {"delta": 10})], reason="x")
    assert readonly.state()["party"]["Kethra"]["gold"] == 10
    assert readonly.journal()
    assert readonly.overview()


@pytest.mark.parametrize(
    "action",
    [
        lambda p: p.upsert_npc("varo", name="Varo"),
        lambda p: p.upsert_quest("q1", title="x"),
        lambda p: p.record_note("clue", "door", "creaks"),
        lambda p: p.set_scene("Elsewhere"),
        lambda p: p.undo_last(),
    ],
)
def test_every_write_is_refused_without_the_lock(readonly, action):
    """Gary is mid-session; interleaving writes would corrupt the journal (§7.3)."""
    with pytest.raises(ReadOnly):
        action(readonly)


def test_refusal_explains_itself(readonly):
    with pytest.raises(ReadOnly, match="locked by another process"):
        readonly.record_note("clue", "door", "creaks")


def test_refused_write_changes_nothing(readonly, store):
    before = store.state()
    with pytest.raises(ReadOnly):
        readonly.set_scene("Elsewhere")
    assert store.state() == before


def test_omitting_disposition_leaves_it_unchanged(prep):
    """The contract the MCP binding has to respect.

    prep treats "not None" as "set it", so any caller defaulting disposition to 0 rather
    than None silently neutralises every NPC it touches for an unrelated reason. That is
    exactly what adapters/mcp_server.py did before 2026-08-20.
    """
    prep.upsert_npc("gorm", name="Gorm", disposition=5)
    prep.upsert_npc("gorm", note="mentioned the caravan")
    assert prep.npcs()["gorm"]["disposition"] == 5

    prep.upsert_npc("gorm", disposition=0)
    assert prep.npcs()["gorm"]["disposition"] == 0
