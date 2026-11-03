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

import re
import json
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import (
    advanced, bonds, choices, companion as companions, damage, hirelings, magic, multiclass, quests, reactions,
    slimes, spells, steadings, trade,
)
from .gear import MELEE, RANGED, Item, armor_of as gear_armor, items_of, rations_of, weapon_for
from .models import CancelReason, MoveOutcome, PendingCheck, Roll
from .moves import ANY, BOND, CASTER, LOYALTY, MOVES, NOTHING, OFFERS
from .ports import Dice, RandomDice
from .registry import PendingCheckRegistry, normalize_actor
from .resolver import band, outcome_for_model, resolve
from .sheets import (
    DEBILITIES, LEVEL_UP_HINT, STAT_NAMES, STATS, SheetError, clumsy, debility_on, encumbrance, stat_modifier,
    xp_line, xp_needed,
)

#: Race moves the model flags with race_move=true: (class, race, move) -> what changes.
_RACE_MOVES = {
    ("Fighter", "Dwarf", "parley"): "con",  # sharing a drink
    ("Fighter", "Halfling", "defy_danger"): "+1",  # using small size
    ("Thief", "Human", "spout_lore"): "+1",  # about criminal activities
    ("Thief", "Human", "discern_realities"): "+1",
}

#: "Other sources of damage": how bad the GM says it is -> its die.
_HAZARDS = {
    "bruises": "d4",  # "It threatens bruises and scrapes at worst"
    "blood": "d6",  # "It's likely to spill some blood, but nothing horrendous"
    "bones": "d8",  # "It might break some bones"
    "lethal": "d10",  # "It could kill a common person"
}

def _living(creature: Optional[Dict[str, Any]]) -> bool:
    """Is this creature alive -- not undead, not a construct? Read from the bestiary."""
    if creature is None:
        return False
    from .monsters import lookup

    known = lookup(creature.get("kind", "")) or {}
    tags = {t.lower() for t in known.get("tags") or ()}
    return known.get("setting") != "Undead" and "construct" not in tags


def _race_move_of(sheet: Dict[str, Any], race: str) -> bool:
    """Do they have this race's move? Half-Elven: "You gain the elf starting move if you took
    the human one at character creation or vice versa." """
    return sheet.get("race") == race or (advanced.has(sheet, "Half-Elven") and race in ("Elf", "Human"))


def _poisons() -> Dict[str, str]:
    """The equipment list's poisons, by name: what each does."""
    import json
    from pathlib import Path

    items = json.loads((Path(__file__).parent / "data" / "equipment.json").read_text(encoding="utf-8"))["items"]
    return {i["name"]: i.get("description", "") for i in items.values() if i.get("kind") == "poison"}


POISONS = _poisons()

#: Every move rolls this.
MOVE_DICE = "2d6"
#: Herculean Appetites: "instead of rolling 2d6 you roll 1d6+1d8" -- the d6 first.
APPETITE_DICE = "1d6+1d8"


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


@dataclass(frozen=True)
class ReactPrompt:
    """A Reduce damage button on a hit, for whoever has a move that answers it."""

    hit_id: str


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
class AfflictMonster(Effect):
    """Set the poisons working on a creature. Journalled by the runner, so it undoes."""

    channel_id: int
    name: str
    afflictions: tuple


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


#: Told to the model when trade happens with no steading on the scene: the town's tags --
#: what is for sale, prices, Market's +1 to Supply -- apply only once it is recorded.
NO_STEADING = ("No steading is recorded for this scene, so its tags (what is for sale, prices, Market) "
               "did not apply. If the party is in a village, town, keep or city, call create_steading "
               "(or set_scene with steading set to one already made) before the next purchase.")

