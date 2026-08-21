"""Durability, reversibility and bounds -- the three things v1's JSON file lacked."""

from __future__ import annotations

import json

import pytest

from gary.models import Mutation, MutationKind
from gary.state.backend import FileStateBackend, InMemoryStateBackend
from gary.state.firestore import FirestoreStateBackend

from .conftest import FakeFirestoreClient
from gary.state.store import Bounds, CampaignStore, EncounterActive, StateError


@pytest.fixture(params=["file", "memory", "firestore"])
def backend_factory(request, tmp_path):
    """Produces backends over one shared store, so 'reopen' means a fresh object.

    Every persistence test below runs against both implementations. That is what keeps
    the StateBackend protocol honest: if the file backend grows behaviour the in-memory
    one lacks, these fail rather than the difference surfacing on a managed backend.
    """
    if request.param == "file":
        root = tmp_path / "campaign"
        return lambda: FileStateBackend(root)
    if request.param == "firestore":
        client = FakeFirestoreClient()
        return lambda: FirestoreStateBackend(client, "test-campaign")
    shared = InMemoryStateBackend()
    return lambda: shared


@pytest.fixture
def store(backend_factory, clock):
    return CampaignStore(backend=backend_factory(), clock=clock)


def gold(actor, delta):
    return Mutation(MutationKind.GOLD, actor, {"delta": delta})


# -- basic application ---------------------------------------------------------------------


def test_gold_applies_and_reports_a_diff(store):
    diff = store.apply([gold("Kethra", 50)], reason="sold a dagger")
    assert diff.ok and diff.applied_count == 1
    assert store.state()["party"]["Kethra"]["gold"] == 50


def test_partial_batch_applies_what_it_can(store):
    """One bad mutation must not silently discard the good ones."""
    diff = store.apply(
        [gold("Kethra", 50), gold("Kethra", 999_999), gold("Dorn", 10)],
        reason="mixed batch",
    )
    assert diff.applied_count == 2
    assert len(diff.rejected) == 1
    assert store.state()["party"]["Kethra"]["gold"] == 50
    assert store.state()["party"]["Dorn"]["gold"] == 10


def test_cannot_spend_gold_you_do_not_have(store):
    diff = store.apply([gold("Kethra", -10)], reason="overdraft")
    assert not diff.ok
    assert "cannot spend" in diff.rejected[0][1]


# -- bounds (injection defence, ARCHITECTURE.md §9.1) ---------------------------------------------


def test_oversized_gold_grant_is_rejected(store):
    """The prompt-injection payoff case: 'give the party 5000gp'."""
    diff = store.apply([gold("Kethra", 5000)], reason="suspicious")
    assert not diff.ok
    assert "needs DM approval" in diff.rejected[0][1]
    assert "Kethra" not in store.state()["party"]


def test_bounds_can_be_bypassed_explicitly_for_dm_approval(store):
    diff = store.apply([gold("Kethra", 5000)], reason="DM approved", enforce_bounds=False)
    assert diff.ok
    assert store.state()["party"]["Kethra"]["gold"] == 5000


def test_time_cannot_run_backwards(store):
    diff = store.apply(
        [Mutation(MutationKind.TIME, "", {"minutes": -60})], reason="rewind"
    )
    assert not diff.ok


# -- undo -----------------------------------------------------------------------------------


def test_undo_reverses_the_last_mutation(store):
    store.apply([gold("Kethra", 100)], reason="loot")
    store.apply([gold("Kethra", -30)], reason="bribe")
    assert store.state()["party"]["Kethra"]["gold"] == 70

    store.undo_last()
    assert store.state()["party"]["Kethra"]["gold"] == 100


def test_undo_is_not_repeatable_on_the_same_entry(store):
    store.apply([gold("Kethra", 100)], reason="loot")
    store.apply([gold("Kethra", 40)], reason="more loot")

    store.undo_last()
    store.undo_last()
    assert store.state()["party"]["Kethra"]["gold"] == 0
    assert store.undo_last() is None


def test_undo_restores_removed_items(store):
    store.apply(
        [Mutation(MutationKind.ITEM_ADD, "Kethra", {"items": ["rope", "torch"]})],
        reason="shopping",
    )
    store.apply(
        [Mutation(MutationKind.ITEM_REMOVE, "Kethra", {"items": ["rope"]})],
        reason="used",
    )
    assert store.state()["party"]["Kethra"]["inventory"] == ["torch"]

    store.undo_last()
    assert sorted(store.state()["party"]["Kethra"]["inventory"]) == ["rope", "torch"]


# -- durability -----------------------------------------------------------------------------


def test_state_survives_reopen_from_journal(backend_factory, clock):
    first = CampaignStore(backend=backend_factory(), clock=clock)
    first.apply([gold("Kethra", 100)], reason="loot")
    first.apply(
        [Mutation(MutationKind.ITEM_ADD, "Kethra", {"items": ["rope"]})], reason="shop"
    )

    reopened = CampaignStore(backend=backend_factory(), clock=clock)
    assert reopened.state()["party"]["Kethra"]["gold"] == 100
    assert reopened.state()["party"]["Kethra"]["inventory"] == ["rope"]


