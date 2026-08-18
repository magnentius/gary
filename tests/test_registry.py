"""Correlation: the property that stops every Avrae message becoming an LLM call."""

from __future__ import annotations

from gary.models import AvraeRoll, CancelReason, CheckKind

CHANNEL = 555
ALICE = 111
BOB = 222


def open_for(registry, user_id, actor, key="stealth", dc=15, ttl=None):
    return registry.open(
        channel_id=CHANNEL,
        user_id=user_id,
        actor=actor,
        kind=CheckKind.CHECK,
        key=key,
        dc=dc,
        on_success="ok",
        on_failure="no",
        stakes="",
        command=f"!check {key}",
        ttl_seconds=ttl,
    )


def test_unmatched_roll_returns_none(registry):
    """The common case: Avrae output nobody is waiting for is dropped."""
    assert registry.match(channel_id=CHANNEL, roll=AvraeRoll("Kethra", 17)) is None


def test_matches_by_actor_name(registry):
    check, _ = open_for(registry, ALICE, "Kethra")
    matched = registry.match(channel_id=CHANNEL, roll=AvraeRoll("Kethra", 17))
    assert matched is not None and matched.check_id == check.check_id


def test_actor_matching_ignores_case_and_punctuation(registry):
    open_for(registry, ALICE, "Kethra Vane")
    assert registry.match(channel_id=CHANNEL, roll=AvraeRoll("kethra vane", 17)) is not None
    assert registry.match(channel_id=CHANNEL, roll=AvraeRoll("Kethra-Vane", 17)) is not None


def test_interaction_id_beats_actor_name(registry):
    """Two players, one character name. Discord's own attribution wins."""
    open_for(registry, ALICE, "Kethra")
    bob_check, _ = open_for(registry, BOB, "Kethra")
    matched = registry.match(
        channel_id=CHANNEL,
        roll=AvraeRoll("Kethra", 17),
        interaction_user_id=BOB,
    )
    assert matched.check_id == bob_check.check_id


def test_reply_reference_used_when_no_interaction(registry):
    alice_check, _ = open_for(registry, ALICE, "Kethra")
    open_for(registry, BOB, "Dorn")
    matched = registry.match(
        channel_id=CHANNEL, roll=AvraeRoll("", 17), referenced_user_id=ALICE
    )
    assert matched.check_id == alice_check.check_id


def test_concurrent_players_do_not_cross_talk(registry):
    """The v1 misattribution bug: simultaneous rolls landing on the wrong check."""
    alice_check, _ = open_for(registry, ALICE, "Kethra")
    bob_check, _ = open_for(registry, BOB, "Dorn")

    assert registry.match(channel_id=CHANNEL, roll=AvraeRoll("Dorn", 9)).check_id == bob_check.check_id
    assert registry.match(channel_id=CHANNEL, roll=AvraeRoll("Kethra", 22)).check_id == alice_check.check_id


def test_other_channels_are_isolated(registry):
    open_for(registry, ALICE, "Kethra")
    assert registry.match(channel_id=CHANNEL + 1, roll=AvraeRoll("Kethra", 17)) is None


def test_second_check_supersedes_first(registry):
    first, displaced = open_for(registry, ALICE, "Kethra")
    assert displaced is None
    second, displaced = open_for(registry, ALICE, "Kethra", key="perception")
    assert displaced is not None and displaced.check_id == first.check_id
    assert list(registry.open_checks(CHANNEL)) == [second]


def test_expiry_stops_matching(registry, clock):
    open_for(registry, ALICE, "Kethra", ttl=60)
    clock.advance(59)
    assert registry.match(channel_id=CHANNEL, roll=AvraeRoll("Kethra", 17)) is not None
    clock.advance(2)
    assert registry.match(channel_id=CHANNEL, roll=AvraeRoll("Kethra", 17)) is None


def test_sweep_reports_expired_checks(registry, clock):
    open_for(registry, ALICE, "Kethra", ttl=60)
    open_for(registry, BOB, "Dorn", ttl=600)
    clock.advance(61)

    swept = registry.sweep_expired()
    assert [c.check.actor for c in swept] == ["Kethra"]
    assert swept[0].reason is CancelReason.EXPIRED
    assert [c.actor for c in registry.open_checks(CHANNEL)] == ["Dorn"]


def test_consume_removes_the_check(registry):
    check, _ = open_for(registry, ALICE, "Kethra")
    assert registry.consume(check.check_id) is not None
    assert registry.consume(check.check_id) is None
    assert not registry.has_open(CHANNEL)


def test_restore_after_restart(registry, clock):
    """Pending checks survive a Gary restart (ARCHITECTURE.md §10)."""
    check, _ = open_for(registry, ALICE, "Kethra")
    from gary.registry import PendingCheckRegistry

    fresh = PendingCheckRegistry(clock=clock)
    assert not fresh.has_open(CHANNEL)
    fresh.restore([check])
    assert fresh.match(channel_id=CHANNEL, roll=AvraeRoll("Kethra", 17)) is not None
