"""Seams between the pure core and the outside world.

The core decides and returns ``Effect`` objects; something else performs them. These
protocols name that something, so the Discord adapter can be built and tested before the
Gemini adapter exists — and so neither can quietly acquire the other's responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore


@dataclass
class NarrationResult:
    """What a narrator produced.

    ``effects`` exists because narrating is not always the end of a turn: the model may
    call ``open_check``, which the LLM adapter routes through the Director and which
    yields further effects (posting the Avrae command). Returning them rather than
    performing them keeps the decision inside the core.
    """

    text: str = ""
    effects: List[Any] = field(default_factory=list)


@runtime_checkable
class Narrator(Protocol):
    """Turns a structured prompt payload into prose. Implemented by the Gemini adapter."""

    async def narrate(
        self,
        *,
        channel_id: int,
        kind: str,
        payload: Dict[str, Any],
    ) -> NarrationResult:
        """``kind`` is one of player_action | check_outcome | check_expired | system_note.

        Must not raise for ordinary model failures — return a NarrationResult with empty
        text instead, and let the caller decide what the channel sees. A raised exception
        here takes down a turn; a silent channel takes down the session's mood.
        """


@runtime_checkable
class MessageTransport(Protocol):
    """Sends text to a channel. Implemented by the Discord adapter."""

    async def send(self, channel_id: int, text: str) -> None: ...


class EchoNarrator:
    """A narrator with no model behind it.

    Useful well beyond tests: it lets the whole Discord path — intents, routing, parsing,
    correlation, resolution, sending — be exercised against a live Avrae with no API key
    and no token spend. Wire this up first; swap in Gemini once the plumbing is proven.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def narrate(
        self, *, channel_id: int, kind: str, payload: Dict[str, Any]
    ) -> NarrationResult:
        self.calls.append({"channel_id": channel_id, "kind": kind, "payload": payload})

        if kind == "check_outcome":
            verdict = "SUCCESS" if payload.get("succeeded") else "FAILURE"
            return NarrationResult(
                text=(
                    f"[{verdict}] {payload.get('actor')} rolled "
                    f"{payload.get('roll_total')} against DC {payload.get('dc')}.\n"
                    f"{payload.get('established_consequence', '')}"
                )
            )
        if kind == "check_expired":
            return NarrationResult(
                text=f"[expired] {payload.get('actor')}'s {payload.get('key')} check lapsed."
            )
        if kind == "system_note":
            return NarrationResult(text=f"[system] {payload.get('error', '')}")
        return NarrationResult(
            text=f"[echo] heard {payload.get('speaker_display_name', 'someone')}."
        )
