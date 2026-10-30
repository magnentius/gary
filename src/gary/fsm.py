"""The Director: Mode A/B state machine (ARCHITECTURE.md §4).

The single rule that makes this system correct:

    **A move is resolved only by the roll of the player it was asked of.**

Gary rolls the dice itself, when that player asks it to. Nothing in the channel -- another
player's text, a pasted number, a name -- can stand in for that, so there is no roll to
parse and nothing to correlate: the request carries the roller. Where the move's result
gives the player a choice, the same holds for the choice.

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
from typing import Callable, Dict, List, Optional, Sequence, Set

from .models import CancelReason, MoveOutcome, PendingCheck, Roll
from .moves import ANY, BOND, MOVES
from .ports import Dice, RandomDice
from .registry import PendingCheckRegistry, normalize_actor
from .resolver import band, outcome_for_model, resolve
from .sheets import STAT_NAMES, STATS, modifier

#: Every move rolls this.
MOVE_DICE = "2d6"


class ChannelState(str, Enum):
    """Derived, never stored.

    Only these two states exist because only they gate behaviour. The earlier NARRATING
    and RESOLVING states decorated the diagram without changing any decision, while
    creating a second, divergent copy of the truth already held by the registry.
    """

    IDLE = "idle"
    AWAITING_ROLL = "awaiting_roll"


# -- prompts: what a message asks the player to click ---------------------------------------


@dataclass(frozen=True)
class RollPrompt:
    """A Roll button for this move."""

    check_id: str


@dataclass(frozen=True)
class ChoicePrompt:
    """The options this move's result offers, for its roller to pick ``count`` of."""

    check_id: str
    move: str
    band: str
    count: int
    options: tuple  # option texts, in rules order


# -- effects ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Effect:
    """Something the adapter should do. Pure data so transitions stay testable."""


@dataclass(frozen=True)
class SendMessage(Effect):
    channel_id: int
    text: str
    # Something for the player to click under the text. Only the request to roll and the
    # choice after it carry one: clicking is how the player acts, and the click is what
    # says who they are.
    prompt: Optional[object] = None


@dataclass(frozen=True)
class PromptModel(Effect):
    """Ask the model to narrate. ``payload`` is a structured part, never prose."""

    channel_id: int
    kind: str  # "player_action" | "check_outcome" | "check_expired" | "system_note"
    payload: dict


