"""The Director: Mode A/B state machine (ARCHITECTURE.md §4).

The single rule that makes this system affordable and correct:

    **Only AWAITING_ROLL consumes Avrae output.**

In every other state an Avrae embed is logged and dropped. That one rule is what stops
``!help``, sheet lookups, and unrelated players' rolls from each triggering a Gemini call
-- the failure mode v1's ``on_message`` had by construction.

This module is pure: no discord.py, no google-genai. Side effects are declared as
``Effect`` objects returned to the caller, which makes every transition testable without
a network. The adapters do the I/O; the Director decides.

**The Director holds no per-channel state.** Channel state is *derived* from the pending-
check registry, which is the only thing that was ever load-bearing. Storing it separately
was both an obstacle to running Gary on a disposable VM and an outright bug: a player
saying anything while holding a pending check flipped the channel out of AWAITING_ROLL,
and their actual roll was then silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Set

from .models import CancelReason, CheckKind, CheckOutcome, PendingCheck
from .parser.avrae import ParsedAvraeMessage, ParseStatus
from .registry import PendingCheckRegistry
from .resolver import outcome_for_model, resolve
from .templates import TemplateError, normalize_key, render_command


class ChannelState(str, Enum):
    """Derived, never stored.

    Only these two states exist because only they gate behaviour. The earlier NARRATING
    and RESOLVING states decorated the diagram without changing any decision, while
    creating a second, divergent copy of the truth already held by the registry.
    """

    IDLE = "idle"
    AWAITING_ROLL = "awaiting_roll"


# -- effects ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Effect:
    """Something the adapter should do. Pure data so transitions stay testable."""


@dataclass(frozen=True)
class SendMessage(Effect):
    channel_id: int
    text: str


@dataclass(frozen=True)
class PromptModel(Effect):
    """Ask the model to narrate. ``payload`` is a structured part, never prose."""

    channel_id: int
    kind: str  # "player_action" | "check_outcome" | "check_expired" | "system_note"
    payload: dict


@dataclass(frozen=True)
class LogDrop(Effect):
    """An Avrae message we deliberately ignored. Tracked: a rising rate of UNKNOWN is
    the leading indicator that Avrae changed its embed format (ARCHITECTURE.md §10)."""

    channel_id: int
    status: str
    reason: str


@dataclass(frozen=True)
class AskPlayer(Effect):
    """Parser could not read an embed. Ask rather than guess (ARCHITECTURE.md §6.1)."""

    channel_id: int
    text: str


# -- director -----------------------------------------------------------------------------


class Director:
    """Stateless decision layer.

    ``game_channels`` and ``freeform_channels`` are configuration loaded at boot, not
    accumulated at runtime, so two Directors reading the same registry behave identically.
    That is what makes the process replaceable.
    """

    def __init__(
        self,
        registry: PendingCheckRegistry,
        clock: Callable[[], float],
        game_channels: Optional[Set[int]] = None,
        freeform_channels: Optional[Set[int]] = None,
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.game_channels = game_channels if game_channels is not None else set()
        self.freeform_channels = (
            freeform_channels if freeform_channels is not None else set()
        )

    def state(self, channel_id: int) -> ChannelState:
        """Derived from the registry -- the single source of truth for 'are we waiting?'."""
        return (
            ChannelState.AWAITING_ROLL
            if self.registry.has_open(channel_id)
            else ChannelState.IDLE
        )

    # -- inbound: players --------------------------------------------------------------

    def on_player_message(
        self,
        *,
        channel_id: int,
        user_id: int,
        display_name: str,
        content: str,
        mentions_bot: bool,
    ) -> List[Effect]:
        """Decide whether a player message reaches the model at all (ARCHITECTURE.md §4.2)."""
        if channel_id not in self.game_channels:
            return []

        addressed = (
            mentions_bot
            or channel_id in self.freeform_channels
            or self._player_has_open_check(channel_id, user_id)
        )
        if not addressed:
            return []

        # Deliberately does NOT change channel state. A player talking while their check
        # is pending is normal table behaviour ("rolling now!"), and must not stop the
        # roll that follows from resolving.
        return [
            PromptModel(
                channel_id=channel_id,
                kind="player_action",
                payload={
                    "speaker_display_name": display_name,
                    "speaker_user_id": user_id,
                    # Sanitised at the context-assembly boundary; carried structured here
                    # so it is never confusable with a system part (ARCHITECTURE.md §9.1).
                    "text": content,
                },
            )
        ]

    def _player_has_open_check(self, channel_id: int, user_id: int) -> bool:
        return any(
            c.user_id == user_id for c in self.registry.open_checks(channel_id)
        )

    # -- outbound: the model opens a check ---------------------------------------------

    def open_check(
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
        stakes: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> List[Effect]:
        """Handle the model's ``open_check`` tool call.

        The DC and both consequences are persisted here, *before* the command is posted.
        After this point no model output can change them.
        """
        try:
            canonical = normalize_key(kind, key)
            command = render_command(kind, key)
        except TemplateError as exc:
            # A bad key is the model's error to fix, not the channel's problem.
            return [
                PromptModel(
                    channel_id=channel_id,
                    kind="system_note",
                    payload={
                        "error": str(exc),
                        "instruction": "Re-issue open_check with a valid key.",
                    },
                )
            ]

        check, displaced = self.registry.open(
            channel_id=channel_id,
            user_id=user_id,
            actor=actor,
            kind=kind,
            key=canonical,
            dc=int(dc),
            on_success=on_success,
            on_failure=on_failure,
            stakes=stakes,
            command=command,
            ttl_seconds=ttl_seconds,
        )

        # No state assignment needed: the channel is AWAITING_ROLL by virtue of the
        # registry now holding an open check.
        effects: List[Effect] = []
        if displaced is not None:
            effects.append(
                SendMessage(
                    channel_id,
                    f"(Superseding {displaced.actor}'s pending {displaced.kind.value}.)",
                )
            )
        effects.append(
            SendMessage(channel_id, f"**{actor}** — run this:\n`{check.command}`")
        )
        return effects

    # -- inbound: Avrae ----------------------------------------------------------------

    def on_avrae_message(
        self, *, channel_id: int, parsed: ParsedAvraeMessage
    ) -> List[Effect]:
        """The gate. Only AWAITING_ROLL consumes Avrae output."""
        if parsed.status is ParseStatus.INITIATIVE:
            # Mirrored read-only for narrative colour; never resolves a check.
            return [LogDrop(channel_id, parsed.status.value, "initiative mirror")]

        state = self.state(channel_id)
        if state is not ChannelState.AWAITING_ROLL:
            return [LogDrop(channel_id, parsed.status.value, f"state={state.value}")]

        if parsed.status is ParseStatus.IRRELEVANT:
            return [LogDrop(channel_id, parsed.status.value, parsed.reason)]

        if parsed.status is ParseStatus.PROMPT_ABANDONED:
            # The selector lapsed, so no roll is coming from that command. The check is
            # still good -- its DC and consequences were fixed before any of this -- so
            # re-post the command rather than making the table wait out the TTL. The
            # message carries no user attribution, so every open check in the channel is
            # re-prompted; in the single-check case that is exactly the right one.
            effects: List[Effect] = [
                LogDrop(channel_id, parsed.status.value, parsed.reason)
            ]
            for pending in self.registry.open_checks(channel_id):
                effects.append(
                    SendMessage(
                        channel_id,
                        f"That selection lapsed. **{pending.actor}** — run this:\n"
                        f"`{pending.command}`",
                    )
                )
            return effects

        if parsed.status is ParseStatus.AVRAE_PROMPT:
            # Avrae asked the player a question instead of rolling. Stay silent and leave
            # the check open: answering the selector produces a real roll that correlates
            # normally. Speaking here would put two bots' questions in front of one player.
            return [LogDrop(channel_id, parsed.status.value, parsed.reason)]

        if parsed.status is ParseStatus.UNKNOWN:
            # We were expecting a roll and could not read this. Ask; do not assume.
            return [
                LogDrop(channel_id, parsed.status.value, parsed.reason),
                AskPlayer(
                    channel_id,
                    "I couldn't read that roll. What was the total?",
                ),
            ]

        assert parsed.roll is not None
        check = self.registry.match(
            channel_id=channel_id,
            roll=parsed.roll,
            interaction_user_id=parsed.interaction_user_id,
            referenced_user_id=parsed.referenced_user_id,
        )
        if check is None:
            return [LogDrop(channel_id, parsed.status.value, "no matching pending check")]

        return self._resolve(channel_id, check, parsed)

    def _resolve(
        self,
        channel_id: int,
        check: PendingCheck,
        parsed: ParsedAvraeMessage,
    ) -> List[Effect]:
        # Consuming the check is what returns the channel to IDLE.
        self.registry.consume(check.check_id)
        outcome: CheckOutcome = resolve(check, parsed.roll)
        return [
            PromptModel(
                channel_id=channel_id,
                kind="check_outcome",
                payload=outcome_for_model(outcome),
            )
        ]

    # -- housekeeping ------------------------------------------------------------------

    def sweep(self) -> List[Effect]:
        """Expire stale checks. Called on a timer (ARCHITECTURE.md §4)."""
        effects: List[Effect] = []
        for cancellation in self.registry.sweep_expired():
            check = cancellation.check
            effects.append(
                PromptModel(
                    channel_id=check.channel_id,
                    kind="check_expired",
                    payload={
                        "actor": check.actor,
                        "kind": check.kind.value,
                        "key": check.key,
                        "stakes": check.stakes,
                        "instruction": (
                            "The player did not attempt the roll. Narrate hesitation, a "
                            "change of approach, or the moment passing. Do not resolve "
                            "the original action."
                        ),
                    },
                )
            )
        return effects

    def cancel(self, channel_id: int, check_id: str) -> List[Effect]:
        cancellation = self.registry.cancel(check_id, CancelReason.MANUAL)
        if cancellation is None:
            return []
        return [SendMessage(channel_id, f"Cancelled {cancellation.check.actor}'s check.")]
