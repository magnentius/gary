#!/usr/bin/env python3
"""Capture real Avrae output into test fixtures (ARCHITECTURE.md §14.1).

Fixtures in ``tests/fixtures/avrae/`` are derived from Avrae's source at ``avrae@cb75be2``
and from running ``d20`` locally. That is strong evidence, but it is not the same as
observing what Discord actually delivers. This closes that gap.

    pip install -e '.[discord]'
    export DISCORD_TOKEN=...                  # Drelm's bot token
    python scripts/capture_avrae_fixtures.py --channel 123456789 --minutes 12

Then run the commands it prints. When you are done:

    python scripts/verify_avrae_fixtures.py   # parses every capture, reports gaps

**Diff, do not blindly replace.** A capture that disagrees with a derived fixture means
either Avrae changed or the derivation was wrong. Both are worth understanding before
overwriting.

Requires the Message Content privileged intent (ARCHITECTURE.md §1). Without it, embeds
and content arrive empty and every capture is worthless; this warns loudly if it sees that
signature.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

try:
    import discord
except ImportError:  # pragma: no cover - operator-facing
    sys.exit("discord.py not installed. Run: pip install -e '.[discord]'")

from drelm.parser.avrae import parse_embed  # noqa: E402

AVRAE_BOT_ID = 261302296103747584
OUT_DIR = REPO / "tests" / "fixtures" / "avrae" / "captured"

# What a complete capture session needs to cover. Printed at start and checked by
# verify_avrae_fixtures.py, so you find out you missed one now rather than in a session.
WANTED = [
    ("check_plain", "!check sleight of hand", "plain skill check"),
    ("check_slash", "/check", "slash command — different attribution path"),
    ("check_adv", "!check perception adv", "advantage; dropped die struck through"),
    ("check_dis", "!check stealth dis", "disadvantage"),
    ("check_dc", "!check athletics -dc 15", "prepends **DC 15**, sets a footer"),
    ("check_rr", "!check stealth -rr 3", "rolls move into 'Check N' FIELDS"),
    ("save", "!save dex", "saving throw"),
    ("save_death", "!save death", "distinct title, no ability name"),
    ("attack", "!attack longsword", "two numbers in one embed"),
    ("init_begin", "!init begin", "tracker: plain ```md block, NOT an embed"),
    ("init_next", "!init next", "tracker is EDITED, not resent — needs on_message_edit"),
    ("help", "!help", "must classify as irrelevant"),
    ("error", "!check notaskill", "error: PLAIN TEXT, not an embed"),
]


def message_to_dict(message: "discord.Message") -> dict:
    """The subset of Discord's payload the parser consumes."""
    out = {
        "id": str(message.id),
        "content": message.content,
        "embeds": [e.to_dict() for e in message.embeds],
    }

    interaction = getattr(message, "interaction_metadata", None) or getattr(
        message, "interaction", None
    )
    user = getattr(interaction, "user", None)
    if user is not None:
        out["interaction"] = {"user": {"id": str(user.id)}}

    ref = message.reference
    if ref is not None:
        resolved = getattr(ref, "resolved", None)
        author = getattr(resolved, "author", None)
        if author is not None:
            out["referenced_message"] = {"author": {"id": str(author.id)}}
        elif getattr(ref, "message_id", None) is not None:
            # Uncached replies resolve to None. Record the id so a human can tell the
            # difference between "Avrae did not reply" and "we could not see the reply".
            out["_unresolved_reference_id"] = str(ref.message_id)
    return out


