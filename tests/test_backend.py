"""Storage backends and pending-check durability (ARCHITECTURE.md §7.3, §11).

The point of these tests is the disposable-VM property: everything the campaign depends
on must survive the process that produced it. Each backend is exercised through the same
suite, so a second implementation cannot quietly diverge.
"""

from __future__ import annotations

import pytest

from drelm.models import AvraeRoll, CheckKind, PendingCheck
from drelm.registry import PendingCheckRegistry
from drelm.state.backend import (
    CheckBackend,
    FileCheckBackend,
    FileStateBackend,
    InMemoryCheckBackend,
    InMemoryStateBackend,
    StateBackend,
)
from drelm.state.firestore import FirestoreCheckBackend, FirestoreStateBackend

from .conftest import FakeFirestoreClient

CHANNEL = 555
ALICE = 111


# -- state backends ----------------------------------------------------------------------


@pytest.fixture(params=["file", "memory", "firestore"])
def state_backend_factory(request, tmp_path):
    if request.param == "file":
        root = tmp_path / "state"
        return lambda: FileStateBackend(root)
    if request.param == "firestore":
        client = FakeFirestoreClient()
        return lambda: FirestoreStateBackend(client, "test-campaign")
    shared = InMemoryStateBackend()
    return lambda: shared


def test_empty_backend_has_no_snapshot(state_backend_factory):
    assert state_backend_factory().load_snapshot() is None
    assert state_backend_factory().read_journal() == []


def test_snapshot_round_trips_with_its_offset(state_backend_factory):
    backend = state_backend_factory()
    backend.write_snapshot({"schema_version": 1, "world_minutes": 90}, 7)

    state, offset = state_backend_factory().load_snapshot()
    assert state["world_minutes"] == 90
    assert offset == 7
    # journal_offset is transport metadata, not campaign state.
    assert "journal_offset" not in state


def test_journal_preserves_seq_order(state_backend_factory):
    backend = state_backend_factory()
    for i in range(10):
        backend.append_journal({"entry_id": f"e{i}", "kind": "gold", "n": i, "seq": i})

    entries = state_backend_factory().read_journal()
    assert [e["n"] for e in entries] == list(range(10))


def test_entry_without_seq_is_refused_not_silently_dropped(state_backend_factory):
    """A document-keyed backend would otherwise overwrite one entry ten times.

    Losing journal history is unrecoverable and fails invisibly -- nothing errors until
    someone replays and finds the campaign wrong.
    """
    backend = state_backend_factory()
    try:
        backend.append_journal({"entry_id": "e0", "kind": "gold", "n": 0})
    except ValueError:
        return  # refused, which is the required behaviour for document-based backends

    # Append-ordered backends may accept it, but must not lose the entry.
    backend.append_journal({"entry_id": "e1", "kind": "gold", "n": 1})
    assert len(state_backend_factory().read_journal()) == 2


def test_snapshot_is_a_copy_not_a_live_reference(state_backend_factory):
    backend = state_backend_factory()
    payload = {"schema_version": 1, "party": {"Kethra": {"gold": 10}}}
    backend.write_snapshot(payload, 0)

    payload["party"]["Kethra"]["gold"] = 999_999  # mutate the caller's dict
    state, _ = backend.load_snapshot()
    assert state["party"]["Kethra"]["gold"] == 10


def test_backends_satisfy_the_protocol(tmp_path):
    client = FakeFirestoreClient()
    assert isinstance(FileStateBackend(tmp_path / "a"), StateBackend)
    assert isinstance(InMemoryStateBackend(), StateBackend)
    assert isinstance(FirestoreStateBackend(client, "c"), StateBackend)
    assert isinstance(FileCheckBackend(tmp_path / "c.json"), CheckBackend)
    assert isinstance(InMemoryCheckBackend(), CheckBackend)
    assert isinstance(FirestoreCheckBackend(client, "c"), CheckBackend)


# -- check backends ----------------------------------------------------------------------


@pytest.fixture(params=["file", "memory", "firestore"])
def check_backend_factory(request, tmp_path):
    if request.param == "file":
        path = tmp_path / "checks.json"
        return lambda: FileCheckBackend(path)
    if request.param == "firestore":
        client = FakeFirestoreClient()
        return lambda: FirestoreCheckBackend(client, "test-campaign")
    shared = InMemoryCheckBackend()
    return lambda: shared


