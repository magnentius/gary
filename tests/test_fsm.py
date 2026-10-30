"""The Director: a check is resolved only by the roll of the player it was asked of.

Gary is the only roller. These tests pin who may roll, that a roll happens once, and that
the outcome is arithmetic against the DC committed before the dice.
"""

from __future__ import annotations

from gary.fsm import ChannelState, PromptModel, RecordOutcome, SendMessage
from gary.models import CheckKind

CHANNEL = 555
ALICE = 111
BOB = 222


def open_check(director, user_id=ALICE, actor="Kethra", dc=15, **kw):
    return director.open_check(
        channel_id=CHANNEL,
        user_id=user_id,
        actor=actor,
        kind=kw.pop("kind", CheckKind.CHECK),
        key=kw.pop("key", "sleight of hand"),
        dc=dc,
        on_success="The door opens.",
        on_failure="The pick snaps.",
        **kw,
    )


def roll(director, user_id=ALICE):
    return director.roll(channel_id=CHANNEL, user_id=user_id)


def prompts_in(effects):
    return [e for e in effects if isinstance(e, PromptModel)]


# -- rolling -------------------------------------------------------------------------------


def test_open_check_asks_the_player_to_roll(director):
    effects = open_check(director)
    sends = [e for e in effects if isinstance(e, SendMessage)]
    assert len(sends) == 1
    assert "Kethra" in sends[0].text and "sleight of hand" in sends[0].text
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_full_loop_resolves_with_the_committed_dc(director, dice):
    open_check(director, dc=15)
    dice.queue(17)
    effects = roll(director)

    (prompt,) = prompts_in(effects)
    assert prompt.kind == "check_outcome"
    assert prompt.payload["succeeded"] is True
    assert prompt.payload["dc"] == 15
    assert prompt.payload["roll_total"] == 17
    assert prompt.payload["established_consequence"] == "The door opens."


def test_a_miss_takes_the_failure_branch(director, dice):
    open_check(director, dc=15)
    dice.queue(14)
    (prompt,) = prompts_in(roll(director))
    assert prompt.payload["succeeded"] is False
    assert prompt.payload["established_consequence"] == "The pick snaps."


def test_the_dice_are_shown_to_the_table(director, dice):
    """Gary is referee and dice both. A roll nobody can see is one nobody can trust."""
    open_check(director)
    dice.queue(13)
    sends = [e.text for e in roll(director) if isinstance(e, SendMessage)]
    assert sends == ["🎲 **Kethra** rolls 13."]


def test_the_dice_are_posted_before_the_narration_is_asked_for(director, dice):
    open_check(director)
    dice.queue(13)
    kinds = [type(e) for e in roll(director)]
    assert kinds.index(SendMessage) < kinds.index(PromptModel)


def test_a_check_rolls_a_d20(director, dice):
    open_check(director)
    dice.queue(9)
    roll(director)
    assert [r.spec for r in dice.rolls] == ["1d20"]


def test_resolution_emits_a_record_alongside_the_narration(director, dice):
    """Narration and record are emitted together so they cannot diverge."""
    open_check(director, dc=15)
    dice.queue(19)
    records = [e for e in roll(director) if isinstance(e, RecordOutcome)]
    assert len(records) == 1
    assert records[0].outcome.dc == 15
    assert records[0].outcome.total == 19
    assert records[0].outcome.faces == (19,)
    assert records[0].outcome.succeeded is True


def test_a_check_rolls_once(director, dice):
    open_check(director)
    dice.queue(17)
    roll(director)
    assert roll(director) == []  # nothing left to roll, and no second die is thrown
    assert len(dice.rolls) == 1
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_another_player_cannot_roll_your_check(director, dice):
    """The request carries the roller. Bob asking finds nothing of Alice's."""
    open_check(director, user_id=ALICE)
    assert roll(director, user_id=BOB) == []
    assert dice.rolls == []
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_two_players_roll_their_own_checks(director, dice):
    """Concurrent checks never cross: each roll resolves its own player's DC."""
    open_check(director, user_id=ALICE, actor="Kethra", dc=10)
    open_check(director, user_id=BOB, actor="Dorn", dc=25)
    dice.queue(18, 18)

    (bob,) = prompts_in(roll(director, user_id=BOB))
    (alice,) = prompts_in(roll(director, user_id=ALICE))
    assert (bob.payload["actor"], bob.payload["succeeded"]) == ("Dorn", False)
    assert (alice.payload["actor"], alice.payload["succeeded"]) == ("Kethra", True)