def build_record(message: "discord.Message", trigger: str) -> dict:
    """Wrap a payload with what the parser made of it.

    The parser's reading is recorded as ``_parser_said``, **not** as ``expect``. Pre-filling
    ``expect`` from the parser would make the resulting fixture self-confirming: the test
    would assert only that the parser agrees with itself, which is exactly no evidence.
    A human fills in ``expect`` from what Discord actually shows.
    """
    payload = message_to_dict(message)
    parsed = parse_embed(json.loads(json.dumps(payload)))

    said = {"status": parsed.status.value}
    if parsed.roll:
        said.update(
            {
                "actor": parsed.roll.actor,
                "total": parsed.roll.total,
                "natural": parsed.roll.natural,
                "kind": parsed.roll.kind.value if parsed.roll.kind else None,
            }
        )
    if parsed.combatants:
        said["combatants"] = len(parsed.combatants)
        said["dying"] = [c.name for c in parsed.combatants if c.is_dying]
    if parsed.reason:
        said["reason"] = parsed.reason

    return {
        "_provenance": f"CAPTURED from live Avrae at {datetime.now(timezone.utc).isoformat()}",
        "_description": "TODO: which command produced this?",
        "_trigger": trigger,
        "_parser_said": said,
        "message": payload,
        "expect": {"status": "TODO — fill in from what Discord shows, not from _parser_said"},
    }


async def run(token: str, channel_id: int, minutes: int) -> None:
    intents = discord.Intents.default()
    intents.message_content = True  # PRIVILEGED — also enable in the Developer Portal
    client = discord.Client(intents=intents)

    stats = {"count": 0, "blank": 0, "unknown": 0, "edits": 0}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def record(message: "discord.Message", trigger: str) -> None:
        entry = build_record(message, trigger)
        payload = entry["message"]

        # The Message Content intent failure signature: a message with neither content
        # nor any embed carrying text. Silent otherwise, which is why it is counted.
        has_embed_text = any(
            e.get("description") or e.get("title") or e.get("fields")
            for e in payload["embeds"]
        )
        if not payload["content"].strip() and not has_embed_text:
            stats["blank"] += 1
        if entry["_parser_said"]["status"] == "unknown":
            stats["unknown"] += 1

        stats["count"] += 1
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        path = OUT_DIR / f"capture_{stamp}_{stats['count']:03d}.json"
        path.write_text(json.dumps(entry, indent=2))

        label = (payload["embeds"][0].get("title") if payload["embeds"] else "") or (
            payload["content"][:60].replace("\n", " ⏎ ") or "(empty)"
        )
        print(f"  [{trigger:6}] {path.name}  {entry['_parser_said']['status']:11} {label}")

    @client.event
    async def on_ready() -> None:
        print(f"\nconnected as {client.user}; watching #{channel_id} for {minutes} min")
        print(f"writing to {OUT_DIR}\n")
        print("Run these in the channel:")
        for _, command, why in WANTED:
            print(f"  {command:32} {why}")
        print()
        asyncio.create_task(stop_later())

    async def stop_later() -> None:
        await asyncio.sleep(minutes * 60)
        await client.close()

    @client.event
    async def on_message(message: "discord.Message") -> None:
        if message.channel.id == channel_id and message.author.id == AVRAE_BOT_ID:
            record(message, "new")

    @client.event
    async def on_message_edit(before: "discord.Message", after: "discord.Message") -> None:
        """The initiative tracker is a pinned message Avrae EDITS in place.

        ``!init next`` never fires on_message, so a capture run without this misses every
        state of combat after the first — which is the format most likely to be wrong,
        since it is the one that is not an embed.
        """
        if after.channel.id == channel_id and after.author.id == AVRAE_BOT_ID:
            stats["edits"] += 1
            record(after, "edit")

    await client.start(token)

    print(f"\ncaptured {stats['count']} message(s), {stats['edits']} of them edits")
    if stats["blank"]:
        print(
            f"\n  ⚠️  {stats['blank']} message(s) arrived with no content AND no embed text.\n"
            "     That is the signature of a missing Message Content intent. Enable it in\n"
            "     the Discord Developer Portal and re-run, or every capture here is worthless."
        )
    if stats["unknown"]:
        print(
            f"\n  ⚠️  the parser could not classify {stats['unknown']} message(s).\n"
            "     That is a real finding — a format it does not handle. Keep those captures."
        )
    print("\nNext: python scripts/verify_avrae_fixtures.py")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=int, required=True, help="channel id to watch")
    parser.add_argument("--minutes", type=int, default=12, help="how long to record")
    args = parser.parse_args()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN not set")

    asyncio.run(run(token, args.channel, args.minutes))


if __name__ == "__main__":
    main()