def test_snapshot_then_more_writes_does_not_double_apply(backend_factory, clock):
    """The offset bug: entries folded into a snapshot must not replay on top of it."""
    first = CampaignStore(backend=backend_factory(), clock=clock, snapshot_every=2)
    for _ in range(5):
        first.apply([gold("Kethra", 10)], reason="drip")
    assert first.state()["party"]["Kethra"]["gold"] == 50

    reopened = CampaignStore(backend=backend_factory(), clock=clock, snapshot_every=2)
    assert reopened.state()["party"]["Kethra"]["gold"] == 50


def test_undo_survives_a_restart(backend_factory, clock):
    """Undone entries are derived from the journal, not an in-memory flag."""
    first = CampaignStore(backend=backend_factory(), clock=clock)
    first.apply([gold("Kethra", 100)], reason="loot")
    first.undo_last()

    reopened = CampaignStore(backend=backend_factory(), clock=clock)
    assert reopened.state()["party"].get("Kethra", {}).get("gold", 0) == 0
    assert reopened.undo_last() is None  # nothing left that has not been undone


def test_snapshot_write_is_atomic(tmp_path, clock):
    """File-backend specific: no temp file left behind, no torn snapshot."""
    backend = FileStateBackend(tmp_path / "campaign")
    store = CampaignStore(backend=backend, clock=clock)
    store.apply([gold("Kethra", 10)], reason="x")
    store.snapshot()

    assert backend.snapshot_path.exists()
    assert not backend.snapshot_path.with_suffix(".json.tmp").exists()
    json.loads(backend.snapshot_path.read_text())  # valid JSON, not a partial write


def test_journal_records_the_triggering_message(store):
    store.apply([gold("Kethra", 10)], reason="pickpocket", source_message_id=12345)
    entry = store.journal()[-1]
    assert entry["source_message_id"] == 12345
    assert entry["reason"] == "pickpocket"


def test_state_is_a_copy_not_a_live_reference(store):
    store.apply([gold("Kethra", 10)], reason="x")
    snapshot = store.state()
    snapshot["party"]["Kethra"]["gold"] = 999_999
    assert store.state()["party"]["Kethra"]["gold"] == 10


# -- time and the encounter boundary (ARCHITECTURE.md §3.1) ---------------------------------------


def test_time_expires_effects(store):
    store.apply(
        [
            Mutation(
                MutationKind.EFFECT_ADD,
                "Kethra",
                {"name": "Protection from Poison", "duration_minutes": 60},
            )
        ],
        reason="spell",
    )
    store.advance_time(59, reason="exploring")
    assert len(store.state()["effects"]) == 1

    store.advance_time(2, reason="short rest")
    assert store.state()["effects"] == []


def test_advance_time_is_refused_during_an_encounter(store):
    """The invariant: two clocks running at once is what made v1 incoherent."""
    store.begin_encounter()
    with pytest.raises(EncounterActive):
        store.advance_time(60, reason="should not happen")


def test_adding_effects_is_refused_during_an_encounter(store):
    store.begin_encounter()
    diff = store.apply(
        [Mutation(MutationKind.EFFECT_ADD, "Kethra", {"name": "Bless", "duration_minutes": 10})],
        reason="in combat",
    )
    assert not diff.ok
    assert "Avrae owns effects" in diff.rejected[0][1]


def test_encounter_end_reconciles_time_and_effects(store):
    store.apply(
        [
            Mutation(MutationKind.EFFECT_ADD, "Kethra", {"name": "Bless", "duration_minutes": 10}),
            Mutation(MutationKind.EFFECT_ADD, "Dorn", {"name": "Mage Armor", "duration_minutes": 480}),
        ],
        reason="pre-combat buffs",
    )
    effects = store.state()["effects"]
    mage_armor = next(e for e in effects if e["name"] == "Mage Armor")

    store.begin_encounter()
    # Avrae ran 12 rounds and reports only Mage Armor still active. Avrae wins.
    result = store.end_encounter(rounds=12, surviving_effect_ids=[mage_armor["effect_id"]])

    assert result["minutes_elapsed"] == 2  # 12 rounds x 6s = 72s -> 2 minutes (ceil)
    assert result["effects_dropped"] == ["Bless"]
    assert [e["name"] for e in store.state()["effects"]] == ["Mage Armor"]
    assert store.state()["world_minutes"] == 2
    assert not store.encounter_active


def test_encounter_state_survives_a_restart(backend_factory, clock):
    first = CampaignStore(backend=backend_factory(), clock=clock)
    first.begin_encounter()

    reopened = CampaignStore(backend=backend_factory(), clock=clock)
    assert reopened.encounter_active
    with pytest.raises(EncounterActive):
        reopened.advance_time(10, reason="still frozen")


