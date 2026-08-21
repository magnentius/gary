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


def test_reset_clears_everything_including_the_roster(store):
    """A reset that leaves something behind is a reset with an exception to remember.

    An earlier version kept the roster, on the grounds that who is playing whom is table
    setup rather than fiction. Forgetting `?dm playing` afterwards is loud -- the boot
    warning, and open_check refusing an actor who is not at the table -- rather than
    silent, so the exception was not worth its cost."""
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
    assert st["players"] == {}                       # the table goes too
    assert st["quests"] == {}
    assert st["party"] == {}
    assert st["world_minutes"] == 0


def test_reset_empties_the_journal(backend_factory, clock):
    """A reset that leaves the dead campaign readable in journal.ndjson has not reset
    anything a person can see. This is the one operation that destroys history."""
    first = CampaignStore(backend=backend_factory(), clock=clock)
    first.apply([gold("Kethra", 10)], reason="before the reset")
    first.apply(
        [Mutation(MutationKind.NOTE, "Leafmoor", {"body": "The Reeve takes bribes."})],
        reason="note",
    )
    first.reset_campaign()

    assert first.journal() == []
    assert first.state()["party"] == {}

    reopened = CampaignStore(backend=backend_factory(), clock=clock)
    assert reopened.journal() == []
    assert reopened.state()["party"] == {}
    assert reopened.state()["notes"] == []

    # And the journal is usable again: seq restarts at 0 rather than continuing from the
    # old length, which would leave a permanent gap that document-based backends key on.
    reopened.apply([gold("Kethra", 5)], reason="after the reset")
    assert [e["seq"] for e in reopened.journal()] == [0]


def test_reset_leaves_no_snapshot_pointing_past_the_journal(backend_factory, clock):
    """The snapshot's journal_offset must come back to 0 with the journal. Left at the
    old length it points past the end of an empty journal -- harmless until the next
    entry lands, which then sits below the offset and is never replayed."""
    store = CampaignStore(backend=backend_factory(), clock=clock)
    for i in range(5):
        store.apply([gold("Kethra", 1)], reason=f"turn {i}")
    store.reset_campaign()

    backend = backend_factory()
    loaded = backend.load_snapshot()
    assert loaded is not None
    _, offset = loaded
    assert offset == 0

    store.apply([gold("Kethra", 7)], reason="after")
    assert CampaignStore(backend=backend_factory(), clock=clock).state()["party"][
        "Kethra"
    ]["gold"] == 7


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


def test_parse_world_time_round_trips_the_formatter():
    """?dm time computes a forward delta, because the clock can only be advanced."""
    from gary.state.schema import empty_state, format_world_time, parse_world_time

    st = empty_state()
    assert format_world_time(st) == "Day 1, 00:00"      # a fresh campaign opens at midnight

    st["world_minutes"] = parse_world_time("18:30", st)
    assert format_world_time(st) == "Day 1, 18:30"

    st["world_minutes"] = parse_world_time("day 2 06:00", st)
    assert format_world_time(st) == "Day 2, 06:00"

    # a bare HH:MM means "today", where today comes from the current clock
    assert parse_world_time("07:00", st) == 1440 + 420

    for bad in ("25:00", "18:60", "soon", "", "day 0 10:00"):
        with pytest.raises(ValueError):
            parse_world_time(bad, st)


def test_mirror_sheet_caches_class_and_level(store):
    """Avrae owns the sheet (§3); this is only what Gary last saw go past."""
    store.mirror_sheet("Caspian Varr", "Human Fighter 1", 1)
    slot = store.state()["party"]["Caspian Varr"]
    assert slot["sheet"] == "Human Fighter 1"
    assert slot["level"] == 1

    store.mirror_sheet("Caspian Varr", "Human Fighter 2", 2)   # after !update, !sheet
    assert store.state()["party"]["Caspian Varr"]["level"] == 2


def test_mirroring_a_sheet_is_not_journalled(store):
    """Nothing to undo, and replaying a stale mirror is worse than not having one."""
    before = len(store.journal())
    store.mirror_sheet("Kethra", "Elf Wizard 4", 4)
    assert len(store.journal()) == before


def test_the_setting_is_journalled_and_undoable(store):
    store.apply([Mutation(MutationKind.SETTING, "", {"text": "A cold port city."})],
                reason="setting")
    assert store.state()["setting"] == "A cold port city."

    store.apply([Mutation(MutationKind.SETTING, "", {"text": "A warm desert city."})],
                reason="setting")
    assert store.state()["setting"] == "A warm desert city."

    store.undo_last()
    assert store.state()["setting"] == "A cold port city."


def test_undo_stops_at_a_reset(store):
    """`?dm reset` then `?dm undo` restored the wiped campaign's setting, silently.

    undo_last walks the whole journal and skips entries with no inverse -- and
    campaign_reset has none, so it was skipped rather than treated as a floor. The scan
    then reached the dead campaign's mutations and applied their inverses into the fresh
    one. Item inverses raise (the party carries nothing); setting, scene and NPC inverses
    apply cleanly, which is the dangerous half: a brand-new campaign silently inherits the
    old premise, and layer 2 of every prompt carries it from then on.
    """
    store.apply(
        [Mutation(MutationKind.SETTING, "", {"text": "The drowned town of Leafmoor."})],
        reason="setting",
    )
    store.apply(
        [Mutation(MutationKind.SCENE, "", {"location": "The Gullet", "summary": "Fog."})],
        reason="scene",
    )
    store.reset_campaign()

    assert store.undo_last() is None
    assert store.state().get("setting") == ""
    assert store.state()["scene"] == {"location": "", "summary": ""}

    # Still undoable above the floor: a reset ends the past, it does not disable undo.
    store.apply(
        [Mutation(MutationKind.SETTING, "", {"text": "A high desert empire."})],
        reason="setting",
    )
    assert store.undo_last() is not None
    assert store.state().get("setting") == ""


def test_replay_across_a_reset_does_not_resurrect_state(backend_factory, clock):
    """The snapshot's journal_offset means a reset is normally never replayed. Rebuilding
    from seq 0 -- a lost or corrupt snapshot -- must reach the same state, or the campaign
    silently depends on a cache file it is supposed to be able to lose."""

    class _NoSnapshot:
        """Forces the full replay path: everything else, straight through."""

        def __init__(self, inner):
            self._inner = inner

        def load_snapshot(self):
            return None

        def __getattr__(self, name):
            return getattr(self._inner, name)

    backend = backend_factory()
    store = CampaignStore(backend=backend, clock=clock)
    store.apply(
        [Mutation(MutationKind.SETTING, "", {"text": "The drowned town of Leafmoor."})],
        reason="setting",
    )
    store.apply(
        [Mutation(MutationKind.NPC_RELATION, "Mother Cray", {"disposition": "hostile"})],
        reason="npc",
    )
    store.reset_campaign()
    store.apply(
        [Mutation(MutationKind.SETTING, "", {"text": "A high desert empire."})],
        reason="setting",
    )

    rebuilt = CampaignStore(backend=_NoSnapshot(backend_factory()), clock=clock)
    assert rebuilt.state().get("setting") == "A high desert empire."
    assert rebuilt.state()["npcs"] == {}
