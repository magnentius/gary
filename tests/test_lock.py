"""Single-writer enforcement (ARCHITECTURE.md §7.3).

Adding an MCP server deliberately breaks the one-process assumption the store was built
on. These tests are what makes that safe rather than merely intended.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from gary.state.lock import CampaignLock, lock_path


@pytest.fixture
def path(tmp_path):
    return lock_path(tmp_path / "campaign")


def test_lock_is_acquired_on_a_fresh_campaign(path):
    lock = CampaignLock(path)
    assert lock.acquire() is True
    assert lock.held
    lock.release()


def test_release_makes_it_available_again(path):
    first = CampaignLock(path, owner="gary")
    assert first.acquire()
    first.release()

    second = CampaignLock(path, owner="mcp")
    assert second.acquire()
    second.release()


def test_acquire_is_idempotent(path):
    lock = CampaignLock(path)
    assert lock.acquire()
    assert lock.acquire()
    lock.release()


def test_context_manager_releases(path):
    with CampaignLock(path) as lock:
        assert lock.held
    assert not lock.held


def test_holder_records_who_and_which_pid(path):
    lock = CampaignLock(path, owner="gary")
    lock.acquire()
    holder = lock.holder()
    assert "gary" in holder
    assert str(os.getpid()) in holder
    lock.release()


def test_release_without_acquire_is_harmless(path):
    CampaignLock(path).release()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="flock is POSIX")
def test_a_second_process_cannot_take_a_held_lock(path, tmp_path):
    """The property that matters.

    Same-process flock re-acquisition succeeds on the same file descriptor, so this has
    to cross a real process boundary to prove anything -- which is exactly the case the
    MCP server creates.
    """
    holder = CampaignLock(path, owner="gary")
    assert holder.acquire()

    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(tmp_path.parent.parent / "src")!r})
        sys.path.insert(0, {os.path.join(os.getcwd(), "src")!r})
        from pathlib import Path
        from gary.state.lock import CampaignLock
        lock = CampaignLock(Path({str(path)!r}), owner="mcp")
        print("ACQUIRED" if lock.acquire() else "REFUSED")
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.stdout.strip() == "REFUSED", result.stderr

    holder.release()

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.stdout.strip() == "ACQUIRED", result.stderr
