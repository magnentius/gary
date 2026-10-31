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

from . import bonds, choices, companion as companions, damage, spells, trade
from .gear import rations_of, weapon_for
from .models import CancelReason, MoveOutcome, PendingCheck, Roll
from .moves import ANY, BOND, CASTER, MOVES, NOTHING
from .ports import Dice, RandomDice
from .registry import PendingCheckRegistry, normalize_actor
from .resolver import band, outcome_for_model, resolve
from .sheets import (
    DEBILITIES, LEVEL_UP_HINT, STAT_NAMES, STATS, debility_on, encumbrance, stat_modifier, xp_line,
    xp_needed,
)

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


@dataclass(frozen=True)
class SpellsPrompt:
    """A Choose spells button, for this player's caster: after Commune or Prepare
    Spells, or for a caster who has never chosen any."""

    user_id: int
    actor: str


@dataclass(frozen=True)
class OverloadPrompt:
    """Encumbrance past load+2: drop one of these (by index into the gear) and roll at -1,
    or fail outright."""

    check_id: str
    items: tuple


@dataclass(frozen=True)
class CurePrompt:
    """A button per debility, for this player to pick which Recover clears."""

    user_id: int
    debilities: tuple


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
        spell: str = "",
        companion: bool = False,
        coins: int = 0,
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

        # A class move is that class's alone: a Fighter cannot Backstab.
        if rules.classes and self._sheet_for is not None:
            sheet = self._sheet_for(actor)
            if sheet is not None and sheet.get("class") not in rules.classes:
                return [self._refuse(
                    channel_id,
                    f"{rules.name} is the {' or '.join(rules.classes)}'s move; {actor} is a "
                    f"{sheet.get('class', 'character without a class')}.",
                    "Choose a move this character has -- a basic move, or one listed under "
                    "their class.",
                )]

        if rules.stat == CASTER:
            return self._open_cast(channel_id, user_id, actor, spell, target, stakes, ttl_seconds)
        if rules.key == "carouse":
            return self._open_carouse(channel_id, user_id, actor, int(coins or 0), stakes, ttl_seconds)

        # A healing move lands on a character with a sheet: the one named, or the mover.
        if rules.heals_target:
            wanted = str(target or "").strip() or actor
            if self._sheet_for is not None:
                found = self._character(wanted)
                if found is None:
                    return [self._refuse(
                        channel_id,
                        f"{rules.name} heals a character; {wanted!r} has no character sheet.",
                        "Re-issue make_move with target set to the character being healed.",
                    )]
                wanted = found
            target = wanted

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
        elif rules.defends:
            # Defend: a person, an item or a place. A character at the table is named as
            # their sheet has it, so an attack on them can find the defender.
            charge = " ".join(str(target or "").split())[:60]
            if not charge:
                return [self._refuse(
                    channel_id, f"{rules.name} needs who or what is defended: a character, an "
                                "item or a place.",
                    "Re-issue make_move with target set to it.",
                )]
            target = self._character(charge) or charge
        elif rules.names_form:
            # Shapeshifter: "any species whose essence you have studied or who lives in your
            # land". Which those are is the fiction's -- the land is not recorded -- so any
            # form named is taken, and the model judges whether it is one of them.
            target = " ".join(str(target or "").split())[:40]
            if not target:
                return [self._refuse(
                    channel_id, f"{rules.name} needs the form to take, e.g. 'bear' or 'hawk'.",
                    "Re-issue make_move with target set to the species.",
                )]
        elif rules.helps_target:
            # Aid or Interfere is with someone: +Bond counts the bonds with them.
            wanted = str(target or "").strip()
            if self._sheet_for is not None:
                found = self._character(wanted) if wanted else None
                if found is None or normalize_actor(found) == normalize_actor(actor):
                    others = ", ".join(sorted(a for a in self.players.values()
                                              if normalize_actor(a) != normalize_actor(actor))) or "none"
                    return [self._refuse(
                        channel_id,
                        f"{rules.name} needs the character being helped or hindered. "
                        f"{'No other character named ' + repr(wanted) + '. ' if wanted else ''}"
                        f"At the table: {others}.",
                        "Re-issue make_move with target set to that character, or if they help "
                        "no one at the table, narrate it without a move.",
                    )]
                wanted = found
            target = wanted
        elif not rules.heals_target:
            target = ""

        mod, notes = 0, []
        if stat == BOND and self._sheet_for is not None:
            sheet = self._sheet_for(actor)
            if sheet is not None:
                unable = _unable(actor, sheet)
                if unable:
                    return [self._refuse(
                        channel_id, unable,
                        "They cannot act. Narrate around them, or ask what someone else does.",
                    )]
                mod = bonds.bond_with(sheet, target)
        if stat != BOND and self._sheet_for is not None:
            sheet = self._sheet_for(actor)
            if sheet is None:
                return [self._refuse(
                    channel_id,
                    f"{actor} has no character sheet, so there is no stat to roll.",
                    "Tell the player, in a sentence, to make one with `?dm new` before "
                    "they roll. Do not resolve the action.",
                )]
            mod = stat_modifier(sheet, stat)
            # A precise weapon hacks and slashes with Dex, not Str -- where Dex is better.
            if rules.key == "hack_and_slash" and "gear" in sheet:
                weapon = self._weapon(sheet, rules.key)
                dex = stat_modifier(sheet, "dex")
                precise = weapon is not None and ("precise" in weapon.tags
                                                  or choices.precise_for(sheet, weapon.name))
                if precise and dex > mod:
                    stat, mod = "dex", dex
            if debility_on(sheet, stat):
                notes.append(f"-1 {debility_on(sheet, stat).title()}")
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
                    if rules.ranged else "a hand, close or reach weapon"
                return [self._refuse(
                    channel_id,
                    f"{actor} has nothing to {rules.name} with: it needs {needs}.",
                    "Narrate what they can do instead, or choose a move that fits what they "
                    "carry. Do not resolve the attack.",
                )]

        if self._blessed(actor):
            mod += 1
            notes.append("+1 Bless")
        burden = self._burden(actor)
        if burden:
            mod -= 1
            notes.append("-1 encumbered")
        pet = ""
        if companion and self._sheet_for is not None:
            sheet = self._sheet_for(actor) or {}
            rule, why = companions.command(sheet, rules.key)
            if rule is None:
                return [self._refuse(
                    channel_id, why,
                    "Re-issue make_move without companion, or narrate what the companion does "
                    "instead.",
                )]
            pet = sheet["companion"]["name"]
            if rule.roll:
                bonus = int(sheet["companion"][rule.roll])
                mod += bonus
                notes.append(f"+{bonus} {pet}")
        at = (f" on {target}" if rules.heals_target else f" with {target}" if rules.helps_target
              else f" into a {target}" if rules.names_form else f" {target}" if rules.defends
              else f" at {target}") if target else ""
        return self._open(channel_id, user_id, actor, rules.key, stat, mod, stakes, target,
                          ttl_seconds, f"{rules.name}{at}" + (f", with {pet}" if pet else ""),
                          notes, companion=pet, overloaded=burden == "overloaded")

    def _open(
        self, channel_id: int, user_id: int, actor: str, move: str, stat: str, mod: int,
        stakes: str, target: str, ttl_seconds: Optional[float], what: str,
        notes: Sequence[str] = (), spell: str = "", companion: str = "", overloaded: bool = False,
    ) -> List[Effect]:
        """Commit the move -- stat, modifier, target, spell -- and ask for the roll."""
        check, displaced = self.registry.open(
            channel_id=channel_id,
            user_id=user_id,
            actor=actor,
            move=move,
            stat=stat,
            modifier=mod,
            stakes=stakes,
            dice=MOVE_DICE,
            target=target,
            spell=spell,
            companion=companion,
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
        rolled_with = f"+Bond {mod:+d}" if stat == BOND else f"{mod:+d}" if stat == NOTHING else \
            f"+{STAT_NAMES[stat][:3].title()} {mod:+d}"
        if notes:
            rolled_with += f" ({', '.join(notes)})"
        if overloaded:
            # "...you have a choice: drop at least 1 weight and roll at -1, or automatically fail."
            gear = (self._sheet_for(actor) or {}).get("gear") or []
            effects.append(SendMessage(
                channel_id,
                f"**{actor}** — {what}, roll {rolled_with}. You carry more than you can: drop "
                "something and roll, or fail.",
                prompt=OverloadPrompt(check.check_id, tuple(
                    (i, g["name"]) for i, g in enumerate(gear) if int(g.get("weight", 0)) > 0)),
            ))
            return effects
        effects.append(SendMessage(
            channel_id,
            f"**{actor}** — {what}, roll {rolled_with}.",
            prompt=RollPrompt(check.check_id),
        ))
        return effects

    def _burden(self, actor: str) -> str:
        """"" if fine, else "burdened" or "overloaded" (Encumbrance): -1 either way."""
        sheet = self._sheet_for(actor) if self._sheet_for is not None else None
        state = encumbrance(sheet) if sheet else "fine"
        return "" if state == "fine" else state

    def drop_and_roll(self, *, channel_id: int, user_id: int, check_id: str, index: int) -> List[Effect]:
        """Overloaded: the player drops an item and rolls at -1 (already in the modifier)."""
        check = self.registry.for_player(channel_id, user_id)
        if check is None or check.rolled or check.check_id != check_id:
            return []
        sheet = self._sheet_for(check.actor) if self._sheet_for is not None else None
        gear = (sheet or {}).get("gear") or []
        if not 0 <= index < len(gear) or int(gear[index].get("weight", 0)) <= 0:
            return []
        dropped, _ = trade.remove_items(sheet, [gear[index]["name"]])
        changes = {k: dropped[k] for k in ("gear", "armor", "ammo", "rations")}
        return [
            SendMessage(channel_id, f"**{check.actor}** drops {gear[index]['name']}."),
            UpdateSheet(channel_id, check.actor, changes),
            *self.roll(channel_id=channel_id, user_id=user_id, check_id=check_id),
        ]

    def fail_overloaded(self, *, channel_id: int, user_id: int, check_id: str) -> List[Effect]:
        """Overloaded and keeping it all: the move fails outright, as a miss. No dice are
        rolled, so no XP is marked."""
        check = self.registry.for_player(channel_id, user_id)
        if check is None or check.rolled or check.check_id != check_id:
            return []
        self.registry.consume(check.check_id)
        failed = Roll(spec="", faces=(), modifier=0)
        outcome = resolve(check, failed)
        return [
            SendMessage(channel_id, f"**{check.actor}** keeps everything, and fails — too much to carry."),
            RecordOutcome(channel_id=channel_id, outcome=outcome),
            PromptModel(channel_id=channel_id,
                        kind="check_outcome" if MOVES[check.move].callable else "rules_event",
                        payload={**outcome_for_model(outcome),
                                 "note": "Overloaded, they chose to fail rather than drop anything."}),
        ]

    def _open_cast(
        self, channel_id: int, user_id: int, actor: str, name: str, target: str, stakes: str,
        ttl_seconds: Optional[float],
    ) -> List[Effect]:
        """Cast a Spell: only a spell the caster has now, at what it costs them to cast.

        The spell is fixed with the move, before the dice, like a target: the model
        cannot swap Magic Missile for Fireball once it sees a 10.
        """
        sheet = self._sheet_for(actor) if self._sheet_for is not None else None
        if sheet is None:
            return [self._refuse(
                channel_id, f"{actor} has no character sheet, so there is no spell to cast.",
                "Tell the player, in a sentence, to make one with `?dm new` first.",
            )]
        unable = _unable(actor, sheet)
        if unable:
            return [self._refuse(channel_id, unable,
                                 "They cannot act. Narrate around them, or ask what someone else does.")]
        have = spells.castable(sheet)
        listing = ", ".join(s.name for s in have) or "none"
        spell = next((s for s in have if s.name.lower() == str(name or "").strip().lower()), None)
        if spell is None:
            known = spells.find(sheet, name) if name else None
            if not name:
                why = "Cast a Spell needs the spell being cast."
            elif known is not None and known.name.lower() in {n.lower() for n in sheet.get("lost") or ()}:
                why = (f"{actor} has lost {known.name} until they next "
                       f"{spells.PREPARE_NAMES[spells.caster_class(sheet)].lower()}.")
            elif known is not None:
                why = f"{actor} does not have {known.name} prepared."
            else:
                why = f"{actor} has no spell called {name!r}."
            setup = " They have not chosen spells yet: tell them `?dm spells`." \
                if spells.needs_setup(sheet) else ""
            return [self._refuse(
                channel_id, f"{why} They can cast: {listing}.{setup}",
                "Re-issue make_move with spell set to one of these, or narrate that they "
                "cannot.",
            )]
        blocked = spells.blocking(sheet)
        if blocked is not None:
            return [self._refuse(
                channel_id, f"While {blocked.name} lasts, {actor} cannot cast a spell.",
                f"If they let {blocked.name} go, call end_spell first. Otherwise narrate "
                "that they cannot.",
            )]

        if spell.target == "creature":
            standing = {n.lower(): n for n, r in self._monsters().items() if r["hp"] > 0}
            wanted = [t.strip() for t in str(target or "").split(",") if t.strip()]
            if not spell.area:
                wanted = wanted[:1]
            missing = [t for t in wanted if t.lower() not in standing]
            if not wanted or missing:
                return [self._refuse(
                    channel_id,
                    f"{spell.name} needs {'its targets' if spell.area else 'a target'} from the "
                    f"creatures in this scene. "
                    f"{'No creature named ' + repr(missing[0]) + '. ' if missing else ''}"
                    f"Standing: {', '.join(sorted(standing.values())) or 'none'}.",
                    "Add the creature with add_monsters first if it is new, then re-issue "
                    "make_move with target set to its exact name"
                    + (" -- for Fireball, every creature caught in it, comma-separated." if spell.area else "."),
                )]
            target = ", ".join(dict.fromkeys(standing[t.lower()] for t in wanted))
        elif spell.target == "character":
            found = self._character(str(target or "").strip() or actor)
            if found is None:
                return [self._refuse(
                    channel_id, f"{spell.name} needs a character; {target!r} has no character sheet.",
                    "Re-issue make_move with target set to the character.",
                )]
            target = found
        else:
            target = ""

        stat = "wis" if spells.caster_class(sheet) == "cleric" else "int"
        mod = stat_modifier(sheet, stat)
        penalty, notes = spells.casting_modifier(sheet)
        if debility_on(sheet, stat):
            notes.append(f"-1 {debility_on(sheet, stat).title()}")
        mod += penalty
        if self._blessed(actor):
            mod += 1
            notes.append("+1 Bless")
        burden = self._burden(actor)
        if burden:
            mod -= 1
            notes.append("-1 encumbered")
        on = f" on {target}" if spell.target == "character" else f" at {target}" if target else ""
        return self._open(channel_id, user_id, actor, "cast_a_spell", stat, mod, stakes, target,
                          ttl_seconds, f"Cast a Spell: {spell.name}{on}", notes, spell=spell.name,
                          overloaded=burden == "overloaded")

    def _open_carouse(
        self, channel_id: int, user_id: int, actor: str, coins: int, stakes: str,
        ttl_seconds: Optional[float],
    ) -> List[Effect]:
        """Carouse: "spend 100 coin and roll + extra 100s of coin spent". Whole hundreds only;
        the coin goes when the party is resolved, so a party never thrown costs nothing."""
        sheet = self._sheet_for(actor) if self._sheet_for is not None else None
        if sheet is None:
            return [self._refuse(channel_id, f"{actor} has no character sheet.",
                                 "Tell the player to make one with `?dm new` first.")]
        spend = coins // 100 * 100
        have = int(sheet.get("coins", 0))
        if spend < 100:
            return [self._refuse(channel_id, "A party worth the name costs at least 100 coins.",
                                 "Re-issue make_move with coins of 100 or more, or narrate a "
                                 "smaller night out without a move.")]
        if spend > have:
            return [self._refuse(channel_id, f"{actor} has {have} coins; that party costs {spend}.",
                                 "Re-issue make_move with coins they can afford, or narrate them "
                                 "short of the money.")]
        extra = spend // 100 - 1
        return self._open(channel_id, user_id, actor, "carouse", NOTHING, extra, stakes, "",
                          ttl_seconds, f"Carouse with {spend} coins", ["+1 for each 100 past the first"]
                          if extra else ())

    def _party(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Every character at the table with a sheet, living."""
        out = []
        for actor in sorted(set(self.players.values())):
            sheet = self._sheet_for(actor) if self._sheet_for is not None else None
            if sheet is not None and not sheet.get("dead"):
                out.append((actor, sheet))
        return out

    def _blessed(self, actor: str) -> bool:
        """Bless: "+1 ongoing so long as battle continues" -- while creatures stand."""
        if not any(r["hp"] > 0 for r in self._monsters().values()):
            return False
        return any(
            s.name == spells.BLESS and normalize_actor(t) == normalize_actor(actor)
            for _, sheet in self._party() for s, t in spells.running(sheet)
        )

    def _character(self, name: str) -> Optional[str]:
        """A character's name as their sheet has it, however the model cased it."""
        if self._sheet_for is None:
            return None
        if self._sheet_for(name) is not None:
            return name
        folded = normalize_actor(name)
        return next((a for a in self.players.values()
                     if normalize_actor(a) == folded and self._sheet_for(a) is not None), None)

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

        if self._deals_damage(check, dice):
            held = self.registry.hold(check, faces=dice.faces)
            die, _, weapon = self._damage_die(check)
            ferocity = self._ferocity(check)
            extra = " ".join(x for x in (self._damage_extra(check, dice),
                                         "+1d4 Magic Weapon" if self._magic_weapon(check) else None,
                                         f"+{ferocity} {check.companion}" if ferocity else None) if x)
            with_what = f", {weapon}" if weapon else ""
            return [SendMessage(
                channel_id,
                f"**{check.actor}**, roll your damage ({die}{' ' + extra if extra else ''}"
                f"{with_what}) against {check.target}.",
                prompt=DamagePrompt(held.check_id),
            )]

        self.registry.consume(check.check_id)
        return self._settle(channel_id, check, dice)

    def _settle(self, channel_id: int, check: PendingCheck, dice: Roll, auto_chosen: bool = False) -> List[Effect]:
        """Resolve a move that deals no damage: heal, cast, and tell the model."""
        cast, spell = self._cast(channel_id, check, dice)
        shift, shape = self._shift(channel_id, check, dice)
        guard = self._guard(channel_id, check, dice)
        outcome = replace(resolve(check, dice, picks=check.picks, auto_chosen=auto_chosen),
                          healing=self._healing(check, dice), spell=spell, shape=shape)
        return [*self._consequences(channel_id, outcome), *cast, *shift, *guard,
                *self._resolved(channel_id, outcome)]

    def _guard(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        """Defend: hold 3 on a 10+, 1 on a 7-9, against attacks on the defender or their
        charge, for as long as they stand in defense."""
        if check.move != "defend" or band(dice) == "miss" or not check.target:
            return []
        hold = 3 if band(dice) == "strong" else 1
        return [
            SendMessage(channel_id, f"🛡️ **{check.actor}** stands in defense of {check.target} — hold {hold}."),
            UpdateSheet(channel_id, check.actor, {"defending": {"charge": check.target, "hold": hold}}),
        ]

    def _shift(self, channel_id: int, check: PendingCheck, dice: Roll) -> Tuple[List[Effect], Optional[Dict[str, Any]]]:
        """Shapeshifter: take the form, with 3, 2 or 1 hold -- even a miss holds 1."""
        if check.move != "shapeshifter":
            return [], None
        hold = _SHAPE_HOLD[band(dice)]
        shape = {"form": check.target, "hold": hold, "moves": []}
        facts = {**shape, "instruction": (
            f"Describe {check.actor} becoming a {check.target}: its innate abilities and "
            f"weaknesses are theirs, though they keep their own stats. "
            + (f"Their land is {land}: an animal that might live there is theirs to take. "
               if (land := ((self._sheet_for(check.actor) or {}).get("choices") or {}).get("land")) else "")
            + "Then name one to three "
            "moves this form can make and record them with form_moves. Each use of one costs "
            "1 hold (spend_hold); at 0 they return to their natural form."
            + (" It is a miss: make a GM move as well." if band(dice) == "miss" else "")
        )}
        return [
            SendMessage(channel_id, f"🐾 **{check.actor}** takes the shape of a {check.target} — hold {hold}."),
            UpdateSheet(channel_id, check.actor, {"shape": shape}),
        ], facts

    def _spell_of(self, check: PendingCheck) -> Optional["spells.Spell"]:
        if not check.spell or self._sheet_for is None:
            return None
        sheet = self._sheet_for(check.actor)
        return spells.find(sheet, check.spell) if sheet is not None else None

    def _cast(self, channel_id: int, check: PendingCheck, dice: Roll) -> Tuple[List[Effect], Optional[Dict[str, Any]]]:
        """What casting did to the caster's sheet, and the facts for the model.

        On a hit the spell goes off: one that lasts goes on their ongoing list, and any
        that last only until they cast again end. On a 7-9 the player's pick may cost a
        -1 ongoing to cast, or the spell itself. On a miss nothing is cast.
        """
        spell = self._spell_of(check)
        if spell is None:
            return [], None
        rules = MOVES[check.move].band(band(dice))
        facts: Dict[str, Any] = {"name": spell.name, "text": spell.text}
        if check.target:
            facts["target"] = check.target
        if not rules.casts:
            facts["cast"] = False
            facts["note"] = "The spell does not take effect."
            return [], facts
        facts["cast"] = True
        sheet = self._sheet_for(check.actor)
        changes: Dict[str, Any] = {}
        lines: List[str] = []
        before = list(sheet.get("ongoing") or [])
        ended = [s.name for s, _ in spells.running(sheet) if s.ends_when_casting]
        ongoing = [e for e in before if e.get("spell") not in ended]
        if ended:
            facts["ended"] = ended
            lines.append(f"{_and(ended)} ends, as {check.actor} casts again.")
        if spell.lasts:
            ongoing.append({"spell": spell.name, "target": check.target})
            facts["ongoing"] = True
            lines.append(f"{spell.name} lasts{' on ' + check.target if check.target else ''}"
                         + (" — -1 to cast while it does." if spell.penalty else
                            " — no casting while it does." if spell.blocks_casting else "."))
        if ongoing != before:
            changes["ongoing"] = ongoing
        picked = [rules.choice.options[p] for p in check.picks] if rules.choice else []
        if any(o.casting_penalty for o in picked):
            changes["cast_penalty"] = int(sheet.get("cast_penalty", 0)) + 1
            facts["cast_penalty"] = changes["cast_penalty"]
            lines.append(f"{check.actor} takes -1 ongoing to cast.")
        if any(o.loses_spell for o in picked):
            changes["lost"] = [*(sheet.get("lost") or []), spell.name]
            facts["lost"] = True
            lines.append(f"{check.actor} loses {spell.name} until they next "
                         f"{spells.PREPARE_NAMES[spells.caster_class(sheet)].lower()}.")
        facts["note"] = ("The spell takes effect as its text says. Any healing or damage in it "
                         "has been rolled and applied; narrate the rest.")
        effects: List[Effect] = []
        if lines:
            effects.append(SendMessage(channel_id, "✨ " + " ".join(lines)))
        if changes:
            effects.append(UpdateSheet(channel_id, check.actor, changes))
        return effects, facts

    def _companion_of(self, check: PendingCheck) -> Optional[Dict[str, Any]]:
        if not check.companion or self._sheet_for is None:
            return None
        return (self._sheet_for(check.actor) or {}).get("companion")

    def _ferocity(self, check: PendingCheck) -> int:
        """Command: "...and you attack the same target, add its ferocity to your damage.\""""
        pet = self._companion_of(check)
        rule = companions.COMMANDS.get(check.move)
        return int(pet["ferocity"]) if pet and rule and rule.damage else 0

    def _companion_armor(self, check: PendingCheck) -> int:
        """Command: "...and you take damage, add its armor to your armor.\""""
        pet = self._companion_of(check)
        rule = companions.COMMANDS.get(check.move)
        return int(pet["armor"]) if pet and rule and rule.armor else 0

    def _magic_weapon(self, check: PendingCheck) -> bool:
        """Magic Weapon: "The weapon you hold while casting does +1d4 damage." The caster's
        weapon attacks only -- not their spells."""
        if check.spell or self._sheet_for is None:
            return False
        sheet = self._sheet_for(check.actor)
        return sheet is not None and any(s.name == spells.MAGIC_WEAPON for s, _ in spells.running(sheet))

    def _consequences(self, channel_id: int, outcome: MoveOutcome) -> List[Effect]:
        """What a move does to the sheet, beyond being narrated."""
        name = outcome.actor
        if outcome.healing:
            return self._heal(channel_id, outcome)
        if outcome.move == "carouse":
            sheet = self._sheet_for(name) if self._sheet_for is not None else None
            if sheet is not None:
                spent = 100 * (outcome.modifier + 1)
                left = max(0, int(sheet.get("coins", 0)) - spent)
                return [SendMessage(channel_id, f"🍻 **{name}** spends {spent} coins on the party "
                                                f"({left} left)."),
                        UpdateSheet(channel_id, name, {"coins": left})]
        if outcome.move == "journey_quartermaster" and outcome.band == "strong":
            sheet = self._sheet_for(name) if self._sheet_for is not None else None
            if sheet is not None and "gear" in sheet:
                have = int(sheet["rations"]) if "rations" in sheet else rations_of(sheet["gear"])
                return [SendMessage(channel_id, f"**{name}** stretches the supplies — one ration saved."),
                        UpdateSheet(channel_id, name, {"rations": have + 1})]
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

    def _heal(self, channel_id: int, outcome: MoveOutcome) -> List[Effect]:
        """Apply a healing move's roll: to the target, and -- Lay on Hands on a 7-9 -- the
        same damage onto the healer, who may fall for it."""
        facts = outcome.healing
        effects: List[Effect] = [
            SendMessage(channel_id, f"✨ **{outcome.actor}** heals **{facts['target']}** — "
                                    f"{facts['rolled_render']} → {facts['healed']} healed, HP "
                                    f"{facts['hp_after']}/{facts['max_hp']}."),
            UpdateSheet(channel_id, facts["target"], {"hp": facts["hp_after"]}),
        ]
        if facts.get("transferred"):
            sheet = self._sheet_for(outcome.actor) if self._sheet_for is not None else None
            if sheet is not None:
                after = max(0, int(sheet["hp"]) - facts["transferred"])
                effects += [
                    SendMessage(channel_id, f"…and the wound passes to **{outcome.actor}**: "
                                            f"{facts['transferred']} damage, HP {after}/{sheet['max_hp']}."),
                    UpdateSheet(channel_id, outcome.actor, {"hp": after}),
                ]
                if after <= 0:
                    effects.extend(self._trigger(channel_id, outcome.actor, "last_breath"))
        return effects

    def _healing(self, check: PendingCheck, dice: Roll) -> Optional[Dict[str, Any]]:
        """Roll a healing move's dice now, if its result or the player's pick heals."""
        rules = MOVES[check.move].band(band(dice))
        picked = [rules.choice.options[p] for p in check.picks] if rules.choice else []
        spec = rules.heals or next((o.heals for o in picked if o.heals), None)
        spell = self._spell_of(check) if rules.casts else None
        if spell is not None:
            spec = spell.heals
        if not spec or self._sheet_for is None:
            return None
        patient = self._sheet_for(check.target or check.actor)
        if patient is None or patient.get("dead"):
            return None
        if spec == "max_hp":
            # Heal: "...heal their damage a number of points up to your maximum HP."
            amount = int(self._sheet_for(check.actor)["max_hp"])
            roll, render = None, f"up to {amount}"
        else:
            roll = self.dice.roll(spec)
            amount, render = roll.total, f"{spec}: {roll.render()}"
        healed = max(0, min(amount, int(patient["max_hp"]) - int(patient["hp"])))
        facts: Dict[str, Any] = {
            "target": patient["name"], "rolled": amount, "rolled_render": render,
            "healed": healed, "hp_after": int(patient["hp"]) + healed, "max_hp": patient["max_hp"],
        }
        if rules.heal_transfers:
            facts["transferred"] = healed
        return facts

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
                mod = stat_modifier(sheet, rules.stat)
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
        if check.move in _DEFENCE:
            self.registry.consume(check.check_id)
            return [said, *self._spend_defence(channel_id, chosen)]
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
        dice = self._dice_of(check)
        rules = MOVES[check.move].band(band(dice))
        return self._deals_damage(check, dice) and (rules.choice is None or bool(check.picks))

    def _deals_damage(self, check: PendingCheck, dice: Roll) -> bool:
        """The result deals damage, or an option the player picked does (Backstab)."""
        rules = MOVES[check.move].band(band(dice))
        if rules.casts:
            spell = self._spell_of(check)
            return spell is not None and spell.damage is not None
        picked = [rules.choice.options[p] for p in check.picks] if rules.choice else []
        return rules.deals_damage or any(o.deals_damage for o in picked)

    def _land_damage(
        self, channel_id: int, check: PendingCheck, auto: bool = False
    ) -> List[Effect]:
        self.registry.consume(check.check_id)
        dice = self._dice_of(check)
        outcome = resolve(check, dice, picks=check.picks)
        effects: List[Effect] = []

        die, piercing, _ = self._damage_die(check)
        spell = self._spell_of(check)
        if spell is not None:
            rolled = self.dice.roll(spell.damage)
            total, shown, ignores = rolled.total, f"{spell.damage}: {rolled.render()}", spell.ignores_armor
        else:
            hit = damage.roll(self.dice, damage.parse(die), self._damage_extra(check, dice))
            total, shown, ignores = hit.total, hit.render(), False
            if self._magic_weapon(check):
                bonus = self.dice.roll("1d4").total
                total, shown = total + bonus, f"{shown}, +1d4 Magic Weapon: {bonus} = {total + bonus}"
            ferocity = self._ferocity(check)
            if ferocity:
                total, shown = total + ferocity, f"{shown}, +{ferocity} {check.companion} = {total + ferocity}"
        line = f"🎲 **{check.actor}**{' (rolled for them)' if auto else ''} damage — {shown}"
        hits = []
        for name in check.target.split(", ") if spell is not None and spell.area else [check.target]:
            hurt, facts, said = self._hurt(channel_id, name, total, piercing, ignores)
            effects.extend(hurt)
            hits.append(facts)
            line += said
        facts = hits[0] if len(hits) == 1 else {"rolled": total, "targets": hits}
        effects.insert(0, SendMessage(channel_id, line))

        effects.extend(self._spend_ammo(channel_id, check, dice))
        cast, spell_facts = self._cast(channel_id, check, dice)
        effects.extend(cast)
        dying: List[Effect] = []
        if spell is not None and spell.self_damage:
            # Harm: "...and 1d6 damage to yourself. This damage ignores armor."
            sheet = self._sheet_for(check.actor)
            cost = self.dice.roll(spell.self_damage)
            after = max(0, int(sheet["hp"]) - cost.total)
            spell_facts["self_damage"] = {"rolled": cost.total, "hp_after": after}
            effects += [
                SendMessage(channel_id, f"…and the wrath burns **{check.actor}** too — "
                                        f"{spell.self_damage}: {cost.render()}, HP {after}/{sheet['max_hp']}."),
                ChangeHp(channel_id, "character", check.actor, after),
            ]
            if after <= 0:
                dying = self._trigger(channel_id, check.actor, "last_breath")

        back = None
        target = self._monsters().get(check.target)  # as it stood before this blow
        if outcome.enemy_attacks and target is not None:
            # The trade is simultaneous: on a 7-9 the enemy's blow lands even if yours
            # finished it. The rules say both happen, so both do.
            attack_effects, back = self._attack(channel_id, check.target, target, check.actor,
                                                extra_armor=self._companion_armor(check))
            effects.extend(attack_effects)

        outcome = replace(outcome, damage=facts, enemy_attack=back, spell=spell_facts)
        if back and back.get("down"):
            dying = self._trigger(channel_id, check.actor, "last_breath")
        return [*effects, *self._resolved(channel_id, outcome), *dying]

    def _hurt(
        self, channel_id: int, name: str, total: int, piercing: int, ignores_armor: bool
    ) -> Tuple[List[Effect], Dict[str, Any], str]:
        """Damage landing on one creature: (effects, facts for the model, the line's tail)."""
        target = self._monsters().get(name)
        facts: Dict[str, Any] = {"target": name, "rolled": total}
        if target is None:
            facts["note"] = "The target was no longer in the scene; the damage lands on nothing."
            return [], facts, f" — {name} is gone."
        dealt = damage.through_armor(total, target["armor"], piercing, ignores_armor)
        after = int(target["hp"]) - dealt
        facts.update(armor=target["armor"], dealt=dealt, hp_before=target["hp"],
                     hp_after=max(after, 0), defeated=after <= 0,
                     severity=damage.severity(dealt, target.get("max_hp", target["hp"]), after))
        armor = "ignores armor" if ignores_armor else f"armor {target['armor']}"
        said = (f" → **{name}** takes {_damage_words(dealt)} ({armor}), "
                f"HP {target['hp']} → {max(after, 0)}" + (" — **defeated**." if after <= 0 else "."))
        return [ChangeHp(channel_id, "monster", name, after)], facts, said

    def _weapon(self, sheet: Dict[str, Any], move: str):
        return weapon_for(sheet.get("gear") or [], ranged=MOVES[move].ranged,
                          ammo=int(sheet.get("ammo", 0)))

    def _damage_die(self, check: PendingCheck) -> Tuple[str, int, str]:
        """(damage expression, piercing, weapon name) for this attack.

        The class's die, plus the weapon's +damage, with its piercing. A sheet with no
        gear -- made before gear existed -- rolls the bare class die, as it always did.
        """
        spell = self._spell_of(check)
        if spell is not None and spell.damage:
            return spell.damage, 0, spell.name + (", ignores armor" if spell.ignores_armor else "")
        sheet = self._sheet_for(check.actor) if self._sheet_for is not None else None
        if not sheet:
            return "d6", 0, ""
        die = sheet["damage"]
        # Halfling Thief: "When you attack with a ranged weapon, deal +2 damage."
        knack = 2 if (MOVES[check.move].ranged and sheet.get("class") == "Thief"
                      and sheet.get("race") == "Halfling") else 0
        weapon = self._weapon(sheet, check.move) if "gear" in sheet else None
        spec = damage.parse(die)
        bonus = knack + (weapon.damage if weapon is not None else 0)
        boosted = str(damage.DamageSpec(spec.sides, spec.mode, spec.modifier + bonus)) if bonus else die
        if weapon is None:
            return boosted, 0, ""
        return boosted, weapon.piercing, weapon.name

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
        character = sheet.get("name", character)
        defender = self._defender_of(character)
        if defender is not None:
            return self._hold_attack(channel_id, name, scene[name], character, defender)
        effects, facts = self._attack(channel_id, name, scene[name], character)
        if facts.get("down"):
            effects.extend(self._trigger(channel_id, character, "last_breath"))
        return effects, {"ok": True, **facts,
                         "note": "Rolled and applied by the rules engine. Narrate it as it "
                                 "landed, as bad as its severity and no worse or better; do "
                                 "not change the numbers."}

    def _defender_of(self, character: str) -> Optional[str]:
        """Who stands in defense of this character -- themselves or another -- with hold
        to spend and able to act. None if nobody."""
        folded = normalize_actor(character)
        for actor, sheet in self._party():
            guard = sheet.get("defending") or {}
            if int(guard.get("hold", 0)) <= 0 or _unable(actor, sheet):
                continue
            if normalize_actor(actor) == folded or normalize_actor(str(guard.get("charge", ""))) == folded:
                return actor
        return None

    def _hold_attack(
        self, channel_id: int, name: str, creature: Dict[str, Any], character: str, defender: str
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The attack is rolled, then held while the defender decides how to spend hold."""
        hit = damage.roll(self.dice, damage.parse(creature["damage"]))
        move = "defend_self" if normalize_actor(defender) == normalize_actor(character) else "defend_charge"
        user_id = next((uid for uid, a in self.players.items()
                        if normalize_actor(a) == normalize_actor(defender)), 0)
        check, _ = self.registry.open(
            channel_id=channel_id, user_id=user_id, actor=defender, move=move, stat=NOTHING,
            modifier=0, stakes=character, dice=creature["damage"], target=name,
        )
        held = self.registry.hold(check, faces=hit.faces)
        hold = int((self._sheet_for(defender) or {}).get("defending", {}).get("hold", 0))
        whom = "you" if move == "defend_self" else f"**{character}**"
        options = MOVES[move].strong.choice.options
        return [SendMessage(
            channel_id,
            f"🗡️ **{name}** attacks {whom} — {hit.render()} damage, before armor.\n"
            f"**{defender}**, you stand in defense ({hold} hold): spend one?",
            prompt=ChoicePrompt(check_id=held.check_id, move=move, band="strong", count=1,
                                options=tuple(o.text for o in options)),
        )], {"ok": True, "held": True, "note": (
            f"{defender} stands in defense and is choosing how to spend hold. The attack has "
            "not landed yet: say only that it comes. What it does arrives as a rules event."
        )}

    def _spend_defence(self, channel_id: int, check: PendingCheck) -> List[Effect]:
        """Land a held attack as the defender chose, spend the hold, tell the model."""
        defender, character, name = check.actor, check.stakes, check.target
        choice = MOVES[check.move].strong.choice.options[check.picks[0]].text
        creature = self._monsters().get(name)
        hit = damage.landed(damage.parse(check.dice), check.faces)
        sheet = self._sheet_for(defender) or {}
        guard = dict(sheet.get("defending") or {})
        spent = not choice.startswith("Don't")
        effects: List[Effect] = []
        victim, halve = character, False
        if choice.startswith("Redirect"):
            victim = defender
        elif choice.startswith("Halve"):
            halve = True
        elif choice.startswith("Deal damage") and creature is not None:
            level = int(sheet.get("level", 1))
            dealt = damage.through_armor(level, creature["armor"])
            after = int(creature["hp"]) - dealt
            effects += [
                SendMessage(channel_id, f"🛡️ **{defender}** strikes back — {name} takes **{dealt} damage** "
                                        f"(level {level}, armor {creature['armor']}), HP {creature['hp']} → "
                                        f"{max(after, 0)}" + (" — **defeated**." if after <= 0 else ".")),
                ChangeHp(channel_id, "monster", name, after),
            ]
        if spent:
            guard["hold"] = int(guard.get("hold", 0)) - 1
            effects.append(UpdateSheet(channel_id, defender,
                                       {"defending": guard if guard["hold"] > 0 else None}))
        attack: Dict[str, Any] = {}
        dying: List[Effect] = []
        if creature is not None:
            landed, attack = self._attack(channel_id, name, creature, victim, hit=hit, halve=halve)
            effects = landed + effects
            if attack.get("down"):
                dying = self._trigger(channel_id, victim, "last_breath")
        left = guard.get("hold", 0) if spent else int(guard.get("hold", 0))
        return [*effects, PromptModel(channel_id, "rules_event", {
            "event": "An attack meets a defender",
            "attacker": name, "attacked": character, "defender": defender,
            "defender_chose": choice, "hold_left": max(left, 0),
            "attack": attack or {"note": f"{name} is gone; the attack lands on nothing."},
            "instruction": ("Narrate the attack as it landed and the defender's part in it. The "
                            "damage and HP are exact. If they opened the attacker up, an ally "
                            "takes +1 forward against it."),
        }), *dying]

    def _attack(
        self, channel_id: int, name: str, creature: Dict[str, Any], character: str,
        extra_armor: int = 0, hit: Optional["damage.DamageRoll"] = None, halve: bool = False,
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        sheet = self._sheet_for(character) if self._sheet_for is not None else None
        if hit is None:
            hit = damage.roll(self.dice, damage.parse(creature["damage"]))
        armor = (int(sheet["armor"]) if sheet else 0) + extra_armor
        dealt = damage.through_armor(
            hit.total, armor, creature.get("piercing", 0), creature.get("ignores_armor", False)
        )
        if halve:
            dealt //= 2  # Defend: "halve its effect or damage"
        facts: Dict[str, Any] = {"attacker": name, "character": character,
                                 "rolled": hit.total, "armor": armor, "dealt": dealt}
        line = f"🗡️ **{name}** hits **{character}** — {hit.render()}"
        effects: List[Effect] = []
        if sheet is not None:
            after = int(sheet["hp"]) - dealt
            facts.update(hp_before=sheet["hp"], hp_after=max(after, 0), down=after <= 0,
                         severity=damage.severity(dealt, sheet.get("max_hp", sheet["hp"]), after))
            # The amount first, in words: "d6: 5 → 4 through armor 1" left the table to work
            # out that 4 was the damage.
            shield = "ignores armor" if creature.get("ignores_armor") else f"armor {armor}"
            line = (
                f"🗡️ **{name}** hits **{character}** for {_damage_words(dealt)} "
                f"({hit.render()}, {shield}) — HP {sheet['hp']} → {max(after, 0)}"
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
            *self._mark_xp(channel_id, outcome),
            RecordOutcome(channel_id=channel_id, outcome=outcome),
            PromptModel(channel_id=channel_id, kind=kind, payload=outcome_for_model(outcome)),
        ]

    def _mark_xp(self, channel_id: int, outcome: MoveOutcome) -> List[Effect]:
        """"Whenever you roll a 6-, mark XP." Every roll, every move -- the misses are how a
        character learns."""
        if outcome.band != "miss" or self._sheet_for is None:
            return []
        sheet = self._sheet_for(outcome.actor)
        if sheet is None or sheet.get("dead"):
            return []
        xp = int(sheet.get("xp", 0)) + 1
        return [
            SendMessage(channel_id, f"**{outcome.actor}** marks XP — {xp_line({**sheet, 'xp': xp})}."
                                    + (LEVEL_UP_HINT if xp == xp_needed(sheet) else "")),
            UpdateSheet(channel_id, outcome.actor, {"xp": xp}),
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
        if check.move in _DEFENCE:
            # Silence spends nothing: the attack lands as it was rolled.
            options = MOVES[check.move].strong.choice.options
            check = replace(check, picks=(len(options) - 1,))
            return [SendMessage(channel_id, f"**{check.actor}** didn't spend hold in time."),
                    *self._spend_defence(channel_id, check)]
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
        if self._deals_damage(check, dice):
            # Consumed by the sweep already; _land_damage consumes again harmlessly.
            landed = self._land_damage(channel_id, check, auto=True)
            if auto_chosen:
                landed = [_auto(e) for e in landed]
            return effects + landed
        return effects + self._settle(channel_id, check, dice, auto_chosen=auto_chosen)

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
        unsettled = self.unsettled(channel_id, "settle in to rest")
        if unsettled:
            return [], {"ok": False, "error": unsettled}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": (
                "Say where the party rests and why it is safe enough to sleep (reason). If "
                "it is not safe, tell them so, or let them try and set something_approaches."
            )}
        party = self._party()
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
        # Halfling Druid: "When you make camp, you and your allies heal +1d6."
        songs = None
        if not approaches and any(s.get("class") == "Druid" and s.get("race") == "Halfling"
                                  for _, s in party):
            songs = self.dice.roll("1d6")
            lines.append(f"A halfling's healing song carries over the camp: +{songs.total} to all healing.")
        for actor, sheet in party:
            changes: Dict[str, Any] = {}
            if sheet.get("defending"):
                changes["defending"] = None  # nobody stands in defense through the night
            fed = True
            sustained = sheet.get("class") == "Druid"  # By Nature Sustained: no rations
            if "gear" in sheet and not sustained:  # sheets made before gear do not count rations
                have = int(sheet["rations"]) if "rations" in sheet else rations_of(sheet["gear"])
                fed = have > 0
                if fed:
                    changes["rations"] = have - 1
            entry: Dict[str, Any] = {"ate": fed}
            if "rations" in changes:
                entry["rations_left"] = changes["rations"]
            if fed and not approaches:
                healed = min(int(sheet["max_hp"]) - int(sheet["hp"]),
                             int(sheet["max_hp"]) // 2 + (songs.total if songs else 0))
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

    def unsettled(self, channel_id: int, doing: str) -> str:
        """Why the party cannot take an hour or a night out, or "" if it can: creatures
        still standing, or anyone mid-move -- above all a Last Breath not yet rolled."""
        standing = sorted(n for n, r in self._monsters().items() if r["hp"] > 0)
        if standing:
            return (f"You can't {doing} with {_and(standing)} still standing. Finish the "
                    "fight, or remove them with remove_monsters if they have truly gone.")
        pending = list(self.registry.open_checks(channel_id))
        if pending:
            what = _and(f"{c.actor}'s {MOVES[c.move].name}" for c in pending)
            return f"Not while {what} is still to be resolved."
        return ""

    # -- Commune, Prepare Spells, and ending a spell --------------------------------------

    def prepare_spells(
        self, *, channel_id: int, character: str, reason: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """A Cleric communes or a Wizard prepares spells: "uninterrupted time (an hour or
        so)". The spells they had are gone now; which they take instead is the player's
        choice, made privately with the Choose spells button this posts.

        Guarded like Make Camp: an hour of prayer is no more possible mid-fight than a
        night's sleep, and the model must say why this hour is quiet.
        """
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        cls = spells.caster_class(sheet)
        if cls is None:
            return [], {"ok": False, "error": f"{actor} is a {sheet.get('class')}, and casts no spells."}
        unable = _unable(actor, sheet)
        if unable:
            return [], {"ok": False, "error": unable}
        doing = "commune" if cls == "cleric" else "prepare spells"
        unsettled = self.unsettled(channel_id, doing)
        if unsettled:
            return [], {"ok": False, "error": unsettled}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": (
                f"Say where {actor} spends the hour and why it is uninterrupted (reason)."
            )}
        user_id = next((uid for uid, a in self.players.items()
                        if normalize_actor(a) == normalize_actor(actor)), 0)
        verb = "communes with their deity" if cls == "cleric" else "studies their spellbook"
        return [
            UpdateSheet(channel_id, actor, spells.forgetting()),
            SendMessage(
                channel_id,
                f"🕯️ **{actor}** {verb} — {reason}\nThe spells they held are gone. "
                f"**{actor}**, choose what you take instead:",
                prompt=SpellsPrompt(user_id, actor),
            ),
        ], {"ok": True, "note": (
            f"An hour passes. {actor}'s player is choosing their spells privately; the "
            "state will show them. Narrate the hour, briefly, and move on."
        )}

    def end_spell(
        self, *, channel_id: int, character: str, spell: str
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """An ongoing spell ends: dismissed, broken (Charm Person when its target is hurt,
        Invisibility when they attack), or its battle over (Bless)."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        before = list(sheet.get("ongoing") or [])
        folded = str(spell or "").strip().lower()
        kept = [e for e in before if e.get("spell", "").lower() != folded]
        if len(kept) == len(before):
            running = ", ".join(e["spell"] for e in before) or "none"
            return [], {"ok": False, "error": f"{actor} has no {spell!r} running. Ongoing: {running}."}
        name = next(e["spell"] for e in before if e.get("spell", "").lower() == folded)
        return [
            SendMessage(channel_id, f"✨ **{actor}**'s {name} ends."),
            UpdateSheet(channel_id, actor, {"ongoing": kept}),
        ], {"ok": True, "ended": name}

    # -- Undertake a Perilous Journey -----------------------------------------------------------

    def perilous_journey(
        self, *, channel_id: int, destination: str = "", days: int = 1, trailblazer: str = "",
        scout: str = "", quartermaster: str = "",
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """"When you travel through hostile territory, choose one member of the party to act as
        trailblazer, one to scout ahead, and one to be quartermaster (the same character
        cannot have two jobs). If you don't have enough party members or choose not to
        assign a job, treat that job as if it had rolled a 6."

        The rations go now -- one a day for everyone travelling -- and each job's holder rolls
        +Wis with their own button. A quartermaster's 10+ hands one back. How long the journey
        really takes is the GM's to say once the trailblazer's roll is in, so the model moves
        the clock.
        """
        unsettled = self.unsettled(channel_id, "set out on a journey")
        if unsettled:
            return [], {"ok": False, "error": unsettled}
        destination = " ".join(str(destination or "").split())
        if not destination:
            return [], {"ok": False, "error": "Say where they are travelling (destination)."}
        days = int(days or 0)
        if not 1 <= days <= MAX_RECOVER_DAYS:
            return [], {"ok": False, "error": f"A journey of 1 to {MAX_RECOVER_DAYS} days."}
        party = self._party()
        if not party:
            return [], {"ok": False, "error": "Nobody at the table has a character sheet."}
        jobs: Dict[str, str] = {}
        for job, who in (("trailblazer", trailblazer), ("scout", scout), ("quartermaster", quartermaster)):
            if not str(who or "").strip():
                continue
            actor = next((a for a, _ in party if normalize_actor(a) == normalize_actor(who)), None)
            if actor is None:
                return [], {"ok": False, "error": f"{who!r} is not travelling with the party. "
                                                  f"Party: {', '.join(a for a, _ in party)}."}
            if actor in jobs.values():
                return [], {"ok": False, "error": f"{actor} already has a job: the same character "
                                                  "cannot have two."}
            jobs[job] = actor

        effects: List[Effect] = []
        lines = [f"🧭 The party sets out for {destination} — about {days} day{'s' if days > 1 else ''} "
                 "through hostile territory."]
        spent: Dict[str, Any] = {}
        for actor, sheet in party:
            if "gear" not in sheet or sheet.get("class") == "Druid":  # By Nature Sustained
                continue
            have = int(sheet["rations"]) if "rations" in sheet else rations_of(sheet["gear"])
            used = min(have, days)
            spent[actor] = {"rations_used": used, "rations_left": have - used,
                            "short": days - used}
            effects.append(UpdateSheet(channel_id, actor, {"rations": have - used}))
            lines.append(f"**{actor}** packs {used} ration{'s' if used != 1 else ''}"
                         + (f" — {days - used} short." if used < days else f" ({have - used} left)."))
        unfilled = [j for j in ("trailblazer", "scout", "quartermaster") if j not in jobs]
        if unfilled:
            lines.append(f"Nobody is {_and(unfilled)}: that counts as a 6.")
        effects.insert(0, SendMessage(channel_id, "\n".join(lines)))
        for job, actor in jobs.items():
            effects.extend(self._trigger(channel_id, actor, f"journey_{job}"))
        return effects, {"ok": True, "destination": destination, "days": days, "jobs": jobs,
                         "rations": spent, "unfilled_jobs_count_as_6": unfilled, "note": (
            "The job rolls are open. Narrate setting out, not arriving: each result comes back as "
            "a rules event. Jobs nobody holds count as a miss -- a GM move each. When the "
            "trailblazer's result is in (or now, with no trailblazer), move the clock with "
            f"advance_time for the journey's real length: about {days} day(s), less on a 10+, "
            "a day at a time. Anyone short of rations goes hungry on the road: a GM move.")}

    # -- Recover ---------------------------------------------------------------------------------

    def recover(
        self, *, channel_id: int, characters: Sequence[str] = (), days: int = 1,
        healer: bool = False, reason: str = "",
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """"When you do nothing but rest in comfort and safety after a day of rest you recover
        all your HP. After three days of rest you remove one debility of your choice. If
        you're under the care of a healer (magical or otherwise) you heal a debility for
        every two days of rest instead."

        Guarded like camp. Where a character has more debilities than the rest clears, which
        go is theirs to pick, with buttons only they can press.
        """
        unsettled = self.unsettled(channel_id, "rest for days")
        if unsettled:
            return [], {"ok": False, "error": unsettled}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": "Say where they rest in comfort and safety (reason)."}
        days = int(days or 0)
        if not 1 <= days <= MAX_RECOVER_DAYS:
            return [], {"ok": False, "error": f"Rest for 1 to {MAX_RECOVER_DAYS} days."}
        party = self._party()
        if characters:
            wanted = {normalize_actor(c) for c in characters}
            party = [(a, s) for a, s in party if normalize_actor(a) in wanted]
        if not party:
            return [], {"ok": False, "error": "Nobody named has a character sheet."}
        per = 2 if healer else 3
        lines = [f"🛏️ {days} day{'s' if days > 1 else ''} of rest — {reason}"
                 + (" (under a healer's care)" if healer else "")]
        effects: List[Effect] = []
        report: Dict[str, Any] = {}
        for actor, sheet in party:
            changes: Dict[str, Any] = {"hp": int(sheet["max_hp"])}
            have = list(sheet.get("debilities") or [])
            cures = days // per + int(sheet.get("cures_owed", 0))
            entry: Dict[str, Any] = {"hp": int(sheet["max_hp"])}
            said = f"**{actor}** is whole again — HP {sheet['max_hp']}/{sheet['max_hp']}"
            if have and cures >= len(have):
                changes["debilities"], changes["cures_owed"] = [], 0
                entry["cured"] = have
                said += f", and no longer {_and([d.title() for d in have])}."
            elif have and cures:
                changes["cures_owed"] = cures
                entry["choosing"] = cures
                said += f"; they choose {cures} debilit{'ies' if cures > 1 else 'y'} to shed."
            else:
                said += "." if not have else f"; {_and([d.title() for d in have])} need{'s' if len(have) == 1 else ''} longer."
            lines.append(said)
            effects.append(UpdateSheet(channel_id, actor, changes))
            if entry.get("choosing"):
                user_id = next((uid for uid, a in self.players.items()
                                if normalize_actor(a) == normalize_actor(actor)), 0)
                effects.append(SendMessage(channel_id, f"**{actor}**, which goes?",
                                           prompt=CurePrompt(user_id, tuple(have))))
            report[actor] = entry
        effects.insert(0, SendMessage(channel_id, "\n".join(lines)))
        return effects, {"ok": True, "party": report, "days": days, "note": (
            f"{days} day(s) pass; the clock has moved for everyone. Narrate the rest briefly.")}

    def cure(self, *, channel_id: int, user_id: int, debility: str) -> List[Effect]:
        """The player picks a debility Recover clears. Only theirs, only while one is owed."""
        actor = self.players.get(user_id)
        sheet = self._sheet_for(actor) if actor and self._sheet_for is not None else None
        if sheet is None or int(sheet.get("cures_owed", 0)) <= 0 or debility not in (sheet.get("debilities") or ()):
            return []
        have = [d for d in sheet["debilities"] if d != debility]
        owed = int(sheet["cures_owed"]) - 1
        return [
            SendMessage(channel_id, f"🩹 **{actor}** is no longer {debility.title()}."),
            UpdateSheet(channel_id, actor, {"debilities": have, "cures_owed": owed if have else 0}),
        ]

    # -- debilities -----------------------------------------------------------------------------

    def debility(
        self, *, channel_id: int, character: str, debility: str, cause: str = "", cured: bool = False
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Give a debility -- the fiction inflicted it -- or cure one. Whether is the model's
        call, like any GM move; the -1 is Gary's, on every roll with that stat."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        name = str(debility or "").strip().lower()
        if name not in DEBILITIES:
            return [], {"ok": False, "error": f"No debility {debility!r}. They are: {', '.join(DEBILITIES)}."}
        cause = " ".join(str(cause or "").split())
        if not cause:
            return [], {"ok": False, "error": "Say what caused it, or what cures it (cause)."}
        have = list(sheet.get("debilities") or [])
        stat = STAT_NAMES[DEBILITIES[name]]
        if cured:
            if name not in have:
                return [], {"ok": False, "error": f"{actor} is not {name}."}
            have.remove(name)
            line = f"🩹 **{actor}** is no longer {name.title()} — {cause}."
        else:
            if name in have:
                return [], {"ok": False, "error": f"{actor} is already {name}; a debility does not stack."}
            have.append(name)
            line = f"🩸 **{actor}** is {name.title()} (-1 {stat}) — {cause}."
        return [SendMessage(channel_id, line), UpdateSheet(channel_id, actor, {"debilities": have})], {
            "ok": True, "debilities": have,
            "note": f"{stat} rolls take -1 while it lasts." if not cured else f"{stat} is whole again."}

    # -- the Thief's poison -------------------------------------------------------------------

    def use_poison(self, *, channel_id: int, character: str, target: str = "") -> Tuple[List[Effect], Dict[str, Any]]:
        """Poisoner: a use of the poison they mastered, which is "no longer dangerous for you
        to use". What it does is the poison's text; the model narrates it."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        name = (sheet.get("choices") or {}).get("poison")
        if not name:
            return [], {"ok": False, "error": f"{actor} has mastered no poison."}
        left = int(sheet.get("poison_uses", 0))
        if left <= 0:
            return [], {"ok": False, "error": (f"{actor} has no {name} left. They can brew more with "
                                               "time and a safe place (brew_poison).")}
        on = f" on {target}" if target else ""
        return [
            SendMessage(channel_id, f"🧪 **{actor}** uses {name}{on} — {left - 1} uses left."),
            UpdateSheet(channel_id, actor, {"poison_uses": left - 1}),
        ], {"ok": True, "poison": name, "effect": choices.poison_text(name), "uses_left": left - 1,
            "note": "Applied poisons must reach the target or what they eat or drink; touch poisons "
                    "need only touch, a blade will do. Narrate the effect as written."}

    def brew_poison(self, *, channel_id: int, character: str, reason: str = "") -> Tuple[List[Effect], Dict[str, Any]]:
        """"Whenever you have time to gather materials and a safe place to brew you can make
        three uses of the poison you choose for free." Guarded like camp."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        name = (sheet.get("choices") or {}).get("poison")
        if not name:
            return [], {"ok": False, "error": f"{actor} has mastered no poison."}
        unsettled = self.unsettled(channel_id, "brew poison")
        if unsettled:
            return [], {"ok": False, "error": unsettled}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": "Say where they brew and why it is safe (reason)."}
        uses = int(sheet.get("poison_uses", 0)) + choices.POISON_BATCH
        return [
            SendMessage(channel_id, f"🧪 **{actor}** brews {name} — {reason}. {uses} uses."),
            UpdateSheet(channel_id, actor, {"poison_uses": uses}),
        ], {"ok": True, "poison": name, "uses": uses}

    # -- Shapeshifter's form ------------------------------------------------------------------

    def _shaped(self, character: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
        """(the character, their sheet, why not) for someone in another shape."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return None, None, f"{character!r} has no character sheet."
        if not sheet.get("shape"):
            return None, None, f"{actor} is in their natural form."
        return actor, sheet, ""

    def form_moves(self, *, channel_id: int, character: str, moves: Sequence[str]) -> Tuple[List[Effect], Dict[str, Any]]:
        """"The GM will also tell you one or more moves associated with your new form.\""""
        actor, sheet, why = self._shaped(character)
        if actor is None:
            return [], {"ok": False, "error": why}
        moves = [" ".join(str(m).split())[:60] for m in moves if str(m).strip()][:4]
        if not moves:
            return [], {"ok": False, "error": "Name at least one move the form can make."}
        shape = {**sheet["shape"], "moves": moves}
        return [
            SendMessage(channel_id, f"🐾 As a {shape['form']}, **{actor}** can: {'; '.join(moves)} "
                                    f"(1 hold each, {shape['hold']} left)."),
            UpdateSheet(channel_id, actor, {"shape": shape}),
        ], {"ok": True, **shape}

    def spend_hold(
        self, *, channel_id: int, character: str, move: str = "", revert: bool = False
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Spend 1 hold to make one of the form's moves -- or all of it to turn back. Out of
        hold, they return to their natural form."""
        actor, sheet, why = self._shaped(character)
        if actor is None:
            return [], {"ok": False, "error": why}
        shape = sheet["shape"]
        if revert:
            return [
                SendMessage(channel_id, f"🐾 **{actor}** lets the {shape['form']} go and returns to their natural form."),
                UpdateSheet(channel_id, actor, {"shape": None}),
            ], {"ok": True, "hold": 0, "reverted": True}
        made = next((m for m in shape.get("moves") or () if m.lower() == str(move or "").strip().lower()), None)
        if made is None:
            listing = "; ".join(shape.get("moves") or ()) or "none recorded -- call form_moves first"
            return [], {"ok": False, "error": f"That is not one of the {shape['form']}'s moves: {listing}."}
        hold = int(shape["hold"]) - 1
        if hold <= 0:
            return [
                SendMessage(channel_id, f"🐾 **{actor}** — {made}, the last of their hold: they return to "
                                        "their natural form."),
                UpdateSheet(channel_id, actor, {"shape": None}),
            ], {"ok": True, "move": made, "hold": 0, "reverted": True,
                "note": "Narrate the move, then the change back."}
        return [
            SendMessage(channel_id, f"🐾 **{actor}** — {made} ({hold} hold left)."),
            UpdateSheet(channel_id, actor, {"shape": {**shape, "hold": hold}}),
        ], {"ok": True, "move": made, "hold": hold}

    def cancel(self, channel_id: int, check_id: str) -> List[Effect]:
        cancellation = self.registry.cancel(check_id, CancelReason.MANUAL)
        if cancellation is None:
            return []
        return [SendMessage(
            channel_id,
            f"Cancelled {cancellation.check.actor}'s {MOVES[cancellation.check.move].name}.",
        )]


_RESULT = {"strong": "strong hit", "weak": "weak hit", "miss": "miss"}

#: The moves that spend Defend's hold on an attack.
_DEFENCE = ("defend_charge", "defend_self")

#: The longest single Recover: weeks are possible, but a model typing 300 is a mistake.
MAX_RECOVER_DAYS = 30

#: Shapeshifter's hold by result.
_SHAPE_HOLD = {"strong": 3, "weak": 2, "miss": 1}


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


def _damage_words(dealt: int) -> str:
    return "**no damage**" if dealt <= 0 else f"**{dealt} damage**"


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
