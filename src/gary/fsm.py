"""The Director: Mode A/B state machine (ARCHITECTURE.md §4).

The single rule that makes this system correct:

    **A check is resolved only by the roll of the player it was asked of.**

Gary rolls the dice itself, when that player asks it to. Nothing in the channel -- another
player's text, a pasted number, a name -- can stand in for that, so there is no roll to
parse and nothing to correlate: the request carries the roller.

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
from typing import Callable, Dict, List, Optional, Set

from .models import CancelReason, CheckKind, CheckOutcome
from .ports import Dice, RandomDice
from .registry import PendingCheckRegistry, normalize_actor
from .resolver import outcome_for_model, resolve

#: What a check rolls until moves replace checks.
CHECK_DICE = "1d20"


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
    # Attach a Roll button for this check. Only the request to roll carries one: the
    # button is how the player rolls, and the click is what says who they are.
    roll_check_id: Optional[str] = None


@dataclass(frozen=True)
class PromptModel(Effect):
    """Ask the model to narrate. ``payload`` is a structured part, never prose."""

    channel_id: int
    kind: str  # "player_action" | "check_outcome" | "check_expired" | "system_note"
    payload: dict


@dataclass(frozen=True)
class RecordOutcome(Effect):
    """Journal a resolved check. History, not state.

    The DC-before-the-dice guarantee is the point of the whole design, and without a
    record there is no evidence afterwards that a given call was made that way. Emitted
    beside the PromptModel so the narration and the record cannot diverge.
    """

    channel_id: int
    outcome: CheckOutcome


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
        players: Optional[Dict[int, str]] = None,
        dice: Optional[Dice] = None,
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.game_channels = game_channels if game_channels is not None else set()
        self.freeform_channels = (
            freeform_channels if freeform_channels is not None else set()
        )
        # discord user id -> character name. The model is otherwise told only a display
        # name, and would open checks for characters nobody is playing.
        self.players = dict(players or {})
        self.dice: Dice = dice if dice is not None else RandomDice()
        self._known_actors = {normalize_actor(a) for a in self.players.values()}

    def set_player_character(self, user_id: int, actor: str) -> None:
        """Register who is playing whom, from ``?dm playing`` or persisted state.

        Deliberately not a model tool. This mapping decides whose check is whose, which is
        the class of decision §5 keeps away from the model entirely.
        """
        self.players[user_id] = actor
        self._known_actors = {normalize_actor(a) for a in self.players.values()}

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
                    # Rendered as "John K (playing Caspian Varr)" so open_check names the
                    # character, not the person. None when unconfigured.
                    "actor": self.players.get(user_id),
                    # Sanitised at the context-assembly boundary; carried structured here
                    # so it is never confusable with a system part (ARCHITECTURE.md §9.1).
                    "text": content,
                },
            )
        ]

    def forget_everything(self) -> None:
        """Drop all derived state after a campaign reset.

        The roster lives in memory here as well as in the store, so clearing the store
        alone would leave a stale roster answering for characters who no longer exist
        until the next restart.
        """
        self.players.clear()
        self._known_actors.clear()

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

        The DC and both consequences are persisted here, *before* anyone is asked to roll.
        After this point no model output can change them.
        """
        if self._known_actors and normalize_actor(actor) not in self._known_actors:
            # Nobody plays this character, so nobody could ever roll for it: the check
            # would sit until TTL and then be narrated as hesitation -- a silent dead end.
            # Refuse it the same way a missing key is refused.
            known = ", ".join(sorted(self.players.values()))
            return [
                PromptModel(
                    channel_id=channel_id,
                    kind="system_note",
                    payload={
                        "error": (
                            f"No character named {actor!r} is at this table. "
                            f"Known characters: {known}."
                        ),
                        "instruction": (
                            "Re-issue open_check with one of the known character names, "
                            "exactly as written."
                        ),
                    },
                )
            ]

        key = " ".join(str(key or "").split())
        if not key:
            return [
                PromptModel(
                    channel_id=channel_id,
                    kind="system_note",
                    payload={
                        "error": "open_check needs a key: the skill or ability being tested.",
                        "instruction": "Re-issue open_check with a key.",
                    },
                )
            ]

        check, displaced = self.registry.open(
            channel_id=channel_id,
            user_id=user_id,
            actor=actor,
            kind=kind,
            key=key,
            dc=int(dc),
            on_success=on_success,
            on_failure=on_failure,
            stakes=stakes,
            dice=CHECK_DICE,
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
            SendMessage(
                channel_id,
                f"**{actor}** — roll for {key} ({kind.value}).",
                roll_check_id=check.check_id,
            )
        )
        return effects

    # -- inbound: the player rolls -----------------------------------------------------

    def roll(
        self, *, channel_id: int, user_id: int, check_id: Optional[str] = None
    ) -> List[Effect]:
        """The player whose check this is asked Gary to roll it.

        ``user_id`` is whoever made the request, as Discord reports it -- so a player can
        roll only their own check, and a check can only be rolled once: consuming it is
        what returns the channel to IDLE, and a second request finds nothing to roll.

        ``check_id`` is the check the request was made from -- the one a button was
        attached to. A button left over from a superseded check must not roll its
        replacement, which the player has not seen yet.

        Returns nothing when there is nothing for this player to roll. The adapter says
        so privately; the table does not need to hear it.
        """
        check = self.registry.for_player(channel_id, user_id)
        if check is None or (check_id is not None and check.check_id != check_id):
            return []

        self.registry.consume(check.check_id)
        dice = self.dice.roll(check.dice)
        outcome = resolve(check, dice)
        return [
            SendMessage(channel_id, f"🎲 **{check.actor}** rolls {dice.render()}."),
            RecordOutcome(channel_id=channel_id, outcome=outcome),
            PromptModel(
                channel_id=channel_id,
                kind="check_outcome",
                payload=outcome_for_model(outcome),
            ),
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
