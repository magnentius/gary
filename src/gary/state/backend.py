"""Storage backends (ARCHITECTURE.md §7.3, §11).

Durable state is addressed through two narrow protocols so the process that runs Gary
owns none of it. Point the backends at managed storage and the VM becomes replaceable:
delete it, recreate it, and the campaign is intact.

The protocols are deliberately small — snapshot, append, read — because that is the whole
of what the journal model needs, and a small surface is what makes a second
implementation cheap to write and cheap to trust.

Two implementations live here:

* ``InMemory*`` — tests, and the default for a registry with no persistence configured.
* ``File*`` — local disk, crash-safe via temp-then-rename. The default for a single VM.

``firestore.py`` adds a managed backend for the disposable-VM setup.

**Concurrency.** Every implementation here assumes a single writer, which is what
ARCHITECTURE.md §7.3 specifies. Running two Gary processes against one backend is not
supported by these classes and needs per-channel serialisation first.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # Protocol is 3.8+, but runtime_checkable helps tests assert conformance.
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from ..models import PendingCheck


# ---------------------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------------------


@runtime_checkable
class StateBackend(Protocol):
    """Where the campaign snapshot and journal live."""

    def load_snapshot(self) -> Optional[Tuple[Dict[str, Any], int]]:
        """Return ``(state, journal_offset)`` or None if nothing is stored yet."""

    def write_snapshot(self, state: Dict[str, Any], journal_offset: int) -> None:
        """Durably replace the snapshot. Must be atomic: a torn snapshot is unrecoverable."""

    def append_journal(self, entry: Dict[str, Any]) -> None:
        """Append one entry. Must be durable before returning.

        **Contract:** ``entry["seq"]`` is a monotonically increasing integer assigned by
        the caller. Document-based backends key on it to preserve order, because a
        batched apply() writes several entries in the same millisecond and timestamps
        cannot break that tie. Implementations must reject an entry without it rather
        than inventing one -- a guessed sequence number silently reorders or overwrites
        history.
        """

    def read_journal(self) -> List[Dict[str, Any]]:
        """Every entry, in ``seq`` order."""


@runtime_checkable
class CheckBackend(Protocol):
    """Where pending checks live between the command and the dice."""

    def save(self, check: PendingCheck) -> None: ...

    def remove(self, check_id: str) -> None: ...

    def load_all(self) -> List[PendingCheck]: ...


# ---------------------------------------------------------------------------------------
# In-memory
# ---------------------------------------------------------------------------------------


class InMemoryStateBackend:
    """Non-durable. For tests, and for a dry run that should leave no trace."""

    def __init__(self) -> None:
        self._snapshot: Optional[Tuple[Dict[str, Any], int]] = None
        self._journal: List[Dict[str, Any]] = []

    def load_snapshot(self) -> Optional[Tuple[Dict[str, Any], int]]:
        if self._snapshot is None:
            return None
        state, offset = self._snapshot
        return json.loads(json.dumps(state)), offset

    def write_snapshot(self, state: Dict[str, Any], journal_offset: int) -> None:
        self._snapshot = (json.loads(json.dumps(state)), journal_offset)

    def append_journal(self, entry: Dict[str, Any]) -> None:
        self._journal.append(json.loads(json.dumps(entry)))

    def read_journal(self) -> List[Dict[str, Any]]:
        return [json.loads(json.dumps(e)) for e in self._journal]


class InMemoryCheckBackend:
    def __init__(self) -> None:
        self._checks: Dict[str, dict] = {}

    def save(self, check: PendingCheck) -> None:
        self._checks[check.check_id] = check.to_dict()

    def remove(self, check_id: str) -> None:
        self._checks.pop(check_id, None)

    def load_all(self) -> List[PendingCheck]:
        return [PendingCheck.from_dict(d) for d in self._checks.values()]


# ---------------------------------------------------------------------------------------
# Local disk
# ---------------------------------------------------------------------------------------


def atomic_write_json(path: Path, obj: Any) -> None:
    """Durably replace ``path``. Atomic on POSIX via rename-over.

    The directory fsync matters as much as the file one: without it the rename itself can
    be lost in a crash, leaving the old snapshot in place.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class FileStateBackend:
    """Snapshot + append-only journal on local disk."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def snapshot_path(self) -> Path:
        return self.root / "snapshot.json"

    @property
    def journal_path(self) -> Path:
        return self.root / "journal.ndjson"

    def load_snapshot(self) -> Optional[Tuple[Dict[str, Any], int]]:
        if not self.snapshot_path.exists():
            return None
        with open(self.snapshot_path, encoding="utf-8") as f:
            payload = json.load(f)
        offset = int(payload.pop("journal_offset", 0))
        return payload, offset

    def write_snapshot(self, state: Dict[str, Any], journal_offset: int) -> None:
        with self._lock:
            payload = dict(state)
            payload["journal_offset"] = journal_offset
            atomic_write_json(self.snapshot_path, payload)

    def append_journal(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())

    def read_journal(self) -> List[Dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with open(self.journal_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


class FileCheckBackend:
    """All pending checks in one small file.

    Rewriting the whole set on each change is fine: the set is bounded by the number of
    players with a check open, which is a handful.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A truncated file must not take the bot down on boot; losing pending checks
            # costs one re-roll, and expiry would have cleared them within minutes anyway.
            return {}

    def save(self, check: PendingCheck) -> None:
        with self._lock:
            checks = self._read()
            checks[check.check_id] = check.to_dict()
            atomic_write_json(self.path, checks)

    def remove(self, check_id: str) -> None:
        with self._lock:
            checks = self._read()
            if checks.pop(check_id, None) is not None:
                atomic_write_json(self.path, checks)

    def load_all(self) -> List[PendingCheck]:
        return [PendingCheck.from_dict(d) for d in self._read().values()]