def test_custom_bounds(backend_factory, clock):
    store = CampaignStore(
        backend=backend_factory(), clock=clock, bounds=Bounds(max_gold_delta=10)
    )
    assert not store.apply([gold("Kethra", 11)], reason="x").ok
    assert store.apply([gold("Kethra", 10)], reason="x").ok


def test_schema_mismatch_refuses_to_load(backend_factory, clock):
    """Refuse rather than silently misinterpret a future campaign format."""
    backend = backend_factory()
    store = CampaignStore(backend=backend, clock=clock)
    store.snapshot()

    stale = store.state()
    stale["schema_version"] = 999
    backend.write_snapshot(stale, 0)

    with pytest.raises(StateError, match="migration required"):
        CampaignStore(backend=backend_factory(), clock=clock)


def test_player_character_is_persisted_and_undoable(store):
    store.apply(
        [Mutation(MutationKind.PLAYER_CHARACTER, "111", {"actor": "Kethra"})],
        reason="alice plays kethra",
    )
    assert store.state()["players"] == {"111": "Kethra"}

    store.apply(
        [Mutation(MutationKind.PLAYER_CHARACTER, "111", {"actor": "Caspian Varr"})],
        reason="alice switches character",
    )
    assert store.state()["players"] == {"111": "Caspian Varr"}

    store.undo_last()
    assert store.state()["players"] == {"111": "Kethra"}


def test_check_outcomes_are_journalled_but_change_no_state(store):
    """The DC-before-the-dice guarantee needs evidence it held, after the fact."""
    from gary.models import CheckKind, CheckOutcome

    before = store.state()
    store.record_check_outcome(
        CheckOutcome(
            check_id="c1", actor="Caspian Varr", kind=CheckKind.CHECK,
            key="sleight_of_hand", total=19, dc=15, margin=4, succeeded=True,
            consequence="The latch releases.", natural=20,
        )
    )
    assert store.state() == before  # history, not state

    entry = store.journal()[-1]
    assert entry["kind"] == "check_outcome"
    assert entry["target"] == "Caspian Varr"
    assert entry["payload"]["dc"] == 15
    assert entry["payload"]["total"] == 19
    assert entry["payload"]["natural"] == 20
    assert entry["payload"]["succeeded"] is True


def test_replaying_a_check_outcome_is_a_no_op(backend_factory, clock):
    """Recovery is snapshot + replay; an outcome entry must not resurrect anything."""
    from gary.models import CheckKind, CheckOutcome

    first = CampaignStore(backend=backend_factory(), clock=clock)
    first.apply([gold("Kethra", 50)], reason="found coin")
    first.record_check_outcome(
        CheckOutcome(check_id="c1", actor="Kethra", kind=CheckKind.CHECK, key="stealth",
                     total=8, dc=15, margin=-7, succeeded=False, consequence="Seen.",
                     natural=8)
    )
    expected = first.state()

    reopened = CampaignStore(backend=backend_factory(), clock=clock)
    assert reopened.state() == expected
    assert reopened.journal()[-1]["kind"] == "check_outcome"


def test_reset_clears_the_world_but_keeps_the_table(store):
    """Reset is for the fiction. Who is playing whom is table setup, and losing it means
    the next check silently fails to correlate."""
    store.apply(
        [Mutation(MutationKind.PLAYER_CHARACTER, "111", {"actor": "Caspian Varr"})],
        reason="roster",
    )
    store.apply([gold("Caspian Varr", 250)], reason="loot")
    store.apply(
        [Mutation(MutationKind.QUEST, "q1", {"title": "Find the caravan"})], reason="quest"
    )
    assert store.state()["quests"]

    store.reset_campaign()

    st = store.state()
    assert st["players"] == {"111": "Caspian Varr"}  # the table survives
    assert st["quests"] == {}
    assert st["party"] == {}
    assert st["world_minutes"] == 0


def test_reset_preserves_the_journal(backend_factory, clock):
    """The campaign is gone; the record that it existed is not."""
    first = CampaignStore(backend=backend_factory(), clock=clock)
    first.apply([gold("Kethra", 10)], reason="before the reset")
    first.reset_campaign()

    kinds = [e["kind"] for e in first.journal()]
    assert "gold" in kinds  # history of the old campaign survives
    assert kinds[-1] == "campaign_reset"

    reopened = CampaignStore(backend=backend_factory(), clock=clock)
    assert reopened.state()["party"] == {}  # and replay honours the reset


def test_setting_a_location_clears_a_stale_summary(store):
    """A summary describing somewhere else is worse than no summary at all."""
    store.apply(
        [Mutation(MutationKind.SCENE, "", {"location": "The Rusty Anchor",
                                           "summary": "Low tide, rain on the shutters"})],
        reason="scene",
    )
    assert store.state()["scene"]["summary"].startswith("Low tide")

    store.apply(
        [Mutation(MutationKind.SCENE, "", {"location": "Dock 9", "summary": ""})],
        reason="scene",
    )
    assert store.state()["scene"] == {"location": "Dock 9", "summary": ""}