def test_rolling_with_nothing_pending_does_nothing(director, dice):
    assert roll(director) == []
    assert dice.rolls == []


def test_a_missing_key_never_reaches_the_table(director):
    """Bounced back to the model, not shown to players."""
    effects = open_check(director, key="   ")
    assert not any(isinstance(e, SendMessage) for e in effects)
    prompts = prompts_in(effects)
    assert prompts and prompts[0].kind == "system_note"
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_supersede_is_announced(director):
    open_check(director, key="stealth")
    effects = open_check(director, key="perception")
    texts = [e.text for e in effects if isinstance(e, SendMessage)]
    assert any("Superseding" in t for t in texts)


# -- expiry --------------------------------------------------------------------------------


def test_expired_check_narrates_a_pivot_and_returns_to_idle(director, clock):
    open_check(director, ttl_seconds=60)
    clock.advance(61)

    prompts = prompts_in(director.sweep())
    assert len(prompts) == 1
    assert prompts[0].kind == "check_expired"
    assert "Do not resolve" in prompts[0].payload["instruction"]
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_an_expired_check_can_no_longer_be_rolled(director, clock, dice):
    open_check(director, ttl_seconds=60)
    clock.advance(61)
    assert roll(director) == []  # even before the sweep has run
    assert dice.rolls == []


# -- player routing ------------------------------------------------------------------------


def test_unregistered_channel_is_ignored(director):
    effects = director.on_player_message(
        channel_id=9999, user_id=ALICE, display_name="Alice",
        content="I open the door", mentions_bot=True,
    )
    assert effects == []


def test_unaddressed_chatter_does_not_reach_the_model(director):
    """Table talk is not a prompt (ARCHITECTURE.md §4.2)."""
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="brb getting coffee", mentions_bot=False,
    )
    assert effects == []


def test_mention_reaches_the_model(director):
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="@Gary I pick the lock", mentions_bot=True,
    )
    assert len(effects) == 1 and isinstance(effects[0], PromptModel)


def test_player_with_open_check_is_heard_without_a_mention(director):
    open_check(director, user_id=ALICE)
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="wait, I use my thieves' tools instead", mentions_bot=False,
    )
    assert len(effects) == 1


def test_freeform_mode_hears_everything(director):
    director.freeform_channels.add(CHANNEL)
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=BOB, display_name="Bob",
        content="I look around", mentions_bot=False,
    )
    assert len(effects) == 1


# -- state is derived, not stored ------------------------------------------------------------


def test_player_chatter_does_not_drop_the_roll_that_follows(director, dice):
    """Regression: storing channel state let ordinary table talk cancel a pending check.

    Sequence that broke it: open a check, the player says "rolling now!", then rolls. The
    chatter flipped the channel out of AWAITING_ROLL and the roll was silently dropped.
    """
    open_check(director, user_id=ALICE, actor="Kethra", dc=15)

    director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="rolling now!", mentions_bot=False,
    )
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL

    dice.queue(17)
    prompts = prompts_in(roll(director))
    assert prompts, "the roll was dropped"
    assert prompts[0].payload["succeeded"] is True


def test_unrelated_player_chatter_also_leaves_the_gate_open(director):
    """A different player talking must not disturb Alice's pending check either."""
    open_check(director, user_id=ALICE, actor="Kethra")
    director.freeform_channels.add(CHANNEL)
    director.on_player_message(
        channel_id=CHANNEL, user_id=BOB, display_name="Bob",
        content="ooh, tense", mentions_bot=False,
    )
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_state_is_derived_from_the_registry(director, registry):
    """No stored copy to diverge: mutate the registry, the state follows."""
    assert director.state(CHANNEL) is ChannelState.IDLE
    check, _ = registry.open(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", kind=CheckKind.CHECK,
        key="stealth", dc=10, on_success="a", on_failure="b", stakes="",
        dice="1d20",
    )
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL
    registry.consume(check.check_id)
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_a_fresh_director_on_the_same_registry_agrees(director, registry, clock, dice):
    """The property that makes the process replaceable: no state to hand over."""
    from gary.fsm import Director

    open_check(director, user_id=ALICE, actor="Kethra", dc=15)

    replacement = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice)
    assert replacement.state(CHANNEL) is ChannelState.AWAITING_ROLL

    dice.queue(17)
    assert prompts_in(replacement.roll(channel_id=CHANNEL, user_id=ALICE))


