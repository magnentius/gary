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


def trim(
    turns: List[Dict[str, Any]], window: int = DEFAULT_WINDOW
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split into ``(kept, overflow)``.

    Never splits a model turn from the tool result that answers it: an orphaned
    ``function_response`` at the head of the window is a malformed conversation, and
    Gemini rejects it. The window edge walks forward until it lands on a clean boundary.
    """
    if len(turns) <= window:
        return turns, []

    cut = len(turns) - window
    while cut < len(turns) and _opens_with_function_response(turns[cut]):
        cut += 1
    return turns[cut:], turns[:cut]


def _opens_with_function_response(turn: Dict[str, Any]) -> bool:
    for part in turn.get("parts", []):
        if "function_response" in part:
            return True
        break
    return False
