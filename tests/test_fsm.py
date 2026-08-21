"""The Director gate: only AWAITING_ROLL consumes Avrae output.

The first test is the one that matters most -- it is the direct regression test for v1's
central bug, where every Avrae message triggered a Gemini call.
"""

from __future__ import annotations

from gary.fsm import AskPlayer, ChannelState, LogDrop, PromptModel, SendMessage
from gary.models import AvraeRoll, CheckKind
from gary.parser.avrae import ParsedAvraeMessage, ParseStatus

CHANNEL = 555
ALICE = 111
BOB = 222


def roll_msg(actor="Kethra", total=17, user_id=None):
    return ParsedAvraeMessage(
        status=ParseStatus.ROLL,
        roll=AvraeRoll(actor=actor, total=total),
        interaction_user_id=user_id,
    )


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


# -- the gate ------------------------------------------------------------------------------


def test_avrae_output_while_idle_never_reaches_the_model(director):
    """v1 regression: !help, sheet lookups and stray rolls each cost an LLM call."""
    assert director.state(CHANNEL) is ChannelState.IDLE
    effects = director.on_avrae_message(channel_id=CHANNEL, parsed=roll_msg())
    assert all(isinstance(e, LogDrop) for e in effects)
    assert not any(isinstance(e, PromptModel) for e in effects)


def test_irrelevant_embed_while_awaiting_is_dropped(director):
    open_check(director)
    effects = director.on_avrae_message(
        channel_id=CHANNEL,
        parsed=ParsedAvraeMessage(status=ParseStatus.IRRELEVANT, reason="help text"),
    )
    assert all(isinstance(e, LogDrop) for e in effects)
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_initiative_embeds_never_resolve_a_check(director):
    open_check(director)
    effects = director.on_avrae_message(
        channel_id=CHANNEL, parsed=ParsedAvraeMessage(status=ParseStatus.INITIATIVE)
    )
    assert all(isinstance(e, LogDrop) for e in effects)
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_unreadable_embed_asks_rather_than_guessing(director):
    """Fail loud (ARCHITECTURE.md §6.1): never degrade a parse miss into a false result."""
    open_check(director)
    effects = director.on_avrae_message(
        channel_id=CHANNEL,
        parsed=ParsedAvraeMessage(status=ParseStatus.UNKNOWN, reason="no total"),
    )
    assert any(isinstance(e, AskPlayer) for e in effects)
    assert not any(isinstance(e, PromptModel) for e in effects)


def test_unmatched_roll_while_awaiting_is_dropped(director):
    """Alice is rolling; Bob's unrelated roll must not resolve her check."""
    open_check(director, user_id=ALICE, actor="Kethra")
    effects = director.on_avrae_message(channel_id=CHANNEL, parsed=roll_msg(actor="Dorn"))
    assert all(isinstance(e, LogDrop) for e in effects)


# -- the happy path ------------------------------------------------------------------------


def test_open_check_posts_a_rendered_command(director):
    effects = open_check(director)
    sends = [e for e in effects if isinstance(e, SendMessage)]
    assert len(sends) == 1
    assert '`!check "sleight of hand"`' in sends[0].text
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_full_loop_resolves_with_the_committed_dc(director):
    open_check(director, dc=15)
    effects = director.on_avrae_message(channel_id=CHANNEL, parsed=roll_msg(total=17))
    prompts = [e for e in effects if isinstance(e, PromptModel)]
    assert len(prompts) == 1
    payload = prompts[0].payload
    assert payload["succeeded"] is True
    assert payload["dc"] == 15
    assert payload["established_consequence"] == "The door opens."


def test_check_is_consumed_so_a_second_roll_does_nothing(director):
    open_check(director)
    director.on_avrae_message(channel_id=CHANNEL, parsed=roll_msg(total=17))
    again = director.on_avrae_message(channel_id=CHANNEL, parsed=roll_msg(total=25))
    assert all(isinstance(e, LogDrop) for e in again)


def test_invalid_key_never_posts_to_the_channel(director):
    """A hallucinated skill is bounced back to the model, not shown to players."""
    effects = open_check(director, key="!init end")
    assert not any(isinstance(e, SendMessage) for e in effects)
    prompts = [e for e in effects if isinstance(e, PromptModel)]
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

    effects = director.sweep()
    prompts = [e for e in effects if isinstance(e, PromptModel)]
    assert len(prompts) == 1
    assert prompts[0].kind == "check_expired"
    assert "Do not resolve" in prompts[0].payload["instruction"]
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_expired_check_no_longer_resolves(director, clock):
    open_check(director, ttl_seconds=60)
    clock.advance(61)
    director.sweep()
    effects = director.on_avrae_message(channel_id=CHANNEL, parsed=roll_msg())
    assert all(isinstance(e, LogDrop) for e in effects)


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


