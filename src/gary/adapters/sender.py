"""Outbound message handling (ARCHITECTURE.md §10).

Two constraints Discord imposes that narrative prose runs into constantly:

* **2000 characters per message.** Gemini will exceed this regularly, and an unsplit
  send is a hard API error that loses the turn.
* **5 messages per 5 seconds per channel.** Exceeding it gets the bot rate-limited at
  exactly the moment a dramatic beat is landing.

Splitting is pure and exhaustively tested. Pacing takes an injected clock and sleep, so
the tests do not spend real seconds proving it waits.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Dict, List, Optional

DISCORD_LIMIT = 2000
FENCE = "```"
# Headroom for the closing and reopening fence when a split lands inside a code block.
FENCE_RESERVE = 8


def _find_cut(text: str, limit: int) -> int:
    """The best place to break ``text`` at or before ``limit``.

    Preference order is paragraph, line, sentence, word, then a hard cut. Searching only
    the last third avoids the degenerate case where an early newline produces a
    two-character chunk and pushes everything into the next one.
    """
    window = text[:limit]
    floor = limit // 3

    for sep in ("\n\n", "\n"):
        idx = window.rfind(sep)
        if idx >= floor:
            return idx + len(sep)

    for sep in (". ", "! ", "? "):
        idx = window.rfind(sep)
        if idx >= floor:
            return idx + len(sep)

    idx = window.rfind(" ")
    if idx >= floor:
        return idx + 1

    return limit  # a single unbroken run; cut it


def _repair_fences(chunks: List[str]) -> List[str]:
    """Close and reopen code fences that a split landed inside.

    Without this, one chunk ends with an unterminated fence — Discord renders the rest of
    that message as code and the next message as plain text, mangling both.
    """
    out: List[str] = []
    open_fence = False
    for chunk in chunks:
        if open_fence:
            chunk = FENCE + "\n" + chunk
        if chunk.count(FENCE) % 2 == 1:
            chunk = chunk + "\n" + FENCE
            open_fence = True
        else:
            open_fence = False
        out.append(chunk)
    return out


def split_message(text: str, limit: int = DISCORD_LIMIT) -> List[str]:
    """Split for Discord. Returns [] for empty input — Discord rejects empty sends."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    effective = limit - FENCE_RESERVE if FENCE in text else limit

    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= effective:
            chunks.append(remaining)
            break
        cut = _find_cut(remaining, effective)
        piece = remaining[:cut].rstrip()
        if piece:
            chunks.append(piece)
        remaining = remaining[cut:].lstrip("\n")

    return _repair_fences(chunks)


class ChannelSender:
    """Serialises and paces sends per channel.

    Per-channel rather than global: two tables running in two channels should not slow
    each other down, and Discord's limit is per-channel anyway.
    """

    def __init__(
        self,
        transport,
        interval_seconds: float = 1.0,
        limit: int = DISCORD_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._transport = transport
        self._interval = interval_seconds
        self._limit = limit
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._locks: Dict[int, asyncio.Lock] = {}
        self._last_sent: Dict[int, float] = {}

    def _lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    async def send(self, channel_id: int, text: str) -> None:
        parts = split_message(text, self._limit)
        if not parts:
            return

        # The lock spans every part, so a long narration cannot be interleaved with
        # another turn's output mid-paragraph.
        async with self._lock(channel_id):
            for part in parts:
                await self._wait_turn(channel_id)
                await self._transport.send(channel_id, part)
                self._last_sent[channel_id] = self._clock()

    async def _wait_turn(self, channel_id: int) -> None:
        last = self._last_sent.get(channel_id)
        if last is None:
            return
        elapsed = self._clock() - last
        if elapsed < self._interval:
            await self._sleep(self._interval - elapsed)
