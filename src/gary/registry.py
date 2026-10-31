"""Pending-check registry (ARCHITECTURE.md §5).

Holds every check whose DC is committed and whose dice have not been rolled. It is what
"are we waiting on a roll?" is answered from, and what a restart reloads so a committed DC
survives it.

Checks are keyed by player, so several players may hold checks in one channel safely, and
a roll is attributed by the Discord user who made it -- never by a name in someone's text.
Time is injected so expiry is testable without sleeping.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import CancelReason, PendingCheck, new_id

if TYPE_CHECKING:  # pragma: no cover
    from .state.backend import CheckBackend

DEFAULT_TTL_SECONDS = 180.0


def normalize_actor(name: str) -> str:
    """Fold an actor name for comparison.

    The model writes character names with varying case and punctuation, so checking one
    against the roster must not depend on exact formatting.
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

        Without it, a restart between "roll for it" and the player rolling loses the
        move that was committed before the dice -- which is exactly the guarantee the whole
        design exists to provide. The in-memory index is kept for lookup speed; the
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
        move: str,
        stat: str,
        modifier: int,
        stakes: str,
        dice: str,
        target: str = "",
        spell: str = "",
        companion: str = "",
        aide: str = "",
        ally: str = "",
        flags: Sequence[str] = (),
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
            move=move,
            stat=stat,
            modifier=modifier,
            stakes=stakes,
            dice=dice,
            target=target,
            spell=spell,
            companion=companion,
            aide=aide,
            ally=ally,
            flags=tuple(flags),
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

    def hold(self, check: PendingCheck, **changes) -> PendingCheck:
        """Keep a rolled move open while it waits on the player again -- for a choice, or
        for their damage roll -- recording what has happened so far (``faces``, ``picks``).

        A fresh TTL each time, so reading the roll does not eat into the time to act on
        it. Written through like every change, so nothing is lost to a restart between.
        """
        held = replace(check, **changes, expires_at=self._clock() + self._ttl)
        with self._lock:
            self._by_player[(check.channel_id, check.user_id)] = held
        if self._backend is not None:
            self._backend.save(held)
        return held

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

    # -- attribution -----------------------------------------------------------------

    def for_player(self, channel_id: int, user_id: int) -> Optional[PendingCheck]:
        """The check this player is being asked to roll, or None.

        Keyed by Discord user, which a click carries exactly. Nothing here reads a name:
        matching rolls to checks by character name was the weakest link in the Avrae
        design, and owning the dice is what lets it go.
        """
        now = self._clock()
        with self._lock:
            check = self._by_player.get((channel_id, user_id))
        if check is None or check.is_expired(now):
            return None
        return check

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
