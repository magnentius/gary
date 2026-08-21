"""The Discord adapter (ARCHITECTURE.md §1, §6.3, §11).

Everything that touches discord.py lives here, and it is deliberately thin: routing,
parsing, correlation, and resolution all happen in the core, which this module feeds and
then obeys.

**Message Content is a privileged intent.** It must be enabled in the Discord Developer
Portal *and* set in code. Enabling only one of the two produces the same silent failure —
``message.embeds`` arrives empty, the parser sees nothing, and Gary looks alive while
resolving no rolls. ``GaryClient.on_ready`` checks for this and complains loudly.

Run it:

    export DISCORD_TOKEN=...
    export GARY_GAME_CHANNELS=123456789
    python -m gary.adapters.discord_bot
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import discord

from ..config import Settings
from ..fsm import Director
from ..models import CancelReason, Mutation, MutationKind
from ..parser.avrae import ParseStatus, parse_embed
from ..ports import EchoNarrator, Narrator
from ..registry import PendingCheckRegistry
from ..state.backend import FileCheckBackend, FileStateBackend
from ..state.history import FileHistoryBackend
from ..state.lock import CampaignLock, lock_path
from ..state.store import CampaignStore
from .routing import Route, route_message
from .runner import EffectRunner
from .sender import ChannelSender

log = logging.getLogger("gary.discord")


def message_to_dict(message: "discord.Message") -> Dict[str, Any]:
    """The subset of Discord's payload the parser consumes.

    Converting at the boundary keeps ``parser/avrae.py`` free of discord.py, so it stays
    testable against captured JSON fixtures.
    """
    out: Dict[str, Any] = {
        "id": str(message.id),
        "content": message.content,
        "embeds": [e.to_dict() for e in message.embeds],
    }

    # discord.py renamed this; support both so a library bump does not silently drop the
    # strongest correlation signal we have (§6.2).
    interaction = getattr(message, "interaction_metadata", None) or getattr(
        message, "interaction", None
    )
    user = getattr(interaction, "user", None)
    if user is not None:
        out["interaction"] = {"user": {"id": str(user.id)}}

    ref = message.reference
    resolved = getattr(ref, "resolved", None) if ref is not None else None
    author = getattr(resolved, "author", None)
    if author is not None:
        out["referenced_message"] = {"author": {"id": str(author.id)}}

    return out


class DiscordTransport:
    """MessageTransport over discord.py."""

    def __init__(self, client: "discord.Client") -> None:
        self._client = client

    async def send(self, channel_id: int, text: str) -> None:
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except discord.HTTPException:
                log.error("cannot reach channel %d; dropping message", channel_id)
                return
        await channel.send(text)


class GaryClient(discord.Client):
    def __init__(
        self,
        settings: Settings,
        director: Director,
        store: CampaignStore,
        narrator: Optional[Narrator] = None,
        clock=time.time,
        lock: Optional[CampaignLock] = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # PRIVILEGED — also enable in the Developer Portal
        intents.guilds = True
        super().__init__(intents=intents)

        self.settings = settings
        self.director = director
        self.store = store
        self.clock = clock
        self.lock = lock
        self.sender = ChannelSender(
            DiscordTransport(self), interval_seconds=settings.send_interval_seconds
        )
        self.runner = EffectRunner(
            self.sender,
            narrator or EchoNarrator(),
            max_depth=settings.max_effect_depth,
            store=store,
        )
        self._sweeper: Optional[asyncio.Task] = None

    # -- lifecycle ---------------------------------------------------------------------

    async def setup_hook(self) -> None:
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def on_ready(self) -> None:
        log.info("connected as %s (%s)", self.user, getattr(self.user, "id", "?"))
        log.info(
            "watching %d channel(s); avrae=%d; %d pending check(s) restored",
            len(self.settings.game_channels),
            self.settings.avrae_bot_id,
            len(list(self.director.registry.open_checks())),
        )
        persisted = {
            int(uid): actor
            for uid, actor in (self.store.state().get("players") or {}).items()
        }
        for user_id, actor in persisted.items():
            self.director.set_player_character(user_id, actor)

        if self.settings.players:
            log.info(
                "roster: %s",
                "; ".join(f"{uid} -> {actor}" for uid, actor in sorted(self.settings.players.items())),
            )
        else:
            log.warning(
                "GARY_PLAYERS is not set. The model is told only Discord display names, so "
                "open_check will name an actor Avrae does not know and the roll will not "
                "correlate -- the check lapses on TTL and is narrated as hesitation."
            )

        if not self.intents.message_content:
            log.error(
                "MESSAGE CONTENT INTENT IS OFF. Embeds will arrive empty and no roll "
                "will ever resolve. Enable it in the Discord Developer Portal."
            )

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
        # Fold the journal into a snapshot so the next boot replays less.
        try:
            self.store.snapshot()
        except Exception:  # noqa: BLE001 - never block shutdown on this
            log.exception("final snapshot failed; journal replay will cover it")
        if self.lock is not None:
            # Released last, so a prep client cannot take the campaign while we are still
            # writing the closing snapshot.
            self.lock.release()
        await super().close()

    async def _sweep_loop(self) -> None:
        """Expire checks nobody rolled (§4).

        Runs on a timer rather than lazily, because the point is to say something at the
        table when the moment passes — which requires noticing without being prompted.
        """
        while True:
            try:
                await asyncio.sleep(self.settings.sweep_interval_seconds)
                effects = self.director.sweep()
                if effects:
                    await self.runner.run(effects)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("sweep failed; continuing")

    # -- ingest ------------------------------------------------------------------------

    async def on_message(self, message: "discord.Message") -> None:
        route = route_message(
            author_id=message.author.id,
            author_is_bot=message.author.bot,
            channel_id=message.channel.id,
            content=message.content or "",
            bot_user_id=getattr(self.user, "id", None),
            avrae_bot_id=self.settings.avrae_bot_id,
            game_channels=self.settings.game_channels,
            command_prefix=self.settings.command_prefix,
        )

        try:
            if route is Route.AVRAE:
                await self._on_avrae(message)
            elif route is Route.PLAYER:
                await self._on_player(message)
            elif route is Route.DM_COMMAND:
                await self._on_dm_command(message)
            elif route is Route.NOT_A_GAME_CHANNEL and not message.author.bot:
                # §6.3 calls an unregistered channel "silence, not an error", which is
                # right in a busy server and indistinguishable from a broken bot when
                # you are waiting in the channel you believe is configured.
                log.info(
                    "message %s is in channel %d, which is not in GARY_GAME_CHANNELS %s; "
                    "ignored",
                    message.id,
                    message.channel.id,
                    sorted(self.settings.game_channels),
                )
            # IGNORE_SELF / IGNORE_BOT: nothing to do, and nothing worth saying.
        except Exception:  # noqa: BLE001 - one bad message must not kill the bot
            log.exception("handling %s message %s failed", route.value, message.id)

    async def _on_avrae(self, message: "discord.Message") -> None:
        parsed = parse_embed(message_to_dict(message))
        if parsed.status is ParseStatus.UNKNOWN:
            log.warning("unparsed avrae embed %s: %s", message.id, parsed.reason)
        effects = self.director.on_avrae_message(
            channel_id=message.channel.id, parsed=parsed
        )
        await self.runner.run(effects)

    def _addressed_directly(self, message: "discord.Message") -> bool:
        """Was Gary mentioned -- as the user, or as his own managed role?

        Discord auto-creates a role named after the bot when it joins, so the picker
        offers two identical-looking "@Gary" entries. Picking the role puts it in
        ``role_mentions``, not ``mentions``, and the message reads as unaddressed while
        looking addressed to everyone at the table.
        """
        if self.user in message.mentions:
            return True
        for role in getattr(message, "role_mentions", ()) or ():
            tags = getattr(role, "tags", None)
            if tags is not None and getattr(tags, "bot_id", None) == self.user.id:
                return True
        return False

    async def _on_player(self, message: "discord.Message") -> None:
        mentions_bot = self._addressed_directly(message)
        effects = self.director.on_player_message(
            channel_id=message.channel.id,
            user_id=message.author.id,
            display_name=message.author.display_name,
            content=message.content,
            mentions_bot=mentions_bot,
        )
        if not effects:
            # §4.2 keeps Gary quiet unless addressed, so five players chatting do not
            # cost five model calls. Silent by design -- but indistinguishable from a
            # broken bot when you are expecting an answer, so say why.
            log.info(
                "player message %s not addressed to Gary (mentions_bot=%s, freeform=%s, "
                "open check=%s); no model call",
                message.id,
                mentions_bot,
                message.channel.id in self.settings.freeform_channels,
                any(
                    c.user_id == message.author.id
                    for c in self.director.registry.open_checks(message.channel.id)
                ),
            )
        await self.runner.run(effects)

    # -- ?dm commands ------------------------------------------------------------------

    async def _on_dm_command(self, message: "discord.Message") -> None:
        body = message.content[len(self.settings.command_prefix) :].strip()
        parts = body.split()
        command = parts[0].lower() if parts else "help"
        channel_id = message.channel.id

        if command == "playing":
            actor = body[len("playing"):].strip()
            if not actor:
                await self.sender.send(
                    channel_id,
                    f"`{self.settings.command_prefix}playing <character name>` — the name "
                    "exactly as Avrae shows it.",
                )
                return
            if self.director.players.get(message.author.id) != actor:
                self.store.apply(
                    [Mutation(MutationKind.PLAYER_CHARACTER, str(message.author.id), {"actor": actor})],
                    reason=f"{message.author.display_name} plays {actor}",
                )
            self.director.set_player_character(message.author.id, actor)
            await self.sender.send(
                channel_id, f"Noted — **{message.author.display_name}** is playing **{actor}**."
            )
            return

        if command == "undo":
            entry = self.store.undo_last()
            if entry is None:
                await self.sender.send(channel_id, "Nothing left to undo.")
            else:
                await self.sender.send(
                    channel_id,
                    f"Reversed `{entry['kind']}` on `{entry['target'] or 'world'}` "
                    f"({entry.get('reason', '')}).",
                )

        elif command == "cancel":
            mine = [
                c
                for c in self.director.registry.open_checks(channel_id)
                if c.user_id == message.author.id
            ]
            if not mine:
                await self.sender.send(channel_id, "You have no pending check.")
            else:
                self.director.registry.cancel(mine[0].check_id, CancelReason.MANUAL)
                await self.sender.send(
                    channel_id, f"Cancelled {mine[0].actor}'s {mine[0].kind.value}."
                )

        elif command == "checks":
            open_checks = list(self.director.registry.open_checks(channel_id))
            if not open_checks:
                await self.sender.send(channel_id, "No pending checks.")
            else:
                now = self.clock()
                lines = [
                    f"- **{c.actor}** {c.kind.value} {c.key} — `{c.command}` "
                    f"({max(0, int(c.expires_at - now))}s left)"
                    for c in open_checks
                ]
                await self.sender.send(channel_id, "\n".join(lines))

        elif command == "state":
            from ..context import render_live_state

            await self.sender.send(
                channel_id, "```\n" + render_live_state(self.store.state()) + "\n```"
            )

        elif command == "stats":
            s = self.runner.stats
            await self.sender.send(
                channel_id,
                f"sent={s.sent} narrated={s.narrated} "
                f"narrator_failures={s.narrator_failures} "
                f"drops={s.drops_by_status or '{}'}",
            )

        else:
            await self.sender.send(
                channel_id,
                f"`{self.settings.command_prefix}`: playing <name> | undo | cancel | checks | state | stats",
            )


# ---------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------


def build_client(
    settings: Settings, narrator: Optional[Narrator] = None, clock=time.time
) -> GaryClient:
    """Assemble the object graph from configuration.

    Pending checks are restored here, before the Gateway connects, so a restart between
    "run this command" and the player rolling still resolves against the committed DC.
    """
    if settings.backend == "firestore":
        from google.cloud import firestore  # imported lazily: optional dependency

        from ..state.firestore import FirestoreCheckBackend, FirestoreStateBackend
        from ..state.history import FirestoreHistoryBackend

        db = firestore.Client()
        state_backend = FirestoreStateBackend(db, settings.firestore_campaign_id)
        check_backend = FirestoreCheckBackend(db, settings.firestore_campaign_id)
        history_backend = FirestoreHistoryBackend(db, settings.firestore_campaign_id)
    else:
        state_backend = FileStateBackend(settings.campaign_root)
        check_backend = FileCheckBackend(settings.campaign_root / "pending_checks.json")
        history_backend = FileHistoryBackend(settings.campaign_root / "history")

    # Gary is the live writer, so it takes the campaign lock (§7.3). An MCP prep client
    # that finds the lock held degrades to read-only rather than interleaving writes into
    # a journal we are appending to. Failing to acquire is a warning, not a refusal:
    # a stale lock must never stop a game from starting.
    lock = CampaignLock(lock_path(settings.campaign_root), owner="gary")
    if not lock.acquire():
        log.warning(
            "campaign lock held by %s. Another Gary or an MCP prep client may be "
            "writing to this campaign. Continuing, but concurrent writes can corrupt "
            "the journal.",
            lock.holder(),
        )

    store = CampaignStore(backend=state_backend, clock=clock)
    registry = PendingCheckRegistry(
        clock=clock, ttl_seconds=settings.check_ttl_seconds, backend=check_backend
    )
    director = Director(
        registry=registry,
        clock=clock,
        game_channels=set(settings.game_channels),
        freeform_channels=set(settings.freeform_channels),
        players=dict(settings.players),
    )

    if narrator is None:
        narrator = build_narrator(settings, director, store, history_backend)

    return GaryClient(settings, director, store, narrator=narrator, clock=clock, lock=lock)


def build_narrator(settings: Settings, director, store, history_backend) -> Narrator:
    """Gemini when a key is configured, EchoNarrator otherwise.

    Falling back rather than failing is deliberate: running the whole Discord path with no
    model attached is the recommended first step (ARCHITECTURE.md §12.2), so an absent key
    is a valid configuration rather than an error. It is logged loudly enough not to be
    mistaken for a broken model.
    """
    if not settings.gemini_api_key:
        log.warning(
            "GEMINI_API_KEY not set — running with EchoNarrator. Rolls resolve and state "
            "updates, but there is no narrative."
        )
        return EchoNarrator()

    from google import genai  # imported lazily: optional dependency

    from .gemini import GeminiNarrator

    log.info("narrating with %s", settings.gemini_model)
    return GeminiNarrator(
        client=genai.Client(api_key=settings.gemini_api_key),
        director=director,
        store=store,
        history_backend=history_backend,
        model=settings.gemini_model,
        summary_model=settings.gemini_summary_model,
        window=settings.history_window,
    )


def main() -> None:  # pragma: no cover - operator entry point
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    settings = Settings.from_env()
    client = build_client(settings)
    client.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":  # pragma: no cover
    main()
