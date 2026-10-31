"""The Gemini adapter (ARCHITECTURE.md §5, §8, §9.1, §12.3).

Implements ``Narrator``. Assembles context, calls the model, dispatches its tool calls,
and returns prose plus whatever effects those calls produced.

The one non-obvious idea here is how a check's result gets back to the model.

**A move is a deferred function call.** When the model calls ``make_move``, the call is
*deliberately left unanswered*. Gary asks the player to roll and the turn ends with that
call still open. Minutes later the player rolls, and the outcome is delivered as the
``function_response`` to that original call. This is not a trick — it is exactly what
happened: the model asked for a check, and this is the answer. It gives three things for
free:

* The conversation stays well-formed, with every call eventually paired to a response.
* The outcome arrives over the **tool transport**, so its trust comes from the channel it
  travelled on rather than from any string in it (§9.1).
* The model cannot mistake the result for a player's claim about the result.

Expiry answers the same call with ``attempted: false``. A missing key answers it with an
error, and the model retries — an ordinary tool-error loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from google.genai import types

from ..context import (
    render_document,
    render_proposal,
    DM_SYSTEM_INSTRUCTION,
    attribute,
    render_live_state,
    render_memory,
)
from ..fsm import Director
from .. import healing, trade
from ..class_moves import render as render_class_moves
from ..fsm import SendMessage
from ..models import Mutation, MutationKind
from ..monsters import describe as describe_monster, natures, prepare as prepare_monsters
from ..registry import normalize_actor
from ..ports import NarrationResult
from ..state.history import DEFAULT_WINDOW, trim
from ..state.store import CampaignStore, StateError
from . import gemini_tools as T

log = logging.getLogger("gary.gemini")

# Narration id verified 2026-08-18, summariser 2026-08-09, against
# ai.google.dev/gemini-api/docs/models. Ids move; recheck.
#
# Flash, not Pro, is deliberate. The 3.x family has no stable Pro: the flagship stable
# model is gemini-3.7-flash, which carries Pro-grade reasoning at Flash latency and cost.
# (gemini-3.1-pro-preview exists but is preview, and gemini-3-pro-preview is retired.)
# Latency and cost are live risks here -- §13 (7) -- and a turn costs two model
# calls around a player's roll, so Flash is the right shape as well as the current one.
DEFAULT_MODEL = "gemini-3.7-flash"

# Summarisation is a cheap background job on text nobody reads directly.
DEFAULT_SUMMARY_MODEL = "gemini-3.5-flash-lite"

# Beyond this, a turn is looping rather than working.
MAX_TOOL_ITERATIONS = 6

SUMMARY_PROMPT = (
    "Compress these tabletop RPG transcript turns into terse third-person notes for the "
    "Game Master's own reference. Keep decisions, promises, names, and unresolved "
    "threads. Drop atmosphere and dialogue wording. No preamble."
)


def _recent_text(payload: Dict[str, Any], contents: Sequence[Any], turns: int = 4) -> str:
    """Text to match notes against: what the player just said, plus the last few turns.

    A few turns rather than one, so "him" and "there" still reach the note that named the
    subject a beat earlier.
    """
    parts = [str(payload.get("text", "") or "")]
    for content in list(contents)[-turns:]:
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(parts)


class GeminiNarrator:
    def __init__(
        self,
        client: Any,
        director: Director,
        store: CampaignStore,
        history_backend: Any,
        model: str = DEFAULT_MODEL,
        summary_model: str = DEFAULT_SUMMARY_MODEL,
        window: int = DEFAULT_WINDOW,
        temperature: Optional[float] = None,
        max_tool_iterations: int = MAX_TOOL_ITERATIONS,
        max_retries: int = 2,
        timeout_seconds: float = 90.0,
        fallback_model: str = "",
        max_context_chars: int = 160_000,
        summarize: bool = True,
    ) -> None:
        self._client = client
        self._director = director
        self._store = store
        self._history = history_backend
        self._model = model
        self._summary_model = summary_model
        self._window = window
        self._temperature = temperature
        self._max_iterations = max_tool_iterations
        self._max_retries = max_retries
        self._timeout = timeout_seconds
        self._fallback_model = fallback_model
        self._max_context_chars = max_context_chars
        self._summarize = summarize
        self._tasks: set = set()
        self._turn_locks: Dict[int, asyncio.Lock] = {}
        # Who spoke to start the turn now running, per channel. Read only under that
        # channel's turn lock, so a concurrent turn elsewhere cannot change it.
        self._speaker: Dict[int, int] = {}

    # -- Narrator ----------------------------------------------------------------------

    def _turn_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._turn_locks:
            self._turn_locks[channel_id] = asyncio.Lock()
        return self._turn_locks[channel_id]

    async def narrate(
        self, *, channel_id: int, kind: str, payload: Dict[str, Any]
    ) -> NarrationResult:
        """One turn at a time per channel.

        discord.py dispatches on_message concurrently, so two players addressing Gary
        within the same few seconds would both load the same history, both run a turn
        against it, and both save -- the second write silently discarding the first
        player's entire exchange. The second turn would also have been assembled without
        the first player's message, so the NPC answered them as though nobody else had
        spoken.

        Invisible with one player, who cannot send two messages at once. The lock also
        gives the right table behaviour: the second player's turn sees the first player's
        message and the reply to it.
        """
        async with self._turn_lock(channel_id):
            self._speaker[channel_id] = int(payload.get("speaker_user_id") or 0)
            return await self._narrate_locked(channel_id, kind, payload)

    async def _narrate_locked(
        self, channel_id: int, kind: str, payload: Dict[str, Any]
    ) -> NarrationResult:
        raw_turns, summary = self._history.load(channel_id)
        kept, overflow = trim(raw_turns, self._window)

        contents = [types.Content.model_validate(t) for t in kept]
        contents.append(self._incoming(kind, payload, kept))
        self._enforce_char_budget(contents)

        # What was just said, plus the recent exchange, so a note surfaces when its
        # subject comes up rather than only when it is a party member or the location.
        system_instruction = self._system_instruction(
            summary, recent_text=_recent_text(payload, contents)
        )

        try:
            texts, contents, effects = await self._run_turn(
                channel_id, contents, system_instruction
            )
        except Exception:  # noqa: BLE001 - contract: never raise for a model failure
            log.exception("gemini turn failed (kind=%s); history not advanced", kind)
            return NarrationResult(text="")

        # Written only on success: a failed turn must not leave a half-finished exchange
        # for the next call to continue from. Turns only, never the summary -- rewriting
        # the value loaded at the start of this turn would clobber anything the background
        # summariser produced while we were waiting on the model.
        self._history.save_turns(
            channel_id, [c.model_dump(mode="json", exclude_none=True) for c in contents]
        )
        if overflow and self._summarize:
            self._spawn(self._fold_summary(channel_id, overflow, summary))

        return NarrationResult(text="\n\n".join(t for t in texts if t).strip(), effects=effects)

    # -- context (§8) ------------------------------------------------------------------

    def _system_instruction(self, summary: str, recent_text: str = "") -> List[str]:
        """Layers 1-3. Rebuilt every call; layer 4 is the contents list."""
        state = self._store.state()
        blocks = [DM_SYSTEM_INSTRUCTION, render_live_state(state)]
        moves = render_class_moves(state.get("sheets") or {})
        if moves:
            blocks.append(moves)

        subjects = list((state.get("party") or {}).keys())
        location = (state.get("scene") or {}).get("location")
        if location:
            subjects.append(location)

        memory = render_memory(
            state.get("notes") or [],
            summary,
            subjects,
            mentioned_in=recent_text,
            npcs=state.get("npcs") or {},
        )
        if memory:
            blocks.append(memory)
        return blocks

    def _enforce_char_budget(self, contents: List[types.Content]) -> None:
        """Backstop for the turn-count window.

        The window bounds turn *count*; one pathological turn can still be enormous. Drops
        from the oldest end, never touching the newest turn, which is the one being
        answered.
        """
        def size() -> int:
            return sum(len(str(c)) for c in contents)

        while len(contents) > 1 and size() > self._max_context_chars:
            contents.pop(0)

    def _incoming(
        self, kind: str, payload: Dict[str, Any], history: Sequence[Dict[str, Any]]
    ) -> types.Content:
        if kind == "player_action":
            text = attribute(
                payload.get("speaker_display_name", "someone"),
                payload.get("actor"),
                payload.get("text", ""),
            )
            return types.Content(role="user", parts=[types.Part.from_text(text=text)])

        if kind == "import_document":
            return types.Content(
                role="user",
                parts=[types.Part.from_text(text=render_document(
                    str(payload.get("filename", "document")),
                    str(payload.get("chunk", "")),
                    int(payload.get("part", 1)),
                    int(payload.get("total", 1)),
                ))],
            )

        if kind in ("propose_setting", "propose_scene"):
            # Deliberately an ordinary turn in this channel's history. Both things worth
            # proposing are things the model can only get right from what it has just
            # read -- the imported document for a premise, the last few turns for a
            # scene -- and neither of those lives in state.
            return types.Content(
                role="user",
                parts=[types.Part.from_text(text=render_proposal(
                    kind.split("_", 1)[1],
                    payload.get("already_offered") or (),
                ))],
            )

        if kind == "rules_event":
            # Never a deferred answer: the rules triggered this move, not a make_move call,
            # so a call that happens to be waiting -- another player's -- is not its.
            return types.Content(
                role="user",
                parts=[types.Part.from_text(text="RULES ENGINE: " + json.dumps(payload))],
            )

        response = dict(payload)
        if kind == "check_expired":
            response = {
                "attempted": False,
                **payload,
                "instruction": (
                    "The player did not roll. Narrate hesitation, a change of approach, "
                    "or the moment passing. Do not resolve the original action."
                ),
            }

        # The deferred answer, if a move call is genuinely waiting for one. Answered under
        # the name it was called by: a history from before moves ends on open_check.
        waiting = _awaiting_move(history)
        if waiting:
            return types.Content(
                role="user",
                parts=[types.Part.from_function_response(name=waiting, response=response)],
            )

        # History was trimmed past the call, or something arrived out of order. Fall back
        # to text rather than emitting an unpaired function_response, which Gemini rejects.
        log.info("no move awaiting a response; delivering %s as text", kind)
        return types.Content(
            role="user",
            parts=[types.Part.from_text(text="RULES ENGINE RESULT: " + json.dumps(response))],
        )

    # -- the turn ----------------------------------------------------------------------

    async def _run_turn(
        self, channel_id: int, contents: List[types.Content], system_instruction: List[str]
    ) -> Tuple[List[str], List[types.Content], List[Any]]:
        texts: List[str] = []
        effects: List[Any] = []

        for iteration in range(self._max_iterations):
            response = await self._generate(contents, system_instruction)
            parts = _parts_of(response)
            if not parts:
                log.warning("empty candidate from model (iteration %d)", iteration)
                break

            contents.append(types.Content(role="model", parts=parts))
            texts.extend(p.text for p in parts if p.text)

            calls = [p.function_call for p in parts if p.function_call]
            if not calls:
                break

            deferred, responses = self._dispatch(channel_id, calls, effects)
            if deferred:
                # make_move was called. The turn ends here with that call unanswered;
                # the roll outcome will answer it. Anything the model wanted to do after
                # asking for a check is exactly what it must not do yet.
                break

            contents.append(types.Content(role="user", parts=responses))
        else:
            log.error(
                "tool loop hit %d iterations; the model is not converging", self._max_iterations
            )

        return texts, contents, effects

    def _dispatch(
        self, channel_id: int, calls: Sequence[Any], effects: List[Any]
    ) -> Tuple[bool, List[types.Part]]:
        """Execute tool calls. Returns (deferred, function_response_parts)."""
        responses: List[types.Part] = []

        for call in calls:
            name = call.name
            args = dict(call.args or {})

            if name == T.USE_ITEM:
                heal_effects, result = self._use_item(channel_id, args)
                effects.extend(heal_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name in (T.GIVE_ITEMS, T.TAKE_ITEMS, T.GIVE_COINS, T.BUY, T.SELL):
                trade_effects, result = self._trade(channel_id, name, args)
                effects.extend(trade_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.MAKE_CAMP:
                camp_effects, result = self._director.make_camp(
                    channel_id=channel_id,
                    approaches=bool(args.get("something_approaches")),
                    watcher=str(args.get("watch", "") or ""),
                    reason=str(args.get("reason", "") or ""),
                    dungeon_or_city=bool(args.get("dungeon_or_city")),
                )
                if result.get("ok") and not args.get("something_approaches"):
                    try:
                        # Once for the party: a rest is per character, the night is not.
                        self._store.advance_time(480, reason="the party makes camp")
                    except StateError as exc:
                        result["clock"] = str(exc)
                effects.extend(camp_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.PERILOUS_JOURNEY:
                journey_effects, result = self._director.perilous_journey(
                    channel_id=channel_id, destination=str(args.get("destination", "") or ""),
                    days=int(args.get("days") or 0), trailblazer=str(args.get("trailblazer", "") or ""),
                    scout=str(args.get("scout", "") or ""), quartermaster=str(args.get("quartermaster", "") or ""),
                    wilderness=bool(args.get("wilderness")))
                effects.extend(journey_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.RECOVER:
                rest_effects, result = self._director.recover(
                    channel_id=channel_id, characters=list(args.get("characters") or []),
                    days=int(args.get("days") or 0), healer=bool(args.get("healer")),
                    reason=str(args.get("reason", "") or ""))
                if result.get("ok"):
                    try:
                        # A day at a time: the store bounds one advance at a day.
                        for _ in range(result["days"]):
                            self._store.advance_time(1440, reason="the party recovers")
                    except StateError as exc:
                        result["clock"] = str(exc)
                effects.extend(rest_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.TREASURE:
                loot_effects, result = self._treasure(channel_id, args)
                effects.extend(loot_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.FIRST_AID:
                aid_effects, result = self._director.first_aid(
                    channel_id=channel_id, hireling=str(args.get("hireling", "")),
                    patient=str(args.get("patient", "")))
                effects.extend(aid_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.BOLSTER:
                study_effects, result = self._director.bolster(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    weeks=int(args.get("weeks") or 0), what=str(args.get("what", "") or ""))
                if result.get("ok"):
                    try:
                        for _ in range(result["days"]):  # a day at a time: the store's bound
                            self._store.advance_time(1440, reason="study and practice")
                    except StateError as exc:
                        result["clock"] = str(exc)
                effects.extend(study_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name in (T.HIRE, T.UPDATE_HIRELING):
                character = str(args.get("character", ""))
                if name == T.HIRE:
                    hire_effects, result = self._director.hire(
                        channel_id=channel_id, character=character, name=str(args.get("name", "")),
                        skills={k: int(v) for k, v in dict(args.get("skills") or {}).items() if v},
                        loyalty=int(args.get("loyalty") or 0), cost=str(args.get("cost", "")),
                        found_in=str(args.get("found_in", "")))
                else:
                    paid = args.get("paid")
                    hire_effects, result = self._director.update_hireling(
                        channel_id=channel_id, character=character, name=str(args.get("name", "")),
                        loyalty=int(args.get("loyalty") or 0), paid=None if paid is None else bool(paid),
                        leaves=bool(args.get("leaves")), reason=str(args.get("reason", "") or ""))
                effects.extend(hire_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.FORWARD:
                fwd_effects, result = self._director.give_forward(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    amount=int(args.get("amount") or 0), reason=str(args.get("reason", "") or ""))
                effects.extend(fwd_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.QUALIFY_COMPENDIUM:
                q_effects, result = self._director.qualify_compendium(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    compendium_class=str(args.get("compendium_class", "")), reason=str(args.get("reason", "") or ""))
                effects.extend(q_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.STUN:
                stun_effects, result = self._director.stun(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    reason=str(args.get("reason", "") or ""), end=bool(args.get("end")))
                effects.extend(stun_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.SPELL_AUGMENTATION:
                aug_effects, result = self._director.spell_augmentation(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    spell=str(args.get("spell", "")))
                effects.extend(aug_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.CHANNEL_BALANCE:
                balance_effects, result = self._director.channel_balance(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    patient=str(args.get("patient", "")), amount=int(args.get("amount") or 0))
                effects.extend(balance_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.LEAD_THE_CHARGE:
                charge_effects, result = self._director.lead_the_charge(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    led=[str(x) for x in args.get("led") or []])
                effects.extend(charge_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.ONGOING:
                ongoing_effects, result = self._director.give_ongoing(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    amount=int(args.get("amount") or 0), reason=str(args.get("reason", "") or ""),
                    until=str(args.get("until", "") or "battle"), move=str(args.get("move", "") or ""),
                    end=bool(args.get("end")))
                effects.extend(ongoing_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.HAZARD_DAMAGE:
                hazard_effects, result = self._director.hazard_damage(
                    channel_id=channel_id, characters=[str(c) for c in args.get("characters") or []],
                    severity=str(args.get("severity", "")), reason=str(args.get("reason", "") or ""),
                    ignores_armor=bool(args.get("ignores_armor")), cover=int(args.get("cover") or 0))
                effects.extend(hazard_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.SWEAR_ENEMY:
                oath_effects, result = self._director.swear_enemy(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    enemy=str(args.get("enemy", "") or ""), released=bool(args.get("released")),
                    reason=str(args.get("reason", "") or ""))
                effects.extend(oath_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name in (T.START_QUEST, T.END_QUEST):
                if name == T.START_QUEST:
                    quest_effects, result = self._director.start_quest(
                        channel_id=channel_id, character=str(args.get("character", "")),
                        mission=str(args.get("mission", "")), subject=str(args.get("subject", "")),
                        boons=[dict(b) for b in args.get("boons") or []],
                        vows=[str(v) for v in args.get("vows") or []])
                else:
                    quest_effects, result = self._director.end_quest(
                        channel_id=channel_id, character=str(args.get("character", "")),
                        how=str(args.get("how", "")), reason=str(args.get("reason", "") or ""))
                effects.extend(quest_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.DEBILITY:
                debility_effects, result = self._director.debility(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    debility=str(args.get("debility", "")), cause=str(args.get("cause", "") or ""),
                    cured=bool(args.get("cured")))
                effects.extend(debility_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name in (T.USE_POISON, T.BREW_POISON):
                character = str(args.get("character", ""))
                if name == T.USE_POISON:
                    poison_effects, result = self._director.use_poison(
                        channel_id=channel_id, character=character, target=str(args.get("target", "") or ""),
                        poison=str(args.get("poison", "") or ""))
                else:
                    poison_effects, result = self._director.brew_poison(
                        channel_id=channel_id, character=character, reason=str(args.get("reason", "") or ""))
                effects.extend(poison_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name in (T.FORM_MOVES, T.SPEND_HOLD):
                character = str(args.get("character", ""))
                if name == T.FORM_MOVES:
                    shape_effects, result = self._director.form_moves(
                        channel_id=channel_id, character=character, moves=list(args.get("moves") or []))
                else:
                    shape_effects, result = self._director.spend_hold(
                        channel_id=channel_id, character=character, move=str(args.get("move", "") or ""),
                        revert=bool(args.get("revert")))
                effects.extend(shape_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name in (T.PREPARE_SPELLS, T.END_SPELL):
                if name == T.PREPARE_SPELLS:
                    spell_effects, result = self._director.prepare_spells(
                        channel_id=channel_id, character=str(args.get("character", "")),
                        reason=str(args.get("reason", "") or ""),
                    )
                    if result.get("ok"):
                        try:
                            self._store.advance_time(60, reason=f"{args.get('character')} prepares spells")
                        except StateError as exc:
                            result["clock"] = str(exc)
                else:
                    spell_effects, result = self._director.end_spell(
                        channel_id=channel_id, character=str(args.get("character", "")),
                        spell=str(args.get("spell", "")),
                    )
                effects.extend(spell_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.DEAL_DAMAGE:
                blow_effects, result = self._director.deal_damage(
                    channel_id=channel_id, character=str(args.get("character", "")),
                    target=str(args.get("target", "")), ranged=bool(args.get("ranged")),
                    reason=str(args.get("reason", "") or ""))
                effects.extend(blow_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.MONSTER_ATTACKS:
                attack_effects, result = self._director.monster_attacks(
                    channel_id=channel_id,
                    monster=str(args.get("monster", "")),
                    character=str(args.get("character", "")),
                    also=[str(a) for a in args.get("also") or []],
                    protector=str(args.get("protector", "") or ""),
                    cover=int(args.get("cover") or 0),
                )
                effects.extend(attack_effects)
                responses.append(types.Part.from_function_response(name=name, response=result))
                continue

            if name == T.MAKE_MOVE:
                effects.extend(self._make_move(channel_id, args))
                if len(calls) > 1:
                    log.warning("make_move issued alongside %d other call(s); skipping them", len(calls) - 1)
                return True, responses

            try:
                result = self._call_tool(name, args)
            except Exception as exc:  # noqa: BLE001 - report to the model, do not crash
                log.exception("tool %s failed", name)
                result = {"ok": False, "error": str(exc)}

            responses.append(types.Part.from_function_response(name=name, response=result))

        return False, responses

    def _make_move(self, channel_id: int, args: Dict[str, Any]) -> List[Any]:
        """Route the model's request through the Director, which owns the decision."""
        actor = str(args.get("actor", ""))
        return self._director.open_move(
            channel_id=channel_id,
            user_id=self._user_id_for(channel_id, actor),
            actor=actor,
            move=str(args.get("move", "")),
            stat=str(args.get("stat", "") or ""),
            stakes=str(args.get("stakes", "")),
            target=str(args.get("target", "") or ""),
            spell=str(args.get("spell", "") or ""),
            companion=bool(args.get("companion")),
            coins=int(args.get("coins") or 0),
            forward=bool(args.get("forward")),
            offers=[str(o) for o in args.get("offers") or []],
            preparation=bool(args.get("preparation")),
            aide=str(args.get("aide", "") or ""),
            race_move=bool(args.get("race_move")),
            ally=str(args.get("ally", "") or ""),
            weak_point=bool(args.get("weak_point")),
            two_weapons=bool(args.get("two_weapons")),
            extra_targets=[str(t) for t in args.get("extra_targets") or []],
            form_boon=str(args.get("form_boon", "") or ""),
            form_plus=str(args.get("form_plus", "") or ""),
            form_minus=str(args.get("form_minus", "") or ""),
        )

    def _user_id_for(self, channel_id: int, actor: str) -> int:
        """The Discord user who may roll this character's check.

        This decides who can resolve it, so it comes from the roster -- who said they were
        playing whom -- and never from anything the model wrote about a person. With no
        roster entry for the character, it is whoever spoke to start this turn: at a table
        nobody has set up, the person acting is the person who should roll. 0 when the
        turn had no speaker either, which leaves a check nobody can roll until it expires.
        """
        wanted = normalize_actor(actor)
        for user_id, name in self._director.players.items():
            if normalize_actor(name) == wanted:
                return int(user_id)
        return self._speaker.get(channel_id, 0)

    def _use_item(self, channel_id: int, args: Dict[str, Any]) -> Tuple[List[Any], Dict[str, Any]]:
        """Heal with an item. User and patient change in one apply, so one undo reverses
        both: the potion back in the pack and the HP back where it was."""
        state = self._store.state()
        sheets = state.get("sheets") or {}

        def find(name: str):
            return next((a for a in sheets if a.lower() == str(name or "").strip().lower()), None)

        actor = find(args.get("actor"))
        if actor is None:
            return [], {"ok": False, "error": f"{args.get('actor')!r} has no character sheet."}
        target_name = str(args.get("target") or "").strip()
        target = None
        if target_name and target_name.lower() != actor.lower():
            found = find(target_name)
            if found is None:
                return [], {"ok": False, "error": f"{target_name!r} has no character sheet."}
            target = sheets[found]
        fighting = any(r["hp"] > 0 for r in (state.get("monsters") or {}).values())
        if "antitoxin" in str(args.get("item", "")).lower():
            # "When you drink antitoxin, you're cured of one poison affecting you."
            try:
                user, _ = trade.remove_items(sheets[actor], ["antitoxin"])
            except trade.TradeError as exc:
                return [], {"ok": False, "error": str(exc)}
            self._store.apply([Mutation(MutationKind.SHEET, actor, {"sheet": user})], reason=f"{actor} drinks antitoxin")
            return [SendMessage(channel_id, f"🧪 **{actor}** drinks antitoxin.")], {
                "ok": True, "note": "They are cured of one poison affecting them. Narrate which, and end its effects."}
        try:
            user, patient, healed, remedy = healing.use(
                sheets[actor], str(args.get("item", "")), target, fighting, str(args.get("cure", "") or "")
            )
        except healing.HealingError as exc:
            return [], {"ok": False, "error": str(exc)}

        mutations = [Mutation(MutationKind.SHEET, actor, {"sheet": user})]
        if patient["name"] != actor:
            mutations.append(Mutation(MutationKind.SHEET, patient["name"], {"sheet": patient}))
        who = "themselves" if patient["name"] == actor else f"**{patient['name']}**"
        cured = str(args.get("cure", "") or "").strip().lower()
        line = (f"🩹 **{actor}** uses {remedy.lower()} on {who} — no longer {cured.title()}." if cured else
                f"🩹 **{actor}** uses {remedy.lower()} on {who} — heals {healed}, "
                f"HP {patient['hp']}/{patient['max_hp']}.")
        self._store.apply(mutations, reason=line)
        return [SendMessage(channel_id, line)], {
            "ok": True, "healed": healed, "patient": patient["name"],
            "hp": patient["hp"], "max_hp": patient["max_hp"],
        }

    def _treasure(self, channel_id: int, args: Dict[str, Any]) -> Tuple[List[Any], Dict[str, Any]]:
        """Roll a creature's treasure and give the finder the coins and valuables. The sheet
        change is journalled last, so the first undo takes the coins and gems back; a second
        leaves the creature unsearched again."""
        from .. import treasure as loot
        from ..monsters import lookup as bestiary

        state = self._store.state()
        sheets = state.get("sheets") or {}
        wanted = str(args.get("finder", "")).strip().lower()
        finder = next((a for a in sheets if a.lower() == wanted), None)
        if finder is None:
            return [], {"ok": False, "error": f"{args.get('finder')!r} has no character sheet."}
        asked = " ".join(str(args.get("creature", "")).split())
        scene = state.get("monsters") or {}
        instance = next((n for n in scene if n.lower() == asked.lower()), None)
        if instance is not None:
            record = scene[instance]
            if record.get("looted"):
                return [], {"ok": False, "error": f"{instance}'s belongings have already been searched."}
            kind_record, damage_expr = bestiary(record.get("kind", "")), record["damage"]
        else:
            kind_record = bestiary(asked)
            if kind_record is None or not kind_record.get("damage"):
                return [], {"ok": False, "error": f"No creature {asked!r} in the scene or the bestiary. "
                                                  f"In the scene: {', '.join(scene) or 'none'}."}
            damage_expr = kind_record["damage"]
        flags = loot.flags_of(kind_record)
        flags += [f for f, key in (("far from home", "far_from_home"), ("lord", "lord"),
                                   ("ancient", "ancient")) if args.get(key)]
        found = loot.roll(lambda spec: self._director.dice.roll(spec).total, damage_expr, flags)

        sheet = dict(sheets[finder])
        gear = list(sheet.get("gear") or [])
        for v in found.valuables:
            gear.append({"name": v["name"], "tags": [f"worth {v['value']} coins"], "weight": v["weight"],
                         "value": v["value"], "armor": 0, "shield": 0, "damage": 0, "piercing": 0,
                         "ammo": 0, "needs_ammo": False})
        rations = int(sheet.get("rations", 0)) + found.rations
        sheet.update(gear=gear, coins=int(sheet.get("coins", 0)) + found.coins, rations=rations)
        mutations = [Mutation(MutationKind.SHEET, finder, {"sheet": sheet})]
        if instance is not None:
            mutations.insert(0, Mutation(MutationKind.MONSTER_LOOTED, instance, {"looted": True}))
        whose = instance or asked
        line = (f"💰 **{finder}** searches {whose}'s belongings — {found.rolled}.\n"
                + "\n".join(f"• {e}" for e in found.entries)
                + (f"\n• {found.rations} ration" if found.rations else "")
                + (f"\n**{finder}** now has {sheet['coins']} coins." if found.coins else ""))
        self._store.apply(mutations, reason=f"{finder} takes {whose}'s treasure")
        return [SendMessage(channel_id, line)], {
            "ok": True, "rolled": found.roll, "found": found.entries, "coins": found.coins,
            "valuables": found.valuables, "rations": found.rations, "you_decide": found.for_gm,
            **({"magic_item_ideas": [{"name": i["name"], "tags": i["tags"], "text": i["text"]}
                                     for i in found.ideas]} if found.ideas else {}),
            "note": ("The coins and valuables are on their sheet already. Describe what they find, "
                     "honestly; name what you_decide asks you to, and give any item with give_items."),
        }

    def _trade(self, channel_id: int, name: str, args: Dict[str, Any]) -> Tuple[List[Any], Dict[str, Any]]:
        """Items and coin, on the character's sheet: one journalled change each, so every
        pickup, purchase and sale undoes. A character with no sheet keeps the old loose
        inventory list for items, and has no purse."""
        store = self._store
        sheets = store.state().get("sheets") or {}
        wanted = str(args.get("actor", "")).strip()
        actor = next((a for a in sheets if a.lower() == wanted.lower()), None)
        reason = str(args.get("reason", "")).strip()

        if actor is None:
            if name in (T.GIVE_ITEMS, T.TAKE_ITEMS):
                kind = MutationKind.ITEM_ADD if name == T.GIVE_ITEMS else MutationKind.ITEM_REMOVE
                return [], _diff_result(store.apply(
                    [Mutation(kind, wanted, {"items": [str(i) for i in args.get("items", [])]})],
                    reason=reason,
                ))
            return [], {"ok": False, "error": f"{wanted!r} has no character sheet, so no purse."}

        sheet = sheets[actor]
        try:
            if name == T.GIVE_ITEMS:
                new, got = trade.add_items(sheet, [str(i) for i in args.get("items", [])])
                line = f"🎒 **{actor}** gets {', '.join(got)}" + (f" — {reason}" if reason else "") + "."
                result = {"ok": True, "got": got}
            elif name == T.TAKE_ITEMS:
                new, lost = trade.remove_items(sheet, [str(i) for i in args.get("items", [])])
                line = f"🎒 **{actor}** loses {', '.join(lost)}" + (f" — {reason}" if reason else "") + "."
                result = {"ok": True, "lost": lost}
            elif name == T.GIVE_COINS:
                amount = int(args.get("amount", 0))
                new = trade.give_coins(sheet, amount)
                verb = "gains" if amount >= 0 else "spends"
                line = f"🪙 **{actor}** {verb} {abs(amount)} coins" + (f" — {reason}" if reason else "") + "."
                result = {"ok": True}
            elif name == T.BUY:
                price = args.get("price")
                from .. import hirelings as hired
                from .. import steadings as places
                from ..gear import EQUIPMENT, item_for, price_of

                town = places.here(store.state())
                wanted, known = item_for(str(args.get("item", "")))
                book = price_of(wanted.name) if known else None
                kind = next((i.get("kind") for i in EQUIPMENT.values() if i["name"] == wanted.name), "")
                refused = places.for_sale(town, wanted.name, kind, book) if known else ""
                if refused:
                    return [], {"ok": False, "error": refused + " (Supply finds what the steading lacks.)"}
                factor, why = places.price_factor(town, wanted.name)
                if price in (None, "") and book is not None and factor != 1.0:
                    price = max(1, int(book * factor))
                discount = hired.best(list(sheets.values()), "Minstrel")
                new, label, cost = trade.buy(sheet, str(args.get("item", "")),
                                             int(price) if price not in (None, "") else None, discount)
                line = f"🪙 **{actor}** buys {label} for {cost} coins" + (f" — {why}." if why else ".")
                result = {"ok": True, "bought": label, "cost": cost}
            else:
                offer = args.get("offer")
                from .. import steadings as places

                factor, why = places.price_factor(places.here(store.state()), str(args.get("item", "")))
                if offer is None and factor != 1.0:
                    # Need: "That resource sells for considerably more"; a Resource fetches less.
                    from ..gear import price_of

                    book = price_of(str(args.get("item", "")))
                    offer = int(book // 2 * factor) if book else None
                new, item, paid = trade.sell(sheet, str(args.get("item", "")),
                                             None if offer is None else int(offer))
                line = f"🪙 **{actor}** sells {item} for {paid} coins" + (f" — {why}." if why else ".")
                result = {"ok": True, "sold": item, "paid": paid}
        except trade.TradeError as exc:
            return [], {"ok": False, "error": str(exc)}

        store.apply([Mutation(MutationKind.SHEET, actor, {"sheet": new})], reason=line)
        result.update(coins=new.get("coins", 0), armor=new["armor"])
        return [SendMessage(channel_id, f"{line} ({new.get('coins', 0)} coins)")], result

    def _steading_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Steadings: made and changed by the model, journalled, shown every turn."""
        from .. import steadings as places
        from ..sheets import SheetError

        store = self._store
        known = store.state().get("steadings") or {}
        try:
            if name == T.CREATE_STEADING:
                kind = str(args.get("kind", "")).lower()
                try:
                    record = places.make(str(args.get("name", "")), kind, int(args.get("option", -1)),
                                         int(args.get("problem", -1)), [str(d) for d in args.get("details") or []])
                except SheetError as exc:
                    hint = f" {places.options_text(kind)}" if kind in places.KINDS else ""
                    return {"ok": False, "error": str(exc) + hint}
                key = places.slug(record["name"])
                if key in known:
                    return {"ok": False, "error": f"{record['name']} already exists: adjust_steading changes it."}
            else:
                key = places.slug(str(args.get("name", "")))
                if key not in known:
                    return {"ok": False, "error": f"No steading {args.get('name')!r}. Known: "
                                                  f"{', '.join(s['name'] for s in known.values()) or 'none'}."}
                record = places.adjust(known[key], [str(x) for x in args.get("steps") or []],
                                       [(str(a.get("tag", "")), str(a.get("detail", "") or "")) for a in args.get("add") or []],
                                       [str(x) for x in args.get("remove") or []], str(args.get("kind", "") or ""))
        except SheetError as exc:
            return {"ok": False, "error": str(exc)}
        diff = store.apply([Mutation(MutationKind.STEADING, key, {"steading": record})],
                           reason=f"steading {record['name']}: {args.get('reason') or name}")
        if not diff.ok:
            return _diff_result(diff)
        return {"ok": True, "steading": places.describe(record), "note": (
            "Recorded. When the party is there, name it as steading in set_scene: what is for sale, "
            "prices, Supply and Recruit follow its tags.")}

    def _front_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Fronts: the GM's own plans, so nothing is posted to the table -- only stored,
        journalled, and shown to the model each turn."""
        from .. import fronts
        from ..sheets import SheetError

        store = self._store
        current = dict(store.state().get("fronts") or {})
        try:
            if name == T.CREATE_FRONT:
                front = fronts.make(str(args.get("name", "")), list(args.get("dangers") or []),
                                    list(args.get("stakes") or []))
                key = fronts.slug(front["name"])
                if key in current:
                    return {"ok": False, "error": f"There is already a front called {front['name']}."}
                if len(current) >= fronts.MAX_FRONTS:
                    return {"ok": False, "error": f"At most {fronts.MAX_FRONTS} fronts; end one first."}
                store.apply([Mutation(MutationKind.FRONT, key, {"front": front})],
                            reason=f"front: {front['name']}")
                return {"ok": True, "front": front["name"],
                        "note": "Foreshadow the first portents; the state shows what comes next."}
            key = fronts.slug(str(args.get("front", "")))
            if key not in current:
                return {"ok": False, "error": f"No front {args.get('front')!r}. Fronts: "
                                              f"{', '.join(f['name'] for f in current.values()) or 'none'}."}
            front = json.loads(json.dumps(current[key]))
            danger = fronts.danger_of(front, str(args.get("danger", "")))
            reason = " ".join(str(args.get("reason", "") or "").split())
            if not reason:
                return {"ok": False, "error": "Say what happened (reason)."}
            if name == T.END_DANGER:
                danger["ended"] = reason
                result = {"ok": True, "ended": danger["name"]}
            else:
                if danger.get("ended"):
                    return {"ok": False, "error": f"{danger['name']} has ended: {danger['ended']}."}
                if int(danger["reached"]) >= len(danger["portents"]):
                    return {"ok": False, "error": f"{danger['name']}'s doom has already come."}
                happened = danger["portents"][int(danger["reached"])]
                danger["reached"] = int(danger["reached"]) + 1
                result = {"ok": True, "portent": happened, "reached": danger["reached"],
                          "of": len(danger["portents"])}
                if danger["reached"] >= len(danger["portents"]):
                    result["doom"] = (f"The impending doom comes to pass: {danger['doom']}. Make it "
                                      "real in the fiction, now or as soon as the party can see it.")
                else:
                    result["next"] = danger["portents"][danger["reached"]]
        except SheetError as exc:
            return {"ok": False, "error": str(exc)}
        store.apply([Mutation(MutationKind.FRONT, key, {"front": front})],
                    reason=f"front {front['name']}: {danger['name']}")
        return result

    def _call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        store = self._store

        if name == T.ADVANCE_TIME:
            try:
                state = store.advance_time(int(args["minutes"]), reason=str(args.get("reason", "")))
            except StateError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "world_minutes": state["world_minutes"],
                    "active_effects": [e["name"] for e in state.get("effects", [])]}

        if name == T.ADD_MONSTERS:
            state = store.state()
            # Names used here before count as taken, though the creatures are gone.
            taken = [*(state.get("monsters") or {}), *(state.get("monster_names_used") or [])]
            added, errors = prepare_monsters(list(args.get("monsters") or []), taken)
            if added:
                store.apply([Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": added})],
                            reason="creatures enter the scene")
            return {
                "ok": not errors,
                "added": [describe_monster(n, r) for n, r in added.items()],
                "what_they_are": natures(added),
                "refused": errors,
            }

        if name == T.REMOVE_MONSTERS:
            asked = args.get("names")
            scene = store.state().get("monsters") or {}
            payload: Dict[str, Any] = {}
            if asked:
                # Names given and none match must not fall through to "clear everything":
                # an empty list here would read exactly like no list at all.
                lowered = {n.lower(): n for n in scene}
                names = [lowered[str(n).lower()] for n in asked if str(n).lower() in lowered]
                if not names:
                    return {"ok": False, "error": f"None of {list(asked)} are in the scene.",
                            "remaining": sorted(scene)}
                payload = {"names": names}
            diff = store.apply([Mutation(MutationKind.MONSTERS_REMOVE, "", payload)],
                               reason="creatures leave the scene")
            return {"ok": diff.ok, "remaining": sorted(store.state().get("monsters") or {})}

        if name == T.UPSERT_NPC:
            payload: Dict[str, Any] = {}
            if args.get("name"):
                payload["name"] = str(args["name"])
            if args.get("disposition") is not None:
                payload["disposition"] = int(args["disposition"])
            if args.get("note"):
                existing = (store.state().get("npcs", {}).get(str(args["npc_id"])) or {}).get(
                    "notes", []
                )
                payload["notes"] = list(existing) + [str(args["note"])]
            return _diff_result(store.apply(
                [Mutation(MutationKind.NPC_RELATION, str(args["npc_id"]), payload)],
                reason=f"npc {args.get('name') or args['npc_id']}",
            ))

        if name == T.GIVE_ITEMS:
            return _diff_result(store.apply(
                [Mutation(MutationKind.ITEM_ADD, str(args["actor"]),
                          {"items": [str(i) for i in args.get("items", [])]})],
                reason=str(args.get("reason", "")),
            ))

        if name == T.TAKE_ITEMS:
            return _diff_result(store.apply(
                [Mutation(MutationKind.ITEM_REMOVE, str(args["actor"]),
                          {"items": [str(i) for i in args.get("items", [])]})],
                reason=str(args.get("reason", "")),
            ))

        if name in (T.CREATE_FRONT, T.ADVANCE_PORTENT, T.END_DANGER):
            return self._front_tool(name, args)

        if name in (T.CREATE_STEADING, T.ADJUST_STEADING):
            return self._steading_tool(name, args)

        if name == T.RECORD_NOTE:
            return _diff_result(store.apply(
                [Mutation(MutationKind.NOTE, str(args.get("subject", "")),
                          {"kind": str(args.get("kind", "general")), "body": str(args.get("body", ""))})],
                reason="narrative note",
            ))

        if name == T.SET_SCENE:
            payload = {"location": str(args.get("location", ""))}
            if args.get("summary"):
                payload["summary"] = str(args["summary"])
            if "steading" in args:
                # The steading they are in, or "" for none (the wilds, a dungeon).
                from ..steadings import slug

                named = str(args.get("steading") or "")
                if named and slug(named) not in (store.state().get("steadings") or {}):
                    return {"ok": False, "error": f"No steading {named!r} yet: create_steading first."}
                payload["steading"] = named
            return _diff_result(store.apply(
                [Mutation(MutationKind.SCENE, "", payload)], reason="scene change"
            ))

        return {"ok": False, "error": f"unknown tool {name!r}"}

    # -- model I/O ---------------------------------------------------------------------

    async def _generate(self, contents, system_instruction) -> Any:
        # temperature is left unset by default. Google's Gemini 3 guidance is to keep it
        # at the model default of 1.0; lowering it "may lead to unexpected behavior, such
        # as looping or degraded performance". Looping is precisely the failure the tool
        # bound in _run_turn exists to catch, so tuning this down to buy a steadier voice
        # would risk causing the thing we are guarding against. Settable for 2.x models.
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=T.tool_config(),
            temperature=self._temperature,
            # We dispatch calls ourselves: make_move must go through the Director, and
            # the SDK's automatic loop would answer it immediately rather than deferring.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        return await self._with_retries(
            lambda model: self._client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
        )

    async def _with_retries(self, make_call) -> Any:
        """Retry the primary model, then try the fallback once before giving up.

        TEMPORARY (2026-08-21): gemini-3.7-flash was eight days old and repeatedly
        answering 503 "experiencing high demand". Retries alone did not clear it. Revisit
        once the model settles — a permanent fallback quietly hides a model that has
        genuinely gone away.

        Only transient failures fall back. A 400 or an auth error would fail identically
        on the second model, and retrying it would just double the latency before the
        table sees the stall message.
        """
        try:
            return await self._attempts(make_call, self._model)
        except Exception as exc:  # noqa: BLE001
            if not self._fallback_model or not _is_transient(exc):
                raise
            log.error(
                "%s unavailable after %d attempts (%s); falling back to %s",
                self._model, self._max_retries + 1, exc, self._fallback_model,
            )
            return await self._attempts(make_call, self._fallback_model)

    async def _attempts(self, make_call, model: str) -> Any:
        """Bounded retries against one model, and — §1 — a bound on each attempt.

        Without the timeout a hung request stalls the turn indefinitely: no exception, so
        no retry, no log, and no stall message at the table. asyncio.TimeoutError is
        classed transient, so a hung attempt becomes a retry.
        """
        last: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                return await asyncio.wait_for(make_call(model), timeout=self._timeout)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not _is_transient(exc) or attempt == self._max_retries:
                    raise
                delay = (2 ** attempt) + random.random()
                log.warning(
                    "gemini call failed on %s (%s); retrying in %.1fs", model, exc, delay
                )
                await asyncio.sleep(delay)
        raise last  # pragma: no cover - unreachable

    # -- summarisation (§8) ------------------------------------------------------------

    def _spawn(self, coro) -> None:
        """Off the player-facing latency path, with a reference held so it is not GC'd."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fold_summary(
        self, channel_id: int, overflow: List[Dict[str, Any]], previous: str
    ) -> None:
        try:
            transcript = _plain_text(overflow)
            if not transcript.strip():
                return
            response = await self._client.aio.models.generate_content(
                model=self._summary_model,
                contents=[types.Content(role="user", parts=[types.Part.from_text(
                    text=f"{SUMMARY_PROMPT}\n\nEXISTING NOTES:\n{previous}\n\nNEW TURNS:\n{transcript}"
                )])],
            )
            text = "\n".join(p.text for p in _parts_of(response) if p.text).strip()
            if not text:
                return
            self._history.save_summary(channel_id, text)
            log.info("summarised %d turn(s) for channel %d", len(overflow), channel_id)
        except Exception:  # noqa: BLE001 - a lost summary costs texture, not correctness
            log.exception("summarisation failed for channel %d", channel_id)


# ---------------------------------------------------------------------------------------


def _parts_of(response: Any) -> List[types.Part]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return list(getattr(content, "parts", None) or [])


def _awaiting_move(history: Sequence[Dict[str, Any]]) -> str:
    """The name of the move call the most recent model turn left open, or ""."""
    if not history:
        return ""
    last = history[-1]
    if last.get("role") != "model":
        return ""
    for part in last.get("parts", []):
        name = (part.get("function_call") or {}).get("name")
        if name in (T.MAKE_MOVE, T.LEGACY_OPEN_CHECK):
            return name
    return ""


def _diff_result(diff) -> Dict[str, Any]:
    """Report what actually happened, including refusals, so the model narrates the truth."""
    return {
        "ok": diff.ok,
        "applied": diff.applied_count,
        "rejected": [reason for _, reason in diff.rejected],
    }


def _plain_text(turns: Sequence[Dict[str, Any]]) -> str:
    lines = []
    for turn in turns:
        for part in turn.get("parts", []):
            if part.get("text"):
                lines.append(f"{turn.get('role', '?')}: {part['text']}")
    return "\n".join(lines)


def _is_transient(exc: Exception) -> bool:
    """Retry server-side and rate-limit failures; fail fast on bad requests and auth."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code == 429 or 500 <= code < 600
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError))
