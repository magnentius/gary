"""Performing effects (ARCHITECTURE.md §4, §10).

The core returns ``Effect`` objects and performs nothing. This is where they become real:
messages sent, model called, outcomes journalled.

Two properties worth stating outright, because both are failure modes that only appear
mid-session:

* **Depth is bounded.** Narrating can produce more effects — the model calls
  ``make_move``, which asks a player to roll. Without a bound, a model that keeps opening
  checks in response to its own output loops until the quota runs out. The bound is
  small and exceeding it is logged loudly, because it means the model is misbehaving.
* **A narrator failure never silences the channel.** If the model errors or returns
  nothing, players get an in-character stall rather than a bot that appears dead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from ..fsm import (
    AfflictMonster, ArmorMonster, Arrive, ChangeHp, ConditionMonster, Dismiss, Effect, PromptModel, RecordOutcome, SendMessage, SpendAmmo,
    UpdateSheet, battle_over, creature_harmed, summon_fell,
)
from ..models import Mutation, MutationKind
from ..ports import Narrator, NarrationResult

log = logging.getLogger("gary.runner")

STALL_MESSAGE = "*(The DM pauses, gathering the thread...)*"


@dataclass
class RunnerStats:
    """Counters worth watching."""

    sent: int = 0
    narrated: int = 0
    narrator_failures: int = 0
    outcomes_recorded: int = 0
    depth_exceeded: int = 0


class EffectRunner:
    def __init__(
        self,
        sender,
        narrator: Narrator,
        max_depth: int = 4,
        stall_message: str = STALL_MESSAGE,
        store=None,
    ) -> None:
        self._sender = sender
        self._narrator = narrator
        self._max_depth = max_depth
        self._stall = stall_message
        self._store = store
        self.stats = RunnerStats()

    async def run(self, effects: Sequence[Effect], depth: int = 0) -> None:
        if depth > self._max_depth:
            self.stats.depth_exceeded += 1
            log.error(
                "effect depth %d exceeded limit %d; dropping %d effect(s). "
                "The model is most likely opening checks in a loop.",
                depth,
                self._max_depth,
                len(effects),
            )
            return

        for effect in effects:
            await self._run_one(effect, depth)

    async def _run_one(self, effect: Effect, depth: int) -> None:
        if isinstance(effect, SendMessage):
            await self._send(effect)
            return

        if isinstance(effect, ChangeHp):
            before = self._hp_of(effect)
            self._change_hp(effect)
            if self._store is not None and before is not None and effect.hp < before:
                state = self._store.state()
                follow = (creature_harmed(state, effect.name, effect.channel_id) if effect.kind == "monster" else
                          summon_fell(state, effect.name, effect.channel_id) if effect.hp <= 0 else [])
                if effect.kind == "monster" and effect.hp <= 0 < before:
                    follow = [*follow, *battle_over(state, effect.channel_id)]
                for more in follow:  # what the blow broke: a spell's hold, a summoned creature
                    await self._run_one(more, depth)
            return

        if isinstance(effect, ConditionMonster):
            if self._store is not None:
                diff = self._store.apply([Mutation(MutationKind.MONSTER_CONDITIONS, effect.name,
                                                   {"conditions": [dict(c) for c in effect.conditions]})],
                                         reason=f"{effect.name}: {[c.get('kind') for c in effect.conditions] or 'free'}")
                if not diff.ok:
                    log.warning("conditions on %s refused: %s", effect.name, diff.rejected)
            return

        if isinstance(effect, ArmorMonster):
            if self._store is not None:
                self._store.apply([Mutation(MutationKind.MONSTER_ARMOR, effect.name, {"armor": effect.armor})],
                                  reason=f"{effect.name}'s armor to {effect.armor}")
            return

        if isinstance(effect, Arrive):
            if self._store is not None:
                from .. import worldmap

                from .. import dungeons, steadings

                state = self._store.state()
                # A known steading or dungeon by name: land in it properly (its shops, its map), not beside it.
                town = (state.get("steadings") or {}).get(steadings.slug(effect.location))
                lair = (state.get("dungeons") or {}).get(dungeons.slug(effect.location))
                changes = [Mutation(MutationKind.SCENE, "", {
                    "location": effect.location, "steading": town["name"] if town else "",
                    "dungeon": lair["name"] if lair and not town else "", "summary": ""})]
                world = state.get("world")
                if worldmap.slug(effect.location) not in ((world or {}).get("places") or {}):
                    try:
                        changes.append(Mutation(MutationKind.WORLD, "", {
                            "world": worldmap.place(world, effect.location, "landmark", "reached by Shadow Walk")}))
                    except ValueError:
                        pass  # a full map: the scene still moves
                self._store.apply(changes, reason=f"arrive at {effect.location}")
            return

        if isinstance(effect, Dismiss):
            if self._store is not None:
                self._store.apply([Mutation(MutationKind.SHEET, effect.name, {"sheet": None})],
                                  reason=f"{effect.name} is gone")
            return

        if isinstance(effect, UpdateSheet):
            was_dead = bool(((self._store.state().get("sheets") or {}).get(effect.name) or {}).get("dead")) \
                if self._store is not None else True
            self._update_sheet(effect.name, dict(effect.changes), f"{effect.name}: {sorted(effect.changes)}")
            if effect.changes.get("dead") and not was_dead:
                from .. import opening

                await self._send(SendMessage(effect.channel_id, opening.death_notice(self._store.state(), effect.name)))
            return

        if isinstance(effect, SpendAmmo):
            self._update_sheet(effect.name, {"ammo": effect.ammo}, f"{effect.name} spends ammo")
            return

        if isinstance(effect, AfflictMonster):
            if self._store is not None:
                diff = self._store.apply([Mutation(MutationKind.MONSTER_AFFLICT, effect.name,
                                                   {"afflictions": list(effect.afflictions)})],
                                         reason=f"{effect.name} is poisoned")
                if not diff.ok:
                    log.warning("affliction on %s refused: %s", effect.name, diff.rejected)
            return

        if isinstance(effect, RecordOutcome):
            if self._store is not None:
                self._store.record_check_outcome(effect.outcome)
            else:
                log.warning("no store wired; check outcome not journalled")
            self.stats.outcomes_recorded += 1
            return

        if isinstance(effect, PromptModel):
            await self._narrate(effect, depth)
            return

        log.warning("no handler for effect %r", type(effect).__name__)

    def _hp_of(self, effect: ChangeHp) -> Optional[int]:
        if self._store is None:
            return None
        state = self._store.state()
        record = (state.get("monsters") if effect.kind == "monster" else state.get("sheets") or {}) or {}
        found = record.get(effect.name)
        return int(found["hp"]) if found and "hp" in found else None

    def _change_hp(self, effect: ChangeHp) -> None:
        """Journalled like any other change, so ``/undo`` takes a blow back."""
        if self._store is None:
            log.warning("no store wired; %s's HP change not applied", effect.name)
            return
        if effect.kind == "character":
            self._update_sheet(effect.name, {"hp": effect.hp}, f"{effect.name} to {effect.hp} HP")
            return
        mutation = Mutation(MutationKind.MONSTER_HP, effect.name, {"hp": effect.hp})
        diff = self._store.apply([mutation], reason=f"{effect.name} to {effect.hp} HP")
        if not diff.ok:
            log.warning("HP change for %s refused: %s", effect.name, diff.rejected)

    def _update_sheet(self, name: str, changes: dict, reason: str) -> None:
        if self._store is None:
            log.warning("no store wired; %s's sheet not updated", name)
            return
        sheet = (self._store.state().get("sheets") or {}).get(name)
        if sheet is None:
            log.warning("no sheet for %s; %s not applied", name, sorted(changes))
            return
        diff = self._store.apply(
            [Mutation(MutationKind.SHEET, name, {"sheet": {**sheet, **changes}})], reason=reason
        )
        if not diff.ok:
            log.warning("sheet update for %s refused: %s", name, diff.rejected)

    async def _send(self, effect: SendMessage) -> None:
        if effect.prompt is not None:
            await self._sender.send(effect.channel_id, effect.text, prompt=effect.prompt)
        else:
            await self._sender.send(effect.channel_id, effect.text)
        self.stats.sent += 1

    async def ask(self, effect: PromptModel) -> str:
        """One model turn whose answer comes back here instead of going to the channel.

        Narration is fire-and-forget by design: ``run`` posts what the model said and
        returns nothing. That is exactly wrong for a proposal, where the text has to be
        held, shown, and written only if the DM accepts it.

        Effects are dropped rather than performed. A proposal decides nothing, so a
        proposal that quietly mutated state while asking permission would be worse than
        either half on its own -- and dropping is safe here because the only effect a
        proposal could plausibly produce is one the prompt told it not to make.
        """
        try:
            result: NarrationResult = await self._narrator.narrate(
                channel_id=effect.channel_id,
                kind=effect.kind,
                payload=effect.payload,
            )
        except Exception:  # noqa: BLE001 - same contract as _narrate: never kill a turn
            self.stats.narrator_failures += 1
            log.exception("narrator failed for kind=%s", effect.kind)
            return ""

        self.stats.narrated += 1
        if result.effects:
            log.warning(
                "dropping %d effect(s) from %s; a proposal decides nothing",
                len(result.effects),
                effect.kind,
            )
        return (result.text or "").strip()

    async def _narrate(self, effect: PromptModel, depth: int) -> None:
        try:
            result: NarrationResult = await self._narrator.narrate(
                channel_id=effect.channel_id,
                kind=effect.kind,
                payload=effect.payload,
            )
        except Exception:  # noqa: BLE001 - a model failure must not kill the session
            self.stats.narrator_failures += 1
            log.exception("narrator failed for kind=%s", effect.kind)
            await self._sender.send(effect.channel_id, self._stall)
            return

        self.stats.narrated += 1

        # A request to roll is a stage direction: it goes before the prose it belongs to.
        # Observed 2026-08-21 — the model narrated a whole night's rest and the following
        # morning, and only then posted what the table had to do about it.
        #
        # Only SendMessage is hoisted. The rest keep their place, because effects may
        # include a PromptModel (a make_move rejection returns one) and running that
        # early would emit a nested turn's narration ahead of this one's.
        posts = [e for e in result.effects if isinstance(e, SendMessage)]
        rest = [e for e in result.effects if not isinstance(e, SendMessage)]
        for post in posts:
            await self._send(post)

        text = (result.text or "").strip()
        if text:
            await self._sender.send(effect.channel_id, text)
            self.stats.sent += 1
        elif not result.effects:
            # Nothing said and nothing to do. Silence here reads as a crashed bot.
            log.warning("narrator returned nothing for kind=%s", effect.kind)
            await self._sender.send(effect.channel_id, self._stall)

        if rest:
            await self.run(rest, depth + 1)
