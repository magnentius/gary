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
from typing import Callable, Dict, List, Optional, Set

from .models import CancelReason, CheckKind, CheckOutcome, PendingCheck
from .parser.avrae import ParsedAvraeMessage, ParseStatus
from .registry import PendingCheckRegistry, normalize_actor
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
class BeginEncounter(Effect):
    """`!init begin` seen. Freeze the wall clock and hand effects to Avrae (§3.1)."""

    channel_id: int


@dataclass(frozen=True)
class EndEncounter(Effect):
    """`!init end` seen. Restart the clock and reconcile effects (§3.1).

    ``rounds`` is unknown from the end message alone, so the store advances by whatever
    the tracker last reported. Avrae wins where the two disagree (§3).
    """

    channel_id: int
    rounds: int = 0


@dataclass(frozen=True)
class MirrorCombat(Effect):
    """Cache Avrae's initiative tracker. Read-only mirror of a fact Avrae owns (§3).

    Without it the model knows only that combat is active: not who is in the fight, whose
    turn it is, what anything is called in the tracker, or who is still standing. It
    cannot take a monster's turn without those, and a fight where only the players act is
    not a fight.
    """

    channel_id: int
    combatants: tuple
    round_number: Optional[int] = None


@dataclass(frozen=True)
class MirrorSheet(Effect):
    """Cache what `!sheet` reported. Read-only mirror of a fact Avrae owns (§3).

    The party's exact levels are what encounter difficulty is judged from, and nothing
    else in Gary knows them: levels live on the character sheet, and Gary never writes
    there. Seeing a sheet go past is the only way it can learn.
    """

    channel_id: int
    actor: str
    descriptor: str
    level: Optional[int]


@dataclass(frozen=True)
class RecordOutcome(Effect):
    """Journal a resolved check. History, not state.

    The DC-before-the-dice guarantee is the point of the whole design, and without a
    record there is no evidence afterwards that a given call was made that way. Emitted
    beside the PromptModel so the narration and the record cannot diverge.
    """

    channel_id: int
    outcome: CheckOutcome


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
        players: Optional[Dict[int, str]] = None,
        encounter_active: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.game_channels = game_channels if game_channels is not None else set()
        self.freeform_channels = (
            freeform_channels if freeform_channels is not None else set()
        )
        # discord user id -> Avrae character name (§6.2). The model is otherwise told
        # only a display name, and names an actor Avrae has never heard of.
        self.players = dict(players or {})
        self._last_round: Dict[int, int] = {}
        self._acted_this_turn: Dict[int, tuple] = {}
        # Injected rather than imported: the core performs no I/O and holds no store.
        # None means "unknown", which permits — an unconfigured Director behaves as before.
        self._encounter_active = encounter_active
        self._known_actors = {normalize_actor(a) for a in self.players.values()}

    def set_player_character(self, user_id: int, actor: str) -> None:
        """Register who is playing whom, from ``?dm playing`` or persisted state.

        Deliberately not a model tool. This mapping decides whose roll resolves whose
        check, which is the class of decision §5 keeps away from the model entirely.
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
                    # Rendered as "John K (playing Caspian Varr)" so open_check has a
                    # name Avrae will echo back. None when unconfigured.
                    "actor": self.players.get(user_id),
                    # Sanitised at the context-assembly boundary; carried structured here
                    # so it is never confusable with a system part (ARCHITECTURE.md §9.1).
                    "text": content,
                },
            )
        ]

    def _monster_turn(
        self, channel_id: int, parsed: ParsedAvraeMessage
    ) -> List[Effect]:
        """Take a monster's turn unprompted when initiative reaches one.

        §4.2 otherwise keeps Gary silent unless addressed, and that is right for
        conversation — but a monster's turn is a system event, not someone speaking, and
        making the DM ask "what does the guard do?" every round is the loop failing to
        run itself.

        Deduplicated on (combatant, round): Avrae edits the tracker several times a turn
        — on a hit, on an HP change — and each edit would otherwise start another turn.
        """
        current = next((c for c in parsed.combatants if c.is_current_turn), None)
        if current is None or current.is_dying:
            return []
        if normalize_actor(current.name) in self._known_actors:
            return []  # a player's turn; Avrae already pinged them
        if not self._known_actors:
            return []  # no roster, so everything looks like a monster

        token = (current.name, parsed.round_number)
        if self._acted_this_turn.get(channel_id) == token:
            return []
        self._acted_this_turn[channel_id] = token

        return [
            PromptModel(
                channel_id=channel_id,
                kind="monster_turn",
                payload={
                    "combatant": current.name,
                    "round": parsed.round_number,
                    "status": current.status or "",
                    "instruction": (
                        "It is this creature's turn. Narrate what it does and call "
                        "enemy_attack for it, targeting by tracker name. Do not take "
                        "anyone else's turn."
                    ),
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
        if (
            kind is CheckKind.ATTACK
            and self._encounter_active is not None
            and not self._encounter_active()
        ):
            # An attack outside initiative is incoherent: Avrae tracks turn order, hit
            # points and durations only once combat has begun, so the swing resolves
            # against nothing and the target never acts. The persona says to call
            # start_combat first and the model does not, so refuse it here instead --
            # advisory has not held, and this is recoverable in one turn.
            return [
                PromptModel(
                    channel_id=channel_id,
                    kind="system_note",
                    payload={
                        "error": (
                            "No combat is running, so an attack cannot be resolved — "
                            "Avrae is not tracking turn order, hit points or durations."
                        ),
                        "instruction": (
                            "Call start_combat first, listing every creature in the fight. "
                            "Once initiative is running, ask for the attack again."
                        ),
                    },
                )
            ]

        if self._known_actors and normalize_actor(actor) not in self._known_actors:
            # Opening this would create a check no roll can ever match: correlation is
            # name-based in practice (§14 (7)), so an actor Avrae does not know is a
            # check that sits until TTL and is then narrated as player hesitation --
            # a silent dead end. Refuse it the same way a bad skill key is refused.
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
        # Combat boundaries drive §3.1 and must not be gated on AWAITING_ROLL: a fight
        # starts when Avrae says so, whether or not anyone has a check pending.
        if parsed.status is ParseStatus.COMBAT_BEGIN:
            return [BeginEncounter(channel_id)]
        if parsed.status is ParseStatus.COMBAT_END:
            return [EndEncounter(channel_id, rounds=self._last_round.get(channel_id, 0))]

        if parsed.status is ParseStatus.INITIATIVE:
            # The tracker header carries the round number; remember it so the handoff can
            # convert rounds to wall-clock minutes when combat ends.
            if parsed.round_number is not None:
                self._last_round[channel_id] = parsed.round_number

        if parsed.status is ParseStatus.SHEET and parsed.sheet is not None:
            # Before the gate, like INITIATIVE: a sheet is state, not a roll, and a
            # player checking their sheet mid-check must not consume the pending one.
            return [
                MirrorSheet(
                    channel_id=channel_id,
                    actor=parsed.sheet.actor,
                    descriptor=parsed.sheet.descriptor,
                    level=parsed.sheet.level,
                )
            ]

        if parsed.status is ParseStatus.INITIATIVE:
            # Mirrored read-only; never resolves a check.
            effects: List[Effect] = [
                MirrorCombat(
                    channel_id=channel_id,
                    combatants=parsed.combatants,
                    round_number=parsed.round_number,
                ),
                LogDrop(channel_id, parsed.status.value, "initiative mirror"),
            ]
            effects.extend(self._monster_turn(channel_id, parsed))
            return effects

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
            RecordOutcome(channel_id=channel_id, outcome=outcome),
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
