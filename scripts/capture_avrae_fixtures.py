#!/usr/bin/env python3
"""Capture real Avrae embeds into test fixtures (ARCHITECTURE.md §14.1).

Fixtures in ``tests/fixtures/avrae/`` are derived from Avrae's own source at
``avrae@cb75be2``, which is strong evidence but not the same as observing what Discord
actually delivers. This script closes that last gap: it joins a channel, records every
message Avrae posts, and writes each one out with the raw payload and a starter
expectation.

**Diff, do not blindly replace.** A capture that disagrees with the derived fixture means
either Avrae changed or the derivation was wrong -- both worth understanding before
overwriting. Pay particular attention to the initiative tracker, which is a plain ```md
code block in ``message.content`` rather than an embed.

    pip install -e '.[discord]'
    export DISCORD_TOKEN=...           # Drelm's bot token
    python scripts/capture_avrae_fixtures.py --channel 123456789 --minutes 10

Then, in that channel, run at least:

    !check sleight of hand          plain skill check
    /check                          same via slash command (different attribution path)
    !check perception adv           advantage: dropped die is struck through
    !check stealth dis              disadvantage: the HIGHER die is struck through
    !save dex                       saving throw
    !attack longsword               two numbers in one embed
    !check athletics -dc 15         prepends "**DC 15**" and sets a Success!/Failure! footer
    !check stealth -rr 3            moves rolls OUT of the description into "Check N" fields
    !save death                     distinct title: "<name> makes a Death Save!"
    !init begin / !init next        tracker: plain ```md code block, NOT an embed
    !help                           must classify as irrelevant
    !check notaskill                error: PLAIN TEXT "Error: ...", not an embed

Review each generated file, correct the ``expect`` block, and run
``pytest tests/test_parser.py``. Once every fixture carries a CAPTURED provenance, delete
``test_fixtures_are_captured_from_a_live_avrae``.

Requires the Message Content privileged intent (ARCHITECTURE.md §1) -- without it, embeds arrive
empty and this script silently captures nothing useful. It warns if that happens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import discord
except ImportError:  # pragma: no cover - operator-facing
    sys.exit("discord.py not installed. Run: pip install -e '.[discord]'")

AVRAE_BOT_ID = 261302296103747584
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "avrae" / "captured"


def guess_expectation(message: "discord.Message") -> dict:
    """A starting point for the operator to correct, not a substitute for reviewing."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from drelm.parser.avrae import parse_embed

    parsed = parse_embed(json.loads(json.dumps(message_to_dict(message))))
    expect = {"status": parsed.status.value}
    if parsed.roll:
        expect.update(
            {
                "actor": parsed.roll.actor,
                "total": parsed.roll.total,
                "natural": parsed.roll.natural,
            }
        )
    if parsed.combatants:
        expect["combatants"] = len(parsed.combatants)
    return expect


def message_to_dict(message: "discord.Message") -> dict:
    """The subset of Discord's message payload the parser consumes."""
    out = {
        "id": str(message.id),
        "content": message.content,
        "embeds": [e.to_dict() for e in message.embeds],
    }
    interaction = getattr(message, "interaction_metadata", None) or getattr(
        message, "interaction", None
    )
    if interaction is not None and getattr(interaction, "user", None) is not None:
        out["interaction"] = {"user": {"id": str(interaction.user.id)}}
    if message.reference is not None and message.reference.resolved is not None:
        resolved = message.reference.resolved
        if getattr(resolved, "author", None) is not None:
            out["referenced_message"] = {"author": {"id": str(resolved.author.id)}}
    return out


async def run(token: str, channel_id: int, minutes: int) -> None:
    intents = discord.Intents.default()
    intents.message_content = True  # PRIVILEGED -- also enable in the Developer Portal
    client = discord.Client(intents=intents)

    captured = {"count": 0, "empty_embeds": 0}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    @client.event
    async def on_ready() -> None:
        print(f"connected as {client.user}; watching #{channel_id} for {minutes} min")
        print(f"writing to {OUT_DIR}")
        asyncio.create_task(stop_later())

    async def stop_later() -> None:
        await asyncio.sleep(minutes * 60)
        await client.close()

    @client.event
    async def on_message(message: "discord.Message") -> None:
        if message.channel.id != channel_id or message.author.id != AVRAE_BOT_ID:
            return

        payload = message_to_dict(message)
        if payload["embeds"] and not any(
            e.get("description") or e.get("title") or e.get("fields")
            for e in payload["embeds"]
        ):
            captured["empty_embeds"] += 1

        captured["count"] += 1
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        path = OUT_DIR / f"capture_{stamp}_{captured['count']:03d}.json"
        path.write_text(
            json.dumps(
                {
                    "_provenance": f"CAPTURED from live Avrae at {datetime.now(timezone.utc).isoformat()}",
                    "_description": "TODO: describe what command produced this.",
                    "message": payload,
                    "expect": guess_expectation(message),
                },
                indent=2,
            )
        )
        title = (payload["embeds"][0].get("title") if payload["embeds"] else "") or "(no embed)"
        print(f"  captured {path.name}: {title}")

    await client.start(token)

    print(f"\ncaptured {captured['count']} message(s)")
    if captured["empty_embeds"]:
        print(
            f"WARNING: {captured['empty_embeds']} embed(s) arrived empty. That is the "
            "signature of a missing Message Content intent -- enable it in the Discord "
            "Developer Portal and re-run, or every fixture here is worthless."
        )
    print("Next: review each file, fix its expect block, drop _provenance, run pytest.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=int, required=True, help="channel id to watch")
    parser.add_argument("--minutes", type=int, default=10, help="how long to record")
    args = parser.parse_args()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN not set")

    asyncio.run(run(token, args.channel, args.minutes))


if __name__ == "__main__":
    main()
