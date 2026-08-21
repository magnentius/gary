"""Conversation history (ARCHITECTURE.md §8, §11.1).

Layer 4 of the context contract — the recent turns — has to survive a restart like
everything else, or the disposable-VM claim has a hole in it.

Losing history is far less damaging than losing a pending check: the authoritative facts
are rebuilt from the campaign store every call (layer 2), so a restart costs conversational
texture, not correctness. It is still worth persisting, and it costs one more backend.

Turns are stored as **plain dicts**, not google-genai objects. That keeps this module — and
therefore the core — free of any LLM SDK; the Gemini adapter converts at its own boundary.

Deliberately *not* the journal. The journal is for reversible campaign mutations, and a
line of dialogue is neither reversible nor a mutation. Mixing them would make ``?dm undo``
walk backwards through conversation.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from .backend import atomic_write_json

# Turns kept verbatim before older ones are folded into the summary.
DEFAULT_WINDOW = 30


@runtime_checkable
class HistoryBackend(Protocol):
    """Per-channel conversation history and its rolling summary."""

    def load(self, channel_id: int) -> Tuple[List[Dict[str, Any]], str]:
        """Return ``(turns, summary)``. Empty is a valid, expected result."""

    def save_turns(self, channel_id: int, turns: List[Dict[str, Any]]) -> None:
        """Replace the turns. Whole-history rewrite is fine: the window is bounded."""

    def save_summary(self, channel_id: int, summary: str) -> None:
        """Replace the summary only.

        Split from ``save_turns`` deliberately. Summarisation runs in the background, so
        a turn that loaded the summary at its start and wrote it back at its end would
        silently clobber any summary produced while that turn was in flight.
        """

    def clear_all(self) -> None:
        """Discard every channel's history. For `?dm reset` only.

        Every channel, not just the configured ones. Reset used to loop over
        ``GARY_GAME_CHANNELS`` and blank each in turn, which left the full transcript of
        any channel that had since been removed from the setting -- readable, and loaded
        straight back into layer 4 if that channel was ever re-added.
        """


class InMemoryHistoryBackend:
    def __init__(self) -> None:
        self._data: Dict[int, Tuple[List[Dict[str, Any]], str]] = {}

    def load(self, channel_id: int) -> Tuple[List[Dict[str, Any]], str]:
        turns, summary = self._data.get(channel_id, ([], ""))
        return json.loads(json.dumps(turns)), summary

    def save_turns(self, channel_id: int, turns: List[Dict[str, Any]]) -> None:
        _, summary = self._data.get(channel_id, ([], ""))
        self._data[channel_id] = (json.loads(json.dumps(turns)), summary)

    def save_summary(self, channel_id: int, summary: str) -> None:
        turns, _ = self._data.get(channel_id, ([], ""))
        self._data[channel_id] = (turns, summary)

    def clear_all(self) -> None:
        self._data.clear()


class FileHistoryBackend:
    """One file per channel, so a busy table never rewrites another's history."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, channel_id: int) -> Path:
        return self.root / f"history_{channel_id}.json"

    def load(self, channel_id: int) -> Tuple[List[Dict[str, Any]], str]:
        path = self._path(channel_id)
        if not path.exists():
            return [], ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Starting a session with no memory of last week beats not starting at all.
            return [], ""
        return payload.get("turns", []), payload.get("summary", "")

    def save_turns(self, channel_id: int, turns: List[Dict[str, Any]]) -> None:
        with self._lock:
            _, summary = self.load(channel_id)
            atomic_write_json(self._path(channel_id), {"turns": turns, "summary": summary})

    def save_summary(self, channel_id: int, summary: str) -> None:
        with self._lock:
            turns, _ = self.load(channel_id)
            atomic_write_json(self._path(channel_id), {"turns": turns, "summary": summary})

    def clear_all(self) -> None:
        with self._lock:
            # Unlink rather than write an empty file: an empty history and an absent one
            # already load identically, so leaving a husk behind only invites the
            # question of whether the reset finished.
            for path in self.root.glob("history_*.json"):
                path.unlink(missing_ok=True)


class FirestoreHistoryBackend:
    """⚠️ Unverified against real Firestore, like the other Firestore backends."""

    def __init__(self, client: Any, campaign_id: str, root_collection: str = "campaigns") -> None:
        self._history = (
            client.collection(root_collection).document(campaign_id).collection("history")
        )

    def load(self, channel_id: int) -> Tuple[List[Dict[str, Any]], str]:
        snap = self._history.document(str(channel_id)).get()
        if not snap.exists:
            return [], ""
        data = snap.to_dict() or {}
        raw = data.get("turns_json")
        return (json.loads(raw) if raw else []), data.get("summary", "")

    def save_turns(self, channel_id: int, turns: List[Dict[str, Any]]) -> None:
        _, summary = self.load(channel_id)
        self._history.document(str(channel_id)).set(
            {"turns_json": json.dumps(turns), "summary": summary}
        )

    def save_summary(self, channel_id: int, summary: str) -> None:
        turns, _ = self.load(channel_id)
        self._history.document(str(channel_id)).set(
            {"turns_json": json.dumps(turns), "summary": summary}
        )

    def clear_all(self) -> None:
        for doc in self._history.stream():
            self._history.document(doc.id).delete()


def trim(
    turns: List[Dict[str, Any]], window: int = DEFAULT_WINDOW
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split into ``(kept, overflow)``.

    Never splits a tool exchange across the window edge. Gemini rejects both halves:

    * a leading ``function_response`` answers a call that is no longer there;
    * a leading ``function_call`` is rejected with "function call turn comes immediately
      after a user turn or after a function response turn" — observed live 2026-08-21,
      once a session's worth of set_scene, record_note and upsert_npc calls had filled the
      history with these pairs and the window finally landed inside one.

    The edge walks forward until the head is a clean start. The two cases cascade: dropping
    a call turn exposes its response turn, which is then dropped in turn.
    """
    if len(turns) <= window:
        return turns, []

    cut = len(turns) - window
    while cut < len(turns) and (
        _opens_with_function_response(turns[cut]) or _has_function_call(turns[cut])
    ):
        cut += 1
    return turns[cut:], turns[:cut]


def _opens_with_function_response(turn: Dict[str, Any]) -> bool:
    for part in turn.get("parts", []):
        if "function_response" in part:
            return True
        break
    return False


def _has_function_call(turn: Dict[str, Any]) -> bool:
    """Any call in the turn, not just the first part: a model turn may carry prose and a
    call together, and the call is still unanchored if it leads the window."""
    return any("function_call" in part for part in turn.get("parts", []))
