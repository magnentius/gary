"""Durable campaign state: journal, snapshot, undo (ARCHITECTURE.md §7.3).

Three properties this must have, all of which v1's "JSON database" lacked:

* **Crash safety.** Snapshots are written temp-then-``os.replace``, never truncated in
  place. A power cut mid-write loses the last few mutations, not the campaign.
* **Reversibility.** Every mutation is journaled with a computed inverse. The model will
  eventually hallucinate a state change during a session, and the DM needs one command
  to take it back.
* **Bounds.** Mutations that exceed configured limits are rejected rather than applied,
  so prompt injection (ARCHITECTURE.md §9.1) cannot mint 5000 gold in a single call.

The inverse is always computed here from observed before/after values -- never supplied
by the caller. The model must not be able to describe its own undo.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..models import (
    AppliedMutation,
    CheckOutcome,
    Mutation,
    MutationKind,
    StateDiff,
    new_id,
)
from .backend import FileStateBackend, StateBackend, atomic_write_json  # noqa: F401
from .schema import SCHEMA_VERSION, EffectRecord, Encounter, empty_state

SNAPSHOT_EVERY = 25


class StateError(RuntimeError):
    """A mutation could not be applied."""


class EncounterActive(StateError):
    """Rejected because Avrae owns the clock right now (ARCHITECTURE.md §3.1)."""


class Bounds:
    """Per-mutation limits. Anything past these needs explicit DM approval."""

    def __init__(
        self,
        max_gold_delta: int = 500,
        max_items_per_call: int = 5,
        max_minutes_per_call: int = 60 * 24,
    ) -> None:
        self.max_gold_delta = max_gold_delta
        self.max_items_per_call = max_items_per_call
        self.max_minutes_per_call = max_minutes_per_call


class CampaignStore:
    """Single-writer campaign state with an append-only journal.

    Recovery is snapshot + journal replay, so the journal is the real record and the
    snapshot is only an optimisation.

    The store holds no file handles and knows no paths -- durability is entirely the
    ``StateBackend``'s job. Swap the backend for a managed one and the process running
    this class becomes disposable (ARCHITECTURE.md §11).
    """

    def __init__(
        self,
        backend: StateBackend,
        clock: Callable[[], float],
        bounds: Optional[Bounds] = None,
        snapshot_every: int = SNAPSHOT_EVERY,
    ) -> None:
        self.backend = backend
        self._clock = clock
        self._bounds = bounds or Bounds()
        self._snapshot_every = snapshot_every
        self._lock = threading.RLock()
        self._since_snapshot = 0
        self._state: Dict[str, Any] = empty_state()
        self._journal: List[Dict[str, Any]] = []
        self._load()

    @classmethod
    def on_disk(
        cls,
        root: Path,
        clock: Callable[[], float],
        bounds: Optional[Bounds] = None,
        snapshot_every: int = SNAPSHOT_EVERY,
    ) -> "CampaignStore":
        """Convenience for the single-VM setup."""
        return cls(FileStateBackend(Path(root)), clock, bounds, snapshot_every)

    # -- durability --------------------------------------------------------------------

    def _load(self) -> None:
        """Recover as snapshot + replay of the journal tail.

        The snapshot records how many journal lines it already contains
        (``journal_offset``); everything after that index is replayed. Deriving this from
        a count rather than a per-entry flag matters, because the journal is append-only
        -- entries already written to disk can never be re-marked.
        """
        replay_from = 0
        loaded = self.backend.load_snapshot()
        if loaded is not None:
            payload, replay_from = loaded
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise StateError(
                    f"snapshot schema {payload.get('schema_version')} != {SCHEMA_VERSION}; "
                    "migration required"
                )
            self._state = payload

        self._journal = self.backend.read_journal()

        for entry in self._journal[replay_from:]:
            self._replay(entry)
        self._since_snapshot = len(self._journal) - replay_from

    def reset_campaign(self) -> None:
        """Wipe the world back to Day 1, keeping who is playing whom.

        The journal is NOT truncated. A reset is appended as its own entry and replays as
        "state becomes empty", so the history of what happened before it survives and
        remains auditable -- the campaign is gone, the record that it existed is not.

        ``players`` is carried across deliberately. It is table setup, not fiction, and
        losing it means the next check silently fails to correlate (§14 (7)).
        """
        with self._lock:
            players = dict(self._state.get("players") or {})
            self._append_journal(
                {
                    "entry_id": new_id(self._clock()),
                    "at": self._clock(),
                    "kind": "campaign_reset",
                    "target": "",
                    "payload": {"players": players},
                    "inverse": None,
                    "reason": "campaign reset",
                }
            )
            self._state = empty_state()
            self._state["players"] = players
        self.snapshot()

    def mirror_sheet(self, actor: str, descriptor: str, level: Optional[int]) -> None:
        """Cache what `!sheet` reported about a character.

        A mirror, not a mutation: Avrae owns the sheet (§3) and this is only what Gary
        last saw. It is not journalled — there is nothing to undo, and replaying a stale
        mirror would be worse than not having one. It is overwritten whenever a newer
        sheet goes past, which is what `!update` then `!sheet` is for after a level-up.
        """
        with self._lock:
            slot = self._party_slot(actor)
            slot["sheet"] = descriptor
            if level is not None:
                slot["level"] = int(level)

    def record_check_outcome(self, outcome: "CheckOutcome") -> None:
        """Journal a resolved check.

        Not a mutation: resolving a check changes no campaign state by itself. It is
        recorded because it is the one thing the whole design exists to make trustworthy
        -- a DC fixed before the dice and compared by arithmetic -- and without this
        there is no evidence afterwards that it happened that way. §2.1 offers MCP for
        "auditing what the model did"; this is what makes the outcomes auditable.
        """
        with self._lock:
            self._append_journal(
                {
                    "entry_id": new_id(self._clock()),
                    "at": self._clock(),
                    "kind": "check_outcome",
                    "target": outcome.actor,
                    "payload": {
                        "check_id": outcome.check_id,
                        "kind": outcome.kind.value,
                        "key": outcome.key,
                        "dc": outcome.dc,
                        "total": outcome.total,
                        "natural": outcome.natural,
                        "margin": outcome.margin,
                        "succeeded": outcome.succeeded,
                        "consequence": outcome.consequence,
                    },
                    "inverse": None,
                    "reason": f"{outcome.actor} {outcome.kind.value} {outcome.key}",
                }
            )

    def _replay(self, entry: Dict[str, Any]) -> None:
        kind = entry["kind"]
        if kind == "encounter_begin":
            self._state["encounter"] = entry["payload"]
            return
        if kind == "campaign_reset":
            preserved = entry.get("payload", {}).get("players", {})
            self._state = empty_state()
            self._state["players"] = dict(preserved)
            return
        if kind == "check_outcome":
            # History, not state. Replaying it must change nothing -- the campaign is
            # whatever the consequences made it, and those are journalled separately as
            # their own mutations.
            return
        if kind == "encounter_end":
            self._state["encounter"] = None
            self._state["effects"] = entry["payload"].get("effects", [])
            return
        self._apply_unchecked(
            Mutation(
                kind=MutationKind(kind),
                target=entry["target"],
                payload=entry.get("payload", {}),
            )
        )

    def _append_journal(self, entry: Dict[str, Any]) -> None:
        # Explicit sequence number. A backend that does not preserve insertion order
        # natively (anything document-based) needs this to sort by: a batched apply()
        # writes several entries within the same millisecond, so the timestamp in
        # entry_id cannot break the tie.
        entry["seq"] = len(self._journal)
        self.backend.append_journal(entry)
        self._journal.append(entry)
        self._since_snapshot += 1
        if self._since_snapshot >= self._snapshot_every:
            self.snapshot()

    def snapshot(self) -> None:
        with self._lock:
            self.backend.write_snapshot(self._state, len(self._journal))
            self._since_snapshot = 0

    # -- reads -------------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        """A deep copy. Callers must not be able to mutate state behind the journal."""
        with self._lock:
            return json.loads(json.dumps(self._state))

    def journal(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            entries = list(self._journal)
        return entries[-limit:] if limit else entries

    @property
    def encounter_active(self) -> bool:
        return self._state.get("encounter") is not None

    # -- writes ------------------------------------------------------------------------

    def apply(
        self,
        mutations: Sequence[Mutation],
        reason: str,
        source_message_id: Optional[int] = None,
        enforce_bounds: bool = True,
    ) -> StateDiff:
        """Apply a batch. Rejections do not abort the batch; they are reported.

        Partial application is the right behaviour here: if the model asks for three
        changes and one is out of bounds, silently dropping all three leaves the
        narrative and the state further apart than applying the two that were fine.
        """
        applied: List[AppliedMutation] = []
        rejected: List[Tuple[Mutation, str]] = []

        with self._lock:
            for mutation in mutations:
                if enforce_bounds:
                    problem = self._check_bounds(mutation)
                    if problem:
                        rejected.append((mutation, problem))
                        continue
                try:
                    result = self._apply_unchecked(mutation)
                except StateError as exc:
                    rejected.append((mutation, str(exc)))
                    continue

                applied.append(result)
                self._append_journal(
                    {
                        "entry_id": new_id(self._clock()),
                        "at": self._clock(),
                        "kind": mutation.kind.value,
                        "target": mutation.target,
                        "payload": mutation.payload,
                        "inverse": (
                            {
                                "kind": result.inverse.kind.value,
                                "target": result.inverse.target,
                                "payload": result.inverse.payload,
                            }
                            if result.inverse
                            else None
                        ),
                        "reason": reason,
                        "source_message_id": source_message_id,
                    }
                )

        return StateDiff(entries=tuple(applied), rejected=tuple(rejected))

    def _undone_ids(self) -> set:
        """Entry ids already reversed, derived from the journal itself.

        Kept derivable rather than stored as a mutable flag: the journal is append-only,
        so a flag set in memory would be lost on restart and the same mutation could be
        undone twice.
        """
        return {e["undoes"] for e in self._journal if e.get("undoes")}

    def undo_last(self) -> Optional[Dict[str, Any]]:
        """Reverse the most recent reversible mutation. Returns the reversed entry."""
        with self._lock:
            undone = self._undone_ids()
            for entry in reversed(self._journal):
                if entry["entry_id"] in undone or not entry.get("inverse"):
                    continue
                inv = entry["inverse"]
                self._apply_unchecked(
                    Mutation(
                        kind=MutationKind(inv["kind"]),
                        target=inv["target"],
                        payload=inv.get("payload", {}),
                    )
                )
                self._append_journal(
                    {
                        "entry_id": new_id(self._clock()),
                        "at": self._clock(),
                        "kind": inv["kind"],
                        "target": inv["target"],
                        "payload": inv.get("payload", {}),
                        "inverse": None,
                        "undoes": entry["entry_id"],
                        "reason": f"undo of {entry['entry_id']}",
                        "source_message_id": None,
                    }
                )
                return entry
        return None

    # -- bounds ------------------------------------------------------------------------

    def _check_bounds(self, mutation: Mutation) -> Optional[str]:
        b = self._bounds
        if mutation.kind is MutationKind.GOLD:
            delta = int(mutation.payload.get("delta", 0))
            if abs(delta) > b.max_gold_delta:
                return f"gold delta {delta} exceeds limit {b.max_gold_delta}; needs DM approval"
        elif mutation.kind is MutationKind.ITEM_ADD:
            items = mutation.payload.get("items", [])
            if len(items) > b.max_items_per_call:
                return f"{len(items)} items exceeds limit {b.max_items_per_call}"
        elif mutation.kind is MutationKind.TIME:
            minutes = int(mutation.payload.get("minutes", 0))
            if minutes < 0:
                return "time cannot run backwards"
            if minutes > b.max_minutes_per_call:
                return f"{minutes} minutes exceeds limit {b.max_minutes_per_call}"
        return None

    # -- application -------------------------------------------------------------------

    def _apply_unchecked(self, mutation: Mutation) -> AppliedMutation:
        handler = _HANDLERS.get(mutation.kind)
        if handler is None:
            raise StateError(f"no handler for {mutation.kind}")
        return handler(self, mutation)

    def _party_slot(self, actor: str) -> Dict[str, Any]:
        party = self._state.setdefault("party", {})
        return party.setdefault(actor, {"gold": 0, "inventory": [], "notes": []})

    # -- time & encounters -------------------------------------------------------------

    def advance_time(self, minutes: int, reason: str) -> Dict[str, Any]:
        """Advance the wall clock and expire effects.

        Refuses while an encounter is open. This is the invariant from ARCHITECTURE.md §3.1:
        during combat Avrae owns duration, and a second clock running in parallel is how
        the two views of the world drift apart.
        """
        with self._lock:
            if self.encounter_active:
                raise EncounterActive(
                    "cannot advance world time during an encounter; Avrae owns combat "
                    "duration until !init end"
                )
        diff = self.apply(
            [Mutation(kind=MutationKind.TIME, target="", payload={"minutes": int(minutes)})],
            reason=reason,
        )
        if diff.rejected:
            raise StateError(diff.rejected[0][1])
        return self.state()

    def begin_encounter(self) -> Encounter:
        """Freeze the clock and hand effect tracking to Avrae."""
        with self._lock:
            if self.encounter_active:
                return Encounter.from_dict(self._state["encounter"])
            encounter = Encounter(
                encounter_id=new_id(self._clock()),
                started_at_minute=int(self._state["world_minutes"]),
                frozen_effects=list(self._state.get("effects", [])),
            )
            self._state["encounter"] = encounter.to_dict()
            self._append_journal(
                {
                    "entry_id": new_id(self._clock()),
                    "at": self._clock(),
                    "kind": "encounter_begin",
                    "target": encounter.encounter_id,
                    "payload": encounter.to_dict(),
                    "inverse": None,
                    "reason": "!init begin observed",
                    "source_message_id": None,
                }
            )
            return encounter

    def end_encounter(self, rounds: int, surviving_effect_ids: Sequence[str]) -> Dict[str, Any]:
        """Reconcile Avrae's combat back onto the wall clock.

        Elapsed time is ``rounds x 6s``, rounded up to a whole minute. Effects Avrae no
        longer shows are dropped: where the two disagree, Avrae wins (ARCHITECTURE.md §3).
        """
        with self._lock:
            if not self.encounter_active:
                raise StateError("no active encounter")
            encounter = Encounter.from_dict(self._state["encounter"])
            self._state["encounter"] = None

            seconds = max(0, int(rounds)) * 6
            minutes = -(-seconds // 60)  # ceil

            survivors = set(surviving_effect_ids)
            dropped = [
                e for e in encounter.frozen_effects if e["effect_id"] not in survivors
            ]
            kept = [e for e in encounter.frozen_effects if e["effect_id"] in survivors]
            self._state["effects"] = kept
            self._append_journal(
                {
                    "entry_id": new_id(self._clock()),
                    "at": self._clock(),
                    "kind": "encounter_end",
                    "target": encounter.encounter_id,
                    "payload": {"effects": kept, "rounds": int(rounds)},
                    "inverse": None,
                    "reason": "!init end observed",
                    "source_message_id": None,
                }
            )

        self.apply(
            [Mutation(kind=MutationKind.TIME, target="", payload={"minutes": minutes})],
            reason=f"encounter {encounter.encounter_id} resolved over {rounds} rounds",
        )
        return {
            "encounter_id": encounter.encounter_id,
            "minutes_elapsed": minutes,
            "effects_dropped": [e["name"] for e in dropped],
            "state": self.state(),
        }


# ---------------------------------------------------------------------------------------
# Mutation handlers. Each returns the inverse it observed, so undo never trusts the caller.
# ---------------------------------------------------------------------------------------


def _h_gold(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    slot = store._party_slot(m.target)
    delta = int(m.payload.get("delta", 0))
    before = int(slot.get("gold", 0))
    if before + delta < 0:
        raise StateError(f"{m.target} has {before}gp; cannot spend {abs(delta)}gp")
    slot["gold"] = before + delta
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.GOLD, m.target, {"delta": -delta}),
        before=before,
        after=slot["gold"],
    )


def _h_item_add(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    slot = store._party_slot(m.target)
    items = [str(i) for i in m.payload.get("items", [])]
    slot.setdefault("inventory", []).extend(items)
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.ITEM_REMOVE, m.target, {"items": items}),
        after=items,
    )


def _h_item_remove(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    slot = store._party_slot(m.target)
    inventory = slot.setdefault("inventory", [])
    removed = []
    for item in m.payload.get("items", []):
        if item in inventory:
            inventory.remove(item)
            removed.append(item)
    if not removed:
        raise StateError(f"{m.target} carries none of {m.payload.get('items')}")
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.ITEM_ADD, m.target, {"items": removed}),
        after=removed,
    )


def _h_time(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    minutes = int(m.payload.get("minutes", 0))
    before = int(store._state.get("world_minutes", 0))
    after = before + minutes
    store._state["world_minutes"] = after

    kept, expired = [], []
    for raw in store._state.get("effects", []):
        effect = EffectRecord.from_dict(raw)
        (expired if effect.expires_at_minute <= after else kept).append(raw)
    store._state["effects"] = kept

    return AppliedMutation(
        mutation=m,
        # Rewinding the clock cannot restore expired effects, so time is not undoable.
        inverse=None,
        before=before,
        after={"world_minutes": after, "expired": [e["name"] for e in expired]},
    )


def _h_effect_add(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    if store.encounter_active:
        raise EncounterActive("Avrae owns effects during an encounter")
    effect = EffectRecord(
        effect_id=m.payload.get("effect_id") or new_id(),
        actor=m.target,
        name=str(m.payload["name"]),
        expires_at_minute=int(store._state["world_minutes"])
        + int(m.payload.get("duration_minutes", 0)),
        source=str(m.payload.get("source", "")),
    )
    store._state.setdefault("effects", []).append(effect.to_dict())
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.EFFECT_REMOVE, m.target, {"effect_id": effect.effect_id}),
        after=effect.to_dict(),
    )


def _h_effect_remove(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    effects = store._state.setdefault("effects", [])
    wanted = m.payload.get("effect_id")
    for i, raw in enumerate(effects):
        if raw["effect_id"] == wanted:
            effects.pop(i)
            return AppliedMutation(
                mutation=m,
                inverse=Mutation(
                    MutationKind.EFFECT_ADD,
                    m.target,
                    {
                        "effect_id": raw["effect_id"],
                        "name": raw["name"],
                        "duration_minutes": raw["expires_at_minute"]
                        - int(store._state["world_minutes"]),
                        "source": raw.get("source", ""),
                    },
                ),
                before=raw,
            )
    raise StateError(f"no effect {wanted!r}")


def _h_quest(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    quests = store._state.setdefault("quests", {})
    before = quests.get(m.target)
    quests[m.target] = {**(before or {"notes": []}), **m.payload}
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.QUEST, m.target, before) if before else None,
        before=before,
        after=quests[m.target],
    )


def _h_npc(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    npcs = store._state.setdefault("npcs", {})
    before = npcs.get(m.target)
    npcs[m.target] = {**(before or {"notes": []}), **m.payload}
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.NPC_RELATION, m.target, before) if before else None,
        before=before,
        after=npcs[m.target],
    )


def _h_note(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    note = {
        "note_id": new_id(store._clock()),
        "kind": m.payload.get("kind", "general"),
        "subject": m.target,
        "body": str(m.payload.get("body", "")),
        "at_minute": int(store._state["world_minutes"]),
    }
    store._state.setdefault("notes", []).append(note)
    return AppliedMutation(mutation=m, inverse=None, after=note)


def _h_player_character(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """``m.target`` is the discord user id as a string; payload carries {"actor": name}."""
    players = store._state.setdefault("players", {})
    before = players.get(m.target)
    players[m.target] = m.payload["actor"]
    return AppliedMutation(
        mutation=m,
        inverse=(
            Mutation(MutationKind.PLAYER_CHARACTER, m.target, {"actor": before})
            if before
            else None
        ),
        before=before,
        after=players[m.target],
    )


def _h_scene(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    before = dict(store._state.get("scene", {}))
    store._state["scene"] = {**before, **m.payload}
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.SCENE, "", before),
        before=before,
        after=store._state["scene"],
    )


_HANDLERS: Dict[MutationKind, Callable[["CampaignStore", Mutation], AppliedMutation]] = {
    MutationKind.GOLD: _h_gold,
    MutationKind.ITEM_ADD: _h_item_add,
    MutationKind.ITEM_REMOVE: _h_item_remove,
    MutationKind.TIME: _h_time,
    MutationKind.EFFECT_ADD: _h_effect_add,
    MutationKind.EFFECT_REMOVE: _h_effect_remove,
    MutationKind.QUEST: _h_quest,
    MutationKind.NPC_RELATION: _h_npc,
    MutationKind.NOTE: _h_note,
    MutationKind.SCENE: _h_scene,
    MutationKind.PLAYER_CHARACTER: _h_player_character,
}
