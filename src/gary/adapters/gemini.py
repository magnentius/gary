"""The Gemini adapter (ARCHITECTURE.md §5, §8, §9.1, §12.3).

Implements ``Narrator``. Assembles context, calls the model, dispatches its tool calls,
and returns prose plus whatever effects those calls produced.

The one non-obvious idea here is how a check's result gets back to the model.

**A check is a deferred function call.** When the model calls ``open_check``, the call is
*deliberately left unanswered*. Gary posts the Avrae command and the turn ends with that
call still open. Minutes later the player rolls, and the outcome is delivered as the
``function_response`` to that original call. This is not a trick — it is exactly what
happened: the model asked for a check, and this is the answer. It gives three things for
free:

* The conversation stays well-formed, with every call eventually paired to a response.
* The outcome arrives over the **tool transport**, so its trust comes from the channel it
  travelled on rather than from any string in it (§9.1).
* The model cannot mistake the result for a player's claim about the result.

Expiry answers the same call with ``attempted: false``. A bad skill key answers it with an
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
    DM_SYSTEM_INSTRUCTION,
    attribute,
    render_live_state,
    render_memory,
)
from ..encounters import benchmark, lookup, weigh
from ..fsm import Director, SendMessage
from ..templates import REST_MINUTES, render_attack_command, render_rest_command, TemplateError, render_coin_command
from ..models import CheckKind, Mutation, MutationKind
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
# Latency and cost are live risks here -- §14 (4) and (5) -- and a turn costs two model
# calls around a human copy-paste, so Flash is the right shape as well as the current one.
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
        coins_command: str = "game coins",
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
        self._coins_command = coins_command
        self._max_context_chars = max_context_chars
        self._summarize = summarize
        self._tasks: set = set()
        self._turn_locks: Dict[int, asyncio.Lock] = {}

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

        # The deferred answer, if an open_check call is genuinely waiting for one.
        if _awaits_open_check(history):
            return types.Content(
                role="user",
                parts=[types.Part.from_function_response(name=T.OPEN_CHECK, response=response)],
            )

        # History was trimmed past the call, or something arrived out of order. Fall back
        # to text rather than emitting an unpaired function_response, which Gemini rejects.
        log.info("no open_check awaiting a response; delivering %s as text", kind)
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
                # open_check was called. The turn ends here with that call unanswered;
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

            if name == T.ATTACK:
                try:
                    command = render_attack_command(
                        str(args.get("weapon", "")), str(args.get("target", ""))
                    )
                except TemplateError as exc:
                    responses.append(types.Part.from_function_response(
                        name=name, response={"ok": False, "error": str(exc)}))
                    continue
                actor = str(args.get("actor", "")).strip() or "Someone"
                effects.append(SendMessage(
                    channel_id, f"**{actor}** — on your turn:\n`{command}`"
                ))
                responses.append(types.Part.from_function_response(
                    name=name,
                    response={
                        "ok": True,
                        "posted": command,
                        "note": "ALREADY POSTED to the channel — do not repeat it in your narration. "
                                "Avrae rolls it against the target's AC and applies the "
                                "damage itself. You are told nothing until it reports, and "
                                "you must not decide whether it hit.",
                    },
                ))
                continue

            if name == T.START_COMBAT:
                names = [str(m) for m in (args.get("monsters") or []) if str(m).strip()]
                unknown = [m for m in names if lookup(m) is None]
                adds = "\n".join(
                    f"`!init madd {(lookup(m) or {}).get('name', m)}`" for m in names
                )
                effects.append(SendMessage(
                    channel_id,
                    "**Roll for initiative.**\n"
                    "`!init begin`\n"
                    "Everyone joins with `!init join`."
                    + (f"\nThen add the opposition:\n{adds}" if adds else "")
                    + "\nStart the first round with `!init next`.",
                ))
                responses.append(types.Part.from_function_response(
                    name=name,
                    response={
                        "ok": True,
                        "monsters": names,
                        "not_in_srd": unknown,
                        "note": "ALREADY POSTED to the channel — do not repeat it in your narration. "
                                "Initiative is Avrae's from here: it owns turn order, hit "
                                "points and durations, and the world clock is frozen until "
                                "!init end. Describe what happens; do not recalculate it.",
                    },
                ))
                continue

            if name == T.REQUEST_REST:
                kind = str(args.get("kind", "")).strip().lower()
                try:
                    command = render_rest_command(kind)
                except TemplateError as exc:
                    responses.append(types.Part.from_function_response(
                        name=name, response={"ok": False, "error": str(exc)}))
                    continue
                minutes = REST_MINUTES[kind]
                try:
                    # ONCE for the party. A rest is per-character but the clock is
                    # per-world, so advancing per character would burn four nights on one.
                    state = self._store.advance_time(minutes, reason=f"{kind} rest")
                except Exception as exc:  # noqa: BLE001 - EncounterActive and friends
                    responses.append(types.Part.from_function_response(
                        name=name, response={"ok": False, "error": str(exc)}))
                    continue
                effects.append(SendMessage(
                    channel_id,
                    f"The party takes a **{kind} rest**. "
                    f"Everyone run:\n`{command}`",
                ))
                responses.append(types.Part.from_function_response(
                    name=name,
                    response={
                        "ok": True,
                        "minutes_elapsed": minutes,
                        "world_minutes": state["world_minutes"],
                        "posted": command,
                        "note": "ALREADY POSTED to the channel — do not repeat it in your narration. "
                                "The clock has moved for the whole party. Each player "
                                "resets their own sheet; you are not told when they do.",
                    },
                ))
                continue

            if name == T.REQUEST_PAYMENT:
                try:
                    command = render_coin_command(
                        str(args.get("amount", "")), self._coins_command
                    )
                except TemplateError as exc:
                    responses.append(
                        types.Part.from_function_response(
                            name=name, response={"ok": False, "error": str(exc)}
                        )
                    )
                    continue
                actor = str(args.get("actor", "")).strip() or "Someone"
                reason = str(args.get("reason", "")).strip()
                effects.append(
                    SendMessage(
                        channel_id,
                        f"**{actor}**"
                        + (f" — {reason}" if reason else "")
                        + f"\n`{command}`",
                    )
                )
                responses.append(
                    types.Part.from_function_response(
                        name=name,
                        response={
                            "ok": True,
                            "posted": command,
                            "note": "ALREADY POSTED to the channel — do not repeat it in your narration. "
                                    "The player runs it. You will not be told the result "
                                    "and must not assume it succeeded.",
                        },
                    )
                )
                continue

            if name == T.OPEN_CHECK:
                effects.extend(self._open_check(channel_id, args))
                if len(calls) > 1:
                    log.warning("open_check issued alongside %d other call(s); skipping them", len(calls) - 1)
                return True, responses

            try:
                result = self._call_tool(name, args)
            except Exception as exc:  # noqa: BLE001 - report to the model, do not crash
                log.exception("tool %s failed", name)
                result = {"ok": False, "error": str(exc)}

            responses.append(types.Part.from_function_response(name=name, response=result))

        return False, responses

    def _open_check(self, channel_id: int, args: Dict[str, Any]) -> List[Any]:
        """Route the model's request through the Director, which owns the decision."""
        try:
            kind = CheckKind(str(args.get("kind", "check")).lower())
        except ValueError:
            kind = CheckKind.CHECK

        return self._director.open_check(
            channel_id=channel_id,
            user_id=self._user_id_for(channel_id, str(args.get("actor", ""))),
            actor=str(args.get("actor", "")),
            kind=kind,
            key=str(args.get("key", "")),
            dc=int(args.get("dc", 10)),
            on_success=str(args.get("on_success", "")),
            on_failure=str(args.get("on_failure", "")),
            stakes=str(args.get("stakes", "")),
        )

    def _user_id_for(self, channel_id: int, actor: str) -> int:
        """Best-effort Discord user for a character name.

        Correlation does not depend on getting this right — the registry also matches on
        the slash-command invoker, the reply reference, and the actor name (§6.2). It only
        improves the ordering of those signals.
        """
        for check in self._director.registry.open_checks(channel_id):
            if check.actor.lower() == actor.lower():
                return check.user_id
        return 0

    def _call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        store = self._store

        if name == T.ADVANCE_TIME:
            try:
                state = store.advance_time(int(args["minutes"]), reason=str(args.get("reason", "")))
            except StateError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "world_minutes": state["world_minutes"],
                    "active_effects": [e["name"] for e in state.get("effects", [])]}

        if name == T.WEIGH_ENCOUNTER:
            state = store.state()
            bench = benchmark(
                state.get("party") or {}, (state.get("players") or {}).values()
            )
            if bench is None:
                return {
                    "ok": False,
                    "error": "No character levels known yet — nobody has run !sheet, so "
                             "there is nothing to weigh against.",
                }
            return weigh(bench, list(args.get("monsters") or []))

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
            # We dispatch calls ourselves: open_check must go through the Director, and
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


def _awaits_open_check(history: Sequence[Dict[str, Any]]) -> bool:
    """True when the most recent turn is a model turn that called open_check."""
    if not history:
        return False
    last = history[-1]
    if last.get("role") != "model":
        return False
    return any(
        (part.get("function_call") or {}).get("name") == T.OPEN_CHECK
        for part in last.get("parts", [])
    )


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
