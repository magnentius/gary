"""Pending-check registry (ARCHITECTURE.md §5).

This is the component that makes Avrae's channel output *addressable*. Without it, every
Avrae message -- ``!help``, a sheet lookup, another player's attack -- looks identical to
the roll you were waiting for. v1 of the design had no such correlation and consequently
fired a Gemini call on every embed.

Correlation is per-player, so several players may hold checks in one channel safely.
Time is injected so expiry is testable without sleeping.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

from .models import AvraeRoll, CancelReason, CheckKind, PendingCheck, new_id

if TYPE_CHECKING:  # pragma: no cover
    from .state.backend import CheckBackend

DEFAULT_TTL_SECONDS = 180.0


def normalize_actor(name: str) -> str:
    """Fold an actor name for comparison.

    Avrae renders character names with varying case and punctuation across embeds, so
    correlation must not depend on exact formatting.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


@dataclass(frozen=True)
class Cancellation:
    check: PendingCheck
    reason: CancelReason


class PendingCheckRegistry:
    """Holds open checks, keyed by (channel_id, user_id).

    One check per player at a time: a second ``open`` from the same player supersedes the
    first and returns the displaced check, so the caller can tell the channel what
    happened rather than leaving a silent orphan.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        backend: Optional["CheckBackend"] = None,
    ) -> None:
        """``backend`` makes open checks durable.

        Without it, a restart between "run this command" and the player rolling loses the
        DC that was committed before the dice -- which is exactly the guarantee the whole
        design exists to provide. The in-memory index is kept for matching speed; the
        backend is written through on every change.
        """
        self._clock = clock
        self._ttl = ttl_seconds
        self._backend = backend
        self._by_player: Dict[Tuple[int, int], PendingCheck] = {}
        self._lock = threading.RLock()
        if backend is not None:
            self.restore(backend.load_all())

    # -- lifecycle ---------------------------------------------------------------------

    def open(
        self,
        *,
        channel_id: int,
        user_id: int,
        actor: str,
        kind: CheckKind,
        key: str,
        dc: int,
        on_success: str,
        on_failure: str,
        stakes: str,
        command: str,
        ttl_seconds: Optional[float] = None,
    ) -> Tuple[PendingCheck, Optional[PendingCheck]]:
        """Open a check. Returns (new_check, superseded_check_or_None)."""
        now = self._clock()
        ttl = self._ttl if ttl_seconds is None else ttl_seconds
        check = PendingCheck(
            check_id=new_id(now),
            channel_id=channel_id,
            user_id=user_id,
            actor=actor,
            kind=kind,
            key=key,
            dc=dc,
            on_success=on_success,
            on_failure=on_failure,
            stakes=stakes,
            command=command,
            created_at=now,
            expires_at=now + ttl,
        )
        with self._lock:
            displaced = self._by_player.get((channel_id, user_id))
            self._by_player[(channel_id, user_id)] = check
        if self._backend is not None:
            if displaced is not None:
                self._backend.remove(displaced.check_id)
            self._backend.save(check)
        return check, displaced

    def cancel(self, check_id: str, reason: CancelReason = CancelReason.MANUAL) -> Optional[Cancellation]:
        with self._lock:
            for slot, check in self._by_player.items():
                if check.check_id == check_id:
                    del self._by_player[slot]
                    self._forget(check_id)
                    return Cancellation(check, reason)
        return None

    def consume(self, check_id: str) -> Optional[PendingCheck]:
        """Remove and return a check that has been resolved."""
        with self._lock:
            for slot, check in self._by_player.items():
                if check.check_id == check_id:
                    del self._by_player[slot]
                    self._forget(check_id)
                    return check
        return None

    def sweep_expired(self) -> List[Cancellation]:
        """Drop and return every check past its TTL. Caller narrates the pivot."""
        now = self._clock()
        with self._lock:
            dead = [c for c in self._by_player.values() if c.is_expired(now)]
            for check in dead:
                self._by_player.pop((check.channel_id, check.user_id), None)
        for check in dead:
            self._forget(check.check_id)
        return [Cancellation(c, CancelReason.EXPIRED) for c in dead]

    def _forget(self, check_id: str) -> None:
        if self._backend is not None:
            self._backend.remove(check_id)

    # -- correlation -------------------------------------------------------------------

    def match(
        self,
        *,
        channel_id: int,
        roll: AvraeRoll,
        interaction_user_id: Optional[int] = None,
        referenced_user_id: Optional[int] = None,
    ) -> Optional[PendingCheck]:
        """Find the open check an Avrae roll belongs to, or None.

        Priority mirrors ARCHITECTURE.md §6.2, strongest signal first:

        1. ``interaction_user_id`` -- present when the player used a slash command.
           Unambiguous: Discord tells us exactly who invoked it.
        2. ``referenced_user_id`` -- Avrae replying to the invoking message.
        3. actor name from the embed, matched against ``PendingCheck.actor``.

        Returning None is the common, correct case: it is how ``!help`` and unrelated
        rolls get dropped instead of triggering an LLM call.
        """
        now = self._clock()
        with self._lock:
            live = [
                c
                for c in self._by_player.values()
                if c.channel_id == channel_id and not c.is_expired(now)
            ]

        if not live:
            return None

        for user_id in (interaction_user_id, referenced_user_id):
            if user_id is not None:
                for check in live:
                    if check.user_id == user_id:
                        return check

        if roll.actor:
            wanted = normalize_actor(roll.actor)
            named = [c for c in live if normalize_actor(c.actor) == wanted]
            if named:
                # Ambiguity is possible if one player somehow holds two checks for the
                # same character; oldest-first keeps resolution deterministic.
                return min(named, key=lambda c: c.created_at)

        return None

    # -- introspection -----------------------------------------------------------------

    def open_checks(self, channel_id: Optional[int] = None) -> Iterable[PendingCheck]:
        with self._lock:
            values = list(self._by_player.values())
        if channel_id is None:
            return values
        return [c for c in values if c.channel_id == channel_id]

    def has_open(self, channel_id: int) -> bool:
        now = self._clock()
        with self._lock:
            return any(
                c.channel_id == channel_id and not c.is_expired(now)
                for c in self._by_player.values()
            )

    def restore(self, checks: Iterable[PendingCheck]) -> None:
        """Reload persisted checks after a restart (ARCHITECTURE.md §10).

        Does not write back to the backend: these came from it.
        """
        with self._lock:
            for check in checks:
                self._by_player[(check.channel_id, check.user_id)] = check
