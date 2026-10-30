"""Ingest routing (ARCHITECTURE.md §4.2).

The ordering of these checks is the whole substance of the module, and getting it wrong
produces failures that look like anything but a routing bug: a bot arguing with itself,
or a player's words narrated as a command.
"""

from __future__ import annotations

import pytest

from gary.adapters.routing import Route, route_message

GARY = 999
DICE_BOT = 261302296103747584
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


def test_dice_bots_are_ignored_like_any_other_bot():
    """Gary rolls its own dice. Another bot's roll means nothing to it."""
    assert route(author_id=DICE_BOT, author_is_bot=True, content="") is Route.IGNORE_BOT


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


@pytest.mark.parametrize("content", ["!check stealth", "!r 2d6", "!attack sword"])
def test_other_bots_commands_typed_by_players_are_ordinary_speech(content):
    """Another bot's prefix is not Gary's: the message is a player talking."""
    assert route(content=content) is Route.PLAYER


def test_missing_bot_id_does_not_crash_before_ready():
    """on_message can fire before the Gateway has told us who we are."""
    assert route(bot_user_id=None) is Route.PLAYER
