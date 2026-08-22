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
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from .encounters import lookup
from .models import CancelReason, CheckKind, CheckOutcome, PendingCheck
from .morale import assess
from .parser.avrae import ParsedAvraeMessage, ParseStatus, combatant_prefix
from .registry import PendingCheckRegistry, normalize_actor
from .resolver import outcome_for_model, resolve
from .templates import (
    DEATHSAVE_FAIL,
    TemplateError,
    normalize_key,
    render_command,
    render_end_combat_command,
    render_flee_command,
)


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
        npc_disposition: Optional[Callable[[str], int]] = None,
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
        # Folded names the tracker last showed as down, per channel.
        self._down: Dict[int, Set[str]] = {}
        # Injected rather than imported: the core performs no I/O and holds no store.
        # None means "unknown", which permits — an unconfigured Director behaves as before.
        self._encounter_active = encounter_active
        self._npc_disposition = npc_disposition
        # SRD names Gary asked the table to add, per channel (§5.4). Avrae renames them
        # -- `!init madd Goblin` becomes GO1 -- so morale needs this to recover what a
        # tracker entry actually is. Without it no creature is assessed at all.
        self._encounter_roster: Dict[int, List[str]] = {}
        # Combatants already suggested as broken, so a rout is offered once and not on
        # every tracker edit for the rest of the fight.
        self._broken: Dict[int, Set[str]] = {}
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

    def attack_refusal(self, channel_id: int, actor: str) -> Optional[str]:
        """Why this combatant may not attack, or None.

        Avrae does not referee. It tracks hit points and rolls dice, and has no concept
        that being at 0 HP stops you acting -- `!attack` will happily roll for a character
        who is unconscious, which is how a downed fighter swung a longsword for two rounds.
        Adjudication is the DM's job, and the DM is Gary, so this is the only place it can
        be caught.
        """
        # None means unknown, and unknown permits — an unconfigured Director behaves as
        # before, which is most of the test suite.
        if self._encounter_active is not None and not self._encounter_active():
            return (
                "No combat is running, so an attack cannot be resolved — Avrae is not "
                "tracking turn order, hit points or durations."
            )
        if normalize_actor(actor) in self._down.get(channel_id, set()):
            return (
                f"{actor} is at 0 HP and unconscious, and cannot act. They roll a death "
                "save on their turn instead."
            )
        return None

    def forget_everything(self) -> None:
        """Drop all derived state after a campaign reset.

        The roster, the combat mirror and the per-turn bookkeeping live in memory here as
        well as in the store, so clearing the store alone would leave a stale roster
        answering for characters who no longer exist until the next restart.
        """
        self.players.clear()
        self._known_actors.clear()
        self._last_round.clear()
        self._encounter_roster.clear()
        self._broken.clear()
        self._acted_this_turn.clear()
        self._down.clear()

    def is_down(self, channel_id: int, name: str) -> bool:
        """Does the tracker show this combatant at 0 HP?

        A creature at 0 HP is Prone and Incapacitated, and a melee attack against a prone
        target has advantage. Avrae applies none of that: it has the hit points and the
        conditions and joins them to nothing, so every attack on a downed character was
        rolled once when it should have been rolled twice.
        """
        return normalize_actor(name) in self._down.get(channel_id, set())

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
        if current is None or not self._known_actors:
            return []  # no tracker, or no roster — everything would look like a monster

        is_player = normalize_actor(current.name) in self._known_actors
        token = (current.name, parsed.round_number)
        if self._acted_this_turn.get(channel_id) == token:
            return []

        if is_player:
            if not current.is_dying:
                return []  # their turn; Avrae already pinged them
            # A character at 0 HP rolls a death save on their turn, and it is the one
            # roll nobody should have to remember to ask for.
            self._acted_this_turn[channel_id] = token
            return [
                PromptModel(
                    channel_id=channel_id,
                    kind="death_save",
                    payload={
                        "actor": current.name,
                        "round": parsed.round_number,
                        "instruction": (
                            "This character is at 0 HP and it is their turn. Call "
                            "open_check with kind=save and key=death. Narrate the moment "
                            "briefly; do not decide whether they live."
                        ),
                    },
                )
            ]

        if current.is_dying:
            return []  # a downed monster does not act

        # Checked before the turn: a creature that has just broken is leaving, not
        # attacking, and narrating an attack it then flees from reads as two creatures.
        broken = self._morale(channel_id, parsed, current)
        if broken:
            self._acted_this_turn[channel_id] = token
            return broken

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

    # -- morale (§5.4) -----------------------------------------------------------------

    def note_encounter(self, channel_id: int, monsters: Sequence[str]) -> None:
        """Remember the SRD names Gary asked the table to add.

        Called when `start_combat` posts its `!init madd` lines. Avrae names the resulting
        combatants itself -- `!init madd Goblin` becomes GO1 -- so this is the only record
        of what the tracker entries actually are, and morale is silent without it.
        """
        self._encounter_roster[channel_id] = [str(m).strip() for m in monsters if str(m).strip()]
        self._broken.pop(channel_id, None)

    def _srd_name(self, channel_id: int, tracker_name: str) -> str:
        """Recover the SRD creature behind a tracker entry, or "" if it is not certain.

        Avrae renames what it adds: `!init madd Bandit` becomes BA1. The rule is
        reproduced exactly by `combatant_prefix` (§6.1, read from Avrae's source), so this
        compares rather than guesses -- strip the trailing number and the rest must equal
        exactly one rostered creature's prefix.

        An earlier version matched loosely, on any shared leading letters. That was wrong
        in both directions: it called Bandit and Bandit Captain ambiguous when Avrae gives
        them BA and BC, and it would have matched `Swarm of Bats` against SOB when Avrae
        writes SoB. Comparison is case-insensitive for that second reason.

        **Ambiguity returns ""**, and so does no match -- a creature renamed with `-name`,
        or a fight nobody opened through `start_combat`. An unidentified creature is one
        morale says nothing about, which is the right way to be wrong: suggesting a rout
        for the wrong creature removes something that was fine.
        """
        roster = self._encounter_roster.get(channel_id) or []
        if not roster:
            return ""

        token = str(tracker_name or "").strip().rstrip("0123456789").strip()
        if not token:
            return ""

        hits = {srd for srd in roster if combatant_prefix(srd).upper() == token.upper()}
        return next(iter(hits)) if len(hits) == 1 else ""

    def _morale(
        self, channel_id: int, parsed: ParsedAvraeMessage, current: Any
    ) -> List[Effect]:
        """Assess the creature whose turn it is, and suggest the rout if it breaks.

        Evaluated on its own turn rather than on every tracker edit: that is the moment it
        would act, it is already deduplicated by round, and it keeps one suggestion per
        creature per round instead of one per HP change.
        """
        srd = self._srd_name(channel_id, current.name)
        if not srd:
            return []
        record = lookup(srd)
        if record is None:
            return []

        folded = normalize_actor(current.name)
        if folded in self._broken.get(channel_id, set()):
            return []  # already offered; do not nag every round

        down = self._down.get(channel_id, set())
        enemies = [
            c for c in parsed.combatants
            if normalize_actor(c.name) not in self._known_actors
        ]
        allies_down = sum(
            1 for c in enemies
            if normalize_actor(c.name) != folded and normalize_actor(c.name) in down
        )
        allies_standing = sum(
            1 for c in enemies
            if normalize_actor(c.name) != folded and normalize_actor(c.name) not in down
        )

        call = assess(
            current.name,
            str(record.get("type") or ""),
            disposition=self._disposition(srd),
            status=current.status or "",
            hp_current=current.hp_current,
            hp_max=current.hp_max,
            allies_down=allies_down,
            allies_standing=allies_standing,
            foes_standing=sum(
                1 for c in parsed.combatants
                if normalize_actor(c.name) in self._known_actors
                and normalize_actor(c.name) not in down
            ),
        )
        if call is None or not call.broke:
            return []

        self._broken.setdefault(channel_id, set()).add(folded)

        # If nothing hostile is left standing that has not already broken, the fight is
        # over rather than one creature short -- so offer `!init end` instead of a removal.
        still_fighting = [
            c for c in enemies
            if normalize_actor(c.name) not in down
            and normalize_actor(c.name) not in self._broken.get(channel_id, set())
        ]
        if still_fighting:
            command = render_flee_command(current.name)
            headline = f"**{current.name}** breaks."
        else:
            command = render_end_combat_command()
            headline = f"**{current.name}** breaks, and it was the last one standing."

        return [
            SendMessage(
                channel_id,
                f"{headline}\n_{call.explain()}_\nRun it if you agree:\n`{command}`",
            ),
            PromptModel(
                channel_id=channel_id,
                kind="morale_break",
                payload={
                    "combatant": current.name,
                    "creature": srd,
                    "creature_type": call.creature_type,
                    "round": parsed.round_number,
                    "why": call.explain(),
                    "instruction": (
                        "This creature's nerve has broken and it is trying to leave the "
                        "fight. Narrate the flight in a sentence or two -- which way it "
                        "goes, what it drops, what it shouts. The command has ALREADY "
                        "been posted; do not repeat it, and do not decide whether it "
                        "escapes. Take no attacks for it and call no tools."
                    ),
                },
            ),
        ]

    def _disposition(self, name: str) -> int:
        if self._npc_disposition is None:
            return 0
        try:
            return int(self._npc_disposition(name))
        except Exception:  # noqa: BLE001 - a missing NPC must never break a turn
            return 0

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

        # Before the gate: a crit on a downed creature is a rules consequence, not a roll
        # to correlate. Avrae applies the damage and knows nothing about the death saves.
        down_crits = [
            name
            for name in parsed.critical_targets
            if normalize_actor(name) in self._down.get(channel_id, set())
        ]
        if down_crits:
            return [
                SendMessage(
                    channel_id,
                    f"**{name}** was critically hit while down — that is two failed "
                    f"death saves. They run:\n"
                    f"`{DEATHSAVE_FAIL}`\n`{DEATHSAVE_FAIL}`",
                )
                for name in down_crits
            ]

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
            self._down[channel_id] = {
                normalize_actor(c.name) for c in parsed.combatants if c.is_dying
            }
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
