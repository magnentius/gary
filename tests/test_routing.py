"""Ingest routing (ARCHITECTURE.md §6.3).

The ordering of these checks is the whole substance of the module, and getting it wrong
produces failures that look like anything but a routing bug: a bot arguing with itself,
or a system that never resolves a roll.
"""

from __future__ import annotations

import pytest

from gary.adapters.routing import Route, route_message

GARY = 999
AVRAE = 261302296103747584
ALICE = 111
OTHER_BOT = 777
GAME = frozenset({555})


def route(**kw) -> Route:
    base = dict(
        author_id=ALICE,
        author_is_bot=False,
        channel_id=555,
        content="I open the door",
        bot_user_id=GARY,
        avrae_bot_id=AVRAE,
        game_channels=GAME,
        command_prefix="?dm ",
    )
    base.update(kw)
    return route_message(**base)


def test_own_messages_are_ignored():
    """Gary posts into the channel it listens to; without this it answers itself."""
    assert route(author_id=GARY, author_is_bot=True) is Route.IGNORE_SELF


def test_self_check_precedes_everything():
    """Even a message that would otherwise look like a command."""
    assert route(author_id=GARY, author_is_bot=True, content="?dm undo") is Route.IGNORE_SELF


def test_avrae_is_routed_despite_being_a_bot():
    """A blanket 'ignore bots' ahead of this discards every roll the system needs."""
    assert route(author_id=AVRAE, author_is_bot=True, content="") is Route.AVRAE


def test_avrae_outside_a_game_channel_still_routes():
    """One drop path, so the logs say why -- 'no pending check', not 'wrong channel'."""
    assert route(author_id=AVRAE, author_is_bot=True, channel_id=42) is Route.AVRAE


def test_other_bots_are_ignored():
    assert route(author_id=OTHER_BOT, author_is_bot=True) is Route.IGNORE_BOT


def test_players_in_game_channels_are_routed():
    assert route() is Route.PLAYER


def test_players_elsewhere_are_ignored():
    assert route(channel_id=42) is Route.NOT_A_GAME_CHANNEL


def test_dm_commands_are_split_from_player_speech():
    """Otherwise '?dm undo' is narrated as though a character said it aloud."""
    assert route(content="?dm undo") is Route.DM_COMMAND


def test_command_prefix_must_match_exactly():
    assert route(content="?dmundo") is Route.PLAYER
    assert route(content=" ?dm undo") is Route.PLAYER


@pytest.mark.parametrize("content", ["!check stealth", "!init begin", "!attack sword"])
def test_avrae_commands_typed_by_players_are_ordinary_speech(content):
    """Gary must not act on the command; it waits for Avrae's answer to it."""
    assert route(content=content) is Route.PLAYER


def test_missing_bot_id_does_not_crash_before_ready():
    """on_message can fire before the Gateway has told us who we are."""
    assert route(bot_user_id=None) is Route.PLAYER


def test_avrae_id_is_configurable():
    """Hardcoding an ID that turns out wrong is how v1 shipped a dead parser."""
    assert route(author_id=12345, author_is_bot=True, avrae_bot_id=12345) is Route.AVRAE