@dataclass(frozen=True)
class RecordOutcome(Effect):
    """Journal a resolved move. History, not state.

    The commit-before-the-dice guarantee is the point of the whole design, and without a
    record there is no evidence afterwards that a given call was made that way. Emitted
    beside the PromptModel so the narration and the record cannot diverge.
    """

    channel_id: int
    outcome: MoveOutcome


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
        sheet_for: Optional[Callable[[str], Optional[dict]]] = None,
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.game_channels = game_channels if game_channels is not None else set()
        self.freeform_channels = (
            freeform_channels if freeform_channels is not None else set()
        )
        # discord user id -> character name. The model is otherwise told only a display
        # name, and would call moves for characters nobody is playing.
        self.players = dict(players or {})
        self.dice: Dice = dice if dice is not None else RandomDice()
        # Injected rather than imported: the core performs no I/O and holds no store.
        # None means "no sheets", and every move rolls +0 -- an unconfigured Director
        # still works, which is most of the test suite.
        self._sheet_for = sheet_for
        self._known_actors = {normalize_actor(a) for a in self.players.values()}

    def set_player_character(self, user_id: int, actor: str) -> None:
        """Register who is playing whom, from ``?dm playing``, ``?dm new`` or state.

        Deliberately not a model tool. This mapping decides whose move is whose, which is
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

        # Deliberately does NOT change channel state. A player talking while their move
        # is pending is normal table behaviour ("rolling now!"), and must not stop the
        # roll that follows from resolving.
        return [
            PromptModel(
                channel_id=channel_id,
                kind="player_action",
                payload={
                    "speaker_display_name": display_name,
                    "speaker_user_id": user_id,
                    # Rendered as "John K (playing Caspian Varr)" so make_move names the
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

    # -- outbound: the model calls a move ----------------------------------------------

    def open_move(
        self,
        *,
        channel_id: int,
        user_id: int,
        actor: str,
        move: str,
        stat: str = "",
        stakes: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> List[Effect]:
        """Handle the model's ``make_move`` tool call.

        The move, the stat and its modifier are fixed here, *before* anyone is asked to
        roll. After this point no model output can change them. Anything wrong with the
        call goes back to the model as a system note, never to the table.
        """
        rules = MOVES.get(str(move or "").strip().lower())
        if rules is None:
            return [self._refuse(
                channel_id,
                f"There is no move {move!r}. Moves: {', '.join(sorted(MOVES))}.",
                "Re-issue make_move with one of these moves.",
            )]

        if self._known_actors and normalize_actor(actor) not in self._known_actors:
            # Nobody plays this character, so nobody could ever roll for it: the move
            # would sit until TTL and then be narrated as hesitation -- a silent dead end.
            known = ", ".join(sorted(self.players.values()))
            return [self._refuse(
                channel_id,
                f"No character named {actor!r} is at this table. Known characters: {known}.",
                "Re-issue make_move with one of the known character names, exactly as "
                "written.",
            )]

        # The move decides the stat, except Defy Danger, where the approach does.
        if rules.stat == ANY:
            stat = str(stat or "").strip().lower()
            if stat not in STATS:
                return [self._refuse(
                    channel_id,
                    "Defy Danger needs the stat the approach uses: str (powering through), "
                    "dex (getting out of the way), con (enduring), int (quick thinking), "
                    "wis (mental fortitude), or cha (charm and social grace).",
                    "Re-issue make_move with stat set.",
                )]
        else:
            stat = rules.stat

        mod = 0
        if stat != BOND and self._sheet_for is not None:
            sheet = self._sheet_for(actor)
            if sheet is None:
                return [self._refuse(
                    channel_id,
                    f"{actor} has no character sheet, so there is no stat to roll.",
                    "Tell the player, in a sentence, to make one with `?dm new` before "
                    "they roll. Do not resolve the action.",
                )]
            mod = modifier(int(sheet["stats"][stat]))

        check, displaced = self.registry.open(
            channel_id=channel_id,
            user_id=user_id,
            actor=actor,
            move=rules.key,
            stat=stat,
            modifier=mod,
            stakes=stakes,
            dice=MOVE_DICE,
            ttl_seconds=ttl_seconds,
        )

        # No state assignment needed: the channel is AWAITING_ROLL by virtue of the
        # registry now holding an open move.
        effects: List[Effect] = []
        if displaced is not None:
            effects.append(SendMessage(
                channel_id,
                f"(Superseding {displaced.actor}'s pending {MOVES[displaced.move].name}.)",
            ))
        rolled_with = "+Bond (not tracked yet, so +0)" if stat == BOND else \
            f"+{STAT_NAMES[stat][:3].title()} {mod:+d}"
        effects.append(SendMessage(
            channel_id,
            f"**{actor}** — {rules.name}, roll {rolled_with}.",
            prompt=RollPrompt(check.check_id),
        ))
        return effects

    def _refuse(self, channel_id: int, error: str, instruction: str) -> PromptModel:
        return PromptModel(
            channel_id=channel_id,
            kind="system_note",
            payload={"error": error, "instruction": instruction},
        )

    # -- inbound: the player rolls -----------------------------------------------------

    def roll(
        self, *, channel_id: int, user_id: int, check_id: Optional[str] = None
    ) -> List[Effect]:
        """The player whose move this is asked Gary to roll it.

        ``user_id`` is whoever made the request, as Discord reports it -- so a player can
        roll only their own move, and only once.

        ``check_id`` is the move the request was made from -- the one a button was
        attached to. A button left over from a superseded move must not roll its
        replacement, which the player has not seen yet.

        If the result offers a choice, the move is held with its dice and the player is
        asked to choose; the narration waits. Otherwise it resolves now.

        Returns nothing when there is nothing for this player to roll. The adapter says
        so privately; the table does not need to hear it.
        """
        check = self.registry.for_player(channel_id, user_id)
        if check is None or check.rolled or (check_id is not None and check.check_id != check_id):
            return []

        dice = self.dice.roll(check.dice, modifier=check.modifier)
        rolled = SendMessage(
            channel_id,
            f"🎲 **{check.actor}** rolls {dice.render()} — {_RESULT[band(dice)]}.",
        )
        rules = MOVES[check.move].band(band(dice))
        if rules.choice is not None:
            held = self.registry.hold_for_choice(check, dice.faces)
            return [rolled, SendMessage(
                channel_id,
                f"{rules.text}\n**{check.actor}**, choose "
                + ("one." if rules.choice.count == 1 else f"{rules.choice.count}."),
                prompt=ChoicePrompt(
                    check_id=held.check_id,
                    move=held.move,
                    band=band(dice),
                    count=rules.choice.count,
                    options=tuple(o.text for o in rules.choice.options),
                ),
            )]

        self.registry.consume(check.check_id)
        return [rolled, *self._resolved(channel_id, resolve(check, dice))]

    # -- inbound: the player chooses ---------------------------------------------------

    def choose(
        self, *, channel_id: int, user_id: int, check_id: str, picks: Sequence[int]
    ) -> List[Effect]:
        """The player picked from the options their result offered.

        The same rules as rolling: only the roller, only for the move the options were
        offered for, only once. ``picks`` must be exactly as many distinct options as
        the move allows; anything else rolls nothing back and resolves nothing.
        """
        check = self.registry.for_player(channel_id, user_id)
        if check is None or not check.rolled or check.check_id != check_id:
            return []
        dice = self._dice_of(check)
        rules = MOVES[check.move].band(band(dice))
        if rules.choice is None:
            return []
        picks = list(picks)
        if (
            len(picks) != rules.choice.count
            or len(set(picks)) != len(picks)
            or not all(0 <= p < len(rules.choice.options) for p in picks)
        ):
            return []

        self.registry.consume(check.check_id)
        outcome = resolve(check, dice, picks=sorted(picks))
        return [
            SendMessage(channel_id, f"**{check.actor}** chooses: " + "; ".join(outcome.chosen)),
            *self._resolved(channel_id, outcome),
        ]

    def _dice_of(self, check: PendingCheck) -> Roll:
        return Roll(spec=check.dice, faces=check.faces, modifier=check.modifier)

    def _resolved(self, channel_id: int, outcome: MoveOutcome) -> List[Effect]:
        return [
            RecordOutcome(channel_id=channel_id, outcome=outcome),
            PromptModel(
                channel_id=channel_id,
                kind="check_outcome",
                payload=outcome_for_model(outcome),
            ),
        ]

    # -- housekeeping ------------------------------------------------------------------

    def sweep(self) -> List[Effect]:
        """Expire stale moves. Called on a timer (ARCHITECTURE.md §4).

        A move nobody rolled lapses: the moment passed. A move that was rolled but whose
        choice was never made does not -- the dice already landed, and the fiction has to
        honour them -- so it resolves with the first options, and the table is told.
        """
        effects: List[Effect] = []
        for cancellation in self.registry.sweep_expired():
            check = cancellation.check
            if check.rolled:
                dice = self._dice_of(check)
                choice = MOVES[check.move].band(band(dice)).choice
                outcome = resolve(
                    check, dice, picks=range(choice.count) if choice else (), auto_chosen=True
                )
                effects.append(SendMessage(
                    check.channel_id,
                    f"**{check.actor}** didn't choose in time, so: " + "; ".join(outcome.chosen),
                ))
                effects.extend(self._resolved(check.channel_id, outcome))
                continue
            effects.append(
                PromptModel(
                    channel_id=check.channel_id,
                    kind="check_expired",
                    payload={
                        "actor": check.actor,
                        "move": MOVES[check.move].name,
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
        return [SendMessage(
            channel_id,
            f"Cancelled {cancellation.check.actor}'s {MOVES[cancellation.check.move].name}.",
        )]


_RESULT = {"strong": "strong hit", "weak": "weak hit", "miss": "miss"}
