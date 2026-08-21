"""MCP server exposing the campaign (ARCHITECTURE.md §2.1, §7.1).

Lets Claude Desktop (or any MCP client) read and edit the campaign between sessions —
auditing what the model did, writing NPCs, planting foreshadowing — without running Gary
or Discord. §2.1 names this as the *only* justification for keeping MCP in the design, so
this server is that justification made real.

**Note the direction.** Gary is not an MCP client. It uses ``CampaignStore`` in-process,
because a protocol, a subprocess, and a serialisation boundary between two objects in one
program buys nothing. MCP sits beside Gary for outside clients, not inside its hot path.

Configure Claude Desktop:

```json
{
  "mcpServers": {
    "gary": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "gary.adapters.mcp_server"],
      "env": {"GARY_CAMPAIGN_ROOT": "/path/to/campaign"}
    }
  }
}
```

⚠️ **UNVERIFIED.** The MCP SDK requires Python 3.10+, and this repository's environment is
3.9, so this module has never been imported or run. Everything with behaviour worth
trusting lives in ``gary/prep.py`` and ``gary/state/lock.py``, which are fully tested;
what is unverified here is the binding — decorator shapes, transport wiring, SDK
signatures. Check those against the SDK before relying on it.

**Writes need the lock.** If Gary holds the campaign (a session is live), this starts
read-only and says so rather than interleaving writes into a journal someone is appending
to (§7.3).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..prep import CampaignPrep, ReadOnly
from ..state.backend import FileStateBackend
from ..state.history import FileHistoryBackend
from ..state.lock import CampaignLock, lock_path
from ..state.store import CampaignStore

log = logging.getLogger("gary.mcp")

mcp = FastMCP("gary")

_prep: CampaignPrep


def _build(campaign_root: Path) -> CampaignPrep:
    lock = CampaignLock(lock_path(campaign_root), owner="mcp")
    writable = lock.acquire()
    if not writable:
        log.warning("campaign locked by %s; serving read-only", lock.holder())

    store = CampaignStore.on_disk(campaign_root, clock=__import__("time").time)
    return CampaignPrep(
        store, history_backend=FileHistoryBackend(campaign_root / "history"), writable=writable
    )


# -- resources (§7.1) ---------------------------------------------------------------------


@mcp.resource("rpg://campaign/overview")
def overview() -> str:
    """Human-readable snapshot: time, place, party, effects, quests, NPCs."""
    return _prep.overview()


@mcp.resource("rpg://campaign/state")
def state() -> str:
    """Full campaign state as JSON."""
    return json.dumps(_prep.state(), indent=2, sort_keys=True)


@mcp.resource("rpg://campaign/journal")
def journal() -> str:
    """Recent mutations with their reasons and triggering Discord message ids.

    The audit trail for everything the model changed. Start here when something in the
    campaign looks wrong.
    """
    return json.dumps(_prep.journal(limit=100), indent=2)


@mcp.resource("rpg://campaign/npcs")
def npcs() -> str:
    return json.dumps(_prep.npcs(), indent=2, sort_keys=True)


@mcp.resource("rpg://campaign/quests")
def quests() -> str:
    return json.dumps(_prep.quests(), indent=2, sort_keys=True)


# -- tools --------------------------------------------------------------------------------


@mcp.tool()
def search_notes(subject: str = "") -> str:
    """Narrative memory, optionally filtered by subject.

    These are what the DM model is shown when a subject reappears, so this is the record
    of what it will and will not remember.
    """
    return json.dumps(_prep.notes(subject or None), indent=2)


@mcp.tool()
def read_last_session(channel_id: int) -> str:
    """The rolling summary and recent turns for a channel."""
    return json.dumps(_prep.last_session(channel_id), indent=2)


@mcp.tool()
def upsert_npc(
    npc_id: str, name: str = "", disposition: Optional[int] = None, note: str = ""
) -> str:
    """Create or update an NPC. Disposition is roughly -5 hostile to +5 devoted.

    Omitting a field leaves it unchanged. ``disposition`` must default to None rather than
    0: prep treats "not None" as "set it", so a 0 default silently reset every NPC touched
    for any other reason -- adding a note to a devoted ally would drop them to neutral.
    """
    return _guard(
        lambda: _prep.upsert_npc(
            npc_id, name=name or None, disposition=disposition, note=note or None
        )
    )


@mcp.tool()
def upsert_quest(quest_id: str, title: str = "", status: str = "", note: str = "") -> str:
    """Create or update a quest. Status is open | done | failed, or your own vocabulary."""
    return _guard(
        lambda: _prep.upsert_quest(
            quest_id, title=title or None, status=status or None, note=note or None
        )
    )


@mcp.tool()
def record_note(kind: str, subject: str, body: str) -> str:
    """Plant a note the DM model will see when the subject next comes up.

    kind: npc | clue | promise | foreshadow | general. This is how to seed foreshadowing
    before a session rather than hoping the model improvises it.
    """
    return _guard(lambda: _prep.record_note(kind, subject, body))


@mcp.tool()
def set_scene(location: str, summary: str = "") -> str:
    """Move the party. Useful for setting up where next session opens."""
    return _guard(lambda: _prep.set_scene(location, summary))


@mcp.tool()
def undo_last_change(confirm: bool = False) -> str:
    """Reverse the most recent reversible mutation.

    Requires confirm=true, because from a chat client this is easy to fire by accident and
    it silently rewrites campaign history.
    """
    if not confirm:
        recent = _prep.journal(limit=1)
        return json.dumps(
            {"ok": False, "error": "pass confirm=true to apply", "would_reverse": recent}
        )
    return _guard(_prep.undo_last)


def _guard(fn) -> str:
    """Turn a read-only refusal into an explanation instead of a stack trace."""
    try:
        return json.dumps(fn(), indent=2)
    except ReadOnly as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def main() -> None:  # pragma: no cover - operator entry point
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    global _prep
    _prep = _build(Path(os.environ.get("GARY_CAMPAIGN_ROOT", "./campaign")))
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
