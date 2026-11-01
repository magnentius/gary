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
    MoveOutcome,
    Mutation,
    MutationKind,
    StateDiff,
    new_id,
)
from .backend import FileStateBackend, StateBackend, atomic_write_json  # noqa: F401
from .schema import (
    DEFAULT_EPOCH_DATE,
    SCHEMA_VERSION,
    EffectRecord,
    default_epoch_date,
    empty_state,
)

SNAPSHOT_EVERY = 25


class StateError(RuntimeError):
    """A mutation could not be applied."""


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

        if loaded is None and not self._journal:
            # A campaign that has never been written to. Anchor its calendar now, while
            # "now" still means anything: the epoch is today a millennium ago, fixed for
            # the life of the campaign.
            self._anchor_calendar()

    def reset_campaign(self) -> None:
        """Wipe everything back to Day 1: state, roster, and the journal itself.

        The journal goes too. Keeping it meant `journal.ndjson` still held the dead
        campaign's notes, gold and NPC dispositions after a reset that was supposed to
        have ended it -- and a reset you can still read is not one a person will trust.
        This is the only operation in the store that destroys history, and it is
        unrecoverable by design.

        The roster goes as well. An earlier version kept it, on the grounds that who is
        playing whom is table setup rather than fiction; a reset that leaves anything
        behind is a reset with an exception to remember, and forgetting `/playing`
        after one is loud (the boot warning, and make_move refusing an unknown actor)
        rather than silent.

        **Order matters, and only one order is safe.** Truncate first, snapshot last.
        A crash anywhere before the snapshot leaves the old one with an offset past the
        end of a short journal, which replays as the old campaign: the reset simply did
        not happen, and re-running it works. Snapshotting first instead would write an
        empty snapshot at offset 0 over a journal that is still full, and replay would
        walk the whole dead campaign back into existence.
        """
        with self._lock:
            self.backend.truncate_journal()
            self._journal.clear()
            self._state = empty_state()
            self._since_snapshot = 0
        # A reset is a new campaign, so it gets a new epoch rather than the old one's.
        # Anchored before the snapshot, not after: snapshotting first leaves a stored
        # snapshot carrying DEFAULT_EPOCH_DATE while the journal carries the real one.
        # Replay reconciles them, so the campaign loads correctly either way -- but the
        # snapshot is what anyone looking at the campaign reads first, and one that
        # disagrees with the journal costs an investigation to dismiss.
        self._anchor_calendar()
        self.snapshot()

    def _anchor_calendar(self) -> None:
        self.apply(
            [Mutation(MutationKind.CALENDAR, "", {"epoch_date": default_epoch_date()})],
            reason="campaign calendar anchored",
        )

    def record_check_outcome(self, outcome: "MoveOutcome") -> None:
        """Journal a resolved move.

        Not a mutation: resolving a move changes no campaign state by itself. It is
        recorded because it is the one thing the whole design exists to make trustworthy
        -- a move and modifier fixed before the dice, resolved by its band -- and without
        this there is no evidence afterwards that it happened that way. Auditing what the
        model did to a campaign rests entirely on this record.
        """
        with self._lock:
            self._append_journal(
                {
                    "entry_id": new_id(self._clock()),
                    "at": self._clock(),
                    "kind": "move_outcome",
                    "target": outcome.actor,
                    "payload": {
                        "check_id": outcome.check_id,
                        "move": outcome.move,
                        "stat": outcome.stat,
                        "modifier": outcome.modifier,
                        "faces": list(outcome.faces),
                        "dice": outcome.dice,
                        "total": outcome.total,
                        "band": outcome.band,
                        "chosen": list(outcome.chosen),
                        "auto_chosen": outcome.auto_chosen,
                        "enemy_attacks": outcome.enemy_attacks,
                        "damage": outcome.damage,
                        "enemy_attack": outcome.enemy_attack,
                        "healing": outcome.healing,
                        "spell": outcome.spell,
                        "shape": outcome.shape,
                    },
                    "inverse": None,
                    "reason": f"{outcome.actor} {outcome.move}",
                }
            )

    def _replay(self, entry: Dict[str, Any]) -> None:
        kind = entry["kind"]
        if kind in ("encounter_begin", "encounter_end"):
            # The Avrae initiative handoff, which no longer exists. Journals written
            # before its removal still carry these, and they changed only the clock-freeze
            # flag and effect ownership -- neither of which means anything now.
            return
        if kind == "campaign_reset":
            # Reset truncates the journal now, so nothing writes this kind any more.
            # Journals written before that change still contain one, and dropping the
            # handler would replay those campaigns straight through their own reset.
            self._state = empty_state()
            return
        if kind in ("check_outcome", "move_outcome"):
            # History, not state. check_outcome is the d20 record from before moves. Replaying it must change nothing -- the campaign is
            # whatever the consequences made it, and those are journalled separately as
            # their own mutations.
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
                # A reset is a floor, not another entry to skip. Everything below it
                # belongs to a campaign that no longer exists, and its inverses are
                # written against a state that is gone: some raise (undoing an item grant
                # from a party that no longer carries it), and the rest are worse, because
                # they apply cleanly and quietly seed the fresh campaign with the dead
                # one's setting, scene or NPCs. `inverse: None` alone does not stop this --
                # that only makes the reset unreversible, and the loop skips right past it.
                if entry["kind"] == "campaign_reset":
                    return None
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

    # -- time --------------------------------------------------------------------------

    def advance_time(self, minutes: int, reason: str) -> Dict[str, Any]:
        """Advance the wall clock and expire effects."""
        diff = self.apply(
            [Mutation(kind=MutationKind.TIME, target="", payload={"minutes": int(minutes)})],
            reason=reason,
        )
        if diff.rejected:
            raise StateError(diff.rejected[0][1])
        return self.state()


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