def starting_move(sheet: Dict[str, Any], name: str) -> bool:
    """A class starting move this character has -- for a Barbarian, only the one of the pair
    they chose."""
    from .class_moves import moves_of

    chosen = (sheet.get("choices") or {}).get("start", "")
    return any(m["name"] == name for m in moves_of(str(sheet.get("class", "")), str(sheet.get("race", "")), chosen))

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
        steading: Optional[Callable[[], Optional[dict]]] = None,
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
        # The steading the party is in, if any: its tags change Supply and Recruit.
        self._steading = steading or (lambda: None)
        self._known_actors = {normalize_actor(a) for a in self.players.values()}

    def set_player_character(self, user_id: int, actor: str) -> None:
        """Register who is playing whom, from ``/playing``, ``/new`` or state.

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
        forward: bool = False,
        offers: Sequence[str] = (),
        preparation: bool = False,
        aide: str = "",
        race_move: bool = False,
        ally: str = "",
        weak_point: bool = False,
        two_weapons: bool = False,
        extra_targets: Sequence[str] = (),
        form_boon: str = "",
        form_plus: str = "",
        form_minus: str = "",
        minion: str = "",
        weapon_leverage: bool = False,
        dragon_nature: bool = False,
        hurl: bool = False,
        appetite: bool = False,
        movement: bool = False,
        mortal_magic: bool = False,
        weapon: str = "",
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
        racial = _RACE_MOVES.get((self._sheet_or_empty(actor).get("class"), self._sheet_or_empty(actor).get("race"),
                                  rules.key)) if race_move else None
        if race_move and racial is None:
            mover = self._sheet_or_empty(actor)
            return [self._refuse(channel_id, f"No race move of {actor}'s ({mover.get('race', '?')} "
                                             f"{mover.get('class', '?')}) applies to {rules.name}.",
                                 "Re-issue make_move without race_move.")]
        if racial == "con":
            stat = "con"  # Dwarf Fighter: "you may parley with them using CON instead of CHA"
        compendium_why = self._compendium_situation(rules.key, actor, weapon_leverage, dragon_nature, hurl)
        if compendium_why:
            return [self._refuse(channel_id, compendium_why, "Re-issue make_move without it.")]
        if hurl:
            stat = "con"  # Velocitas Eradico: "When volleying this way use Con instead of Dex."

        mover_sheet = self._sheet_or_empty(actor)
        if mover_sheet.get("retired") or mover_sheet.get("mentor"):
            return [self._refuse(channel_id, f"{actor} is {'retired' if mover_sheet.get('retired') else 'a mentor now, played by you'}"
                                             " and makes no moves.", "Narrate what they do instead, or choose "
                                             "another character.")]

        # A class move is that class's alone: a Fighter cannot Backstab.
        if rules.classes and self._sheet_for is not None:
            sheet = self._sheet_for(actor)
            if (sheet is not None and sheet.get("class") not in rules.classes
                    and not multiclass.grants_key(sheet, rules.key)  # Multiclass Dabbler's move
                    and not (rules.key == "cast_a_spell" and spells.favored(sheet))):  # Divine Favor
                return [self._refuse(
                    channel_id,
                    f"{rules.name} is the {' or '.join(rules.classes)}'s move; {actor} is a "
                    f"{sheet.get('class', 'character without a class')}.",
                    "Choose a move this character has -- a basic move, or one listed under "
                    "their class.",
                )]

        if rules.requires and self._sheet_for is not None:
            sheet = self._sheet_for(actor)
            if sheet is not None and not advanced.has(sheet, rules.requires):
                return [self._refuse(channel_id, f"{rules.name} is an advanced move {actor} has not taken.",
                                     "Choose a move this character has.")]

        if rules.key != "defy_danger" and self._sheet_or_empty(actor).get("stunned"):
            return [self._refuse(
                channel_id, f"{actor} is stunned ({self._sheet_or_empty(actor)['stunned']}): anything they do "
                            "is Defy Danger, the danger being that they are stunned.",
                "Re-issue make_move as defy_danger (the stat their approach uses), or end the stun with "
                "stun end=true once they have cleared their head.")]

        if rules.stat == CASTER:
            cast = self._open_cast(channel_id, user_id, actor, spell, target, stakes, ttl_seconds)
            if forward and any(isinstance(e, SendMessage) and isinstance(e.prompt, RollPrompt) for e in cast):
                # Fixed after the fact on the committed check, so the cast's own checks run first.
                held, spend = self._forward(channel_id, actor)
                if held:
                    check = next(c for c in self.registry.open_checks(channel_id) if c.user_id == user_id)
                    self.registry.hold(check, modifier=check.modifier + held)
                    cast = [e if not (isinstance(e, SendMessage) and isinstance(e.prompt, RollPrompt))
                            else replace(e, text=e.text.rstrip(".") + f" ({held:+d} forward).") for e in cast]
                    return [*spend, *cast]
            return cast
        if rules.key == "carouse":
            return self._open_carouse(channel_id, user_id, actor, int(coins or 0), stakes, ttl_seconds)
        if rules.stat == OFFERS:
            return self._open_recruit(channel_id, user_id, actor, offers, stakes, ttl_seconds, forward, target)
        if rules.stat == LOYALTY:
            return self._open_order(channel_id, user_id, actor, target, stakes, ttl_seconds)

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
                    "Tell the player, in a sentence, to make one with `/new` before "
                    "they roll. Do not resolve the action.",
                )]
            mod = stat_modifier(sheet, stat)
            if minion:
                # Ambeoid Overlord: a slime "triggers basic moves as a character would", with its stats.
                pet = slimes.find(sheet, minion)
                if pet is None or pet["hp"] <= 0:
                    return [self._refuse(channel_id, f"{actor} has no slime minion {minion!r} able to act.",
                                         "Re-issue make_move without minion.")]
                try:
                    mod = slimes.modifier(pet, sheet, stat)
                except SheetError as exc:
                    return [self._refuse(channel_id, str(exc), "Choose a move the slime can make.")]
                notes.append(f"{pet['name']} acts")
            # A precise weapon hacks and slashes with Dex, not Str -- where Dex is better.
            if weapon and rules.attacks and not rules.damage_die and "gear" in sheet:
                from .gear import weapon_named

                chosen = weapon_named(sheet["gear"], weapon, rules.ranged, int(sheet.get("ammo", 0)))
                if chosen is None:
                    fits = [i.name for i in items_of(sheet["gear"]) if any(
                        r in (RANGED if rules.ranged else MELEE) for r in i.ranges)]
                    return [self._refuse(
                        channel_id, f"{actor} has no {weapon!r} to {rules.name} with"
                        + (f"; what fits: {', '.join(fits)}." if fits else "."),
                        "Re-issue make_move with one of those as weapon, or without weapon.")]
                weapon = chosen.name
                notes.append(f"with the {weapon.lower()}")
            if rules.key == "hack_and_slash" and "gear" in sheet:
                weapon_item = self._weapon(sheet, rules.key, weapon)
                dex = stat_modifier(sheet, "dex")
                precise = weapon_item is not None and ("precise" in weapon_item.tags
                                                       or choices.precise_for(sheet, weapon_item.name))
                if precise and dex > mod:
                    stat, mod = "dex", dex
            if debility_on(sheet, stat):
                notes.append(f"-1 {debility_on(sheet, stat).title()}")
            if clumsy(sheet):
                mod -= 1
                notes.append(f"-1 clumsy {clumsy(sheet)}")
            form, why = self._form_mod(sheet, stat)
            mod += form
            notes.extend(why)
            unable = _unable(actor, sheet)
            if unable:
                return [self._refuse(
                    channel_id, unable,
                    "They cannot act. Narrate around them, or ask what someone else does.",
                )]
            # A sheet made before gear existed has no gear to check, and is let through
            # as before; `/gear` gives it some.
            if (rules.attacks and not rules.damage_die and not hurl and not minion and "gear" in sheet
                    and self._weapon(sheet, rules.key) is None):
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
        more, why = self._ongoing(actor, rules.key)
        mod += more
        notes.extend(why)
        burden = self._burden(actor)
        if burden:
            mod -= 1
            notes.append("-1 encumbered")
        held, spend = self._forward(channel_id, actor) if forward else (0, [])
        if held:
            mod += held
            notes.append(f"{held:+d} forward")
        mover = self._sheet_or_empty(actor)
        if racial == "+1":
            mod += 1
            notes.append(f"+1 {mover.get('race')}")
        if weapon_leverage:
            # Its Name Is Dread: "you can parley with +bonds with your weapon instead of +Cha".
            mod += bonds.weapon_bonds(mover) - stat_modifier(mover, "cha")
            notes.append(f"+{bonds.weapon_bonds(mover)} bonds with their weapon, not Cha")
        if dragon_nature:
            mod += int(mover.get("blood", 0))  # Wake the Dragon: "+Blood to the roll"
            notes.append(f"+{int(mover.get('blood', 0))} Blood")
        if rules.key == "bind" and mover.get("faraday"):
            mod -= 1  # Faraday's Law: "Until you dismiss this effect you take -1 to bind."
            notes.append("-1 Faraday's Law")
        if rules.key == "supply" and steadings.has(self._steading(), "market"):
            mod += 1  # Market: "+1 to supply"
            notes.append(f"+1 Market ({self._steading()['name']})")
        if advanced.has(mover, "A Good Day to Die") and mover.get("hp") is not None and \
                int(mover["hp"]) < max(int((mover.get("stats") or {}).get("con", 0)), 1):
            mod += 1  # "As long as you have less than your Con in current HP... take +1 ongoing"
            notes.append("+1 A Good Day to Die")
        if (rules.key == "discern_realities" and advanced.has(mover, "Seeing Red")
                and any(r["hp"] > 0 for r in self._monsters().values())):
            mod += 1
            notes.append("+1 Seeing Red")
        ready, prepared = self._preparation(channel_id, actor) if preparation else (0, [])
        if ready:
            mod += ready
            notes.append("+1 preparation")
            spend = [*spend, *prepared]
        helper = ""
        if aide:
            found, skill_name, why = self._aide(rules.key, aide)
            if found is None:
                return [self._refuse(channel_id, why, "Re-issue make_move without aide, or narrate "
                                                      "what the hireling does instead.")]
            helper = found["name"]
            if skill_name == "Protector":
                mod += 1
                notes.append(f"+1 {helper}; a 10+ counts as 7–9")
            else:
                notes.append(f"+{hirelings.skill(found, 'Warrior')} damage from {helper}")
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
        flags, situation, spent_ammo = self._situation(
            rules.key, actor, target, weak_point, two_weapons, extra_targets, form_boon, form_plus, form_minus)
        if situation:
            return [self._refuse(channel_id, situation, "Re-issue make_move without it.")]
        if hurl:
            flags.append("hurl")
        if weapon:
            flags.append(f"weapon:{weapon}")
        barbarian_why = self._barbarian_situation(rules.key, actor, appetite, movement, mortal_magic)
        if barbarian_why:
            return [self._refuse(channel_id, barbarian_why, "Re-issue make_move without it.")]
        if appetite:
            flags.append("appetite")
            notes.append("1d6+1d8, chasing an appetite")
        if movement:
            mod += 1  # On the Move: "When you defy a danger caused by movement... take +1"
            notes.append("+1 On the Move")
        if mortal_magic:
            flags.append("no_miss")  # Healthy Distrust: "treat any result of 6- as a 7-9"
        if minion:
            flags.append(f"minion:{slimes.find(self._sheet_or_empty(actor), minion)['name']}")
        if "spread" in flags:
            target = ", ".join([target, *[n for n in self._standing() for e in extra_targets
                                          if n.lower() == str(e).strip().lower()]])
            spend = [*spend, SpendAmmo(channel_id, actor, spent_ammo)]
            notes.append(f"{len(extra_targets)} extra ammo for extra targets")
        striker = ""
        if ally:
            # Setup Strike, Tandem Strike: "When you hack and slash, choose an ally."
            mover = self._sheet_or_empty(actor)
            who = self._character(ally)
            if rules.key != "hack_and_slash" or not (advanced.has(mover, "Setup Strike")
                                                     or advanced.has(mover, "Tandem Strike")):
                return [self._refuse(channel_id, f"{actor} has no move that sets up an ally's strike.",
                                     "Re-issue make_move without ally.")]
            if who is None or normalize_actor(who) == normalize_actor(actor):
                return [self._refuse(channel_id, f"{ally!r} is not another character at the table.",
                                     "Name the ally by their character name.")]
            striker = who
        staked = ""
        if rules.key == "counterspell":
            # "...stake one of your prepared spells on the defense": named as spell.
            mover = self._sheet_or_empty(actor)
            found = spells.find(mover, spell) if spell else None
            if found is None or found.free or found.name not in [s.name for s in spells.castable(mover)]:
                held = ", ".join(s.name for s in spells.castable(mover) if not s.free) or "none"
                return [self._refuse(channel_id, f"Counterspell stakes one of {actor}'s prepared spells "
                                                 f"(spell). Prepared: {held}.",
                                     "Re-issue make_move with spell set to the one they stake.")]
            staked = found.name
            notes.append(f"staking {staked}")
        return [*spend, *self._open(channel_id, user_id, actor, rules.key, stat, mod, stakes, target,
                                    ttl_seconds, f"{rules.name}{at}" + (f", with {pet}" if pet else ""),
                                    notes, spell=staked, companion=pet, overloaded=burden == "overloaded",
                                    aide=helper, ally=striker, flags=flags)]

    def _open(
        self, channel_id: int, user_id: int, actor: str, move: str, stat: str, mod: int,
        stakes: str, target: str, ttl_seconds: Optional[float], what: str,
        notes: Sequence[str] = (), spell: str = "", companion: str = "", overloaded: bool = False,
        aide: str = "", ally: str = "", flags: Sequence[str] = (),
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
            dice=APPETITE_DICE if "appetite" in flags else MOVE_DICE,
            target=target,
            spell=spell,
            companion=companion,
            aide=aide,
            ally=ally,
            flags=flags,
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
        rolled_with = (f"+Bond {mod:+d}" if stat == BOND else f"+Loyalty {mod:+d}" if stat == LOYALTY
                       else f"{mod:+d}" if stat in (NOTHING, OFFERS)
                       else f"+{STAT_NAMES[stat][:3].title()} {mod:+d}")
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
                "Tell the player, in a sentence, to make one with `/new` first.",
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
            setup = " They have not chosen spells yet: tell them `/sheet` has the Choose spells button." \
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
        if clumsy(sheet):
            mod -= 1
            notes.append(f"-1 clumsy {clumsy(sheet)}")
        mod += penalty
        if self._blessed(actor):
            mod += 1
            notes.append("+1 Bless")
        more, why = self._ongoing(actor, "cast_a_spell")
        mod += more
        notes.extend(why)
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
                                 "Tell the player to make one with `/new` first.")]
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

    def _open_recruit(
        self, channel_id: int, user_id: int, actor: str, offers: Sequence[str], stakes: str,
        ttl_seconds: Optional[float], forward: bool, seeking: str = "",
    ) -> List[Effect]:
        """Recruit: +1 for each thing made known -- generous pay, the purpose, a share -- and +1
        for a useful reputation here."""
        made = []
        for offer in offers or ():
            key = str(offer).strip().lower()
            if key not in hirelings.OFFERS:
                return [self._refuse(channel_id, f"No Recruit offer {offer!r}. They are: "
                                                 f"{', '.join(hirelings.OFFERS)}.",
                                     "Re-issue make_move with offers from that list.")]
            if key not in made:
                made.append(key)
        notes = [f"+1 {k}" for k in made]
        # Arcane, Divine, Guild: "+1 to recruit when you put out word you're looking for an adept".
        local, why = steadings.recruit_bonus(self._steading(), seeking)
        if local:
            made.append(why)
            notes.append(f"+1 {why} ({self._steading()['name']})")
        held, spend = self._forward(channel_id, actor) if forward else (0, [])
        if held:
            notes.append(f"{held:+d} forward")
        return [*spend, *self._open(channel_id, user_id, actor, "recruit", OFFERS, len(made) + held,
                                    stakes, "", ttl_seconds, "Recruit", notes)]

    def _open_order(
        self, channel_id: int, user_id: int, actor: str, target: str, stakes: str,
        ttl_seconds: Optional[float],
    ) -> List[Effect]:
        """Order Hirelings: +loyalty of the hireling given the order -- one of this
        character's own."""
        sheet = self._sheet_for(actor) if self._sheet_for is not None else None
        hired = hirelings.find(sheet or {}, target)
        if hired is None:
            have = ", ".join(h["name"] for h in (sheet or {}).get("hirelings") or ()) or "none"
            return [self._refuse(channel_id, f"{actor} has no hireling named {target!r}. Theirs: {have}.",
                                 "Re-issue make_move with target set to one of their hirelings, or "
                                 "hire one first with hire.")]
        mod = hirelings.loyalty_of(hired)
        notes = ["-1 unpaid"] if hired.get("unpaid") else []
        authority = next((n for n in ("Divine Authority", "Voice of Authority") if advanced.has(sheet, n)), None)
        if authority:
            mod += 1
            notes.append(f"+1 {authority}")
        return self._open(channel_id, user_id, actor, "order_hirelings", LOYALTY, mod, stakes,
                          hired["name"], ttl_seconds, f"Order Hirelings: {hired['name']}", notes)

    def _sheet_or_empty(self, actor: str) -> Dict[str, Any]:
        return (self._sheet_for(actor) or {}) if self._sheet_for is not None else {}

    def _party(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Every character at the table with a sheet, living."""
        out = []
        for actor in sorted(set(self.players.values())):
            sheet = self._sheet_for(actor) if self._sheet_for is not None else None
            if sheet is not None and not sheet.get("dead"):
                out.append((actor, sheet))
        return out

    def _forward(self, channel_id: int, actor: str) -> Tuple[int, List[Effect]]:
        """The roll forwards this character holds, summed, and the effect spending them. The
        model says they apply (make_move forward=true); Gary adds them and they are gone."""
        sheet = self._sheet_for(actor) if self._sheet_for is not None else None
        held = [f for f in (sheet or {}).get("forwards") or () if not f.get("damage")]
        if not held:
            return 0, []
        left = [f for f in sheet["forwards"] if f.get("damage")]
        return sum(int(f["amount"]) for f in held), [UpdateSheet(channel_id, actor, {"forwards": left})]

    def _preparation(self, channel_id: int, actor: str) -> Tuple[int, List[Effect]]:
        """Bolster: "spend 1 preparation for +1 to any roll. You can only spend one preparation
        per roll." (1, the spending) if they have some, else (0, [])."""
        sheet = self._sheet_for(actor) if self._sheet_for is not None else None
        have = int((sheet or {}).get("preparation", 0))
        if have <= 0:
            return 0, []
        return 1, [UpdateSheet(channel_id, actor, {"preparation": have - 1})]

    def bolster(
        self, *, channel_id: int, character: str, weeks: int, what: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Bolster: leisure time in study, meditation or hard practice. A week or two is 1
        preparation; a month or longer, 3. Downtime, guarded like camp."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        unsettled = self.unsettled(channel_id, "spend weeks in study")
        if unsettled:
            return [], {"ok": False, "error": unsettled}
        what = " ".join(str(what or "").split())
        if not what:
            return [], {"ok": False, "error": "Say what they study, meditate on or practise (what)."}
        weeks = int(weeks or 0)
        if not 1 <= weeks <= 26:
            return [], {"ok": False, "error": "Bolster takes 1 to 26 weeks."}
        gained = 3 if weeks >= 4 else 1
        total = int(sheet.get("preparation", 0)) + gained
        return [
            SendMessage(channel_id, f"📚 **{actor}** spends {weeks} week{'s' if weeks > 1 else ''} in {what} — "
                                    f"+{gained} preparation ({total})."),
            UpdateSheet(channel_id, actor, {"preparation": total}),
        ], {"ok": True, "preparation": total, "days": weeks * 7, "note": (
            "Preparation is +1 to one roll each, when it pays off: set preparation=true on that move.")}

    def _grant(self, channel_id: int, actor: str, reason: str, amount: int = 1,
               damage: str = "") -> List[Effect]:
        """Give a character a forward: +1 (or -1) to a later roll, or extra damage dice."""
        sheet = self._sheet_for(actor) if self._sheet_for is not None else None
        if sheet is None:
            return []
        entry = {"damage": damage, "reason": reason} if damage else {"amount": amount, "reason": reason}
        what = f"+{damage} damage" if damage else f"{amount:+d}"
        return [SendMessage(channel_id, f"➕ **{actor}** takes {what} forward — {reason}."),
                UpdateSheet(channel_id, actor, {"forwards": [*(sheet.get("forwards") or []), entry]})]

    def _forwards_from(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        """What a resolved move leaves behind for later: the book's forwards, and what
        a Hack and Slash sets up (armor forward, a marked foe, an ally's strike)."""
        return [*self._book_forwards(channel_id, check, dice), *self._after_hack(channel_id, check),
                *self._invigorate(channel_id, check, dice), *self._challenged(channel_id, check, dice)]

    def _challenged(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        """What Are You Waiting For?, 10+: the creatures standing now take +2 from them."""
        if check.move != "what_are_you_waiting_for" or band(dice) != "strong":
            return []
        return [UpdateSheet(channel_id, check.actor, {"challenged": list(self._standing())})]

    def _situation(
        self, move: str, actor: str, target: str, weak_point: bool, two_weapons: bool,
        extra_targets: Sequence[str], form_boon: str, form_plus: str, form_minus: str,
    ) -> Tuple[List[str], str, int]:
        """(flags for the check, why not, the ammo left after Blot Out the Sun) for the
        situational parts of a move -- each checked against the move that grants it."""
        sheet = self._sheet_or_empty(actor)
        flags: List[str] = []
        if weak_point:
            if move != "volley" or not advanced.has(sheet, "Smaug\u2019s Belly"):
                return [], f"{actor} has nothing that uses a weak point (Smaug's Belly, on a Volley).", 0
            flags.append("weak_point")
        if two_weapons:
            if move != "hack_and_slash" or not (advanced.has(sheet, "Viper\u2019s Strike")
                                                or advanced.has(sheet, "Viper\u2019s Fangs")):
                return [], f"{actor} has no move for striking with two weapons (Viper's Strike).", 0
            melee = [i for i in items_of(sheet.get("gear") or []) if {"hand", "close"} & set(i.ranges)]
            if "gear" in sheet and len(melee) < 2:
                return [], f"{actor} does not carry two hand or close weapons.", 0
            flags.append("two_weapons")
        ammo_left = int(sheet.get("ammo", 0))
        if extra_targets:
            if move != "volley" or not advanced.has(sheet, "Blot Out the Sun"):
                return [], f"{actor} cannot volley at extra targets (Blot Out the Sun).", 0
            standing = {n.lower() for n in self._standing()}
            wrong = [t for t in extra_targets if str(t).strip().lower() not in standing
                     or str(t).strip().lower() == str(target).strip().lower()]
            if wrong:
                return [], f"Extra targets must be other creatures still standing: not {', '.join(wrong)}.", 0
            if ammo_left < len(extra_targets) + 1:
                return [], (f"{actor} has {ammo_left} ammo: one for the volley and one per extra target is "
                            f"{len(extra_targets) + 1}."), 0
            flags.append("spread")
            ammo_left -= len(extra_targets)
        boon = str(form_boon or "").strip().lower()
        if boon:
            if move != "shapeshifter" or not advanced.has(sheet, "Formshaper") or boon not in ("armor", "damage"):
                return [], "form_boon is Formshaper's, on Shapeshifter: armor or damage.", 0
            flags.append(f"boon:{boon}")
        plus, minus = str(form_plus or "").strip().lower(), str(form_minus or "").strip().lower()
        if plus or minus:
            if move != "shapeshifter" or not advanced.has(sheet, "Formcrafter") or plus not in STATS \
                    or minus not in STATS:
                return [], "Formcrafter, on Shapeshifter, takes both form_plus (theirs) and form_minus (yours).", 0
            flags += [f"plus:{plus}", f"minus:{minus}"]
        return flags, "", ammo_left

    def _after_strike(self, channel_id: int, actor: str, hits: Sequence[Dict[str, Any]]) -> List[Effect]:
        """After a character deals damage: Balance's point ("When you deal damage, take 1
        balance") and, for Spell Augmentation, the creature they struck."""
        sheet = self._sheet_or_empty(actor)
        struck = [h["target"] for h in hits if int(h.get("dealt", 0)) > 0 and h.get("target")]
        changes: Dict[str, Any] = {}
        if struck and advanced.has(sheet, "Spell Augmentation"):
            changes["last_struck"] = struck[-1]  # where Spell Augmentation may shunt a spell
        if struck and advanced.has(sheet, "Balance"):
            changes["balance"] = int(sheet.get("balance", 0)) + 1
        return [UpdateSheet(channel_id, actor, changes)] if changes else []

    def spell_augmentation(
        self, *, channel_id: int, character: str, spell: str
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Spell Augmentation: "When you deal damage to a creature you can shunt a spell's energy
        into them -- end one of your ongoing spells and add the spell's level to the damage
        dealt." Onto the creature they last dealt damage to."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None or not advanced.has(sheet, "Spell Augmentation"):
            return [], {"ok": False, "error": f"{character!r} does not have Spell Augmentation."}
        target = sheet.get("last_struck")
        creature = self._monsters().get(target or "")
        if creature is None:
            return [], {"ok": False, "error": f"{actor} has not just dealt damage to a creature still here."}
        running = [(i, s) for i, (s, _) in enumerate(spells.running(sheet)) if s.name.lower() == str(spell).strip().lower()]
        if not running:
            names = ", ".join(s.name for s, _ in spells.running(sheet)) or "none"
            return [], {"ok": False, "error": f"{actor} has no {spell!r} running. Ongoing: {names}."}
        index, found = running[0]
        kept = list(sheet.get("ongoing") or [])
        del kept[index]
        after = int(creature["hp"]) - found.level
        return [
            SendMessage(channel_id, f"✨ **{actor}** ends {found.name} and shunts it into {target} — "
                                    f"**{found.level} more damage**, HP {creature['hp']} → {max(after, 0)}"
                                    + (" — **defeated**." if after <= 0 else ".")),
            ChangeHp(channel_id, "monster", target, after),
            UpdateSheet(channel_id, actor, {"ongoing": kept, "last_struck": None}),
        ], {"ok": True, "dealt": found.level, "hp_after": max(after, 0), "defeated": after <= 0}

    def channel_balance(
        self, *, channel_id: int, character: str, patient: str, amount: int = 1
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Balance: "When you touch someone and channel the spirits of life you may spend
        balance. For each balance spent, heal 1d4 HP"."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None or not advanced.has(sheet, "Balance"):
            return [], {"ok": False, "error": f"{character!r} has no Balance to spend."}
        have, amount = int(sheet.get("balance", 0)), int(amount or 0)
        if not 1 <= amount <= have:
            return [], {"ok": False, "error": f"{actor} has {have} balance."}
        who = self._character(patient)
        target = self._sheet_for(who) if who else None
        if target is None or target.get("dead"):
            return [], {"ok": False, "error": f"{patient!r} is not a living character here."}
        rolled = self.dice.roll(f"{amount}d4")
        healed = min(rolled.total, int(target["max_hp"]) - int(target["hp"]))
        hp = int(target["hp"]) + healed
        effects: List[Effect] = [
            SendMessage(channel_id, f"🌿 **{actor}** channels the spirits of life into **{who}** — {amount}d4: "
                                    f"{rolled.render()}, heals {healed}, HP {hp}/{target['max_hp']}."),
            ChangeHp(channel_id, "character", who, hp),
            UpdateSheet(channel_id, actor, {"balance": have - amount}),
        ]
        return effects, {"ok": True, "healed": healed, "balance_left": have - amount}

    def _form_mod(self, sheet: Dict[str, Any], stat: str) -> Tuple[int, List[str]]:
        """Formcrafter: "+1 ongoing to rolls using that stat while shifted" -- and the GM's -1."""
        shape = sheet.get("shape") or {}
        mod, notes = 0, []
        if shape.get("plus") == stat:
            mod, notes = mod + 1, [*notes, f"+1 {shape['form']} form"]
        if shape.get("minus") == stat:
            mod, notes = mod - 1, [*notes, f"-1 {shape['form']} form"]
        return mod, notes

    def _after_hack(self, channel_id: int, check: PendingCheck) -> List[Effect]:
        """"When you hack and slash..." -- whatever the result."""
        if check.move != "hack_and_slash":
            return []
        sheet = self._sheet_or_empty(check.actor)
        effects: List[Effect] = []
        parry = 2 if advanced.has(sheet, "Duelist\u2019s Block") else 1 if advanced.has(sheet, "Duelist\u2019s Parry") else 0
        if parry:
            effects += self._armor_forward(channel_id, check.actor, parry, "Duelist's " + ("Block" if parry == 2 else "Parry"))
        blood = "1d8" if advanced.has(sheet, "Taste of Blood") else "1d4" if advanced.has(sheet, "Scent of Blood") else ""
        if blood and check.target:
            # "...your next attack against that same foe deals +1d4 damage."
            why = "Taste of Blood" if blood == "1d8" else "Scent of Blood"
            effects += self._mark(channel_id, check.actor, check.target, blood, why)
        if check.ally and check.target:
            tandem = advanced.has(sheet, "Tandem Strike")
            why = "Tandem Strike" if tandem else "Setup Strike"
            effects += self._mark(channel_id, check.ally, check.target, "1d4", f"{check.actor}'s {why}")
            if tandem:
                effects += self._grant(channel_id, check.ally, f"against {check.target} ({check.actor}'s Tandem Strike)")
        return effects

    def _mark(self, channel_id: int, actor: str, target: str, dice: str, why: str) -> List[Effect]:
        """A bonus waiting on this character's next attack against one foe."""
        sheet = self._sheet_or_empty(actor)
        if not sheet:
            return []
        kept = [m for m in sheet.get("marks") or () if not (m["target"] == target and m["reason"] == why)]
        return [SendMessage(channel_id, f"🎯 **{actor}**'s next attack on {target}: +{dice} damage ({why})."),
                UpdateSheet(channel_id, actor, {"marks": [*kept, {"target": target, "damage": dice, "reason": why}]})]

    def _use_marks(self, channel_id: int, actor: str, target: str, total: int,
                   shown: str) -> Tuple[int, str, List[Effect]]:
        """Spend the bonuses waiting on an attack against ``target``."""
        sheet = self._sheet_or_empty(actor)
        marks = sheet.get("marks") or []
        hit = [m for m in marks if normalize_actor(m["target"]) == normalize_actor(target or "")]
        for m in hit:
            extra = self.dice.roll(m["damage"]).total
            total, shown = total + extra, f"{shown}, +{m['damage']} {m['reason']}: {extra} = {total + extra}"
        if not hit:
            return total, shown, []
        return total, shown, [UpdateSheet(channel_id, actor, {"marks": [m for m in marks if m not in hit]})]

    def _armor_forward(self, channel_id: int, actor: str, amount: int, why: str) -> List[Effect]:
        """"Take +1 armor forward": more armor against the next blow that lands on them."""
        sheet = self._sheet_or_empty(actor)
        if not sheet:
            return []
        return [SendMessage(channel_id, f"🛡️ **{actor}** takes +{amount} armor forward — {why}."),
                UpdateSheet(channel_id, actor, {"armor_forward": [*(sheet.get("armor_forward") or []),
                                                                   {"amount": amount, "reason": why}]})]

    def _invigorate(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        """Invigorate: "When you heal someone they take +2 forward to their damage"."""
        if band(dice) == "miss" or not check.target:
            return []
        healer = self._sheet_or_empty(check.actor)
        if not advanced.has(healer, "Invigorate"):
            return []
        rules = MOVES[check.move].band(band(dice))
        picked = [rules.choice.options[p] for p in check.picks] if rules.choice else []
        spell = self._spell_of(check) if rules.casts else None
        heals = rules.heals or any(o.heals for o in picked) or (spell is not None and spell.heals)
        patient = self._character(check.target)
        if not heals or patient is None or normalize_actor(patient) == normalize_actor(check.actor):
            return []
        return self._grant(channel_id, patient, f"{check.actor}'s Invigorate", damage="2")

    def lead_the_charge(
        self, *, channel_id: int, character: str, led: Sequence[str]
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Charge!: "When you lead the charge into combat, those you lead take +1 forward."
        Ever Onward adds "+2 armor forward"."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        onward = advanced.has(sheet, "Ever Onward")
        if not onward and not advanced.has(sheet, "Charge!"):
            return [], {"ok": False, "error": f"{actor} has neither Charge! nor Ever Onward."}
        effects: List[Effect] = []
        followers = []
        for who in led or ():
            name = self._character(who)
            if name is None or normalize_actor(name) == normalize_actor(actor):
                return [], {"ok": False, "error": f"{who!r} is not another character at the table."}
            followers.append(name)
            effects += self._grant(channel_id, name, f"following {actor}'s charge")
            if onward:
                effects += self._armor_forward(channel_id, name, 2, f"following {actor}'s charge (Ever Onward)")
        if not followers:
            return [], {"ok": False, "error": "Name who follows the charge (led)."}
        return effects, {"ok": True, "led": followers}

    def _book_forwards(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        """The forwards a move's result grants, as the book writes them."""
        b = band(dice)
        rules = MOVES[check.move].band(b)
        picked = [rules.choice.options[p].text for p in check.picks] if rules.choice else []
        actor = check.actor
        mover = self._sheet_or_empty(actor)
        if check.move == "parley" and b != "miss" and (advanced.has(mover, "Bamboozle") or advanced.has(mover, "Con")):
            return self._grant(channel_id, actor, "with the one they parleyed")
        if check.move == "aid_or_interfere" and b != "miss" and advanced.has(mover, "A Little Help From My Friends") \
                and any(t.startswith("Help them") for t in picked):
            return self._grant(channel_id, actor, "for helping a friend (A Little Help From My Friends)")
        if check.move == "discern_realities" and b != "miss":
            return self._grant(channel_id, actor, "when acting on what Discern Realities showed")
        if check.move == "communion_of_whispers" and b == "miss":
            return self._grant(channel_id, actor, "shaken by the vision", amount=-1)
        if check.move == "through_deaths_eyes" and b == "miss" and self._standing():
            # "you see your own death and consequently take -1 ongoing throughout the battle"
            return self._put_ongoing(channel_id, actor, -1, "saw their own death", "battle")
        if check.move == "i_am_the_law" and b == "strong":
            return self._grant(channel_id, actor, "against the one they gave the order")
        if check.move == "i_am_the_law" and b == "miss":
            return self._grant(channel_id, actor, "against the one who defied their order", amount=-1)
        if check.move == "take_watch" and b == "strong":
            return [e for a, _ in self._party() for e in self._grant(channel_id, a, "the camp was ready")]
        if check.move == "backstab" and any(t.startswith("You create an advantage") for t in picked):
            return self._grant(channel_id, actor, "acting on the advantage Backstab made")
        if check.move == "arcane_art" and any(t.startswith("+1d4 forward") for t in picked):
            # Vicious Cacophony: "an extra +1d4"; Vicious Blast: "an extra +2d4".
            dice = "3d4" if advanced.has(mover, "Vicious Blast") else "2d4" if advanced.has(mover, "Vicious Cacophony") else "1d4"
            return self._grant(channel_id, check.target or actor, "Arcane Art", damage=dice)
        return []

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

        dice, capped = self._intervened(check, self._roll_move(check))
        dice, capped = self._banners(check, dice, capped)
        rolled = SendMessage(
            channel_id,
            f"🎲 **{check.actor}** rolls {dice.render()} — {_RESULT[band(dice)]}." + capped,
        )
        thrown = self._thrown_weapon(check)
        if thrown is not None and not any(f.startswith("weapon:") for f in check.flags):
            check = replace(check, flags=(*check.flags, f"weapon:{thrown.name}"))  # damage after it's gone
        if thrown is not None and not thrown.needs_ammo:
            # Held with the check, so the damage still comes from the spear once it has left the sheet.
            check = replace(check, flags=(*check.flags, f"thrown:{json.dumps(asdict(thrown))}"))
        return [rolled, *self._thrown(channel_id, check), *self._next(channel_id, check, dice)]

    def _thrown_weapon(self, check: PendingCheck):
        """The thrown weapon this Volley uses, if it is one."""
        if check.move != "volley" or "hurl" in check.flags or self._sheet_for is None:
            return None
        sheet = self._sheet_for(check.actor)
        if not sheet or "gear" not in sheet:
            return None
        used = self._weapon(sheet, check.move, check)
        return used if used is not None and "thrown" in used.tags else None

    def _thrown(self, channel_id: int, check: PendingCheck) -> List[Effect]:
        """Thrown: "once you throw it, it's gone until you can recover it" -- whatever the roll.
        A weapon counted in ammo (throwing daggers) loses one; a spear leaves the sheet."""
        used = self._thrown_weapon(check)
        if used is None:
            return []
        sheet = self._sheet_for(check.actor)
        thrown = [dict(t) for t in sheet.get("thrown") or []]
        if used.needs_ammo:
            left = max(0, int(sheet.get("ammo", 0)) - 1)
            pile = next((t for t in thrown if t["name"] == used.name and not t.get("item")), None)
            if pile is None:
                thrown.append({"name": used.name, "ammo": 1})
            else:
                pile["ammo"] = int(pile.get("ammo", 0)) + 1
            return [SendMessage(channel_id, f"**{check.actor}** throws one — {left} left."),
                    SpendAmmo(channel_id, check.actor, left),
                    UpdateSheet(channel_id, check.actor, {"thrown": thrown})]
        gear = [dict(g) for g in sheet["gear"]]
        at = next((i for i, g in enumerate(gear) if g.get("name") == used.name), None)
        if at is None:
            return []
        # Kept, stats and magic and all, until it's picked up -- or left behind.
        thrown.append({"name": used.name, "item": gear.pop(at)})
        return [SendMessage(channel_id, f"**{check.actor}**'s {used.name.lower()} is thrown — gone until "
                                        "it's recovered."),
                UpdateSheet(channel_id, check.actor, {"gear": gear, "thrown": thrown, "armor": gear_armor(
                    gear, str(sheet.get("grip") or ""))})]

    def _banners(self, check: PendingCheck, dice: Roll, said: str) -> Tuple[Roll, str]:
        """Call the Banners: "When you recruit from your stronghold or the lands it protects you
        cannot get a 6-, treat that as a 7-9." Here, in the steading that is their stronghold."""
        if "no_miss" in check.flags and dice.total < 7:
            lifted = Roll(spec=dice.spec, faces=dice.faces, modifier=7 - sum(dice.faces))
            return lifted, said + " (Healthy Distrust: against mortal magic a 6- is a 7–9)"
        here = self._steading()
        sheet = self._sheet_or_empty(check.actor)
        if (check.move != "recruit" or dice.total >= 7 or not advanced.has(sheet, "Call the Banners")
                or here is None or steadings.slug(here["name"]) != sheet.get("stronghold")):
            return dice, said
        lifted = Roll(spec=dice.spec, faces=dice.faces, modifier=7 - sum(dice.faces))
        return lifted, said + " (Call the Banners: no miss at their stronghold, a 7–9)"

    def _barbarian_situation(self, move: str, actor: str, appetite: bool, movement: bool,
                             mortal_magic: bool) -> str:
        """Why a Barbarian flag on make_move does not apply, or ""."""
        sheet = self._sheet_or_empty(actor)
        if appetite and not (sheet.get("choices") or {}).get("appetite"):
            return f"{actor} has no Herculean Appetites to chase."
        if movement and (move != "defy_danger" or not advanced.has(sheet, "On the Move")):
            return f"{actor} has no move for defying danger caused by movement (On the Move)."
        if mortal_magic and (move != "defy_danger" or not advanced.has(sheet, "Healthy Distrust")):
            return f"{actor} has no Healthy Distrust of mortal magic."
        return ""

    def _roll_move(self, check: PendingCheck) -> Roll:
        """The move's dice: 2d6, or a Barbarian's 1d6+1d8, kept as two faces, d6 first."""
        if check.dice == APPETITE_DICE:
            d6, d8 = self.dice.roll("1d6"), self.dice.roll("1d8")
            return Roll(spec=APPETITE_DICE, faces=(*d6.faces, *d8.faces), modifier=check.modifier)
        return self.dice.roll(check.dice, modifier=check.modifier)

    def _compendium_situation(self, move: str, actor: str, weapon_leverage: bool, dragon_nature: bool,
                              hurl: bool) -> str:
        """Why a compendium flag on make_move does not apply, or ""."""
        sheet = self._sheet_or_empty(actor)
        if weapon_leverage and (move != "parley" or not advanced.has(sheet, "Its Name Is Dread")):
            return f"{actor} cannot parley with their weapon (Its Name Is Dread)."
        if dragon_nature and (move != "parley" or not advanced.has(sheet, "Wake the Dragon")):
            return f"{actor} cannot parley with dragon nature (Wake the Dragon)."
        if hurl and (move != "volley" or not advanced.has(sheet, "Velocitas Eradico")):
            return f"{actor} cannot hurl objects with a volley (Velocitas Eradico)."
        return ""

    def _intervened(self, check: PendingCheck, dice: Roll) -> Tuple[Roll, str]:
        """Protector, Intervene: "you may take +1 from their aid. If you do you cannot get a
        10+ result, a 10+ instead counts as a 7-9." Held to 9, and said so."""
        if not check.aide or check.move != "defy_danger" or dice.total < 10:
            return dice, ""
        held = Roll(spec=dice.spec, faces=dice.faces, modifier=9 - sum(dice.faces))
        return held, f" ({check.aide}'s help: a 10+ counts as 7–9)"

    def _aide(self, move: str, name: str) -> Tuple[Optional[Dict[str, Any]], str, str]:
        """(the hireling, the skill that helps, why not) for a hireling aiding this move:
        a Warrior on an attack, a Protector on Defy Danger."""
        for _, sheet in self._party():
            found = hirelings.find(sheet, name)
            if found is None:
                continue
            if MOVES[move].attacks and hirelings.skill(found, "Warrior"):
                return found, "Warrior", ""
            if move == "defy_danger" and hirelings.skill(found, "Protector"):
                return found, "Protector", ""
            return None, "", (f"{found['name']} adds nothing to {MOVES[move].name}: a Warrior aids "
                              "attacks, a Protector helps Defy Danger.")
        return None, "", f"No hireling named {name!r} is with the party."

    def _hireling_named(self, name: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """(employer, hireling) for a hireling anywhere in the party."""
        for actor, sheet in self._party():
            found = hirelings.find(sheet, name)
            if found is not None:
                return actor, found
        return None, None

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
                        f"The Upper Hand: {check.actor} makes an offer to Death in return for their "
                        "life, not the other way round. Describe what they glimpse beyond the Black "
                        "Gates and ask the player what they offer. If Death accepts he returns them "
                        "to life; if not, they die -- the player chooses next whether to stand by it."
                        if starting_move(self._sheet_or_empty(check.actor), "The Upper Hand") else
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
        ahead = self._forwards_from(channel_id, check, dice)
        staked = self._counterspell(channel_id, check, dice)
        outcome = replace(resolve(check, dice, picks=check.picks, auto_chosen=auto_chosen),
                          healing=self._healing(check, dice), spell=spell, shape=shape,
                          notes=self._advanced_notes(check, dice))
        return [*self._consequences(channel_id, outcome), *cast, *shift, *guard, *ahead, *staked,
                *self._resolved(channel_id, outcome)]

    def _advanced_notes(self, check: PendingCheck, dice: Roll) -> Tuple[str, ...]:
        """What an advanced move adds to a result that Gary does not track itself."""
        sheet = self._sheet_or_empty(check.actor)
        notes: List[str] = []
        if check.move == "supply" and self._steading() is None:
            notes.append(NO_STEADING)
        if check.dice == APPETITE_DICE and len(dice.faces) == 2 and dice.faces[0] > dice.faces[1]:
            notes.append("Herculean Appetites: the d6 is the higher die, so also introduce a complication "
                         "or danger that comes of their heedless pursuit.")
        if check.move == "hack_and_slash" and dice.total >= 12 and advanced.has(sheet, "Smash!"):
            notes.append("Smash!: they deal their damage and choose something physical the target has (a weapon, "
                         "their position, a limb): it loses it.")
        if check.move == "discern_realities" and band(dice) != "miss" and advanced.has(sheet, "Eye for Weakness"):
            notes.append("Eye for Weakness: \u201cWhat here is weak or vulnerable?\u201d is on their list too -- "
                         "if they ask it, answer it as one of their questions.")
        if check.move == "defy_danger" and dice.total >= 12 and advanced.has(sheet, "The One Who Knocks"):
            notes.append("The One Who Knocks: they turn the danger back on itself -- describe how.")
        if check.move == "what_are_you_waiting_for" and dice.total >= 10:
            notes.append("Gary adds their +2 damage against the creatures standing now; they ignore the "
                         "companions and come at the challenger.")
        if check.move == "trap_expert":
            if advanced.has(sheet, "Extremely Cautious"):
                notes.append("Extremely Cautious: +1 hold, even on a 6-. On a 12+ they get 3 hold, and the next "
                             "time they come near a trap you tell them at once what it does, what triggers it, "
                             "who set it and how they can use it.")
            elif advanced.has(sheet, "Cautious"):
                notes.append("Cautious: +1 hold, even on a 6-.")
        if check.move == "cast_a_spell" and dice.total >= 10:
            # Empower (Cleric) / Empowered Magic (Wizard), and their Greater forms.
            greater = next((n for n in ("Greater Empower", "Greater Empowered Magic") if advanced.has(sheet, n)), None)
            lesser = next((n for n in ("Empower", "Empowered Magic") if advanced.has(sheet, n)), None)
            boost = ("effects doubled or targets doubled" if (greater or lesser) != "Empowered Magic"
                     else "effects maximized or targets doubled")
            if greater and dice.total >= 12:
                notes.append(f"{greater}, 12+: they may choose one -- {boost} -- for free. Ask them.")
            elif greater or lesser:
                notes.append(f"{greater or lesser}: they may take one of the 7-9 costs to also have {boost}. "
                             "Ask them; if they do, apply the cost and double (or maximize) what the spell does "
                             "yourself -- Gary rolled it once.")
        if check.move == "defend" and dice.total >= 12 and advanced.has(sheet, "Impervious Defender"):
            notes.append("Impervious Defender, 12+: instead of hold, the nearest attacking creature is "
                         "stymied, giving them a clear advantage. Describe it.")
        return tuple(notes)

    def _counterspell(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        """Counterspell, 7-9: "the spell is countered and you forget the spell you staked"."""
        if check.move != "counterspell" or band(dice) != "weak" or not check.spell:
            return []
        sheet = self._sheet_or_empty(check.actor)
        return [SendMessage(channel_id, f"✨ **{check.actor}** forgets {check.spell} — staked on the defense."),
                UpdateSheet(channel_id, check.actor, {"lost": [*(sheet.get("lost") or []), check.spell]})]

    def _guard(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        """Defend: hold 3 on a 10+, 1 on a 7-9, against attacks on the defender or their
        charge, for as long as they stand in defense."""
        if check.move != "defend" or not check.target:
            return []
        defender = self._sheet_or_empty(check.actor)
        if dice.total >= 12 and advanced.has(defender, "Impervious Defender"):
            return [SendMessage(channel_id, f"🛡️ **{check.actor}** stands in defense of {check.target} — "
                                            "the nearest attacker is stymied (Impervious Defender).")]
        staunch = advanced.has(defender, "Staunch Defender") or advanced.has(defender, "Impervious Defender")
        if band(dice) == "miss" and not staunch:
            return []
        # Staunch Defender: "you always get +1 hold, even on a 6-".
        hold = {"strong": 3, "weak": 1, "miss": 0}[band(dice)] + (1 if staunch else 0)
        return [
            SendMessage(channel_id, f"🛡️ **{check.actor}** stands in defense of {check.target} — hold {hold}."),
            UpdateSheet(channel_id, check.actor, {"defending": {"charge": check.target, "hold": hold}}),
        ]

    def _shift(self, channel_id: int, check: PendingCheck, dice: Roll) -> Tuple[List[Effect], Optional[Dict[str, Any]]]:
        """Shapeshifter: take the form, with 3, 2 or 1 hold -- even a miss holds 1."""
        if check.move != "shapeshifter":
            return [], None
        hold = _SHAPE_HOLD[band(dice)]
        if advanced.has(self._sheet_or_empty(check.actor), "Embracing No Form"):
            hold += self.dice.roll("1d4").total  # "roll 1d4 and add that total to your hold"
        shape = {"form": check.target, "hold": hold, "moves": []}
        for flag in check.flags:  # Formshaper's boon, Formcrafter's stats
            key, _, value = flag.partition(":")
            if key in ("boon", "plus", "minus"):
                shape[key] = value
        facts = {**shape, "instruction": (
            f"Describe {check.actor} becoming a {check.target}: its innate abilities and "
            f"weaknesses are theirs, though they keep their own stats. "
            + (f"Their land is {land}: an animal that might live there is theirs to take. "
               if (land := ((self._sheet_or_empty(check.actor)).get("choices") or {}).get("land")) else "")
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
        """The spell being cast. Only Cast a Spell casts: Counterspell's spell is staked."""
        if not check.spell or check.move != "cast_a_spell" or self._sheet_for is None:
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
        if spell.name == "Consume Unlife":
            return self._consume_unlife(channel_id, check, sheet, facts)
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
        return (self._sheet_or_empty(check.actor)).get("companion")

    def _ferocity(self, check: PendingCheck) -> int:
        """Command: "...and you attack the same target, add its ferocity to your damage.\""""
        pet = self._companion_of(check)
        rule = companions.COMMANDS.get(check.move)
        if pet and pet.get("spent"):
            return 0  # Man's Best Friend: it took a blow and its ferocity is 0 until a rest
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
        healer = self._sheet_or_empty(check.actor)
        other = normalize_actor(patient["name"]) != normalize_actor(check.actor)
        for spec_more, why in advanced.healing_dice(healer, check.move, check.spell, other):
            more = int(spec_more) if spec_more.isdigit() else self.dice.roll(spec_more).total
            amount, render = amount + more, f"{render}, +{spec_more} {why}: {more}"
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
                mod = stat_modifier(sheet, rules.stat) - (1 if clumsy(sheet) else 0)
        others = [s for a, s in self._party() if normalize_actor(a) != normalize_actor(actor)]
        if move == "take_watch" and any(advanced.has(s, "A Safe Place") or advanced.has(s, "A Safer Place")
                                        for _, s in self._party()):
            mod += 1  # A Safe Place: "everyone takes +1 to take watch"
        if move == "last_breath" and any(advanced.has(s, "The Scales of Life and Death") for s in others):
            mod += 1  # "When someone takes their last breath in your presence, they take +1"
        if move == "last_breath" and starting_move(self._sheet_or_empty(actor), "The Upper Hand"):
            mod += 1  # The Upper Hand: "You take +1 ongoing to last breath rolls"
        if rules.stat != NOTHING:  # "no stat is added to the Last Breath roll" -- nor anything ongoing
            mod += self._ongoing(actor, move)[0]
        check, _ = self.registry.open(
            channel_id=channel_id, user_id=user_id, actor=actor, move=move,
            stat=rules.stat, modifier=mod, stakes="", dice=MOVE_DICE,
        )
        with_what = ("+nothing" + (f" {mod:+d}" if mod else "")) if rules.stat == NOTHING else \
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

        if any(rules.choice.options[p].spends_ammo for p in picks) and self._thrown_weapon(check) is not None:
            # Thrown: "If you volley with this weapon, you can't choose to mark off ammo on a 7-9."
            return [SendMessage(channel_id, f"**{check.actor}**, a thrown weapon can't take several shots — "
                                            "it's gone once it's thrown. Choose another.")]
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
            total, shown = self._serpents_tears(check.target, die, total, shown, self._damage_extra(check, dice))
            if self._magic_weapon(check):
                bonus = self.dice.roll("1d4").total
                total, shown = total + bonus, f"{shown}, +1d4 Magic Weapon: {bonus} = {total + bonus}"
            ferocity = self._ferocity(check)
            if ferocity:
                total, shown = total + ferocity, f"{shown}, +{ferocity} {check.companion} = {total + ferocity}"
            # Warrior, Man-at-arms: "add their skill to the damage done".
            _, warrior = self._hireling_named(check.aide) if check.aide else (None, None)
            might = hirelings.skill(warrior, "Warrior") if warrior else 0
            if might:
                total, shown = total + might, f"{shown}, +{might} {warrior['name']} = {total + might}"
            sheet = self._sheet_for(check.actor) if self._sheet_for is not None else None
            weapon = self._weapon(sheet, check.move, check) if sheet and "gear" in sheet else None
            for dice_spec, why in advanced.damage_dice(sheet or {}, check.move, weapon.tags if weapon else ()):
                extra = int(dice_spec) if dice_spec.isdigit() else self.dice.roll(dice_spec).total
                total, shown = total + extra, f"{shown}, +{dice_spec} {why}: {extra} = {total + extra}"
            total, shown = self._sworn_damage(sheet or {}, check.target, total, shown)
            total, shown = self._challenge_damage(sheet or {}, check.target, total, shown)
            total, shown, spent_marks = self._use_marks(channel_id, check.actor, check.target, total, shown)
            effects.extend(spent_marks)
            if (sheet or {}).get("fury"):
                effects.append(UpdateSheet(channel_id, check.actor, {"fury": None}))  # spent on this attack
            if "two_weapons" in check.flags:
                # Viper's Strike: "add an extra 1d4 damage for your off-hand strike" (Fangs: 1d8).
                fangs = advanced.has(sheet or {}, "Viper\u2019s Fangs")
                off = self.dice.roll("1d8" if fangs else "1d4").total
                total, shown = total + off, f"{shown}, +{'1d8' if fangs else '1d4'} off-hand: {off} = {total + off}"
            # A damage forward (Arcane Art's +1d4) goes on the next damage they deal.
            boosts = [f for f in (sheet or {}).get("forwards") or () if f.get("damage")]
            for boost in boosts:
                extra = (int(boost["damage"]) if str(boost["damage"]).isdigit()
                         else self.dice.roll(boost["damage"]).total)
                total, shown = total + extra, f"{shown}, +{boost['damage']} forward: {extra} = {total + extra}"
            if boosts:
                effects.append(UpdateSheet(channel_id, check.actor, {
                    "forwards": [f for f in sheet["forwards"] if not f.get("damage")]}))
        line = f"🎲 **{check.actor}**{' (rolled for them)' if auto else ''} damage — {shown}"
        hits = []
        spread = (spell is not None and spell.area) or "spread" in check.flags  # Blot Out the Sun
        for name in check.target.split(", ") if spread else [check.target]:
            hurt, facts, said = self._hurt(channel_id, name, total, piercing, ignores)
            effects.extend(hurt)
            hits.append(facts)
            line += said
            effects.extend(self._oath_kept(channel_id, check.actor, name, facts))
        effects.extend(self._after_strike(channel_id, check.actor, hits))
        facts = hits[0] if len(hits) == 1 else {"rolled": total, "targets": hits}
        effects.insert(0, SendMessage(channel_id, line))

        effects.extend(self._spend_ammo(channel_id, check, dice))
        cast, spell_facts = self._cast(channel_id, check, dice)
        effects.extend(cast)
        effects.extend(self._forwards_from(channel_id, check, dice))
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
        if (outcome.enemy_attacks and check.move == "hack_and_slash" and dice.total >= 12
                and advanced.has(self._sheet_or_empty(check.actor), "Superior Warrior")):
            # "When you hack and slash on a 12+ you deal your damage, avoid their attack, and
            # impress, dismay, or frighten your enemy."
            outcome = replace(outcome, enemy_attacks=False)
            facts["superior_warrior"] = (f"A 12+: {check.actor} avoids the counter-attack and impresses, "
                                         "dismays or frightens their enemy. Narrate it.")
        _, warrior = self._hireling_named(check.aide) if check.aide and spell is None else (None, None)
        if outcome.enemy_attacks and target is not None and warrior and hirelings.skill(warrior, "Warrior"):
            # "If your attack results in consequences (like a counter attack) the man-at-arms
            # takes the brunt of it." Hirelings keep no HP: the model narrates what it costs them.
            effects.append(SendMessage(channel_id, f"🛡️ {warrior['name']} takes the brunt of "
                                                   f"{check.target}'s counter-attack."))
            facts["counter_attack"] = (f"{warrior['name']} (a hireling) took the brunt of the "
                                       "counter-attack instead; narrate what it cost them.")
        elif outcome.enemy_attacks and target is not None and any(f.startswith("minion:") for f in check.flags):
            # A slime's attack draws the blow back onto the slime.
            slime = next(f.split(":", 1)[1] for f in check.flags if f.startswith("minion:"))
            attack_effects, back = self._hit_minion(channel_id, check.target, check.actor, slime)
            effects.extend(attack_effects)
            back = {**back, "minion": slime}
        elif outcome.enemy_attacks and target is not None:
            # The trade is simultaneous: on a 7-9 the enemy's blow lands even if yours
            # finished it. The rules say both happen, so both do.
            attack_effects, back = self._attack(channel_id, check.target, target, check.actor,
                                                extra_armor=self._companion_armor(check))
            effects.extend(attack_effects)

        outcome = replace(outcome, damage=facts, enemy_attack=back, spell=spell_facts)
        if back and back.get("down"):
            dying = self._trigger(channel_id, check.actor, "last_breath")
        return [*effects, *self._resolved(channel_id, outcome), *dying]

    # -- Exterminatus ---------------------------------------------------------------------------

    def _is_sworn(self, sheet: Dict[str, Any], target: str) -> bool:
        """Is ``target`` (a creature instance) the enemy they swore to defeat -- by its name,
        or by its kind when they swore against "the Dragon"?"""
        def bare(name: str) -> str:
            return normalize_actor(re.sub(r"^\s*(the|a|an)\s+", "", (name or "").lower()))

        sworn = bare(sheet.get("sworn_enemy"))
        if not sworn or not target:
            return False
        record = self._monsters().get(target) or {}
        return sworn in (bare(target), bare(record.get("kind", "")))

    def _challenge_damage(self, sheet: Dict[str, Any], target: str, total: int, shown: str) -> Tuple[int, str]:
        """What Are You Waiting For?: on a 10+, "take +2 damage ongoing against them" -- the
        creatures standing when they cried out the challenge."""
        if not target or target not in (sheet.get("challenged") or ()):
            return total, shown
        return total + 2, f"{shown}, +2 challenged = {total + 2}"

    def _sworn_damage(self, sheet: Dict[str, Any], target: str, total: int, shown: str) -> Tuple[int, str]:
        """Exterminatus: "+2d4 damage against that enemy and -4 damage against anyone else"."""
        if not sheet.get("sworn_enemy") or not target:
            return total, shown
        if self._is_sworn(sheet, target):
            extra = self.dice.roll("2d4").total
            return total + extra, f"{shown}, +2d4 sworn enemy: {extra} = {total + extra}"
        return max(0, total - 4), f"{shown}, -4 not the sworn enemy = {max(0, total - 4)}"

    def _oath_kept(self, channel_id: int, actor: str, target: str, facts: Dict[str, Any]) -> List[Effect]:
        """"This effect lasts until the enemy is defeated." Defeating it ends the oath."""
        sheet = self._sheet_or_empty(actor)
        if not facts.get("defeated") or not self._is_sworn(sheet, target):
            return []
        return [SendMessage(channel_id, f"⚔️ **{actor}** has kept their promise: {target} is defeated."),
                UpdateSheet(channel_id, actor, {"sworn_enemy": None})]

    def mark_xp(
        self, *, channel_id: int, character: str, reason: str, session: int
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """XP a move gives for answering the GM -- the Barbarian's Outsider: "At the beginning of
        each session, the GM will ask you something about your homeland... If you answer them,
        mark XP." Once a session."""
        from .opening import outsiders

        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        if str(reason or "").strip().lower() != "outsider" or not outsiders([sheet]):
            return [], {"ok": False, "error": f"mark_xp is for the Outsider's answer; {actor} is not an Outsider. "
                                              "Other XP comes from misses and the end of session."}
        if int(sheet.get("outsider_xp", 0)) >= int(session):
            return [], {"ok": False, "error": f"{actor} has already marked XP for answering this session."}
        xp = int(sheet.get("xp", 0)) + 1
        return [SendMessage(channel_id, f"⭐ **{actor}** marks XP for speaking of their homeland ({xp})."),
                UpdateSheet(channel_id, actor, {"xp": xp, "outsider_xp": int(session)})], {"ok": True, "xp": xp}

    def resolve_appetite(
        self, *, channel_id: int, character: str, appetite: str, new: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """More! Always More!: an appetite satisfied to the extreme is resolved -- crossed off,
        1 XP -- and a new one takes its place, from the list or in their own words."""
        from .choices import appetites_of

        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        if not advanced.has(sheet, "More! Always More!"):
            return [], {"ok": False, "error": f"{actor} does not have More! Always More!"}
        have = appetites_of(sheet)
        old = next((a for a in have if a.lower() == str(appetite or "").strip().lower()), None)
        if old is None:
            return [], {"ok": False, "error": f"{actor}'s appetites: {', '.join(have) or 'none'}."}
        new = " ".join(str(new or "").split())[:60]
        if not new or new.lower() in (a.lower() for a in have):
            return [], {"ok": False, "error": "Name the new appetite (new): one from the list or their own words, "
                                              "not one they have."}
        xp = int(sheet.get("xp", 0)) + 1
        return [SendMessage(channel_id, f"🍖 **{actor}** has had their fill of {old.lower()} — resolved, +1 XP "
                                        f"({xp}). Their new appetite: {new.lower()}."),
                UpdateSheet(channel_id, actor, {
                    "xp": xp, "resolved_appetites": [*(sheet.get("resolved_appetites") or ()), old],
                    "more_appetites": [*(sheet.get("more_appetites") or ()), new]})], {"ok": True, "xp": xp}

    def swear_enemy(
        self, *, channel_id: int, character: str, enemy: str = "", released: bool = False, reason: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Exterminatus: the promise spoken aloud -- or, released=true, the failure redeemed."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        if not advanced.has(sheet, "Exterminatus"):
            return [], {"ok": False, "error": f"{actor} does not have Exterminatus."}
        if released:
            if not sheet.get("sworn_enemy"):
                return [], {"ok": False, "error": f"{actor} has sworn against no one."}
            reason = " ".join(str(reason or "").split())
            if not reason:
                return [], {"ok": False, "error": "Say how they redeemed themselves (reason)."}
            return [SendMessage(channel_id, f"⚔️ **{actor}** is released from their promise against "
                                            f"{sheet['sworn_enemy']} — {reason}."),
                    UpdateSheet(channel_id, actor, {"sworn_enemy": None})], {"ok": True}
        if sheet.get("sworn_enemy"):
            return [], {"ok": False, "error": (f"{actor} is already sworn against {sheet['sworn_enemy']}: it lasts "
                                               "until that enemy is defeated, or they redeem a failure.")}
        enemy = " ".join(str(enemy or "").split())[:80]
        if not enemy:
            return [], {"ok": False, "error": "Name the enemy they promise to defeat."}
        here = next((n for n in self._monsters() if normalize_actor(n) == normalize_actor(enemy)), None)
        enemy = here or enemy
        return [SendMessage(channel_id, f"⚔️ **{actor}** swears aloud to defeat {enemy}: +2d4 damage against "
                                        "it, -4 against anyone else, until it falls."),
                UpdateSheet(channel_id, actor, {"sworn_enemy": enemy})], {
            "ok": True, "sworn": enemy, "note": (
                "Gary applies the damage. If the enemy escapes or they give up the fight, the oath "
                "holds until they redeem themselves -- then swear_enemy with released=true.")}

    def _consume_unlife(self, channel_id: int, check: PendingCheck, sheet: Dict[str, Any],
                        facts: Dict[str, Any]) -> Tuple[List[Effect], Dict[str, Any]]:
        """Consume Unlife: "The mindless undead creature you touch is destroyed and you steal its
        death energy to heal yourself ... The amount of damage healed is equal to the HP that the
        creature had remaining before you destroyed it." Healing the caster; whether the creature
        is mindless undead is the model's to have judged before it was cast."""
        creature = self._monsters().get(check.target) or {}
        left = max(0, int(creature.get("hp", 0)))
        healed = min(left, int(sheet["max_hp"]) - int(sheet["hp"]))
        hp = int(sheet["hp"]) + healed
        facts.update(destroyed=check.target, healed=healed, note=(
            "Destroyed and drained, as rolled. If they would rather heal the next ally they touch, "
            "narrate it -- the HP went to the caster."))
        return [SendMessage(channel_id, f"✨ **{check.target}** is consumed — **{check.actor}** heals {healed}, "
                                        f"HP {hp}/{sheet['max_hp']}."),
                ChangeHp(channel_id, "monster", check.target, 0),
                ChangeHp(channel_id, "character", check.actor, hp)], facts

    def _serpents_tears(self, target: str, die: str, total: int, shown: str,
                        extra: Optional[str] = None) -> Tuple[int, str]:
        """Serpent's Tears: "Anyone dealing damage against the target rolls twice and takes
        the better result"."""
        creature = self._monsters().get(target or "") or {}
        if "Serpent\u2019s Tears" not in (creature.get("afflictions") or ()):
            return total, shown
        again = damage.roll(self.dice, damage.parse(die), extra)
        if again.total <= total:
            return total, f"{shown} (Serpent's Tears: rolled again, {again.total})"
        return again.total, f"{again.render()} (Serpent's Tears: rolled again, better than {total})"

    def _cloudkill(self) -> bool:
        """Is anyone's Cloudkill running? Gary cannot see who stands in the fog, so while it
        lasts it counts for every creature in the scene; the model ends it when it no longer
        covers them (end_spell)."""
        return any(s.name == "Cloudkill" for _, sheet in self._party() for s, _ in spells.running(sheet))

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
        cloud = ""
        if dealt > 0 and self._cloudkill():
            # Cloudkill: "Whenever a creature in the area takes damage it takes an additional,
            # separate 1d6 damage which ignores armor."
            fog = self.dice.roll("1d6").total
            dealt, cloud = dealt + fog, f", +{fog} Cloudkill"
        after = int(target["hp"]) - dealt
        facts.update(armor=target["armor"], dealt=dealt, hp_before=target["hp"],
                     hp_after=max(after, 0), defeated=after <= 0,
                     severity=damage.severity(dealt, target.get("max_hp", target["hp"]), after))
        armor = ("ignores armor" if ignores_armor else f"armor {target['armor']}") + cloud
        said = (f" → **{name}** takes {_damage_words(dealt)} ({armor}), "
                f"HP {target['hp']} → {max(after, 0)}" + (" — **defeated**." if after <= 0 else "."))
        return [ChangeHp(channel_id, "monster", name, after)], facts, said

    def _weapon(self, sheet: Dict[str, Any], move: str, wanted: str = ""):
        """The weapon this attack uses: the one they named, else the best that fits."""
        flags: Tuple[str, ...] = ()
        if isinstance(wanted, PendingCheck):
            flags = wanted.flags
            wanted = next((f[len("weapon:"):] for f in wanted.flags if f.startswith("weapon:")), "")
        thrown = next((f[len("thrown:"):] for f in (flags or ()) if f.startswith("thrown:")), "")
        if thrown:
            return Item.from_dict(json.loads(thrown))  # already in the air
        if wanted:
            from .gear import weapon_named

            named = weapon_named(sheet.get("gear") or [], wanted, MOVES[move].ranged, int(sheet.get("ammo", 0)))
            if named is not None:
                return named
        return weapon_for(sheet.get("gear") or [], ranged=MOVES[move].ranged,
                          ammo=int(sheet.get("ammo", 0)), grip=str(sheet.get("grip") or ""))

    def _damage_die(self, check: PendingCheck) -> Tuple[str, int, str]:
        """(damage expression, piercing, weapon name) for this attack.

        The class's die, plus the weapon's +damage, with its piercing. A sheet with no
        gear -- made before gear existed -- rolls the bare class die, as it always did.
        """
        spell = self._spell_of(check)
        if spell is not None and spell.damage:
            return spell.damage, 0, spell.name + (", ignores armor" if spell.ignores_armor else "")
        if MOVES[check.move].damage_die:
            # "If a move specifies an amount of damage, use that in place of the class's damage roll."
            return MOVES[check.move].damage_die, 0, MOVES[check.move].name
        slime = next((f.split(":", 1)[1] for f in check.flags if f.startswith("minion:")), "")
        if slime:
            pet = slimes.find(self._sheet_or_empty(check.actor), slime) or {}
            if not pet.get("copy"):
                return slimes.MINION_DAMAGE, 0, slime  # "They have d4 damage"
        sheet = self._sheet_for(check.actor) if self._sheet_for is not None else None
        if not sheet:
            return "d6", 0, ""
        die = advanced.shaped_die(sheet, magic.damage_die(sheet) or sheet["damage"])
        # Halfling Thief: "When you attack with a ranged weapon, deal +2 damage."
        knack = 2 if (MOVES[check.move].ranged and sheet.get("class") == "Thief"
                      and sheet.get("race") == "Halfling") else 0
        weapon = self._weapon(sheet, check.move, check) if "gear" in sheet else None
        spec = damage.parse(die)
        bonus = knack + (weapon.damage if weapon is not None else 0)
        boosted = str(damage.DamageSpec(spec.sides, spec.mode, spec.modifier + bonus)) if bonus else die
        # Smaug's Belly: "When you know your target's weakest point your arrows have 2 piercing."
        weak = 2 if "weak_point" in check.flags else 0
        # Wraithlander's Arm: "It gains +2 piercing against living creatures" -- on top of the weapon's.
        arm = 2 if ("Arm" in (sheet.get("wraith_marks") or ())
                    and _living(self._monsters().get(check.target))) else 0
        arm += 1 if sheet.get("fury") else 0  # Fury: "your next attack also becomes messy, forceful, and +1 piercing"
        if weapon is None:
            return boosted, weak + arm, ""
        return boosted, max(weapon.piercing, weak) + arm, weapon.name

    def _spend_ammo(self, channel_id: int, check: PendingCheck, dice: Roll) -> List[Effect]:
        if "hurl" in check.flags:
            return []  # Velocitas Eradico throws whatever is to hand, not arrows
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
        self, *, channel_id: int, monster: str, character: str, also: Sequence[str] = (),
        protector: str = "", cover: int = 0, minion: str = "",
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """A creature deals its damage to a character -- the GM move "deal damage", on a
        miss or when everyone looks to the GM.

        Whether it happens is the model's call. How much is not: Gary rolls the creature's
        damage, takes the character's armor off, and reports the result as fact.

        ``also``: more creatures attacking the same character at once, by the book's "Damage
        From Multiple Creatures": the highest damage among them, rolled once, +1 for each
        attacker beyond the first. ``minion``: the blow falls on one of the character's slime
        minions instead (Blob).

        Returns (effects, the tool result for the model).
        """
        scene = self._monsters()
        if minion:
            return self._hit_minion(channel_id, monster, character, minion)
        standing = {n.lower(): n for n, r in scene.items() if r["hp"] > 0}
        names = []
        for wanted in [monster, *also]:
            found = standing.get(str(wanted or "").strip().lower())
            if found is None:
                listing = ", ".join(sorted(standing.values())) or "none"
                return [], {"ok": False, "error": f"No standing creature named {wanted!r}. "
                                                  f"Standing: {listing}."}
            if found not in names:
                names.append(found)
        # "If multiple creatures attack at once roll the highest damage among them and add +1
        # damage for each monster beyond the first." The one with the highest damage leads: its
        # die, piercing and armor-breaking count.
        def strength(n: str) -> Tuple[int, int]:
            spec = damage.parse(scene[n]["damage"])
            return spec.sides, spec.modifier

        name = max(names, key=strength)
        hit, pack = None, ""
        if len(names) > 1:
            spec = damage.parse(scene[name]["damage"])
            extra = len(names) - 1
            hit = damage.roll(self.dice, damage.DamageSpec(spec.sides, spec.mode, spec.modifier + extra))
            pack = (f"{_and(names)} attack together — {name}'s {spec}, +{extra} for "
                    f"{'the other' if extra == 1 else f'the {extra} others'}.")
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
            effects, facts = self._hold_attack(channel_id, name, scene[name], character, defender, hit=hit)
            return ([SendMessage(channel_id, pack)] if pack else []) + effects, facts
        # Protector, Sentry: "When a protector stands between you and an attack you increase your
        # armor against that attack by the defender's skill, then reduce their skill by 1 until
        # they receive healing or have time to mend."
        guard_armor, strained = 0, []
        if protector:
            employer, guard = self._hireling_named(protector)
            if guard is None or not hirelings.skill(guard, "Protector"):
                return [], {"ok": False, "error": f"No protector named {protector!r} is with the party."}
            guard_armor = hirelings.skill(guard, "Protector")
            esheet = self._sheet_for(employer)
            strained = [SendMessage(channel_id, f"🛡️ {guard['name']} steps in: +{guard_armor} armor."),
                        UpdateSheet(channel_id, employer, {"hirelings": [
                            {**h, "strain": int(h.get("strain", 0)) + 1} if h is guard or h["name"] == guard["name"] else h
                            for h in esheet["hirelings"]]})]
        # "Temporary or circumstantial armor works the same way as armor that you wear: 1 armor
        # for partial cover, 2 armor for major cover."
        effects, facts = self._attack(channel_id, name, scene[name], character, hit=hit,
                                      extra_armor=guard_armor + max(0, min(2, int(cover or 0))))
        effects = strained + effects
        if pack:
            effects.insert(0, SendMessage(channel_id, pack))
            facts["attackers"] = names
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

    def deal_damage(
        self, *, channel_id: int, character: str, target: str, ranged: bool = False, reason: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """A character just deals their damage, no move: the fiction gave them the blow -- the
        enemy is helpless, surprised, unaware, pinned. "Attacking a helpless or surprised
        enemy -- that's not Hack & Slash, they just get to deal damage." Their weapon must
        reach, as for any attack; the damage is rolled and applied like a hit."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        unable = _unable(actor, sheet)
        if unable:
            return [], {"ok": False, "error": unable}
        standing = {n.lower(): n for n, r in self._monsters().items() if r["hp"] > 0}
        name = standing.get(str(target or "").strip().lower())
        if name is None:
            return [], {"ok": False, "error": f"No standing creature named {target!r}. "
                                              f"Standing: {', '.join(sorted(standing.values())) or 'none'}."}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": "Say why no roll is needed -- what leaves the enemy "
                                              "open (reason)."}
        move = "volley" if ranged else "hack_and_slash"
        if "gear" in sheet and self._weapon(sheet, move) is None:
            needs = "a near, far or thrown weapon" if ranged else "a hand, close or reach weapon"
            return [], {"ok": False, "error": f"{actor} has nothing to strike with: it needs {needs}."}
        blow = PendingCheck(check_id="", channel_id=channel_id, user_id=0, actor=actor, move=move,
                            stat="", modifier=0, stakes="", dice="", created_at=0, expires_at=0, target=name)
        die, piercing, weapon = self._damage_die(blow)
        hit = damage.roll(self.dice, damage.parse(die))
        total, shown = hit.total, hit.render()
        total, shown = self._serpents_tears(name, die, total, shown)
        weapon_now = self._weapon(sheet, move) if "gear" in sheet else None
        for dice_spec, why in advanced.damage_dice(sheet, move, weapon_now.tags if weapon_now else ()):
            extra = int(dice_spec) if dice_spec.isdigit() else self.dice.roll(dice_spec).total
            total, shown = total + extra, f"{shown}, +{dice_spec} {why}: {extra} = {total + extra}"
        total, shown = self._sworn_damage(sheet, name, total, shown)
        total, shown = self._challenge_damage(sheet, name, total, shown)
        total, shown, spent_marks = self._use_marks(channel_id, actor, name, total, shown)
        if self._magic_weapon(blow):
            bonus = self.dice.roll("1d4").total
            total, shown = total + bonus, f"{shown}, +1d4 Magic Weapon: {bonus} = {total + bonus}"
        effects, facts, said = self._hurt(channel_id, name, total, piercing, False)
        effects.extend(spent_marks)
        if sheet.get("fury"):
            effects.append(UpdateSheet(channel_id, actor, {"fury": None}))
        effects.extend(self._oath_kept(channel_id, actor, name, facts))
        effects.extend(self._after_strike(channel_id, actor, [facts]))
        with_what = f" with {weapon}" if weapon else ""
        effects.insert(0, SendMessage(channel_id, f"🗡️ **{actor}** strikes{with_what} — {reason}. "
                                                  f"No roll needed: {shown}{said}"))
        return effects, {"ok": True, **facts, "note": (
            "No move was rolled: the fiction gave them the blow. Narrate it landing, as bad as its "
            "severity; a creature not defeated is still in the fight.")}

    def _hold_attack(
        self, channel_id: int, name: str, creature: Dict[str, Any], character: str, defender: str,
        hit: Optional["damage.DamageRoll"] = None,
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The attack is rolled, then held while the defender decides how to spend hold."""
        if hit is None:
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
        elif choice.startswith("Open the attacker up") and normalize_actor(character) != normalize_actor(defender):
            effects += self._grant(channel_id, character, f"against {name}, opened up by {defender}")
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
        hazard: bool = False,
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """A blow landing on a character: a creature's (``name`` is it), or a hazard's
        (``name`` is what happened -- a fall, a trap, fire)."""
        sheet = self._sheet_for(character) if self._sheet_for is not None else None
        if hit is None:
            hit = damage.roll(self.dice, damage.parse(creature["damage"]))
        rolled, shown = hit.total, hit.render()
        if "Bloodweed" in (creature.get("afflictions") or ()):
            # "whenever the afflicted rolls damage, they roll an additional d4 and subtract that
            # result from their normal damage"
            weed = self.dice.roll("1d4").total
            rolled, shown = max(0, rolled - weed), f"{shown}, -1d4 Bloodweed: {weed} = {max(0, rolled - weed)}"
        armor = (int(sheet["armor"]) if sheet else 0) + extra_armor
        braced = (sheet or {}).get("armor_forward") or []
        armor += sum(int(b["amount"]) for b in braced)
        if sheet:
            standing = sum(1 for r in self._monsters().values() if r["hp"] > 0)
            more, _ = advanced.armor_bonus(sheet, outnumbered=standing > len(self._party()))
            armor += more
        dealt = damage.through_armor(
            rolled, armor, creature.get("piercing", 0), creature.get("ignores_armor", False)
        )
        if halve:
            dealt //= 2  # Defend: "halve its effect or damage"
        facts: Dict[str, Any] = {"attacker": name, "character": character,
                                 "rolled": rolled, "armor": armor, "dealt": dealt}
        line = f"🗡️ **{name}** hits **{character}** — {shown}"
        effects: List[Effect] = []
        if sheet is not None:
            after = int(sheet["hp"]) - dealt
            facts.update(hp_before=sheet["hp"], hp_after=max(after, 0), down=after <= 0,
                         severity=damage.severity(dealt, sheet.get("max_hp", sheet["hp"]), after))
            # The amount first, in words: "d6: 5 → 4 through armor 1" left the table to work
            # out that 4 was the damage.
            shield = "ignores armor" if creature.get("ignores_armor") else f"armor {armor}"
            line = (
                (f"💥 **{character}** is hurt — {name} — {_damage_words(dealt)} " if hazard else
                 f"🗡️ **{name}** hits **{character}** for {_damage_words(dealt)} ")
                + f"({shown}, {shield}) — HP {sheet['hp']} → {max(after, 0)}"
                + (" — **down**." if after <= 0 else ".")
            )
            if after <= 0:
                facts["dying"] = (
                    f"{character} is at 0 HP and dying. They roll Last Breath next: narrate "
                    "them falling, and leave whether they live open."
                )
            effects.append(ChangeHp(channel_id, "character", character, max(after, 0)))
            if braced:
                effects.append(UpdateSheet(channel_id, character, {"armor_forward": []}))  # spent on this blow
                facts["armor_forward"] = [b["reason"] for b in braced]
            # Unique across restarts (the time) and within one instant (the count).
            self._hits = getattr(self, "_hits", 0) + 1
            hit_id = f"{int(self.clock() * 1000):x}x{self._hits:x}"
            last_hit = {"id": hit_id, "by": name, "dealt": dealt, "down": after <= 0,
                        "lost": int(sheet["hp"]) - max(after, 0),  # overkill is not given back
                        # what a reroll needs (Human Fighter)
                        "hp_before": int(sheet["hp"]), "damage": creature["damage"], "armor": armor,
                        "piercing": creature.get("piercing", 0), "ignores": creature.get("ignores_armor", False),
                        "halve": halve}
            effects.append(UpdateSheet(channel_id, character, {"last_hit": last_hit}))
            struck = {**sheet, "hp": max(after, 0), "last_hit": last_hit}
            up = [n for n, r in self._monsters().items() if r["hp"] > 0]
            if any(reactions.options(a, struck if a == character else s, character, struck, up)
                   for a, s in self._party()):
                return [SendMessage(channel_id, line, prompt=ReactPrompt(hit_id)), *effects], facts
        return [SendMessage(channel_id, line), *effects], facts

    def hazard_damage(
        self, *, channel_id: int, characters: Sequence[str], severity: str, reason: str,
        ignores_armor: bool = False, cover: int = 0,
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Damage from no creature -- a fall, a trap, fire, a collapsing tower. "Other sources
        of damage ... are left to the GM based on these options": the model says how bad,
        Gary rolls it for each character caught, through their armor unless it ignores armor
        ("if the source of the damage is particularly large or if the damage comes from
        magic or poison"), with cover adding 1 or 2."""
        die = _HAZARDS.get(str(severity or "").strip().lower())
        if die is None:
            return [], {"ok": False, "error": f"severity is one of: {', '.join(_HAZARDS)}."}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": "Say what hurt them (reason): 'the floor gave way'."}
        cover = max(0, min(2, int(cover or 0)))
        effects: List[Effect] = []
        results: Dict[str, Any] = {}
        for who in characters or ():
            actor = self._character(who)
            sheet = self._sheet_for(actor) if actor else None
            if sheet is None:
                return [], {"ok": False, "error": f"{who!r} has no character sheet."}
            if sheet.get("dead") or int(sheet.get("hp", 0)) <= 0:
                results[actor] = {"note": f"{actor} is already down; damage can never take them below 0 HP."}
                continue
            landed, facts = self._attack(channel_id, reason, {"damage": die, "ignores_armor": bool(ignores_armor)},
                                         actor, extra_armor=cover, hazard=True)
            effects.extend(landed)
            if facts.get("down"):
                effects.extend(self._trigger(channel_id, actor, "last_breath"))
            results[actor] = facts
        if not results:
            return [], {"ok": False, "error": "Name who is caught (characters)."}
        return effects, {"ok": True, "die": die, "hurt": results, "note": (
            "Rolled and applied. Narrate it as bad as each severity says, and remember the hurt is "
            "often more than HP: a specific harm -- a wrenched arm -- has consequences of its own.")}

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
                dice, capped = self._intervened(check, self._roll_move(check))
                effects.append(SendMessage(
                    check.channel_id,
                    f"🎲 **{check.actor}** (rolled for them) rolls {dice.render()} — "
                    f"{_RESULT[band(dice)]}." + capped,
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
        self, *, channel_id: int, approaches: bool = False, watcher: str = "", reason: str = "",
        dungeon_or_city: bool = False, fed_by_host: bool = False,
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The party settles in to rest -- once for all of them, however many sleep.

        Everyone eats a ration -- unless they paid for a meal or are someone's guest:
        "only mark off a ration if you're eating from the food you carry, not paying for a
        meal or receiving hospitality". If the night passes quietly, everyone heals half their
        max HP. The book sets no penalty for having no ration, so the hungry heal too, and
        the model is told to make a GM move of their hunger. If something approaches -- the
        model's call, like any GM move -- nobody heals, and the watcher rolls Take Watch.

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
        # Priest, Ministry: "When you make camp with a priest if you would normally heal you
        # heal +skill HP."
        ministry = 0 if approaches else hirelings.best([s for _, s in party], "Priest")
        if ministry:
            lines.append(f"A hired priest tends the camp: +{ministry} to all healing.")
        for actor, sheet in party:
            changes: Dict[str, Any] = {}
            if any(h.get("strain") for h in sheet.get("hirelings") or ()):
                # A night's rest: a protector's sentry strain mends.
                changes["hirelings"] = [{**h, "strain": 0} for h in sheet["hirelings"]]
            if (sheet.get("companion") or {}).get("spent"):
                changes["companion"] = {**sheet["companion"], "spent": False}  # a few hours' rest
            if sheet.get("defending"):
                changes["defending"] = None  # nobody stands in defense through the night
            if sheet.get("forwards"):
                changes["forwards"] = []  # a forward is for the scene it came from
            if any(o.get("until") == "battle" for o in sheet.get("ongoing_mods") or ()):
                changes["ongoing_mods"] = [o for o in sheet["ongoing_mods"] if o.get("until") != "battle"]
            fed = True
            sustained = sheet.get("class") == "Druid"  # By Nature Sustained: no rations
            # Human Ranger: "When you make camp in a dungeon or city, you don't need to consume a ration."
            sustained = sustained or (dungeon_or_city and sheet.get("class") == "Ranger"
                                      and _race_move_of(sheet, "Human"))
            sustained = sustained or quests.has_boon(sheet, "sustenance")  # Quest: no hunger
            if "gear" in sheet and not sustained and not fed_by_host:  # sheets made before gear do not count rations
                have = int(sheet["rations"]) if "rations" in sheet else rations_of(sheet["gear"])
                fed = have > 0
                if fed:
                    changes["rations"] = have - 1
            entry: Dict[str, Any] = {"ate": fed}
            if "rations" in changes:
                entry["rations_left"] = changes["rations"]
            if not approaches:
                healed = min(int(sheet["max_hp"]) - int(sheet["hp"]),
                             int(sheet["max_hp"]) // 2 + (songs.total if songs else 0) + ministry)
                changes["hp"] = int(sheet["hp"]) + healed
                entry.update(healed=healed, hp=changes["hp"], max_hp=sheet["max_hp"])
                ate = ("eats at their host's table" if fed_by_host else "eats" if fed else "goes hungry, with no rations,")
                lines.append(
                    f"**{actor}** {ate}" + (f" ({changes['rations']} rations left)" if "rations" in changes else "")
                    + (f" and heals {healed} — HP {changes['hp']}/{sheet['max_hp']}." if healed
                       else f", already whole — HP {changes['hp']}/{sheet['max_hp']}.")
                )
            elif fed:
                lines.append(f"**{actor}** eats" + (f" ({changes['rations']} rations left)." if "rations" in changes else "."))
            else:
                lines.append(f"**{actor}** has no rations and goes hungry.")
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
        hungry = [a for a, e in report.items() if not e["ate"]]
        if hungry:
            # The book sets no penalty for an empty pack: hunger is the GM's to make something of.
            facts["hungry"] = hungry
            facts["note"] += (f" {', '.join(hungry)} had nothing to eat: make a GM move of it -- weakness, a "
                              "temper, a debility if it goes on, a desperate choice. Not a lost night's healing.")
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
        forgetting = spells.forgetting()
        if cls == "cleric" and reactions.hold_from_commune(sheet):
            forgetting["intervention"] = reactions.hold_from_commune(sheet)
        return [
            UpdateSheet(channel_id, actor, forgetting),
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
        scout: str = "", quartermaster: str = "", wilderness: bool = False,
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
            if quests.has_boon(sheet, "sustenance"):  # Quest: freedom from hunger and thirst
                continue
            have = int(sheet["rations"]) if "rations" in sheet else rations_of(sheet["gear"])
            used = min(have, days)
            spent[actor] = {"rations_used": used, "rations_left": have - used,
                            "short": days - used}
            effects.append(UpdateSheet(channel_id, actor, {"rations": have - used,
                                                           **({"forwards": []} if sheet.get("forwards") else {})}))
            lines.append(f"**{actor}** packs {used} ration{'s' if used != 1 else ''}"
                         + (f" — {days - used} short." if used < days else f" ({have - used} left)."))
        unfilled = [j for j in ("trailblazer", "scout", "quartermaster") if j not in jobs]
        if unfilled:
            lines.append(f"Nobody is {_and(unfilled)}: that counts as a 6.")
        effects.insert(0, SendMessage(channel_id, "\n".join(lines)))
        guide = hirelings.best([s for _, s in party], "Tracker")
        if guide > days:
            # Tracker, Guide: "you automatically succeed on any Perilous Journey of a distance (in
            # rations) lower than the tracker's skill." Every job counts as a 10+.
            effects[0] = replace(effects[0], text=effects[0].text + "\nA hired tracker leads the "
                                                 "way: the journey succeeds without a roll.")
            quartermaster = jobs.get("quartermaster")
            if quartermaster:
                qs = dict(self._sheet_for(quartermaster) or {})
                if "gear" in qs:
                    left = spent.get(quartermaster, {}).get("rations_left", int(qs.get("rations", 0)))
                    effects.append(UpdateSheet(channel_id, quartermaster, {"rations": left + 1}))
            return effects, {"ok": True, "destination": destination, "days": days, "guided": True,
                             "rations": spent, "note": (
                                 "A hired tracker guided them: every job counts as a 10+ -- the trip is "
                                 "shorter than expected (you say by how much), they get the drop on any "
                                 "trouble, and a ration is saved. Move the clock; narrate the arrival.")}
        sure: Dict[str, str] = {}
        for job, actor in jobs.items():
            js = self._sheet_or_empty(actor)
            if wilderness and js.get("class") == "Ranger" and _race_move_of(js, "Elf"):
                # Elf Ranger: "When you undertake a perilous journey through wilderness whatever
                # job you take you succeed as if you rolled a 10+."
                sure[job] = actor
                effects.append(SendMessage(channel_id, f"🌲 **{actor}** is at home in the wild: as "
                                                       f"{job}, a 10+ without a roll."))
                if job == "quartermaster" and "gear" in js:
                    left = spent.get(actor, {}).get("rations_left", int(js.get("rations", 0)))
                    effects.append(UpdateSheet(channel_id, actor, {"rations": left + 1}))
                continue
            effects.extend(self._trigger(channel_id, actor, f"journey_{job}"))
        return effects, {"ok": True, "destination": destination, "days": days, "jobs": jobs,
                         **({"succeeded_as_10_plus": sure} if sure else {}),
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
            if any(h.get("strain") for h in sheet.get("hirelings") or ()):
                changes["hirelings"] = [{**h, "strain": 0} for h in sheet["hirelings"]]
            if sheet.get("forwards"):
                changes["forwards"] = []  # a forward is for the scene it came from
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

    # -- answering a blow (reactions.py) --------------------------------------------------------

    def _struck(self, hit_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        for actor, sheet in self._party():
            if (sheet.get("last_hit") or {}).get("id") == hit_id:
                return actor, sheet
        return None, None

    def reactions(self, *, user_id: int, hit_id: str) -> Tuple[List[reactions.Reaction], str]:
        """What this player can do about this hit, or why nothing."""
        presser = self.players.get(user_id)
        mine = self._sheet_for(presser) if presser and self._sheet_for is not None else None
        if mine is None:
            return [], "You have no character here."
        victim, theirs = self._struck(hit_id)
        if victim is None:
            return [], "That blow is past answering: another has landed since, or it has been answered."
        if (theirs.get("last_hit") or {}).get("down") and int(theirs.get("hp", 0)) <= 0 \
                and not self._dying(victim):
            return [], f"Too late: {victim}'s Last Breath has been rolled."
        up = [n for n, r in self._monsters().items() if r["hp"] > 0]
        found = reactions.options(presser, theirs if presser == victim else mine, victim, theirs, up)
        return found, "" if found else "You have no move that answers this blow."

    def _dying(self, actor: str) -> Optional[PendingCheck]:
        return next((c for c in self.registry.open_checks()
                     if c.move == "last_breath" and normalize_actor(c.actor) == normalize_actor(actor)), None)

    def react(self, *, channel_id: int, user_id: int, hit_id: str, key: str) -> List[Effect]:
        """Apply one answer to a blow. [] if it is no longer on offer."""
        found, _ = self.reactions(user_id=user_id, hit_id=hit_id)
        chosen = next((r for r in found if r.key == key), None)
        if chosen is None:
            return []
        presser = self.players[user_id]
        victim, theirs = self._struck(hit_id)
        mine = theirs if presser == victim else self._sheet_for(presser)
        hit = dict(theirs["last_hit"])
        effects: List[Effect] = []
        mine_changes: Dict[str, Any] = {}
        facts: Dict[str, Any] = {"move": chosen.move, "by": presser, "struck": victim, "attacker": hit["by"]}
        if key == "reroll":
            return self._reroll(channel_id, presser, victim, theirs, hit, facts)
        if key == "pain":
            more = self.dice.roll("1d4").total
            hp = max(1, int(theirs["hp"]) - more)
            hit["pain"] = True
            effects += [SendMessage(channel_id, f"🩸 **{victim}** embraces the pain — {more} more damage, "
                                                f"HP {theirs['hp']} → {hp}."),
                        ChangeHp(channel_id, "character", victim, hp),
                        UpdateSheet(channel_id, victim, {"last_hit": hit}),
                        *self._grant(channel_id, victim, f"to cast a spell ({chosen.move})")]
            facts["instruction"] = "They took the pain willingly for their deity. Narrate it in a line."
            return [*effects, PromptModel(channel_id, "rules_event", {"event": "A blow answered", **facts})]
        hp = min(int(theirs["max_hp"]), int(theirs["hp"]) + min(chosen.restores, int(hit.get("lost", chosen.restores))))
        hit["reduced"] = True
        what = ""
        if key.startswith("spell:"):
            spell, _ = spells.running(mine)[int(key.split(":")[1])]
            kept = list(mine.get("ongoing") or [])
            del kept[int(key.split(":")[1])]
            mine_changes["ongoing"] = kept
            what = f"ends {spell.name} and turns {chosen.restores} of the damage aside"
        elif key.startswith("armor:"):
            index = int(key.split(":")[1])
            gear = list(mine["gear"])
            item = gear[index]
            worn = reactions.worn_down(item)
            if worn:
                gear[index] = worn
            else:
                del gear[index]
            before, after = gear_armor(mine["gear"]), gear_armor(gear)
            mine_changes.update(gear=gear, armor=max(0, int(mine.get("armor", 0)) - (before - after)))
            what = (f"lets their {item['name']} take the blow" + (" — it is **destroyed**" if not worn else
                    f" — armor now {mine_changes['armor']}"))
            if chosen.move == "Armored Perfection":
                effects += self._grant(channel_id, presser, f"against {hit['by']}")
        elif key.startswith("aegis:"):
            name = key.split(":")[1]
            mine_changes["debilities"] = [*(mine.get("debilities") or []), name]
            what = f"grits their teeth and takes it as **{name.title()}** (-1 {STAT_NAMES[DEBILITIES[name]]})"
            if advanced.has(mine, "Indomitable"):
                effects += self._grant(channel_id, presser, f"against {hit['by']} (Indomitable)")
        elif key == "hunger":
            effects += self._put_ongoing(channel_id, presser, -1,
                                         "Indestructible Hunger: until they sate one of their appetites", "it ends")
            what = "takes the blow into their hunger instead"
        elif key == "shed":
            mine_changes["shape"] = None
            what = f"sheds the {mine['shape'].get('form', 'form')} and the blow with it"
        elif key == "pet":
            mine_changes["companion"] = {**mine["companion"], "spent": True}
            what = f"lets {mine['companion']['name']} take the blow — its ferocity is 0 until they rest"
        elif key == "deity":
            mine_changes["intervention"] = int(mine["intervention"]) - 1
            deity = (mine.get("choices") or {}).get("deity") or "their deity"
            what = f"calls on {deity}, who spares {'them' if presser == victim else victim}"
        line = f"✨ **{presser}** {what} — **{victim}** HP {theirs['hp']} → {hp}."
        effects.insert(0, SendMessage(channel_id, line))
        effects.append(ChangeHp(channel_id, "character", victim, hp))
        if presser == victim:
            mine_changes["last_hit"] = hit
        else:
            effects.append(UpdateSheet(channel_id, victim, {"last_hit": hit}))
        effects.append(UpdateSheet(channel_id, presser, mine_changes))
        dying = self._dying(victim) if int(theirs["hp"]) <= 0 < hp else None
        if dying is not None:
            self.registry.cancel(dying.check_id)
            facts["spared"] = f"{victim} is no longer at 0 HP: there is no Last Breath."
        facts.update(hp=hp, instruction=("Narrate how the blow was turned, in a line or two; the HP is "
                                         "exact. Whatever it cost is real."))
        return [*effects, PromptModel(channel_id, "rules_event", {"event": "A blow answered", **facts})]

    def _reroll(self, channel_id: int, presser: str, victim: str, theirs: Dict[str, Any],
                hit: Dict[str, Any], facts: Dict[str, Any]) -> List[Effect]:
        """Human Fighter: "Once per battle you may reroll a single damage roll (yours or someone
        else's)." The new roll stands, better or worse."""
        again = damage.roll(self.dice, damage.parse(hit["damage"]))
        dealt = damage.through_armor(again.total, hit["armor"], hit["piercing"], hit["ignores"])
        if hit.get("halve"):
            dealt //= 2
        lost = min(int(hit["hp_before"]), dealt)
        before = int(theirs["hp"])
        hp = max(0, min(int(theirs["max_hp"]), before + int(hit["lost"]) - lost))
        hit.update(dealt=dealt, lost=lost, rerolled=True, down=hp <= 0)
        standing = sorted(n for n, r in self._monsters().items() if r["hp"] > 0)
        effects: List[Effect] = [
            SendMessage(channel_id, f"🎲 **{presser}** rerolls {hit['by']}'s damage ({again.render()}) — "
                                    f"now {_damage_words(dealt)} — **{victim}** HP {before} → {hp}."
                                    + (" — **down**." if hp <= 0 < before else "")),
            ChangeHp(channel_id, "character", victim, hp),
            UpdateSheet(channel_id, victim, {"last_hit": hit}),
            UpdateSheet(channel_id, presser, {"reroll_used": standing}),
        ]
        if presser == victim:  # one sheet: fold both changes into one
            effects[2:] = [UpdateSheet(channel_id, victim, {"last_hit": hit, "reroll_used": standing})]
        dying = self._dying(victim)
        if hp > 0 and dying is not None:
            self.registry.cancel(dying.check_id)
            facts["spared"] = f"{victim} is no longer at 0 HP: there is no Last Breath."
        elif hp <= 0 < before:
            effects.extend(self._trigger(channel_id, victim, "last_breath"))
        facts.update(move="Human Fighter reroll", hp=hp, dealt=dealt, instruction=(
            "The blow was rerolled; the new damage stands. Narrate the difference in a line."))
        return [*effects, PromptModel(channel_id, "rules_event", {"event": "A blow answered", **facts})]

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

    # -- hirelings --------------------------------------------------------------------------------

    def hire(
        self, *, channel_id: int, character: str, name: str, skills: Dict[str, int], loyalty: int,
        cost: str, found_in: str,
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """A hireling joins a character, made as the book makes one."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        try:
            hired = hirelings.make(name, skills, loyalty, cost, found_in)
        except SheetError as exc:
            return [], {"ok": False, "error": str(exc)}
        existing = hirelings.find(sheet, hired["name"])
        if existing:
            return [], {"ok": False, "error": f"{actor} already employs {existing['name']}."}
        top = max((int(r) for r in (hired.get("skills") or {}).values()), default=0)
        if top > int(sheet.get("level", 1)):
            # "Generally hirelings only work for adventurers of equal or higher level than their
            # highest skill."
            return [], {"ok": False, "error": (
                f"{hired['name']}'s best skill is rank {top}, and hirelings generally only work for "
                f"adventurers of at least that level; {actor} is level {sheet.get('level', 1)}. Offer someone "
                f"whose best rank is {sheet.get('level', 1)} or less.")}
        return [
            SendMessage(channel_id, f"🤝 **{actor}** hires {hirelings.label(hired)}."),
            UpdateSheet(channel_id, actor, {"hirelings": [*(sheet.get("hirelings") or []), hired]}),
        ], {"ok": True, "hired": hired}

    def update_hireling(
        self, *, channel_id: int, character: str, name: str, loyalty: int = 0,
        paid: Optional[bool] = None, leaves: bool = False, reason: str = "",
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Pay them, let their pay lapse, change their loyalty for a great deed or a bad
        beating, or see them go."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        hired = hirelings.find(sheet or {}, name) if sheet else None
        if hired is None:
            return [], {"ok": False, "error": f"{character!r} employs no hireling named {name!r}."}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": "Say what happened (reason)."}
        others = [h for h in sheet["hirelings"] if h is not hired]
        if leaves:
            return [SendMessage(channel_id, f"🤝 {hired['name']} leaves **{actor}**'s service — {reason}."),
                    UpdateSheet(channel_id, actor, {"hirelings": others})], {"ok": True, "left": hired["name"]}
        loyalty = int(loyalty or 0)
        if loyalty not in (-1, 0, 1):
            return [], {"ok": False, "error": "Loyalty changes by 1 at a time."}
        changed = {**hired, "loyalty": int(hired["loyalty"]) + loyalty}
        if paid is not None:
            changed["unpaid"] = not paid
        parts = []
        if loyalty:
            parts.append(f"loyalty {changed['loyalty']:+d}")
        if paid is not None:
            parts.append("paid" if paid else "unpaid: -1 loyalty until paid")
        index = sheet["hirelings"].index(hired)
        updated = [*sheet["hirelings"][:index], changed, *sheet["hirelings"][index + 1:]]
        return [SendMessage(channel_id, f"🤝 {hired['name']} — {', '.join(parts) or 'unchanged'} — {reason}."),
                UpdateSheet(channel_id, actor, {"hirelings": updated})], {"ok": True, "hireling": changed}

    def first_aid(
        self, *, channel_id: int, hireling: str, patient: str
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Priest, First Aid: "When a priest staunches your wounds heal 2×skill HP. You take -1
        forward as their healing is painful and distracting.\""""
        _, priest = self._hireling_named(hireling)
        rank = hirelings.skill(priest, "Priest") if priest else 0
        if not rank:
            return [], {"ok": False, "error": f"No priest named {hireling!r} is with the party."}
        actor = self._character(patient)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{patient!r} has no character sheet."}
        if sheet.get("dead"):
            return [], {"ok": False, "error": f"{actor} is dead."}
        healed = min(2 * rank, int(sheet["max_hp"]) - int(sheet["hp"]))
        hp = int(sheet["hp"]) + healed
        effects: List[Effect] = [
            SendMessage(channel_id, f"🩹 {priest['name']} staunches **{actor}**'s wounds — heals {healed}, "
                                    f"HP {hp}/{sheet['max_hp']}."),
            UpdateSheet(channel_id, actor, {"hp": hp}),
            *self._grant(channel_id, actor, "the priest's healing is painful and distracting", amount=-1),
        ]
        return effects, {"ok": True, "healed": healed, "hp": hp, "note": "They take -1 forward."}

    # -- forwards ---------------------------------------------------------------------------------

    # -- ongoing bonuses --------------------------------------------------------------------------

    def _standing(self) -> List[str]:
        return sorted(n for n, r in self._monsters().items() if r["hp"] > 0)

    def _live_ongoing(self, sheet: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The ongoing bonuses still in force. One that lasts the battle ends when every
        creature that stood when it began is down or gone."""
        standing = set(self._standing())
        return [o for o in sheet.get("ongoing_mods") or ()
                if o.get("until") != "battle" or set(o.get("battle") or ()) & standing]

    def _ongoing(self, actor: str, move: str) -> Tuple[int, List[str]]:
        """"Take +1 ongoing": +1 to all move rolls -- or to one move -- until it ends."""
        total, notes = 0, []
        for o in self._live_ongoing(self._sheet_or_empty(actor)):
            if o.get("move") and o["move"] != move:
                continue
            total += int(o["amount"])
            notes.append(f"{int(o['amount']):+d} ongoing ({o['reason']})")
        return total, notes

    def _put_ongoing(self, channel_id: int, actor: str, amount: int, reason: str, until: str,
                     move: str = "") -> List[Effect]:
        sheet = self._sheet_or_empty(actor)
        entry = {"amount": amount, "reason": reason, "until": until, "move": move}
        if until == "battle":
            entry["battle"] = self._standing()
        kept = self._live_ongoing(sheet)
        to = f" to {MOVES[move].name}" if move else ""
        ends = "for the battle" if until == "battle" else "until it ends"
        return [SendMessage(channel_id, f"➕ **{actor}** takes {amount:+d} ongoing{to} — {reason} ({ends})."),
                UpdateSheet(channel_id, actor, {"ongoing_mods": [*kept, entry]})]

    def give_ongoing(
        self, *, channel_id: int, character: str, amount: int = 0, reason: str = "", until: str = "battle",
        move: str = "", end: bool = False,
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The model gives an ongoing bonus or penalty -- a GM move, a spell, a blessing --
        or ends one (end=true, matched by its reason; all of them with no reason)."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        reason = " ".join(str(reason or "").split())
        if end:
            have = self._live_ongoing(sheet)
            folded = reason.lower()
            kept = [o for o in have if folded and folded not in o["reason"].lower()]
            if len(kept) == len(have):
                return [], {"ok": False, "error": f"{actor} has no ongoing bonus like {reason!r}. Ongoing: "
                                                  f"{'; '.join(o['reason'] for o in have) or 'none'}."}
            gone = [o["reason"] for o in have if o not in kept]
            return [SendMessage(channel_id, f"➖ **{actor}**'s ongoing ends — {'; '.join(gone)}."),
                    UpdateSheet(channel_id, actor, {"ongoing_mods": kept})], {"ok": True, "ended": gone}
        amount = int(amount or 0)
        if amount not in (-2, -1, 1, 2):
            return [], {"ok": False, "error": "An ongoing bonus is +1 or -1 (at most 2 either way)."}
        if not reason:
            return [], {"ok": False, "error": "Say what causes it (reason)."}
        until = str(until or "battle").strip().lower()
        if until not in ("battle", "ended"):
            return [], {"ok": False, "error": "until is 'battle' (ends when this fight does) or 'ended' "
                                              "(lasts until you end it)."}
        if until == "battle" and not self._standing():
            return [], {"ok": False, "error": "No battle is going on: use until='ended' and end it yourself."}
        move = str(move or "").strip().lower()
        if move and move not in MOVES:
            return [], {"ok": False, "error": f"No move {move!r}."}
        return self._put_ongoing(channel_id, actor, amount, reason, until, move), {"ok": True}

    def give_forward(
        self, *, channel_id: int, character: str, amount: int = 1, reason: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The model gives (or takes) a forward as a GM move or for a 7-9's price: "you're off
        balance, -1 forward". It waits until a move the model flags forward=true."""
        actor = self._character(character)
        if actor is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        amount = int(amount or 0)
        if amount not in (-2, -1, 1, 2):
            return [], {"ok": False, "error": "A forward is +1 or -1 (at most 2 either way)."}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": "Say what it is for (reason): when it applies."}
        return self._grant(channel_id, actor, reason, amount=amount), {"ok": True, "forward": amount}

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
        indomitable = (self._grant(channel_id, actor, f"against {cause} (Indomitable)")
                       if not cured and advanced.has(sheet, "Indomitable") else [])
        return [SendMessage(channel_id, line), UpdateSheet(channel_id, actor, {"debilities": have}),
                *indomitable], {
            "ok": True, "debilities": have,
            "note": f"{stat} rolls take -1 while it lasts." if not cured else f"{stat} is whole again."}

    # -- the Paladin's Quest (quests.py) ---------------------------------------------------------

    def creature_attacks(self, *, channel_id: int, attacker: str, target: str, reason: str = ""
                         ) -> Tuple[List[Effect], Dict[str, Any]]:
        """"A monster deals its damage to another monster or a player whenever it causes them
        physical harm": one creature's damage rolled through another's armor."""
        scene = self._monsters()
        by = next((n for n in scene if normalize_actor(n) == normalize_actor(attacker)), None)
        at = next((n for n in scene if normalize_actor(n) == normalize_actor(target)), None)
        if by is None or at is None or by == at:
            standing = ", ".join(n for n, r in scene.items() if int(r.get("hp", 0)) > 0) or "none"
            return [], {"ok": False, "error": f"Name two different creatures in the scene. Here: {standing}."}
        if int(scene[by].get("hp", 0)) <= 0 or int(scene[at].get("hp", 0)) <= 0:
            return [], {"ok": False, "error": "A creature at 0 HP is out of the fight."}
        hit = damage.roll(self.dice, damage.parse(str(scene[by]["damage"])))
        target_record = scene[at]
        dealt = damage.through_armor(hit.total, int(target_record.get("armor", 0)), int(scene[by].get("piercing", 0)),
                                     bool(scene[by].get("ignores_armor")))
        hp = int(target_record["hp"]) - dealt
        down = " — **defeated**" if hp <= 0 else ""
        line = (f"🗡️ **{by}** hits **{at}** for **{dealt} damage** ({hit.render()}, armor "
                f"{target_record.get('armor', 0)}) — HP {target_record['hp']} → {max(hp, 0)}{down}."
                + (f" {reason}" if reason else ""))
        return [SendMessage(channel_id, line), ChangeHp(channel_id, "monster", at, hp)], {
            "ok": True, "dealt": dealt, "hp": max(hp, 0), "defeated": hp <= 0}

    def ritual(
        self, *, channel_id: int, character: str, action: str, goal: str = "",
        conditions: Sequence[str] = (), condition: str = "", outcome: str = "",
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The Wizard's Ritual, kept: start it with the GM's conditions, meet them one by one, and
        finish (or abandon) it. What it makes, if an item, is give_magic_item."""
        from . import crafting

        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        if not starting_move(sheet, "Ritual") and not advanced.has(sheet, "Ritual"):
            return [], {"ok": False, "error": f"{actor} has no Ritual (a Wizard's move)."}
        current = sheet.get("ritual")
        try:
            if action == "start":
                if current:
                    return [], {"ok": False, "error": f"{actor} is already working a ritual: {current['goal']}."}
                made = crafting.start_ritual(goal, conditions)
                terms = "; ".join(c["text"] for c in made["conditions"])
                return [SendMessage(channel_id, f"🔮 **{actor}** begins a ritual: {made['goal']}.\nThe conditions: {terms}."),
                        UpdateSheet(channel_id, actor, {"ritual": made})], {
                    "ok": True, "note": "Recorded on their sheet, shown to you every turn. Mark each condition with "
                                        "action=meet as play fulfils it; action=finish when it's done."}
            if not current:
                return [], {"ok": False, "error": f"{actor} has no ritual under way."}
            if action == "meet":
                updated = crafting.meet(current, condition)
                met = next(c["text"] for o, c in zip(current["conditions"], updated["conditions"]) if c["met"] and not o["met"])
                return [SendMessage(channel_id, f"🔮 **{actor}**'s ritual: {met} — done."
                                    + (" Every condition is met." if crafting.done(updated) else "")),
                        UpdateSheet(channel_id, actor, {"ritual": updated})], {"ok": True, "all_met": crafting.done(updated)}
            if action in ("finish", "abandon"):
                outcome = " ".join(str(outcome or "").split())[:200]
                if action == "finish" and not crafting.done(current):
                    left = "; ".join(c["text"] for c in current["conditions"] if not c["met"])
                    return [], {"ok": False, "error": f"Conditions still unmet: {left}. Meet them, or abandon it."}
                said = (f"🔮 **{actor}** completes the ritual: {current['goal']}" if action == "finish"
                        else f"🔮 **{actor}** abandons the ritual: {current['goal']}")
                return [SendMessage(channel_id, said + (f" — {outcome}." if outcome else ".")),
                        UpdateSheet(channel_id, actor, {"ritual": None})], {
                    "ok": True, "note": "If it made an item, give it with give_magic_item." if action == "finish" else ""}
        except SheetError as exc:
            return [], {"ok": False, "error": str(exc)}
        return [], {"ok": False, "error": "action is start, meet, finish or abandon."}

    def graft(self, *, channel_id: int, character: str, weapon: str, reason: str = "") -> Tuple[List[Effect], Dict[str, Any]]:
        """Blacksmith: a magic weapon's powers grafted onto the signature weapon, at a forge."""
        from . import crafting

        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        if not advanced.has(sheet, "Blacksmith"):
            return [], {"ok": False, "error": f"{actor} doesn't have Blacksmith."}
        try:
            gear, signature, destroyed = crafting.graft(sheet, weapon)
        except SheetError as exc:
            return [], {"ok": False, "error": str(exc)}
        return [SendMessage(channel_id, f"🔨 At the forge, **{actor}** grafts the powers of {destroyed} onto their "
                                        f"{signature.lower()}; {destroyed} is no more."
                            + (f" — {reason}" if reason else "")),
                UpdateSheet(channel_id, actor, {"gear": gear})], {"ok": True, "signature": signature}

    def start_quest(
        self, *, channel_id: int, character: str, mission: str, subject: str,
        boons: Sequence[Dict[str, Any]] = (), vows: Sequence[str] = (),
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The Paladin dedicates themselves to a mission; the model records what they swore,
        the boons they chose and the vows it named."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        if sheet.get("class") != "Paladin":
            return [], {"ok": False, "error": f"{actor} is a {sheet.get('class')}: Quest is a Paladin's move."}
        if sheet.get("quest"):
            return [], {"ok": False, "error": (f"{actor} is already on a quest ({sheet['quest']['text']}). "
                                               "End it with end_quest first.")}
        try:
            quest = quests.make(mission, subject, boons, vows)
        except SheetError as exc:
            return [], {"ok": False, "error": str(exc)}
        boons_text = "; ".join(b["text"] for b in quest["boons"]) or "none"
        vows_text = ", ".join(quests.VOWS[v].split(" (")[0] for v in quest["vows"])
        return [
            SendMessage(channel_id, f"⚜️ **{actor}** sets out on a quest: {quest['text']}.\n"
                                    f"Boons: {boons_text}. Vows: {vows_text}."),
            UpdateSheet(channel_id, actor, {"quest": quest}),
        ], {"ok": True, "quest": quest["text"], "note": (
            "Recorded. Gary applies Smite and Holy Protection while it lasts, and freedom from hunger "
            "at camp and on journeys; honour the other boons yourself (invulnerability: an attack of "
            "that kind does them no harm -- do not call monster_attacks for it). Hold them to the "
            "vows: breaking one ends the blessing (end_quest).")}

    def end_quest(
        self, *, channel_id: int, character: str, how: str, reason: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The quest is fulfilled, or a vow is broken and the blessing goes, or they give it up."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        quest = sheet.get("quest")
        if not quest:
            return [], {"ok": False, "error": f"{actor} is not on a quest."}
        how = str(how or "").strip().lower()
        words = {"fulfilled": "fulfils their quest", "broken": "breaks a vow — the blessing leaves them",
                 "abandoned": "gives up their quest"}
        if how not in words:
            return [], {"ok": False, "error": f"How it ended is one of: {', '.join(words)}."}
        reason = " ".join(str(reason or "").split())
        return [
            SendMessage(channel_id, f"⚜️ **{actor}** {words[how]}: {quest['text']}."
                                    + (f" — {reason}" if reason else "")),
            UpdateSheet(channel_id, actor, {"quest": None}),
        ], {"ok": True, "ended": quest["text"], "how": how}

    # -- the Thief's poison -------------------------------------------------------------------

    # -- compendium powers -------------------------------------------------------------------

    def compendium_power(
        self, *, channel_id: int, character: str, power: str, boons: Sequence[str] = (),
        targets: Sequence[str] = (), deity: str = "", on: Optional[bool] = None, name: str = "", stat: str = "",
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """What a compendium move does when the fiction calls on it: a weapon's boons, a god's
        favor, dragon blood, dragon fire, a weapon's stirred charge."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        if power in ("add_minion", "shape_minion", "rescoop", "ooze_form", "slime_copy"):
            return self._slime_power(channel_id, actor, sheet, power, name=name, stat=stat, on=on)
        handler = {"weapon_boons": self._weapon_boons, "deity_favor": self._deity_favor,
                   "dragon_blood": self._dragon_blood, "drink_heart_blood": self._drink_heart_blood,
                   "dragons_breath": self._dragons_breath, "faraday": self._faraday}.get(str(power))
        if handler is None:
            return [], {"ok": False, "error": f"No compendium power {power!r}."}
        return handler(channel_id, actor, sheet, boons=boons, targets=targets, deity=deity, on=on)

    def _hit_minion(self, channel_id: int, monster: str, character: str,
                    minion: str) -> Tuple[List[Effect], Dict[str, Any]]:
        """A creature's blow on a slime minion: no armor; at 0 HP it "splatters and becomes inert
        instead of dying"."""
        actor = self._character(character)
        sheet = self._sheet_or_empty(actor) if actor else {}
        pet = slimes.find(sheet, minion)
        scene = self._monsters()
        name = next((n for n in scene if n.lower() == str(monster).strip().lower() and scene[n]["hp"] > 0), None)
        if pet is None or name is None:
            return [], {"ok": False, "error": f"Need a standing creature and one of {character}'s minions."}
        hit = damage.roll(self.dice, damage.parse(scene[name]["damage"]))
        hp = max(0, int(pet["hp"]) - hit.total)
        minions = [{**m, "hp": hp} if m is pet else m for m in sheet["minions"]]
        line = (f"🗡️ **{name}** hits {pet['name']} for {_damage_words(hit.total)} ({hit.render()}) — HP {pet['hp']} → {hp}"
                + (" — it **splatters**, inert until scooped together." if hp <= 0 else "."))
        return [SendMessage(channel_id, line), UpdateSheet(channel_id, actor, {"minions": minions})], {
            "ok": True, "dealt": hit.total, "hp_after": hp, "inert": hp <= 0}

    def _slime_power(self, channel_id, actor, sheet, power, name="", stat="", on=None):
        """Blob's powers: gain a minion, shape one, scoop one back together, ooze form, a copy."""
        minions = list(sheet.get("minions") or [])
        if power == "ooze_form":
            why = self._needs(sheet, actor, "Scuzzy Metamorphosis")
            if why:
                return [], {"ok": False, "error": why}
            on = True if on is None else bool(on)
            return [SendMessage(channel_id, f"🟢 **{actor}** " + ("slumps into an ooze: +1 armor, -1 Int, Wis and Cha."
                                                               if on else "pulls back into shape.")),
                    UpdateSheet(channel_id, actor, {"ooze": on})], {"ok": True, "ooze": on}
        why = self._needs(sheet, actor, "Ambeoid Overlord")
        if why:
            return [], {"ok": False, "error": why}
        try:
            if power == "add_minion":
                if not advanced.has(sheet, "Slime Recruiter") and any(not m.get("copy") for m in minions):
                    return [], {"ok": False, "error": f"{actor} commands one slime; Slime Recruiter keeps more."}
                if sum(1 for m in minions if not m.get("copy")) >= slimes.limit(sheet):
                    return [], {"ok": False, "error": f"{actor} can command at most {slimes.limit(sheet)} (2+Cha)."}
                pet = slimes.make(name)
                minions.append(pet)
                line = f"🟢 **{actor}** keeps a piece of it: **{pet['name']}** obeys (STR +1, DEX +0, CON +2, 4 HP)."
            elif power == "slime_copy":
                why = self._needs(sheet, actor, "Divide and Conquer")
                if why:
                    return [], {"ok": False, "error": why}
                if on is False:
                    gone = slimes.find(sheet, name)
                    if gone is None or not gone.get("copy"):
                        return [], {"ok": False, "error": f"{actor} has no copy named {name!r}."}
                    minions.remove(gone)
                    line = f"🟢 **{actor}** takes {gone['name']} back into themselves."
                else:
                    copy = slimes.make_copy(sheet, name)
                    minions.append(copy)
                    line = f"🟢 **{actor}** divides: **{copy['name']}** — -1 to their Int, Wis and Cha for each copy."
            elif power == "shape_minion":
                why = self._needs(sheet, actor, "Slime Shaper")
                pet = slimes.find(sheet, name)
                stat = str(stat or "").lower()
                if why or pet is None or pet.get("copy") or stat not in slimes.BODY:
                    return [], {"ok": False, "error": why or "Shape a slime minion: one of str, dex, con becomes +2."}
                minions = [{**m, **{s: (2 if s == stat else 1) for s in slimes.BODY}} if m is pet else m
                           for m in minions]
                line = f"🟢 **{actor}** reshapes {pet['name']}: {stat.upper()} +2, the rest +1."
            elif power == "rescoop":
                pet = slimes.find(sheet, name)
                if pet is None or pet["hp"] > 0:
                    return [], {"ok": False, "error": f"{name!r} is not a splattered minion of {actor}'s."}
                minions = [{**m, "hp": m["max_hp"]} if m is pet else m for m in minions]
                line = f"🟢 **{actor}** scoops {pet['name']} back together — it'll be fine."
            else:
                return [], {"ok": False, "error": f"No compendium power {power!r}."}
        except SheetError as exc:
            return [], {"ok": False, "error": str(exc)}
        return [SendMessage(channel_id, line), UpdateSheet(channel_id, actor, {"minions": minions})], {"ok": True}

    def _needs(self, sheet: Dict[str, Any], actor: str, move: str) -> str:
        return "" if advanced.has(sheet, move) else f"{actor} does not have {move}."

    def _weapon_boons(self, channel_id, actor, sheet, boons, **_) -> Tuple[List[Effect], Dict[str, Any]]:
        """Whosoever Holdeth: "When you accomplish something your weapon desires, it bestows boons
        on you -- choose as many different boons as you have bonds with your weapon"."""
        why = self._needs(sheet, actor, "Whosoever Holdeth")
        if why:
            return [], {"ok": False, "error": why}
        chosen = list(dict.fromkeys(str(b).lower() for b in boons))
        allowed = {"heal", "knowledge", "insight", "peace"}
        if not chosen or not set(chosen) <= allowed:
            return [], {"ok": False, "error": f"Boons are some of: {', '.join(sorted(allowed))}."}
        most = bonds.weapon_bonds(sheet)
        if len(chosen) > most:
            return [], {"ok": False, "error": f"{actor} has {most} bond(s) with their weapon: that many boons."}
        effects: List[Effect] = []
        facts: Dict[str, Any] = {"ok": True, "boons": chosen}
        if "heal" in chosen:
            rolled = self.dice.roll("3d6")
            hp = min(int(sheet["max_hp"]), int(sheet["hp"]) + rolled.total)
            effects += [SendMessage(channel_id, f"🗡️ **{actor}**'s weapon restores them — 3d6: {rolled.render()}, "
                                                f"HP {sheet['hp']} → {hp}."),
                        ChangeHp(channel_id, "character", actor, hp)]
        if "insight" in chosen:
            xp = int(sheet.get("xp", 0)) + 1
            effects += [SendMessage(channel_id, f"🗡️ **{actor}**'s weapon bestows insight — marks XP ({xp})."),
                        UpdateSheet(channel_id, actor, {"xp": xp})]
        facts["note"] = ("Narrate the boons. Knowledge: you describe what the weapon tells them. Peace: the "
                         "weapon does not intrude on them, for a time.")
        return effects, facts

    def _deity_favor(self, channel_id, actor, sheet, deity, **_) -> Tuple[List[Effect], Dict[str, Any]]:
        """Divine Bond: mark off a bond with a deity "to call on the deity's favor in a clear and
        decisive way that the GM describes"."""
        why = self._needs(sheet, actor, "Divine Bond")
        if why:
            return [], {"ok": False, "error": why}
        held = list(sheet.get("bonds") or [])
        match = next((b for b in held if normalize_actor(b["with"]) == normalize_actor(deity)), None) if deity else \
            next((b for b in held if b["with"] in bonds.partners(sheet) and b["with"] != bonds.WEAPON), None)
        if match is None:
            return [], {"ok": False, "error": f"{actor} has no bond with {deity or 'a deity'} to mark off."}
        held.remove(match)
        return [SendMessage(channel_id, f"🙏 **{actor}** calls on {match['with']} — the bond is marked off."),
                UpdateSheet(channel_id, actor, {"bonds": held})], {
            "ok": True, "note": ("Describe the deity's favor, clear and decisive. They write a new bond -- with a "
                                 "deity or a character -- at the end of the session.")}

    def _nature(self, actor: str, blood: int) -> Tuple[str, bool]:
        """"Then roll 1d4, if the result is less than or equal to your Blood you must give in to
        dragon nature: greed, wrath, or disdain"."""
        rolled = self.dice.roll("1d4").total
        gave_in = rolled <= blood
        return (f"1d4: {rolled} against Blood {blood} — " + ("**gives in to dragon nature**: greed, wrath or disdain."
                                                            if gave_in else "holds the dragon down.")), gave_in

    def _dragon_blood(self, channel_id, actor, sheet, **_) -> Tuple[List[Effect], Dict[str, Any]]:
        """Dovahkiin: "When you call on your dragon blood for strength you take +Blood forward to
        Str rolls." Fury makes their next attack messy, forceful and +1 piercing."""
        why = self._needs(sheet, actor, "Dovahkiin")
        if why:
            return [], {"ok": False, "error": why}
        blood = int(sheet.get("blood", 1))
        line, gave_in = self._nature(actor, blood)
        effects: List[Effect] = [SendMessage(channel_id, f"🐉 **{actor}** calls on their dragon blood. {line}"),
                                 *self._grant(channel_id, actor, "Str rolls, from dragon blood", amount=blood)]
        if advanced.has(sheet, "Fury"):
            effects.append(UpdateSheet(channel_id, actor, {"fury": True}))
        return effects, {"ok": True, "gave_in": gave_in, "note": (
            "Gary set the +Blood forward (spend it on a Str roll with forward=true)"
            + (" and Fury: their next attack is messy, forceful and +1 piercing" if advanced.has(sheet, "Fury") else "")
            + ("; they give in to greed, wrath or disdain -- play it." if gave_in else "."))}

    def _drink_heart_blood(self, channel_id, actor, sheet, **_) -> Tuple[List[Effect], Dict[str, Any]]:
        """"When you drink the still burning heart-blood of a dragon your Blood increases by 1"."""
        why = self._needs(sheet, actor, "Dovahkiin")
        if why:
            return [], {"ok": False, "error": why}
        blood = int(sheet.get("blood", 1)) + 1
        return [SendMessage(channel_id, f"🐉 **{actor}** drinks the burning heart-blood — Blood {blood}."),
                UpdateSheet(channel_id, actor, {"blood": blood})], {"ok": True, "blood": blood}

    def _dragons_breath(self, channel_id, actor, sheet, targets, **_) -> Tuple[List[Effect], Dict[str, Any]]:
        """Dragon's Breath: "deal your damage to all creatures in the path of the flames and to
        yourself. Then roll 1d4..." One roll, on everyone it reaches."""
        why = self._needs(sheet, actor, "Dragon\u2019s Breath")
        if why:
            return [], {"ok": False, "error": why}
        scene = self._monsters()
        named = [next((n for n in scene if n.lower() == str(t).strip().lower() and scene[n]["hp"] > 0), None)
                 for t in targets]
        if not named or None in named:
            return [], {"ok": False, "error": f"Name the standing creatures in the flames' path. Standing: "
                                              f"{', '.join(self._standing()) or 'none'}."}
        hit = damage.roll(self.dice, damage.parse(sheet["damage"]))
        line = f"🔥 **{actor}** breathes fire — {hit.render()}"
        effects: List[Effect] = []
        hurt = []
        for name in named:
            landed, facts, said = self._hurt(channel_id, name, hit.total, 0, False)
            effects += landed
            hurt.append(facts)
            line += said
        burned, self_facts = self._attack(channel_id, "their own dragon fire", {"damage": sheet["damage"]},
                                          actor, hit=hit, hazard=True)
        nature, gave_in = self._nature(actor, int(sheet.get("blood", 1)))
        effects = [SendMessage(channel_id, line), *effects, *burned, SendMessage(channel_id, nature)]
        if self_facts.get("down"):
            effects += self._trigger(channel_id, actor, "last_breath")
        return effects, {"ok": True, "targets": hurt, "self": self_facts, "gave_in": gave_in}

    def _faraday(self, channel_id, actor, sheet, on, **_) -> Tuple[List[Effect], Dict[str, Any]]:
        """Faraday's Law: the weapon held "does +1d4 damage ongoing. Until you dismiss this effect
        you take -1 to bind"."""
        why = self._needs(sheet, actor, "Faraday\u2019s Law")
        if why:
            return [], {"ok": False, "error": why}
        on = True if on is None else bool(on)
        return [SendMessage(channel_id, f"⚡ **{actor}** " + ("stirs the charge in their weapon: +1d4 damage, -1 to Bind."
                                                           if on else "lets the charge settle.")),
                UpdateSheet(channel_id, actor, {"faraday": on})], {"ok": True, "faraday": on}

    def qualify_compendium(
        self, *, channel_id: int, character: str, compendium_class: str, reason: str = ""
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """The fiction met a compendium class's requirement: its starting move may be taken
        "the next time you level up"."""
        from . import compendium

        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        found = compendium.find_class(compendium_class)
        if found is None:
            return [], {"ok": False, "error": f"No compendium class {compendium_class!r}. They are: "
                                              f"{', '.join(compendium.CLASSES)}."}
        if found["starting"]["name"] in (sheet.get("advanced_moves") or ()):
            return [], {"ok": False, "error": f"{actor} already has {found['name']}."}
        reason = " ".join(str(reason or "").split())
        if not reason:
            return [], {"ok": False, "error": "Say what happened that meets the requirement (reason)."}
        have = list(sheet.get("compendium_qualified") or [])
        if found["name"] not in have:
            have.append(found["name"])
        return [SendMessage(channel_id, f"🌟 **{actor}** — {reason}. At their next level they may take "
                                        f"**{found['starting']['name']}** ({found['name']}) instead of a class move."),
                UpdateSheet(channel_id, actor, {"compendium_qualified": have})], {
            "ok": True, "note": "Only at their next level-up; it lapses if they take something else."}

    def stun(
        self, *, channel_id: int, character: str, reason: str = "", end: bool = False
    ) -> Tuple[List[Effect], Dict[str, Any]]:
        """Stun damage: "non-lethal damage. A PC who takes stun damage is defying danger to do
        anything at all, the danger being 'you're stunned.' This lasts as long as makes sense
        in the fiction." No HP is lost."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        reason = " ".join(str(reason or "").split())
        if end:
            if not sheet.get("stunned"):
                return [], {"ok": False, "error": f"{actor} is not stunned."}
            return [SendMessage(channel_id, f"💫 **{actor}** clears their head" + (f" — {reason}." if reason else ".")),
                    UpdateSheet(channel_id, actor, {"stunned": None})], {"ok": True}
        if not reason:
            return [], {"ok": False, "error": "Say what stunned them (reason)."}
        return [SendMessage(channel_id, f"💫 **{actor}** is stunned — {reason}. Until they clear their head, "
                                        "anything they do is Defy Danger."),
                UpdateSheet(channel_id, actor, {"stunned": reason})], {
            "ok": True, "note": "No HP lost. Their moves are Defy Danger until you end it (stun end=true)."}

    def use_poison(self, *, channel_id: int, character: str, target: str = "",
                   poison: str = "") -> Tuple[List[Effect], Dict[str, Any]]:
        """A poison used: the one a Thief mastered, which is "no longer dangerous for you to
        use", or a dose anyone carries (bought or found), which is spent -- and Dangerous.
        What it does is the poison's text; on a creature in the scene, Bloodweed and Serpent's
        Tears change its numbers from then on."""
        actor = self._character(character)
        sheet = self._sheet_for(actor) if actor else None
        if sheet is None:
            return [], {"ok": False, "error": f"{character!r} has no character sheet."}
        name = (sheet.get("choices") or {}).get("poison")
        wanted = " ".join(str(poison or "").split()).lower().replace("'", "\u2019")
        if wanted and (not name or name.lower() != wanted):
            return self._use_carried_poison(channel_id, actor, sheet, wanted, target)
        if not name:
            carried = [g for g in sheet.get("gear") or () if g["name"] in POISONS]
            if carried:
                return self._use_carried_poison(channel_id, actor, sheet, carried[0]["name"].lower(), target)
            return [], {"ok": False, "error": f"{actor} has mastered no poison and carries none."}
        left = int(sheet.get("poison_uses", 0))
        if left <= 0:
            return [], {"ok": False, "error": (f"{actor} has no {name} left. They can brew more with "
                                               "time and a safe place (brew_poison).")}
        on = f" on {target}" if target else ""
        return [
            SendMessage(channel_id, f"🧪 **{actor}** uses {name}{on} — {left - 1} uses left."),
            UpdateSheet(channel_id, actor, {"poison_uses": left - 1}),
            *self._afflict(channel_id, target, name),
        ], {"ok": True, "poison": name, "effect": choices.poison_text(name), "uses_left": left - 1,
            "note": "Applied poisons must reach the target or what they eat or drink; touch poisons "
                    "need only touch, a blade will do. Narrate the effect as written."}

    def _use_carried_poison(self, channel_id: int, actor: str, sheet: Dict[str, Any], wanted: str,
                            target: str) -> Tuple[List[Effect], Dict[str, Any]]:
        dose = next((g for g in sheet.get("gear") or () if g["name"].lower() == wanted and g["name"] in POISONS), None)
        if dose is None:
            carried = ", ".join(g["name"] for g in sheet.get("gear") or () if g["name"] in POISONS) or "none"
            return [], {"ok": False, "error": f"{actor} carries no {wanted}. Poisons carried: {carried}."}
        gear = list(sheet["gear"])
        gear.remove(dose)
        on = f" on {target}" if target else ""
        return [
            SendMessage(channel_id, f"🧪 **{actor}** uses {dose['name']}{on} — the dose is spent."),
            UpdateSheet(channel_id, actor, {"gear": gear}),
            *self._afflict(channel_id, target, dose["name"]),
        ], {"ok": True, "poison": dose["name"], "effect": POISONS[dose["name"]], "note": (
            "Dangerous: used without the proper precautions, it may turn on them -- a GM move if the "
            "fiction allows. Applied poisons must reach the target or what they eat or drink; touch "
            "poisons need only touch. Narrate the effect as written.")}

    def _afflict(self, channel_id: int, target: str, poison: str) -> List[Effect]:
        """Bloodweed and Serpent's Tears on a creature in the scene, kept on its record."""
        creature = self._monsters().get(target or "")
        if creature is None or poison not in ("Bloodweed", "Serpent\u2019s Tears"):
            return []
        have = list(creature.get("afflictions") or [])
        return [AfflictMonster(channel_id, target, tuple(have + [poison] if poison not in have else have))]

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
