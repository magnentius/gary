"""Seams between the pure core and the outside world.

The core decides and returns ``Effect`` objects; something else performs them. These
protocols name that something, so the Discord adapter can be built and tested before the
Gemini adapter exists — and so neither can quietly acquire the other's responsibilities.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from .models import Roll


@dataclass
class NarrationResult:
    """What a narrator produced.

    ``effects`` exists because narrating is not always the end of a turn: the model may
    call ``make_move``, which the LLM adapter routes through the Director and which
    yields further effects (asking the player to roll). Returning them rather than
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
    """Sends text to a channel. Implemented by the Discord adapter.

    ``prompt``, when given, is something for a player to click under the text -- an
    ``fsm.RollPrompt`` or ``fsm.ChoicePrompt``. Passed only when set, so a transport with
    nothing to attach need not accept it.
    """

    async def send(self, channel_id: int, text: str, prompt: Optional[object] = None) -> None: ...


@runtime_checkable
class Dice(Protocol):
    """Rolls dice. Gary is the only roller, so this is the only source of randomness.

    A port rather than a call to ``random`` so a test can say exactly what lands -- "a 7
    is a weak hit" is not testable against real dice -- and so anything that ever wants
    to roll instead of Gary has one place to plug in.
    """

    def roll(self, spec: str, modifier: int = 0) -> Roll: ...


_SPEC = re.compile(r"^\s*(\d*)d(\d+)\s*$", re.IGNORECASE)


def parse_spec(spec: str) -> tuple:
    """`"2d6"` -> (2, 6). Raises ValueError on anything else, including modifiers.

    Modifiers travel separately (``Dice.roll``'s argument) so the faces stay pure dice
    and the arithmetic stays visible.
    """
    match = _SPEC.match(spec or "")
    if not match:
        raise ValueError(f"not a dice spec: {spec!r}")
    count, sides = int(match[1] or 1), int(match[2])
    if not (1 <= count <= 100 and 2 <= sides <= 1000):
        raise ValueError(f"dice out of range: {spec!r}")
    return count, sides


class RandomDice:
    """Real dice. ``SystemRandom`` because nobody at the table should be able to argue
    the sequence was predictable."""

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self._rng = rng or random.SystemRandom()

    def roll(self, spec: str, modifier: int = 0) -> Roll:
        count, sides = parse_spec(spec)
        faces = tuple(self._rng.randint(1, sides) for _ in range(count))
        return Roll(spec=spec, faces=faces, modifier=int(modifier))


class ScriptedDice:
    """Dice that land where a test says. Faces are consumed in order, across rolls.

    Running out is an error rather than a wrap-around: a test that rolls more than it
    scripted has a bug in either the test or the code, and silently reusing faces would
    hide which.
    """

    def __init__(self, faces: Iterable[int] = ()) -> None:
        self._faces: List[int] = list(faces)
        self.rolls: List[Roll] = []

    def queue(self, *faces: int) -> None:
        self._faces.extend(faces)

    def roll(self, spec: str, modifier: int = 0) -> Roll:
        count, sides = parse_spec(spec)
        if len(self._faces) < count:
            raise AssertionError(f"ScriptedDice ran out rolling {spec}")
        faces = tuple(self._faces[:count])
        del self._faces[:count]
        for face in faces:
            if not 1 <= face <= sides:
                raise AssertionError(f"scripted face {face} cannot land on a d{sides}")
        roll = Roll(spec=spec, faces=faces, modifier=int(modifier))
        self.rolls.append(roll)
        return roll


class EchoNarrator:
    """A narrator with no model behind it.

    Useful well beyond tests: it lets the whole Discord path — intents, routing, parsing,
    resolution, sending — be exercised with no API key and no token spend. Wire this up first; swap in Gemini once the plumbing is proven.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def narrate(
        self, *, channel_id: int, kind: str, payload: Dict[str, Any]
    ) -> NarrationResult:
        self.calls.append({"channel_id": channel_id, "kind": kind, "payload": payload})

        if kind in ("check_outcome", "rules_event"):
            chose = payload.get("player_chose")
            return NarrationResult(
                text=(
                    f"[{payload.get('result')}] {payload.get('actor')}, "
                    f"{payload.get('move')}: {payload.get('what_it_means', '')}"
                    + (f"\nChose: {'; '.join(chose)}" if chose else "")
                )
            )
        if kind == "check_expired":
            return NarrationResult(
                text=f"[expired] {payload.get('actor')}'s {payload.get('move')} lapsed."
            )
        if kind == "system_note":
            return NarrationResult(text=f"[system] {payload.get('error', '')}")
        return NarrationResult(
            text=f"[echo] heard {payload.get('speaker_display_name', 'someone')}."
        )