def test_a_director_without_dice_rolls_real_ones(registry, clock):
    """Production passes nothing and gets real dice; only tests script them."""
    from gary.fsm import Director
    from gary.ports import RandomDice

    assert isinstance(Director(registry=registry, clock=clock).dice, RandomDice)


# -- the roster: who is playing which character ------------------------------------------


def _cast(director):
    """A Director that knows Alice plays Kethra."""
    director.set_player_character(ALICE, "Kethra")
    return director


def test_player_text_carries_the_character_name(director):
    """Without this the model only ever sees a Discord display name, and opens checks for
    a character nobody is playing."""
    _cast(director)
    open_check(director)  # so the message is 'addressed'
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="John K",
        content="I pick the lock", mentions_bot=False,
    )
    prompts = prompts_in(effects)
    assert prompts and prompts[0].payload["actor"] == "Kethra"


def test_unknown_actor_is_refused_not_opened(director):
    """Nobody could ever roll it -- a silent dead end that TTL later narrates as player
    hesitation."""
    _cast(director)
    effects = director.open_check(
        channel_id=CHANNEL, user_id=ALICE, actor="John K", kind=CheckKind.CHECK,
        key="stealth", dc=15, on_success="ok", on_failure="no",
    )
    assert len(effects) == 1
    assert isinstance(effects[0], PromptModel)
    assert effects[0].kind == "system_note"
    assert "Kethra" in effects[0].payload["error"]
    assert director.state(CHANNEL) is ChannelState.IDLE  # nothing was opened
    assert not any(isinstance(e, SendMessage) for e in effects)


def test_known_actor_still_opens(director):
    _cast(director)
    effects = director.open_check(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", kind=CheckKind.CHECK,
        key="stealth", dc=15, on_success="ok", on_failure="no",
    )
    assert any(isinstance(e, SendMessage) for e in effects)
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_no_roster_configured_validates_nothing(director):
    """An unconfigured table must keep working exactly as before."""
    effects = director.open_check(
        channel_id=CHANNEL, user_id=ALICE, actor="Anyone At All", kind=CheckKind.CHECK,
        key="stealth", dc=15, on_success="ok", on_failure="no",
    )
    assert any(isinstance(e, SendMessage) for e in effects)


def test_set_player_character_updates_validation(director):
    """?dm playing must take effect immediately, without a restart."""
    effects = director.open_check(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", kind=CheckKind.CHECK,
        key="stealth", dc=15, on_success="ok", on_failure="no",
    )
    assert any(isinstance(e, SendMessage) for e in effects)  # no roster yet: anything goes

    director.set_player_character(ALICE, "Kethra")
    rejected = director.open_check(
        channel_id=CHANNEL, user_id=ALICE, actor="John K", kind=CheckKind.CHECK,
        key="stealth", dc=15, on_success="ok", on_failure="no",
    )
    assert rejected[0].kind == "system_note"


def test_forget_everything_drops_the_in_memory_roster(director):
    """The roster lives here as well as in the store, so clearing the store alone would
    leave a stale roster answering for characters who no longer exist."""
    director.set_player_character(ALICE, "Kethra")
    assert director.players

    director.forget_everything()

    assert director.players == {}
    # and an actor is no longer validated against a roster that is gone
    effects = director.open_check(
        channel_id=CHANNEL, user_id=ALICE, actor="Anyone", kind=CheckKind.CHECK,
        key="stealth", dc=10, on_success="ok", on_failure="no",
    )
    assert any(isinstance(e, SendMessage) for e in effects)
