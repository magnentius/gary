"""Ingest routing (ARCHITECTURE.md §6.3).

Extracted from the discord.py event handler as a pure function, because the ordering of
these checks is the part that matters and the part that broke in v1. The handler around
it is glue.

Order is load-bearing:

1. **Self first.** Gary posts narration into the same channel it listens to. Anything
   else first risks a feedback loop where the bot answers itself forever.
2. **Avrae before the generic bot check.** Avrae *is* a bot; a blanket "ignore bots"
   ahead of this would discard every roll the system depends on.
3. **All other bots.** Other dice bots, webhooks, bridges — none are ours to interpret.
4. **Gary's own commands**, before ordinary player text, so ``?dm undo`` is never
   narrated at the table as though a character said it.
5. **Game channels only.** An unregistered channel is silence, not an error.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Optional


class Route(Enum):
    IGNORE_SELF = "ignore_self"
    AVRAE = "avrae"
    IGNORE_BOT = "ignore_bot"
    DM_COMMAND = "dm_command"
    PLAYER = "player"
    NOT_A_GAME_CHANNEL = "not_a_game_channel"


def route_message(
    *,
    author_id: int,
    author_is_bot: bool,
    channel_id: int,
    content: str,
    bot_user_id: Optional[int],
    avrae_bot_id: int,
    game_channels: FrozenSet[int],
    command_prefix: str,
) -> Route:
    """Decide what an inbound Discord message is. Pure; no I/O, no state."""
    if bot_user_id is not None and author_id == bot_user_id:
        return Route.IGNORE_SELF

    if author_id == avrae_bot_id:
        # Deliberately not gated on game_channels: Avrae output in an unregistered
        # channel still routes here, and the Director drops it for want of a pending
        # check. Keeping one drop path makes the logs tell the truth about why.
        return Route.AVRAE

    if author_is_bot:
        return Route.IGNORE_BOT

    if channel_id not in game_channels:
        return Route.NOT_A_GAME_CHANNEL

    if content.startswith(command_prefix):
        return Route.DM_COMMAND

    return Route.PLAYER