def _h_sheet(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """``m.target`` is the character name; payload carries {"sheet": dict}, or
    {"sheet": None} to remove one -- which is what undoing a first sheet needs."""
    sheets = store._state.setdefault("sheets", {})
    before = sheets.get(m.target)
    after = m.payload.get("sheet")
    if after is None:
        sheets.pop(m.target, None)
    else:
        sheets[m.target] = dict(after)
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.SHEET, m.target, {"sheet": before}),
        before=before,
        after=after,
    )


def _h_monsters_add(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """payload {"monsters": {instance name: record}}. Undone by removing exactly those."""
    scene = store._state.setdefault("monsters", {})
    added = dict(m.payload.get("monsters") or {})
    scene.update(added)
    # Remembered after they are gone, so a later wave here is "Goblin 6", never a second
    # "Goblin 1" the table could mistake for the one that fell. A new location forgets.
    used = store._state.setdefault("monster_names_used", [])
    used.extend(n for n in added if n not in used)
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.MONSTERS_REMOVE, "", {"names": list(added)}),
        after=list(added),
    )


def _h_monsters_remove(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """payload {"names": [...]}, or no names to clear the scene. Undone by putting the
    same records back, HP and all."""
    scene = store._state.setdefault("monsters", {})
    names = m.payload.get("names")
    gone = {n: scene.pop(n) for n in list(scene if names is None else names) if n in scene}
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": gone}) if gone else None,
        before=list(gone),
    )


def _h_monster_hp(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """``m.target`` is the instance name; payload {"hp": int}. HP may go below 0; a
    creature at 0 or less is defeated."""
    record = store._state.setdefault("monsters", {}).get(m.target)
    if record is None:
        raise StateError(f"no creature named {m.target!r} in this scene")
    before = int(record["hp"])
    record["hp"] = int(m.payload["hp"])
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.MONSTER_HP, m.target, {"hp": before}),
        before=before,
        after=record["hp"],
    )


def _h_monster_looted(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """``m.target`` is the instance name; payload {"looted": bool}. Its treasure has been
    rolled, so it is not rolled twice. Undone by putting the flag back."""
    record = store._state.setdefault("monsters", {}).get(m.target)
    if record is None:
        raise StateError(f"no creature named {m.target!r} in this scene")
    before = bool(record.get("looted"))
    record["looted"] = bool(m.payload.get("looted", True))
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.MONSTER_LOOTED, m.target, {"looted": before}),
        before=before,
        after=record["looted"],
    )


def _h_monster_afflict(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """``m.target`` is the instance name; payload {"afflictions": [...]}: the poisons working on
    it (Bloodweed, Serpent's Tears). Undone by putting the old list back."""
    record = store._state.setdefault("monsters", {}).get(m.target)
    if record is None:
        raise StateError(f"no creature named {m.target!r} in this scene")
    before = list(record.get("afflictions") or [])
    record["afflictions"] = list(m.payload.get("afflictions") or [])
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.MONSTER_AFFLICT, m.target, {"afflictions": before}),
        before=before,
        after=record["afflictions"],
    )


