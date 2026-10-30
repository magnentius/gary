"""Attribution: a check belongs to the player it was asked of, and to nobody else."""

from __future__ import annotations

from gary.models import CancelReason, CheckKind

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
        dice="1d20",
        ttl_seconds=ttl,
    )


def test_nobody_waiting_returns_none(registry):
    assert registry.for_player(CHANNEL, ALICE) is None


def test_finds_the_players_own_check(registry):
    check, _ = open_for(registry, ALICE, "Kethra")
    found = registry.for_player(CHANNEL, ALICE)
    assert found is not None and found.check_id == check.check_id


def test_another_player_cannot_reach_it(registry):
    """Bob asking to roll finds nothing of Alice's, even playing a same-named character."""
    open_for(registry, ALICE, "Kethra")
    assert registry.for_player(CHANNEL, BOB) is None


def test_two_players_sharing_a_character_name_stay_apart(registry):
    """The case name matching could not resolve (§14 (6)): attribution is by user now."""
    alice_check, _ = open_for(registry, ALICE, "Kethra", dc=10)
    bob_check, _ = open_for(registry, BOB, "Kethra", dc=25)
    assert registry.for_player(CHANNEL, ALICE).check_id == alice_check.check_id
    assert registry.for_player(CHANNEL, BOB).check_id == bob_check.check_id


def test_other_channels_are_isolated(registry):
    open_for(registry, ALICE, "Kethra")
    assert registry.for_player(CHANNEL + 1, ALICE) is None


def test_second_check_supersedes_first(registry):
    first, displaced = open_for(registry, ALICE, "Kethra")
    assert displaced is None
    second, displaced = open_for(registry, ALICE, "Kethra", key="perception")
    assert displaced is not None and displaced.check_id == first.check_id
    assert list(registry.open_checks(CHANNEL)) == [second]


def test_expiry_stops_rolling(registry, clock):
    open_for(registry, ALICE, "Kethra", ttl=60)
    clock.advance(59)
    assert registry.for_player(CHANNEL, ALICE) is not None
    clock.advance(2)
    assert registry.for_player(CHANNEL, ALICE) is None


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
    assert fresh.for_player(CHANNEL, ALICE) is not None