def test_player_chatter_does_not_drop_the_roll_that_follows(director):
    """Regression: storing channel state let ordinary table talk cancel a pending check.

    Sequence that broke it: open a check, the player says "rolling now!", then the roll
    arrives. The chatter flipped the channel out of AWAITING_ROLL and the roll was
    silently dropped -- the player sees dice in the channel and the DM says nothing.
    """
    open_check(director, user_id=ALICE, actor="Kethra", dc=15)

    director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="rolling now!", mentions_bot=False,
    )
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL

    effects = director.on_avrae_message(channel_id=CHANNEL, parsed=roll_msg(total=17))
    prompts = [e for e in effects if isinstance(e, PromptModel)]
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
        command="!check stealth",
    )
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL
    registry.consume(check.check_id)
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_a_fresh_director_on_the_same_registry_agrees(director, registry, clock):
    """The property that makes the process replaceable: no state to hand over."""
    from gary.fsm import Director

    open_check(director, user_id=ALICE, actor="Kethra", dc=15)

    replacement = Director(registry=registry, clock=clock, game_channels={CHANNEL})
    assert replacement.state(CHANNEL) is ChannelState.AWAITING_ROLL

    effects = replacement.on_avrae_message(channel_id=CHANNEL, parsed=roll_msg(total=17))
    assert any(isinstance(e, PromptModel) for e in effects)


def test_avrae_prompt_stays_silent_and_keeps_the_check_open(director):
    """Avrae asked the player a question instead of rolling.

    Captured live 2026-08-20: `!check notaskill` yields a "Multiple Matches Found"
    selector. Answering it produces a real roll that correlates normally, so the check
    must stay open -- and Gary must not speak, or the player is fielding two questions
    from two bots at once.
    """
    open_check(director)
    effects = director.on_avrae_message(
        channel_id=CHANNEL,
        parsed=ParsedAvraeMessage(
            status=ParseStatus.AVRAE_PROMPT, reason="avrae is awaiting player input"
        ),
    )
    assert all(isinstance(e, LogDrop) for e in effects)
    assert not any(isinstance(e, (AskPlayer, SendMessage)) for e in effects)
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_abandoned_selector_reposts_the_command(director):
    """The player never answered Avrae's selector, so no roll is coming.

    Captured live 2026-08-20: "Selection timed out or was cancelled." The check itself is
    still good -- its DC and consequences were fixed before the dice -- so the command is
    re-posted rather than left to lapse on TTL.
    """
    open_check(director)
    effects = director.on_avrae_message(
        channel_id=CHANNEL,
        parsed=ParsedAvraeMessage(
            status=ParseStatus.PROMPT_ABANDONED, reason="selector lapsed without an answer"
        ),
    )

    sends = [e for e in effects if isinstance(e, SendMessage)]
    assert len(sends) == 1
    assert "Kethra" in sends[0].text
    assert "!check" in sends[0].text or "sleight" in sends[0].text.lower()
    assert not any(isinstance(e, AskPlayer) for e in effects)
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_abandoned_selector_while_idle_says_nothing(director):
    """No open check means nothing to re-prompt; the gate drops it like any other."""
    assert director.state(CHANNEL) is ChannelState.IDLE
    effects = director.on_avrae_message(
        channel_id=CHANNEL,
        parsed=ParsedAvraeMessage(status=ParseStatus.PROMPT_ABANDONED, reason="lapsed"),
    )
    assert all(isinstance(e, LogDrop) for e in effects)


# -- the roster: who is playing which character ------------------------------------------


def _cast(director):
    """A Director that knows Alice plays Kethra."""
    director.players = {ALICE: "Kethra"}
    director._known_actors = {"kethra"}
    return director


def test_player_text_carries_the_character_name(director):
    """Without this the model only ever sees a Discord display name, so open_check names
    an actor Avrae has never heard of and the roll cannot correlate."""
    _cast(director)
    open_check(director)  # so the message is 'addressed'
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="John K",
        content="I pick the lock", mentions_bot=False,
    )
    prompts = [e for e in effects if isinstance(e, PromptModel)]
    assert prompts and prompts[0].payload["actor"] == "Kethra"


def test_unknown_actor_is_refused_not_opened(director):
    """Opening it would create a check no roll can match -- a silent dead end that TTL
    later narrates as player hesitation."""
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