def _h_front(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """payload {"front": record}, or {"front": None} to drop it. Undone by putting back what
    was there -- a portent ticked off comes un-ticked."""
    fronts = store._state.setdefault("fronts", {})
    before = fronts.get(m.target)
    record = m.payload.get("front")
    if record is None:
        fronts.pop(m.target, None)
    else:
        fronts[m.target] = record
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.FRONT, m.target, {"front": before}),
        before=before,
        after=record,
    )


def _h_steading(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """payload {"steading": record}, or None to drop it. Undone by putting back what was there."""
    steadings = store._state.setdefault("steadings", {})
    before = steadings.get(m.target)
    record = m.payload.get("steading")
    if record is None:
        steadings.pop(m.target, None)
    else:
        steadings[m.target] = record
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.STEADING, m.target, {"steading": before}),
        before=before,
        after=record,
    )


def _h_session_questions(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """payload {"questions": [...]}, [] for the book's three. Undone by putting back the last."""
    before = list(store._state.get("session_questions") or [])
    store._state["session_questions"] = list(m.payload.get("questions") or [])
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.SESSION_QUESTIONS, "", {"questions": before}),
        before=before,
        after=store._state["session_questions"],
    )


def _h_opening(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """payload: the fields of the opening to set ({"stage": ..., "starter": ...}). Undone by
    putting back what was there."""
    before = dict(store._state.get("opening") or {})
    # "__state__" is the undo: the whole opening as it was, not merged in.
    store._state["opening"] = (dict(m.payload["__state__"]) if "__state__" in m.payload
                               else {**before, **m.payload})
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.OPENING, "", {"__state__": before}),
        before=before,
        after=store._state["opening"],
    )


def _h_setting(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    before = store._state.get("setting", "")
    store._state["setting"] = str(m.payload.get("text", ""))
    return AppliedMutation(
        mutation=m,
        inverse=Mutation(MutationKind.SETTING, "", {"text": before}),
        before=before,
        after=store._state["setting"],
    )


def _h_calendar(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    """Set the epoch the world clock counts from.

    Journalled rather than written straight into the snapshot, so that a campaign which
    loses its snapshot rebuilds to the same dates. The journal is the record and the
    snapshot is an optimisation (§7.3); an epoch that lived only in the snapshot would
    quietly make that false, and every date in the campaign would shift by the gap
    between its real start and DEFAULT_EPOCH_DATE.
    """
    calendar = dict(store._state.get("calendar", {}))
    before = calendar.get("epoch_date", DEFAULT_EPOCH_DATE)
    calendar["epoch_date"] = str(m.payload.get("epoch_date", before))
    store._state["calendar"] = calendar
    return AppliedMutation(
        mutation=m,
        # Deliberately irreversible. The epoch is set once, at campaign creation, and
        # nothing offers to change it -- so an inverse here would only ever be reached by
        # `/undo` running out of real mutations, at which point it would silently
        # rewind every date in the campaign and report that it had undone something.
        inverse=None,
        before=before,
        after=calendar["epoch_date"],
    )


def _h_scene(store: "CampaignStore", m: Mutation) -> AppliedMutation:
    before = dict(store._state.get("scene", {}))
    store._state["scene"] = {**before, **m.payload}
    if store._state["scene"].get("location") != before.get("location"):
        # Somewhere new: creature numbering starts again from what is actually here.
        store._state["monster_names_used"] = list(store._state.get("monsters") or {})
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
    MutationKind.SETTING: _h_setting,
    MutationKind.FRONT: _h_front,
    MutationKind.STEADING: _h_steading,
    MutationKind.SESSION_QUESTIONS: _h_session_questions,
    MutationKind.OPENING: _h_opening,
    MutationKind.CALENDAR: _h_calendar,
    MutationKind.PLAYER_CHARACTER: _h_player_character,
    MutationKind.SHEET: _h_sheet,
    MutationKind.MONSTERS_ADD: _h_monsters_add,
    MutationKind.MONSTERS_REMOVE: _h_monsters_remove,
    MutationKind.MONSTER_HP: _h_monster_hp,
    MutationKind.MONSTER_LOOTED: _h_monster_looted,
    MutationKind.MONSTER_AFFLICT: _h_monster_afflict,
}
