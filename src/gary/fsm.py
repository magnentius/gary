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

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import damage
from .gear import rations_of, weapon_for
from .models import CancelReason, MoveOutcome, PendingCheck, Roll
from .moves import ANY, BOND, MOVES, NOTHING
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


@dataclass(frozen=True)
class DamagePrompt:
    """A Roll damage button for this move's hit."""

    check_id: str


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
class ChangeHp(Effect):
    """Set a creature's or a character's HP. Journalled by the runner, so it undoes.

    The Director computes the new value from what it read; it writes nothing itself.
    """

    channel_id: int
    kind: str  # "monster" | "character"
    name: str
    hp: int


@dataclass(frozen=True)
class SpendAmmo(Effect):
    """Set a character's ammo. Journalled by the runner, like an HP change."""

    channel_id: int
    name: str
    ammo: int


@dataclass(frozen=True)
class UpdateSheet(Effect):
    """Change fields on a character's sheet -- HP and rations at camp, Death's marks.
    Journalled by the runner, like an HP change."""

    channel_id: int
    name: str
    changes: dict


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
        monsters: Optional[Callable[[], Dict[str, dict]]] = None,
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
        # The creatures in the scene, by instance name. Read when an attack is aimed and
        # when its damage lands. None means none: an unconfigured Director cannot attack.
        self._monsters = monsters or (lambda: {})
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
        target: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> List[Effect]:
        """Handle the model's ``make_move`` tool call.

        The move, the stat and its modifier are fixed here, *before* anyone is asked to
        roll. After this point no model output can change them. Anything wrong with the
        call goes back to the model as a system note, never to the table.
        """
        rules = MOVES.get(str(move or "").strip().lower())
        if rules is not None and not rules.callable:
            rules = None  # Last Breath, Take Watch: the rules trigger these, not the model
        if rules is None:
            return [self._refuse(
                channel_id,
                f"There is no move {move!r}. Moves: "
                f"{', '.join(sorted(k for k, m in MOVES.items() if m.callable))}.",
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

        # An attack lands on a creature someone can see the HP of, or not at all: the
        # model adds creatures when they appear, so a target it never added is a mistake
        # to fix, not a creature to invent mid-swing.
        if rules.attacks:
            standing = {n: r for n, r in self._monsters().items() if r["hp"] > 0}
            match = next((n for n in standing if n.lower() == str(target or "").strip().lower()), None)
            if match is None:
                listing = ", ".join(sorted(standing)) or "none"
                return [self._refuse(
                    channel_id,
                    f"{rules.name} needs a target from the creatures in this scene. "
                    f"{'No creature named ' + repr(target) + '. ' if target else ''}"
                    f"Standing: {listing}.",
                    "Add the creature with add_monsters first if it is new, then re-issue "
                    "make_move with its exact name as target.",
                )]
            target = match
        else:
            target = ""

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
            unable = _unable(actor, sheet)
            if unable:
                return [self._refuse(
                    channel_id, unable,
                    "They cannot act. Narrate around them, or ask what someone else does.",
                )]
            # A sheet made before gear existed has no gear to check, and is let through
            # as before; `?dm gear` gives it some.
            if rules.attacks and "gear" in sheet and self._weapon(sheet, rules.key) is None:
                needs = "a near, far or thrown weapon (and ammo, for a bow)" \
                    if rules.key == "volley" else "a hand, close or reach weapon"
                return [self._refuse(
                    channel_id,
                    f"{actor} has nothing to {rules.name} with: it needs {needs}.",
                    "Narrate what they can do instead, or choose a move that fits what they "
                    "carry. Do not resolve the attack.",
                )]

        check, displaced = self.registry.open(
            channel_id=channel_id,
            user_id=user_id,
            actor=actor,
            move=rules.key,
            stat=stat,
            modifier=mod,
            stakes=stakes,
            dice=MOVE_DICE,
            target=target,
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
        at = f" at {target}" if target else ""
        effects.append(SendMessage(
            channel_id,
            f"**{actor}** — {rules.name}{at}, roll {rolled_with}.",
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

        If the result still needs the player -- a choice, then damage -- the move is held
        with its dice and the narration waits. Otherwise it resolves now.

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
        return [rolled, *self._next(channel_id, check, dice)]

    def _next(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        """Whatever the move needs next, now that its dice have landed."""
        rules = MOVES[check.move].band(band(dice))

        if check.move == "last_breath" and rules.choice is not None and not check.picks:
            # Death names the price before the player decides: the bargain is the GM's to
            # invent, the choice is theirs, and a price nobody has heard is no choice.
            held = self.registry.hold(check, faces=dice.faces)
            return [
                PromptModel(channel_id, "rules_event", {
                    "event": "Last Breath: Death offers a bargain",
                    "actor": check.actor,
                    "roll_total": dice.total,
                    "what_it_means": rules.text,
                    "instruction": (
                        f"Describe what {check.actor} glimpses beyond the Black Gates, and "
                        "have Death offer a specific bargain: a real, lasting price for "
                        "stabilizing. End on the offer. Do NOT decide whether they take it -- "
                        "the player chooses next."
                    ),
                }),
                SendMessage(
                    channel_id,
                    f"**{check.actor}**, Death waits for your answer.",
                    prompt=ChoicePrompt(
                        check_id=held.check_id,
                        move=held.move,
                        band=band(dice),
                        count=1,
                        options=tuple(o.text for o in rules.choice.options),
                    ),
                ),
            ]

        if rules.choice is not None and not check.picks:
            held = self.registry.hold(check, faces=dice.faces)
            return [SendMessage(
                channel_id,
                # Who picks and where. How many is in the rules text for most moves, so
                # it is only repeated when there is more than one.
                f"{rules.text}\nPick "
                + ("" if rules.choice.count == 1 else f"{rules.choice.count} ")
                + f"below, **{check.actor}**.",
                prompt=ChoicePrompt(
                    check_id=held.check_id,
                    move=held.move,
                    band=band(dice),
                    count=rules.choice.count,
                    options=tuple(o.text for o in rules.choice.options),
                ),
            )]

        if rules.deals_damage:
            held = self.registry.hold(check, faces=dice.faces)
            die, _, weapon = self._damage_die(check)
            extra = self._damage_extra(check, dice)
            with_what = f", {weapon}" if weapon else ""
            return [SendMessage(
                channel_id,
                f"**{check.actor}**, roll your damage ({die}{' ' + extra if extra else ''}"
                f"{with_what}) against {check.target}.",
                prompt=DamagePrompt(held.check_id),
            )]

        self.registry.consume(check.check_id)
        outcome = resolve(check, dice, picks=check.picks)
        return [*self._consequences(channel_id, outcome), *self._resolved(channel_id, outcome)]

    def _consequences(self, channel_id: int, outcome: MoveOutcome) -> List[Effect]:
        """What a rules-triggered move does to the sheet, beyond being narrated."""
        name = outcome.actor
        if outcome.move != "last_breath":
            return []
        took_bargain = outcome.chosen[:1] == (MOVES["last_breath"].weak.choice.options[0].text,)
        if outcome.band == "strong" or took_bargain:
            return [
                SendMessage(channel_id, f"**{name}** lives — back to 1 HP."),
                UpdateSheet(channel_id, name, {"hp": 1}),
            ]
        if outcome.band == "weak":
            return [
                SendMessage(channel_id, f"**{name}** refuses Death, and passes beyond the Black Gates."),
                UpdateSheet(channel_id, name, {"dead": True}),
            ]
        return [
            SendMessage(channel_id, f"**{name}** is marked as Death's own."),
            UpdateSheet(channel_id, name, {"deaths_mark": True}),
        ]

    # -- the rules trigger a move ------------------------------------------------------

    def _trigger(self, channel_id: int, actor: str, move: str) -> List[Effect]:
        """Open a move the rules call for, for whoever plays this character.

        Their player rolls it like any other. With no player on record nobody could click,
        so it stays open only until the sweep rolls it for them: a rules-triggered move
        never lapses the way a declined one does.
        """
        rules = MOVES[move]
        user_id = next((uid for uid, a in self.players.items()
                        if normalize_actor(a) == normalize_actor(actor)), 0)
        mod = 0
        if rules.stat not in (NOTHING, BOND) and self._sheet_for is not None:
            sheet = self._sheet_for(actor)
            if sheet is not None:
                mod = modifier(int(sheet["stats"][rules.stat]))
        check, _ = self.registry.open(
            channel_id=channel_id, user_id=user_id, actor=actor, move=move,
            stat=rules.stat, modifier=mod, stakes="", dice=MOVE_DICE,
        )
        with_what = "+nothing" if rules.stat == NOTHING else \
            f"+{STAT_NAMES[rules.stat][:3].title()} {mod:+d}"
        return [SendMessage(
            channel_id,
            f"**{actor}** — {rules.name}, roll {with_what}.",
            prompt=RollPrompt(check.check_id),
        )]

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
        if check is None or not check.rolled or check.picks or check.check_id != check_id:
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

        chosen = replace(check, picks=tuple(sorted(picks)))
        said = SendMessage(
            channel_id,
            f"**{check.actor}** chooses: "
            + "; ".join(rules.choice.options[p].text for p in chosen.picks),
        )
        return [said, *self._next(channel_id, chosen, dice)]

    # -- inbound: the player rolls damage ----------------------------------------------

    def roll_damage(self, *, channel_id: int, user_id: int, check_id: str) -> List[Effect]:
        """The roller rolls their damage against their target. Same rules as rolling:
        only them, only for this move, only once, and only once the move is ready for it.

        If the rules say the enemy attacks back, that lands in the same breath -- Gary
        rolls the creature's damage against the character -- and then the whole exchange
        goes to the model as fact.
        """
        check = self.registry.for_player(channel_id, user_id)
        if check is None or check.check_id != check_id or not self._awaits_damage(check):
            return []
        return self._land_damage(channel_id, check)

    def _awaits_damage(self, check: PendingCheck) -> bool:
        if not check.rolled:
            return False
        rules = MOVES[check.move].band(band(self._dice_of(check)))
        return rules.deals_damage and (rules.choice is None or bool(check.picks))

    def _land_damage(
        self, channel_id: int, check: PendingCheck, auto: bool = False
    ) -> List[Effect]:
        self.registry.consume(check.check_id)
        dice = self._dice_of(check)
        outcome = resolve(check, dice, picks=check.picks)
        effects: List[Effect] = []

        die, piercing, _ = self._damage_die(check)
        hit = damage.roll(self.dice, damage.parse(die), self._damage_extra(check, dice))
        target = self._monsters().get(check.target)
        facts: Dict[str, Any] = {"target": check.target, "rolled": hit.total}
        line = f"🎲 **{check.actor}**{' (rolled for them)' if auto else ''} damage — {hit.render()}"
        if target is None:
            facts["note"] = "The target was no longer in the scene; the damage lands on nothing."
            line += f" — {check.target} is gone."
        else:
            dealt = damage.through_armor(hit.total, target["armor"], piercing)
            after = int(target["hp"]) - dealt
            facts.update(armor=target["armor"], dealt=dealt, hp_before=target["hp"],
                         hp_after=max(after, 0), defeated=after <= 0,
                         severity=damage.severity(dealt, target.get("max_hp", target["hp"]), after))
            line += (
                f" → {check.target}: {dealt} through armor {target['armor']}, "
                f"HP {target['hp']} → {max(after, 0)}" + (" — **defeated**." if after <= 0 else ".")
            )
            effects.append(ChangeHp(channel_id, "monster", check.target, after))
        effects.insert(0, SendMessage(channel_id, line))

        effects.extend(self._spend_ammo(channel_id, check, dice))

        back = None
        if outcome.enemy_attacks and target is not None:
            # The trade is simultaneous: on a 7-9 the enemy's blow lands even if yours
            # finished it. The rules say both happen, so both do.
            attack_effects, back = self._attack(channel_id, check.target, target, check.actor)
            effects.extend(attack_effects)

        outcome = replace(outcome, damage=facts, enemy_attack=back)
        dying = self._trigger(channel_id, check.actor, "last_breath") \
            if back and back.get("down") else []
        return [*effects, *self._resolved(channel_id, outcome), *dying]

    def _weapon(self, sheet: Dict[str, Any], move: str):
        return weapon_for(sheet.get("gear") or [], ranged=move == "volley",
                          ammo=int(sheet.get("ammo", 0)))

    def _damage_die(self, check: PendingCheck) -> Tuple[str, int, str]:
        """(damage expression, piercing, weapon name) for this attack.

        The class's die, plus the weapon's +damage, with its piercing. A sheet with no
        gear -- made before gear existed -- rolls the bare class die, as it always did.
        """
        sheet = self._sheet_for(check.actor) if self._sheet_for is not None else None
        if not sheet:
            return "d6", 0, ""
        die = sheet["damage"]
        weapon = self._weapon(sheet, check.move) if "gear" in sheet else None
        if weapon is None:
            return die, 0, ""
        spec = damage.parse(die)
        boosted = damage.DamageSpec(spec.sides, spec.mode, spec.modifier + weapon.damage)
        return str(boosted), weapon.piercing, weapon.name

    def _spend_ammo(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        choice = MOVES[check.move].band(band(dice)).choice
        if not choice or not any(choice.options[p].spends_ammo for p in check.picks):
            return []
        sheet = self._sheet_for(check.actor) if self._sheet_for is not None else None
        if not sheet or "gear" not in sheet:
            return []
        left = max(0, int(sheet.get("ammo", 0)) - 1)
        return [
            SendMessage(channel_id, f"**{check.actor}** has {left} ammo left."),
            SpendAmmo(channel_id, check.actor, left),
        ]

    def _damage_extra(self, check: PendingCheck, dice: Roll) -> Optional[str]:
        choice = MOVES[check.move].band(band(dice)).choice
        mods = [choice.options[p].damage_mod for p in check.picks] if choice else []
        return next((m for m in mods if m), None)

    # -- the GM's side: a creature attacks ---------------------------------------------

    def monster_attacks(
        self, *, channel_id: int, monster: str, character: str
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """A creature deals its damage to a character -- the GM move "deal damage", on a
        miss or when everyone looks to the GM.

        Whether it happens is the model's call. How much is not: Gary rolls the creature's
        damage, takes the character's armor off, and reports the result as fact.
        Returns (effects, the tool result for the model).
        """
        scene = self._monsters()
        name = next((n for n in scene if n.lower() == str(monster or "").strip().lower()), None)
        if name is None or scene[name]["hp"] <= 0:
            standing = ", ".join(sorted(n for n, r in scene.items() if r["hp"] > 0)) or "none"
            return [], {"ok": False, "error": f"No standing creature named {monster!r}. "
                                              f"Standing: {standing}."}
        sheet = self._sheet_for(character) if self._sheet_for is not None else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet to "
                                              "deal damage to."}
        unable = _unable(character, sheet)
        if unable:
            return [], {"ok": False, "error": unable}
        effects, facts = self._attack(channel_id, name, scene[name], character)
        if facts.get("down"):
            effects.extend(self._trigger(channel_id, character, "last_breath"))
        return effects, {"ok": True, **facts,
                         "note": "Rolled and applied by the rules engine. Narrate it as it "
                                 "landed, as bad as its severity and no worse or better; do "
                                 "not change the numbers."}

    def _attack(
        self, channel_id: int, name: str, creature: Dict[str, Any], character: str
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        sheet = self._sheet_for(character) if self._sheet_for is not None else None
        hit = damage.roll(self.dice, damage.parse(creature["damage"]))
        armor = int(sheet["armor"]) if sheet else 0
        dealt = damage.through_armor(
            hit.total, armor, creature.get("piercing", 0), creature.get("ignores_armor", False)
        )
        facts: Dict[str, Any] = {"attacker": name, "character": character,
                                 "rolled": hit.total, "armor": armor, "dealt": dealt}
        line = f"🗡️ **{name}** hits **{character}** — {hit.render()}"
        effects: List[Effect] = []
        if sheet is not None:
            after = int(sheet["hp"]) - dealt
            facts.update(hp_before=sheet["hp"], hp_after=max(after, 0), down=after <= 0,
                         severity=damage.severity(dealt, sheet.get("max_hp", sheet["hp"]), after))
            line += (
                f" → {dealt} through armor {armor}, HP {sheet['hp']} → {max(after, 0)}"
                + (" — **down**." if after <= 0 else ".")
            )
            if after <= 0:
                facts["dying"] = (
                    f"{character} is at 0 HP and dying. They roll Last Breath next: narrate "
                    "them falling, and leave whether they live open."
                )
            effects.append(ChangeHp(channel_id, "character", character, max(after, 0)))
        return [SendMessage(channel_id, line), *effects], facts

    # -- shared --------------------------------------------------------------------------

    def _dice_of(self, check: PendingCheck) -> Roll:
        return Roll(spec=check.dice, faces=check.faces, modifier=check.modifier)

    def _resolved(self, channel_id: int, outcome: MoveOutcome) -> List[Effect]:
        # A move the rules triggered answers no make_move call, so it must not be handed
        # to one that happens to be waiting -- someone else's, perhaps.
        kind = "check_outcome" if MOVES[outcome.move].callable else "rules_event"
        return [
            RecordOutcome(channel_id=channel_id, outcome=outcome),
            PromptModel(channel_id=channel_id, kind=kind, payload=outcome_for_model(outcome)),
        ]

    # -- housekeeping ------------------------------------------------------------------

    def sweep(self) -> List[Effect]:
        """Expire stale moves. Called on a timer (ARCHITECTURE.md §4).

        A move nobody rolled lapses: the moment passed. A move that was rolled does not --
        the dice already landed, and the fiction has to honour them. A choice never made
        takes the first options; damage never rolled is rolled for them. Either way the
        table is told.
        """
        effects: List[Effect] = []
        for cancellation in self.registry.sweep_expired():
            check = cancellation.check
            if check.rolled:
                effects.extend(self._finish_for_them(check))
                continue
            if not MOVES[check.move].callable:
                # Death and whatever approaches the camp do not wait on a click.
                dice = self.dice.roll(check.dice, modifier=check.modifier)
                effects.append(SendMessage(
                    check.channel_id,
                    f"🎲 **{check.actor}** (rolled for them) rolls {dice.render()} — "
                    f"{_RESULT[band(dice)]}.",
                ))
                effects.extend(self._next(check.channel_id, check, dice))
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

    def _finish_for_them(self, check: PendingCheck) -> List[Effect]:
        channel_id = check.channel_id
        dice = self._dice_of(check)
        rules = MOVES[check.move].band(band(dice))
        effects: List[Effect] = []
        auto_chosen = False
        if rules.choice is not None and not check.picks:
            check = replace(check, picks=tuple(range(rules.choice.count)))
            auto_chosen = True
            effects.append(SendMessage(
                channel_id,
                f"**{check.actor}** didn't choose in time, so: "
                + "; ".join(rules.choice.options[p].text for p in check.picks),
            ))
        if rules.deals_damage:
            # Consumed by the sweep already; _land_damage consumes again harmlessly.
            landed = self._land_damage(channel_id, check, auto=True)
            if auto_chosen:
                landed = [_auto(e) for e in landed]
            return effects + landed
        outcome = resolve(check, dice, picks=check.picks, auto_chosen=auto_chosen)
        return effects + self._consequences(channel_id, outcome) + self._resolved(channel_id, outcome)

    # -- Make Camp -------------------------------------------------------------------------

    def make_camp(
        self, *, channel_id: int, approaches: bool = False, watcher: str = "", reason: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The party settles in to rest -- once for all of them, however many sleep.

        Everyone eats a ration. If the night passes quietly, everyone who ate heals half
        their max HP; whoever had no ration goes hungry and heals nothing. If something
        approaches -- the model's call, like any GM move -- nobody heals, and the watcher
        rolls Take Watch.

        Refused, back to the model, when the rules can see the party cannot settle in:
        creatures still standing, or anyone mid-move -- above all a Last Breath not yet
        rolled. What the rules cannot see (a chase, a storm at sea) is the model's to
        judge, so it must say where and why it is safe: ``reason``, shown to the table.
        """
        standing = sorted(n for n, r in self._monsters().items() if r["hp"] > 0)
        if standing:
            return [], {"ok": False, "error": (
                f"You can't settle in to rest with {_and(standing)} still standing. Finish "
                "the fight, or remove them with remove_monsters if they have truly gone."
            )}
        pending = list(self.registry.open_checks(channel_id))
        if pending:
            what = _and(f"{c.actor}'s {MOVES[c.move].name}" for c in pending)
            return [], {"ok": False, "error": f"Not while {what} is still to be resolved."}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": (
                "Say where the party rests and why it is safe enough to sleep (reason). If "
                "it is not safe, tell them so, or let them try and set something_approaches."
            )}
        party = []
        for actor in sorted(set(self.players.values())):
            sheet = self._sheet_for(actor) if self._sheet_for is not None else None
            if sheet is not None and not sheet.get("dead"):
                party.append((actor, sheet))
        if not party:
            return [], {"ok": False, "error": "Nobody at the table has a character sheet."}

        watch = None
        if approaches:
            watch = next((a for a, _ in party if normalize_actor(a) == normalize_actor(watcher)), None)
            if watch is None:
                names = ", ".join(a for a, _ in party)
                return [], {"ok": False,
                            "error": f"Name who is on watch, from: {names}."}

        effects: List[Effect] = []
        lines = [f"🏕️ The party makes camp — {reason}"]
        report: Dict[str, Any] = {}
        for actor, sheet in party:
            changes: Dict[str, Any] = {}
            fed = True
            if "gear" in sheet:  # sheets made before gear do not count rations
                have = int(sheet["rations"]) if "rations" in sheet else rations_of(sheet["gear"])
                fed = have > 0
                if fed:
                    changes["rations"] = have - 1
            entry: Dict[str, Any] = {"ate": fed}
            if "rations" in changes:
                entry["rations_left"] = changes["rations"]
            if fed and not approaches:
                healed = min(int(sheet["max_hp"]) - int(sheet["hp"]), int(sheet["max_hp"]) // 2)
                changes["hp"] = int(sheet["hp"]) + healed
                entry.update(healed=healed, hp=changes["hp"], max_hp=sheet["max_hp"])
                lines.append(
                    f"**{actor}** eats" + (f" ({changes['rations']} rations left)" if "rations" in changes else "")
                    + f" and heals {healed} — HP {changes['hp']}/{sheet['max_hp']}."
                )
            elif fed:
                lines.append(f"**{actor}** eats" + (f" ({changes['rations']} rations left)." if "rations" in changes else "."))
            else:
                lines.append(f"**{actor}** has no rations: goes hungry, and does not heal.")
            if changes:
                effects.append(UpdateSheet(channel_id, actor, changes))
            report[actor] = entry

        effects.insert(0, SendMessage(channel_id, "\n".join(lines)))
        facts: Dict[str, Any] = {"ok": True, "party": report}
        if approaches:
            effects.append(SendMessage(channel_id, "Something stirs beyond the firelight…"))
            effects.extend(self._trigger(channel_id, watch, "take_watch"))
            facts["note"] = (
                f"Something approaches in the night. {watch} is on watch and rolls Take "
                "Watch; nobody heals, because the sleep is broken. Narrate the approach once "
                "the roll is in, not before."
            )
        else:
            facts["note"] = ("The night passes. The world clock has moved eight hours for the "
                             "whole party; do not advance it again.")
        return effects, facts

    def cancel(self, channel_id: int, check_id: str) -> List[Effect]:
        cancellation = self.registry.cancel(check_id, CancelReason.MANUAL)
        if cancellation is None:
            return []
        return [SendMessage(
            channel_id,
            f"Cancelled {cancellation.check.actor}'s {MOVES[cancellation.check.move].name}.",
        )]


_RESULT = {"strong": "strong hit", "weak": "weak hit", "miss": "miss"}


def _auto(effect: Effect) -> Effect:
    """Mark an outcome as auto-chosen, in both the record and what the model is told."""
    if isinstance(effect, RecordOutcome):
        return replace(effect, outcome=replace(effect.outcome, auto_chosen=True))
    if isinstance(effect, PromptModel) and effect.kind == "check_outcome":
        return replace(effect, payload={
            **effect.payload,
            "note": "The player did not choose in time, so the first options were taken.",
        })
    return effect


def _and(names) -> str:
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _unable(actor: str, sheet: Dict[str, Any]) -> str:
    """Why this character cannot act, or "" if they can."""
    if sheet.get("dead"):
        return f"{actor} is dead."
    if int(sheet.get("hp", 1)) <= 0:
        return f"{actor} is down at 0 HP" + (
            " and marked as Death's own." if sheet.get("deaths_mark") else "."
        )
    return ""
