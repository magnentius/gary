"""Performing effects (ARCHITECTURE.md §4, §10).

The core returns ``Effect`` objects and performs nothing. This is where they become real:
messages sent, model called, drops counted.

Two properties worth stating outright, because both are failure modes that only appear
mid-session:

* **Depth is bounded.** Narrating can produce more effects — the model calls
  ``open_check``, which posts a command. Without a bound, a model that keeps opening
  checks in response to its own output loops until the quota runs out. The bound is
  small and exceeding it is logged loudly, because it means the model is misbehaving.
* **A narrator failure never silences the channel.** If the model errors or returns
  nothing, players get an in-character stall rather than a bot that appears dead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..fsm import AskPlayer, Effect, LogDrop, PromptModel, SendMessage
from ..ports import Narrator, NarrationResult

log = logging.getLogger("drelm.runner")

STALL_MESSAGE = "*(The DM pauses, gathering the thread...)*"


@dataclass
class RunnerStats:
    """Counters worth watching.

    ``drops_by_status`` is the leading indicator from §10: a rising ``unknown`` rate is
    how an Avrae format change announces itself, well before anyone reports a bug.
    """

    sent: int = 0
    narrated: int = 0
    narrator_failures: int = 0
    depth_exceeded: int = 0
    drops_by_status: Dict[str, int] = field(default_factory=dict)

    def drop(self, status: str) -> None:
        self.drops_by_status[status] = self.drops_by_status.get(status, 0) + 1


class EffectRunner:
    def __init__(
        self,
        sender,
        narrator: Narrator,
        max_depth: int = 4,
        stall_message: str = STALL_MESSAGE,
    ) -> None:
        self._sender = sender
        self._narrator = narrator
        self._max_depth = max_depth
        self._stall = stall_message
        self.stats = RunnerStats()

    async def run(self, effects: Sequence[Effect], depth: int = 0) -> None:
        if depth > self._max_depth:
            self.stats.depth_exceeded += 1
            log.error(
                "effect depth %d exceeded limit %d; dropping %d effect(s). "
                "The model is most likely opening checks in a loop.",
                depth,
                self._max_depth,
                len(effects),
            )
            return

        for effect in effects:
            await self._run_one(effect, depth)

    async def _run_one(self, effect: Effect, depth: int) -> None:
        if isinstance(effect, (SendMessage, AskPlayer)):
            await self._sender.send(effect.channel_id, effect.text)
            self.stats.sent += 1
            return

        if isinstance(effect, LogDrop):
            self.stats.drop(effect.status)
            log.info(
                "dropped avrae message in %d (status=%s): %s",
                effect.channel_id,
                effect.status,
                effect.reason,
            )
            return

        if isinstance(effect, PromptModel):
            await self._narrate(effect, depth)
            return

        log.warning("no handler for effect %r", type(effect).__name__)

    async def _narrate(self, effect: PromptModel, depth: int) -> None:
        try:
            result: NarrationResult = await self._narrator.narrate(
                channel_id=effect.channel_id,
                kind=effect.kind,
                payload=effect.payload,
            )
        except Exception:  # noqa: BLE001 - a model failure must not kill the session
            self.stats.narrator_failures += 1
            log.exception("narrator failed for kind=%s", effect.kind)
            await self._sender.send(effect.channel_id, self._stall)
            return

        self.stats.narrated += 1

        text = (result.text or "").strip()
        if text:
            await self._sender.send(effect.channel_id, text)
            self.stats.sent += 1
        elif not result.effects:
            # Nothing said and nothing to do. Silence here reads as a crashed bot.
            log.warning("narrator returned nothing for kind=%s", effect.kind)
            await self._sender.send(effect.channel_id, self._stall)

        if result.effects:
            await self.run(result.effects, depth + 1)
