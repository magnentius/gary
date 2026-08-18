"""Between-session campaign prep (ARCHITECTURE.md §2.1).

§2.1 keeps MCP for exactly one reason: the campaign state should be reachable from
another client — Claude Desktop, an IDE — while you plan next week, without running Gary
or Discord. This module is that capability. ``adapters/mcp_server.py`` is a thin binding
over it.

Kept free of the MCP SDK on purpose. The SDK needs Python 3.10+, and confining it to the
binding means everything with behaviour worth trusting is testable wherever the core is.

**Read-only is the default.** Writes require the campaign lock (§7.3). If Gary holds it
— mid-session, at the table — this degrades to reads and says why, rather than corrupting
a journal that someone is actively appending to.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from .models import Mutation, MutationKind
from .state.schema import format_world_time
from .state.store import CampaignStore

log = logging.getLogger("gary.prep")

READ_ONLY_REASON = (
    "This campaign is locked by another process (usually Gary running a live session). "
    "Reads are available; writes are refused to protect the journal."
)


class ReadOnly(RuntimeError):
    """A write was attempted without the lock."""


class CampaignPrep:
    """Read and edit a campaign outside a session.

    ``writable`` is decided by the caller, which holds the lock. Passing it in rather than
    acquiring it here keeps lock ownership with the process lifecycle where it belongs.
    """

    def __init__(
        self,
        store: CampaignStore,
        history_backend: Any = None,
        writable: bool = False,
    ) -> None:
        self._store = store
        self._history = history_backend
        self._writable = writable

    @property
    def writable(self) -> bool:
        return self._writable

    def _require_write(self) -> None:
        if not self._writable:
            raise ReadOnly(READ_ONLY_REASON)

    # -- reads -------------------------------------------------------------------------

    def overview(self) -> str:
        """A human-readable snapshot. The first thing you want when opening a campaign."""
        state = self._store.state()
        lines = [
            f"# Campaign overview",
            f"- World time: {format_world_time(state)}",
        ]
        scene = state.get("scene") or {}
        if scene.get("location"):
            lines.append(f"- Location: {scene['location']}")
        if scene.get("summary"):
            lines.append(f"- Situation: {scene['summary']}")
        if state.get("encounter"):
            lines.append("- **Combat is active.** Avrae owns hit points and durations.")

        party = state.get("party") or {}
        if party:
            lines.append("\n## Party")
            for actor, data in sorted(party.items()):
                inventory = ", ".join(data.get("inventory", [])) or "nothing recorded"
                lines.append(f"- **{actor}** — {data.get('gold', 0)}gp; carrying {inventory}")

        effects = state.get("effects") or []
        if effects:
            lines.append("\n## Active effects")
            for e in effects:
                lines.append(f"- {e['actor']}: {e['name']} (expires minute {e['expires_at_minute']})")

        quests = state.get("quests") or {}
        if quests:
            lines.append("\n## Quests")
            for qid, q in sorted(quests.items()):
                lines.append(f"- [{q.get('status', 'open')}] **{q.get('title', qid)}** (`{qid}`)")

        npcs = state.get("npcs") or {}
        if npcs:
            lines.append("\n## NPCs")
            for nid, npc in sorted(npcs.items()):
                lines.append(
                    f"- **{npc.get('name', nid)}** (`{nid}`) — disposition "
                    f"{npc.get('disposition', 0)}"
                )

        if not self._writable:
            lines.append(f"\n---\n*Read-only. {READ_ONLY_REASON}*")
        return "\n".join(lines)

    def state(self) -> Dict[str, Any]:
        return self._store.state()

    def journal(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent mutations, newest last.

        The audit trail for everything the model did to the campaign. Each entry carries
        the reason and the Discord message that triggered it, so an odd change can be
        traced back to the moment it happened.
        """
        return self._store.journal(limit=limit)

    def notes(self, subject: Optional[str] = None) -> List[Dict[str, Any]]:
        all_notes = self._store.state().get("notes") or []
        if subject is None:
            return all_notes
        wanted = subject.lower()
        return [n for n in all_notes if wanted in str(n.get("subject", "")).lower()]

    def npcs(self) -> Dict[str, Any]:
        return self._store.state().get("npcs") or {}

    def quests(self) -> Dict[str, Any]:
        return self._store.state().get("quests") or {}

    def last_session(self, channel_id: int) -> Dict[str, Any]:
        """The rolling summary and recent turns — what actually happened last time."""
        if self._history is None:
            return {"summary": "", "turns": 0, "recent": []}
        turns, summary = self._history.load(channel_id)
        recent = []
        for turn in turns[-12:]:
            for part in turn.get("parts", []):
                if part.get("text"):
                    recent.append({"role": turn.get("role", "?"), "text": part["text"]})
        return {"summary": summary, "turns": len(turns), "recent": recent}

    # -- writes ------------------------------------------------------------------------

    def _apply(self, mutations: Sequence[Mutation], reason: str) -> Dict[str, Any]:
        self._require_write()
        # Prep is a human editing their own campaign deliberately, not a model being
        # steered by player text, so the §9.1 injection bounds do not apply here.
        diff = self._store.apply(mutations, reason=f"prep: {reason}", enforce_bounds=False)
        return {
            "ok": diff.ok,
            "applied": diff.applied_count,
            "rejected": [r for _, r in diff.rejected],
        }

    def upsert_npc(
        self,
        npc_id: str,
        name: Optional[str] = None,
        disposition: Optional[int] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if disposition is not None:
            payload["disposition"] = int(disposition)
        if note:
            existing = (self.npcs().get(npc_id) or {}).get("notes", [])
            payload["notes"] = list(existing) + [note]
        return self._apply(
            [Mutation(MutationKind.NPC_RELATION, npc_id, payload)], reason=f"npc {npc_id}"
        )

    def upsert_quest(
        self,
        quest_id: str,
        title: Optional[str] = None,
        status: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if status is not None:
            payload["status"] = status
        if note:
            existing = (self.quests().get(quest_id) or {}).get("notes", [])
            payload["notes"] = list(existing) + [note]
        return self._apply(
            [Mutation(MutationKind.QUEST, quest_id, payload)], reason=f"quest {quest_id}"
        )

    def record_note(self, kind: str, subject: str, body: str) -> Dict[str, Any]:
        """Plant something for the model to find later.

        Notes are retrieved into context when their subject is present (§8 layer 3), so
        this is how you seed foreshadowing before a session rather than improvising it.
        """
        return self._apply(
            [Mutation(MutationKind.NOTE, subject, {"kind": kind, "body": body})],
            reason=f"note on {subject}",
        )

    def set_scene(self, location: str, summary: str = "") -> Dict[str, Any]:
        payload = {"location": location}
        if summary:
            payload["summary"] = summary
        return self._apply([Mutation(MutationKind.SCENE, "", payload)], reason="scene")

    def undo_last(self) -> Dict[str, Any]:
        """Reverse the most recent reversible mutation.

        The same escape hatch as ``?dm undo``, available after the session when you notice
        the model quietly granted someone a magic sword.
        """
        self._require_write()
        entry = self._store.undo_last()
        if entry is None:
            return {"ok": False, "error": "nothing left to undo"}
        return {
            "ok": True,
            "reversed": {
                "entry_id": entry["entry_id"],
                "kind": entry["kind"],
                "target": entry["target"],
                "reason": entry.get("reason", ""),
            },
        }
