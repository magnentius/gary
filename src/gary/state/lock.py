"""Single-writer enforcement (ARCHITECTURE.md §7.3).

§7.3 specifies exactly one writer, and until now that was true by construction: one
process, one store. Adding an MCP server breaks that assumption on purpose — the whole
point is a second process reaching the same campaign from Claude Desktop while you plan
next week's session.

If Gary is live at the table and someone opens the campaign in another client, two
processes interleave writes to one journal. This makes that impossible rather than
unlikely: the writer holds an advisory exclusive lock, and anyone who cannot take it runs
read-only and says so.

**Scope.** ``flock`` is per-machine. It protects the file backend, which is the setup
that can actually suffer this. It does **not** coordinate across machines, so it offers no
protection for the Firestore backend — that would need a lease held in Firestore itself,
and is not implemented. ``CampaignLock`` reports ``supported=False`` where the platform
lacks ``fcntl`` rather than pretending to hold anything.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("gary.lock")

try:
    import fcntl

    _SUPPORTED = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore
    _SUPPORTED = False


class CampaignLock:
    """Advisory exclusive lock over a campaign directory.

    Deliberately non-blocking. A prep client that waits for the lock would hang silently
    for the length of a game session; one that is told it cannot write can offer read-only
    access immediately and explain why.
    """

    def __init__(self, path: Path, owner: str = "gary") -> None:
        self.path = Path(path)
        self.owner = owner
        self._fd: Optional[int] = None

    @property
    def supported(self) -> bool:
        return _SUPPORTED

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Try to take the lock. Returns whether it was acquired."""
        if not _SUPPORTED:
            log.warning("file locking unsupported on this platform; not enforcing single-writer")
            return True
        if self._fd is not None:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False

        os.ftruncate(fd, 0)
        os.write(fd, f"{self.owner} pid={os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd
        return True

    def holder(self) -> str:
        """Best-effort description of who holds it. For a human-readable refusal."""
        try:
            return self.path.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            return "unknown"

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if _SUPPORTED:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "CampaignLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock_path(campaign_root: Path) -> Path:
    return Path(campaign_root) / ".gary.lock"