def make_check(check_id="01ABC", user_id=ALICE, dc=15) -> PendingCheck:
    return PendingCheck(
        check_id=check_id,
        channel_id=CHANNEL,
        user_id=user_id,
        actor="Kethra",
        kind=CheckKind.CHECK,
        key="sleight_of_hand",
        dc=dc,
        on_success="The tumblers give.",
        on_failure="The pick snaps.",
        stakes="the guard next door",
        command="!check sleightOfHand",
        created_at=1000.0,
        expires_at=1180.0,
    )


def test_check_round_trips_completely(check_backend_factory):
    """Every field must survive: a DC that comes back wrong is worse than none at all."""
    original = make_check()
    check_backend_factory().save(original)

    (restored,) = check_backend_factory().load_all()
    assert restored == original


def test_removed_checks_do_not_come_back(check_backend_factory):
    backend = check_backend_factory()
    backend.save(make_check("01A"))
    backend.save(make_check("01B", user_id=222))
    backend.remove("01A")

    assert [c.check_id for c in check_backend_factory().load_all()] == ["01B"]


def test_removing_an_unknown_check_is_harmless(check_backend_factory):
    check_backend_factory().remove("nope")


def test_corrupt_check_file_does_not_block_boot(tmp_path):
    """Losing pending checks costs one re-roll; failing to start costs the session."""
    path = tmp_path / "checks.json"
    path.write_text("{ this is not json")
    assert FileCheckBackend(path).load_all() == []


# -- the property that matters: restart mid-check ------------------------------------------


def test_pending_check_survives_a_process_restart(check_backend_factory, clock):
    """Drelm dies between "run this command" and the player rolling.

    The DC was committed before the dice; a restart must not lose it, or the guarantee
    the whole design exists to provide is void.
    """
    first = PendingCheckRegistry(clock=clock, backend=check_backend_factory())
    check, _ = first.open(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", kind=CheckKind.CHECK,
        key="stealth", dc=17, on_success="unseen", on_failure="spotted",
        stakes="", command="!check stealth",
    )

    # ... process restarts ...
    revived = PendingCheckRegistry(clock=clock, backend=check_backend_factory())

    matched = revived.match(channel_id=CHANNEL, roll=AvraeRoll("Kethra", 20))
    assert matched is not None
    assert matched.check_id == check.check_id
    assert matched.dc == 17
    assert matched.on_success == "unseen"


def test_resolved_check_does_not_survive_a_restart(check_backend_factory, clock):
    first = PendingCheckRegistry(clock=clock, backend=check_backend_factory())
    check, _ = first.open(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", kind=CheckKind.CHECK,
        key="stealth", dc=17, on_success="a", on_failure="b",
        stakes="", command="!check stealth",
    )
    first.consume(check.check_id)

    revived = PendingCheckRegistry(clock=clock, backend=check_backend_factory())
    assert list(revived.open_checks()) == []


def test_superseded_check_is_evicted_from_storage(check_backend_factory, clock):
    """One check per player -- the displaced one must not linger in the backend."""
    reg = PendingCheckRegistry(clock=clock, backend=check_backend_factory())
    kw = dict(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", kind=CheckKind.CHECK,
        dc=10, on_success="a", on_failure="b", stakes="", command="!check x",
    )
    reg.open(key="stealth", **kw)
    second, displaced = reg.open(key="perception", **kw)
    assert displaced is not None

    stored = check_backend_factory().load_all()
    assert [c.check_id for c in stored] == [second.check_id]


def test_expired_check_is_evicted_from_storage(check_backend_factory, clock):
    reg = PendingCheckRegistry(clock=clock, backend=check_backend_factory())
    reg.open(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", kind=CheckKind.CHECK,
        key="stealth", dc=10, on_success="a", on_failure="b", stakes="",
        command="!check stealth", ttl_seconds=60,
    )
    clock.advance(61)
    reg.sweep_expired()

    assert check_backend_factory().load_all() == []


def test_registry_without_a_backend_still_works(clock):
    """Persistence is opt-in; the core must not require it."""
    reg = PendingCheckRegistry(clock=clock)
    reg.open(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", kind=CheckKind.CHECK,
        key="stealth", dc=10, on_success="a", on_failure="b", stakes="",
        command="!check stealth",
    )
    assert reg.has_open(CHANNEL)
