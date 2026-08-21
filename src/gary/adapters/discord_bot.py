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
from ..context import chunk_document
from ..fsm import Director, PromptModel
from ..models import CancelReason, Mutation, MutationKind
from ..parser.avrae import ParseStatus, parse_embed
from ..ports import EchoNarrator, Narrator
from ..registry import PendingCheckRegistry
from ..state.backend import FileCheckBackend, FileStateBackend
from ..state.history import FileHistoryBackend
from ..state.lock import CampaignLock, lock_path
from ..state.schema import format_world_time, parse_world_time
from ..state.store import CampaignStore, StateError
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
        history_backend=None,
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
        self.history_backend = history_backend
        self._reset_token: Optional[str] = None
        self._sheet_asked_by: Dict[int, tuple] = {}
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

    async def on_message_edit(
        self, before: "discord.Message", after: "discord.Message"
    ) -> None:
        """Avrae EDITS the initiative tracker rather than reposting it.

        `!init next` fires no on_message at all, so a bot without this sees the tracker
        once — when combat starts — and never again. Gary's mirror stayed empty for a
        whole fight because of it, and the model could not tell whose turn it was.

        scripts/capture_avrae_fixtures.py had this from the start and says so in a
        comment; the bot never got the same handler, which is why every captured
        initiative fixture carries `trigger: edit`.

        Only Avrae's edits are forwarded. Re-running every edit would mean a player
        fixing a typo costs another model call and another narration, and an edited roll
        embed would re-enter the resolution path.
        """
        if after.author.id != self.settings.avrae_bot_id:
            return
        await self.on_message(after)

    async def on_raw_message_edit(self, payload) -> None:
        """Edits to messages the bot never saw posted.

        ``on_message_edit`` only fires for messages in discord.py's own cache — those seen
        since this process connected. The initiative tracker is pinned and long-lived, so
        after any restart mid-combat it is not cached, and every `!init next` edits it
        silently. That is not an edge case here: Gary was restarted repeatedly during one
        fight and the combat mirror stayed empty the whole time.

        Fetching costs an API call per edit, which is a handful per turn.
        """
        if payload.cached_message is not None:
            return  # on_message_edit already handled it
        channel = self.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            log.warning("could not fetch edited message %s", payload.message_id)
            return
        if message.author.id != self.settings.avrae_bot_id:
            return
        await self.on_message(message)

    #: How long a `!sheet` request stays claimable, in seconds. Avrae answers in well
    #: under a second; anything older is a different request.
    SHEET_CLAIM_SECONDS = 30.0

    #: The setting rides on every turn, so it is capped. Detail belongs in
    #: notes, which surface only when their subject comes up.
    MAX_SETTING_CHARS = 600

    #: An imported setting file is read whole into memory and then into prompts. These
    #: bound what one `?dm import` can cost before it starts, not after.
    MAX_IMPORT_BYTES = 200_000
    IMPORT_CHUNK_CHARS = 6000
    MAX_IMPORT_CHUNKS = 12
    IMPORT_SUFFIXES = (".md", ".markdown", ".txt")

    async def _import_document(self, message: "discord.Message", channel_id: int) -> None:
        """`?dm import` with a Markdown attachment — fan a setting file into the stores.

        The file is somebody else's prose read while the model holds state-mutating tools,
        so it travels as data (`render_document`) rather than as a turn of play. Everything
        that can be refused is refused before a single token is spent.
        """
        prefix = self.settings.command_prefix
        attachments = list(getattr(message, "attachments", None) or [])
        if not attachments:
            await self.sender.send(
                channel_id,
                f"`{prefix}import` needs a file attached — a `.md` or `.txt` export from a "
                "setting generator, or anything you have written yourself. I read it and "
                "file what it describes: people as NPCs, hooks as quests, places and "
                "factions as notes.\n"
                f"Set the one-line premise separately with `{prefix}setting` — that is what "
                "rides on every turn; this is the detail behind it.",
            )
            return

        attachment = attachments[0]
        filename = str(getattr(attachment, "filename", "document"))
        if not filename.lower().endswith(self.IMPORT_SUFFIXES):
            await self.sender.send(
                channel_id,
                f"I can only read {', '.join(self.IMPORT_SUFFIXES)} — `{filename}` is "
                "something else. Export as Markdown or plain text.",
            )
            return
        size = int(getattr(attachment, "size", 0) or 0)
        if size > self.MAX_IMPORT_BYTES:
            await self.sender.send(
                channel_id,
                f"`{filename}` is {size // 1000}KB and the limit is "
                f"{self.MAX_IMPORT_BYTES // 1000}KB. Split it and import the parts.",
            )
            return

        try:
            raw = await attachment.read()
        except Exception as exc:  # discord.HTTPException and friends
            LOG.warning("import: could not read attachment %s: %s", filename, exc)
            await self.sender.send(channel_id, f"Couldn't download `{filename}` — {exc}")
            return

        # Decode defensively: a generator's export may carry smart quotes in some other
        # encoding, and a mangled character is a better outcome than a dead import.
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        chunks = chunk_document(text, self.IMPORT_CHUNK_CHARS)
        if not chunks:
            await self.sender.send(channel_id, f"`{filename}` is empty.")
            return
        if len(chunks) > self.MAX_IMPORT_CHUNKS:
            await self.sender.send(
                channel_id,
                f"`{filename}` comes to {len(chunks)} parts and I import at most "
                f"{self.MAX_IMPORT_CHUNKS} at a time — each one is a model call. Import "
                "the most important sections first.",
            )
            return

        if isinstance(getattr(self.runner, "_narrator", None), EchoNarrator):
            await self.sender.send(
                channel_id,
                "No model is attached, so there is nothing that can read a document — set "
                "`GEMINI_API_KEY` and restart. Everything else still works.",
            )
            return

        total = len(chunks)
        await self.sender.send(
            channel_id,
            f"Reading `{filename}` — {total} part{'s' if total > 1 else ''}. "
            "I'll file people, places, factions and hooks as I go.",
        )
        for index, chunk in enumerate(chunks, start=1):
            await self.runner.run([
                PromptModel(
                    channel_id=channel_id,
                    kind="import_document",
                    payload={
                        "filename": filename,
                        "chunk": chunk,
                        "part": index,
                        "total": total,
                    },
                )
            ])
        await self.sender.send(
            channel_id,
            f"Done with `{filename}`. `{prefix}notes` lists what stuck — anything it "
            "missed you can add by telling me in the channel.",
        )

    async def on_message(self, message: "discord.Message") -> None:
        # `!sheet` carries no interaction metadata — it is a prefix command — so the sheet
        # embed that follows cannot say who asked for it. Remember the asker, and claim
        # the next sheet for them. This is what lets `!sheet` register a player outright:
        # character name and level from the embed, Discord user from here.
        if (message.content or "").strip().lower().startswith("!sheet"):
            self._sheet_asked_by[message.channel.id] = (
                message.author.id,
                message.author.display_name,
                self.clock(),
            )

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
        if parsed.status is ParseStatus.SHEET and parsed.sheet is not None:
            await self._claim_sheet(message.channel.id, parsed.sheet.actor)
        effects = self.director.on_avrae_message(
            channel_id=message.channel.id, parsed=parsed
        )
        await self.runner.run(effects)

    async def _claim_sheet(self, channel_id: int, actor: str) -> None:
        """Register whoever just ran `!sheet` as playing the character it reported.

        The embed names the character but not the person: `!sheet` is a prefix command and
        carries no interaction metadata, so this is the only way to connect the two
        without asking. It makes `!sheet` the whole of setup -- name, level, and who is
        playing it -- from a command everyone runs anyway.

        Claims lapse after SHEET_CLAIM_SECONDS. Two people running `!sheet` in the same
        instant could be swapped; re-running fixes it, and `?dm playing` overrides.
        """
        claim = self._sheet_asked_by.pop(channel_id, None)
        if claim is None:
            return
        user_id, display_name, asked_at = claim
        if self.clock() - asked_at > self.SHEET_CLAIM_SECONDS:
            return
        if self.director.players.get(user_id) == actor:
            return  # already registered; nothing to journal

        self.store.apply(
            [Mutation(MutationKind.PLAYER_CHARACTER, str(user_id), {"actor": actor})],
            reason=f"{display_name} plays {actor} (from !sheet)",
        )
        self.director.set_player_character(user_id, actor)
        log.info("registered %s as playing %s from !sheet", display_name, actor)
        await self.sender.send(
            channel_id, f"Noted — **{display_name}** is playing **{actor}**."
        )

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
        prefix = self.settings.command_prefix

        if command == "time":
            raw = body[len("time"):].strip()
            state = self.store.state()
            if not raw:
                await self.sender.send(
                    channel_id,
                    f"It is {format_world_time(state)}. Set it with "
                    f"`{self.settings.command_prefix}time 18:30` for later today, or "
                    f"`{self.settings.command_prefix}time 4 Harvest Moon 1026 06:00` for a date. "
                    "The clock only moves forward.",
                )
                return
            try:
                target = parse_world_time(raw, state)
            except ValueError as exc:
                await self.sender.send(channel_id, f"Can't read that time — {exc}")
                return

            delta = target - int(state.get("world_minutes", 0))
            if delta < 0:
                # Effects expire on the way past their minute and do not un-expire, so
                # rewinding would leave state that no longer matches the clock.
                await self.sender.send(
                    channel_id,
                    f"That is in the past — it is {format_world_time(state)}. The clock "
                    "only moves forward; give a later time or a later day.",
                )
                return
            try:
                self.store.advance_time(delta, reason=f"clock set by {message.author.display_name}")
            except StateError as exc:
                await self.sender.send(channel_id, f"Can't move the clock: {exc}")
                return
            await self.sender.send(
                channel_id, f"It is now {format_world_time(self.store.state())}."
            )
            return

        if command == "setting":
            raw = body[len("setting"):].strip()
            if not raw:
                current = self.store.state().get("setting") or ""
                await self.sender.send(
                    channel_id,
                    (f"Setting: {current}" if current else "No setting yet.")
                    + f"\nSet it with `{self.settings.command_prefix}setting <premise, "
                    "tone, region>` — it is shown to the DM on every single turn, so keep "
                    "it to the standing facts of the world, not what is happening now.",
                )
                return
            if len(raw) > self.MAX_SETTING_CHARS:
                await self.sender.send(
                    channel_id,
                    f"That is {len(raw)} characters and the limit is "
                    f"{self.MAX_SETTING_CHARS} — it rides on every turn, so it has to stay "
                    "short. Put the detail in notes, which surface when their subject "
                    "comes up.",
                )
                return
            self.store.apply(
                [Mutation(MutationKind.SETTING, "", {"text": raw})],
                reason=f"setting by {message.author.display_name}",
            )
            await self.sender.send(channel_id, f"Setting noted.\n> {raw}")
            return

        if command == "import":
            await self._import_document(message, channel_id)
            return

        if command == "scene":
            raw = body[len("scene"):].strip()
            if not raw:
                await self.sender.send(
                    channel_id,
                    f"`{self.settings.command_prefix}scene <location> | <standing state>`"
                    " — the summary is optional, and is re-read every turn, so write what"
                    " is *true while you are there*, not what happens on arrival.\n"
                    "Good: `The Rusty Anchor | Half-empty common room, rain on the"
                    " shutters. Barkeep watches the door.`",
                )
                return
            location, _, summary = raw.partition("|")
            # Both fields are set, always. Setting a location while leaving a summary
            # that described somewhere else is worse than setting no summary at all.
            self.store.apply(
                [
                    Mutation(
                        MutationKind.SCENE,
                        "",
                        {"location": location.strip(), "summary": summary.strip()},
                    )
                ],
                reason=f"scene set by {message.author.display_name}",
            )
            await self.sender.send(
                channel_id,
                f"Scene set: **{location.strip()}**"
                + (f" — {summary.strip()}" if summary.strip() else ""),
            )
            return

        if command == "reset":
            token = parts[1] if len(parts) > 1 else ""
            if not token:
                self._reset_token = f"{int(self.clock()) % 10000:04d}"
                state = self.store.state()
                await self.sender.send(
                    channel_id,
                    "**This deletes the campaign.** World state, quests, NPCs, notes, "
                    "inventories and conversation memory all go — and so does the "
                    "journal, so there is no record afterwards and no undo. "
                    f"Currently: {len(state.get('notes') or [])} note(s), "
                    f"{len(state.get('quests') or {})} quest(s), "
                    f"{len(state.get('npcs') or {})} NPC(s).\n"
                    "Everything goes, including who is playing whom.\n"
                    f"To confirm: `{self.settings.command_prefix}reset "
                    f"{self._reset_token}`"
                )
                return
            if token != self._reset_token:
                await self.sender.send(
                    channel_id,
                    f"That code is not current. Run `{self.settings.command_prefix}reset` "
                    "again to get a fresh one.",
                )
                return

            self._reset_token = None
            for check in list(self.director.registry.open_checks()):
                self.director.registry.cancel(check.check_id, CancelReason.MANUAL)
            if self.history_backend is not None:
                self.history_backend.clear_all()
            self.store.reset_campaign()
            self.director.forget_everything()
            log.warning("campaign reset by %s in channel %d", message.author, channel_id)
            await self.sender.send(
                channel_id,
                f"Campaign reset. It is {format_world_time(self.store.state())}. "
                "Everyone run "
                f"`{self.settings.command_prefix}playing <character name>` again before "
                "the first roll.",
            )
            return

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

            state = self.store.state()
            # Layer 2 verbatim, because the point of this command is to show what the
            # model is actually told. Notes and NPCs are layer 3 and surface only when
            # their subject comes up (§8), so they are legitimately absent here -- but
            # absent and *missing* look identical on screen, and after an import that
            # filed ten notes this read as if nothing had been stored at all. The counts
            # go outside the block: still not the model's view, and no longer a silence.
            body = "```\n" + render_live_state(state) + "\n```"
            notes, npcs = len(state.get("notes") or []), len(state.get("npcs") or {})
            if notes or npcs:
                body += (
                    f"\nAlso stored: **{notes} note(s)**, **{npcs} NPC(s)** — these are "
                    "retrieved when their subject is mentioned rather than shown every "
                    f"turn, so they are not in the block above. `{prefix}notes` lists them."
                )
            await self.sender.send(channel_id, body)

        elif command == "notes":
            state = self.store.state()
            lines = []
            for subject in sorted({str(n.get("subject", "")) for n in state.get("notes") or []}):
                count = sum(1 for n in state["notes"] if n.get("subject") == subject)
                lines.append(f"- {subject} ({count})" if count > 1 else f"- {subject}")
            people = sorted(
                str(v.get("name") or k) for k, v in (state.get("npcs") or {}).items()
            )
            parts = []
            if lines:
                parts.append("**Notes**, by subject — say one to bring it into play:\n"
                             + "\n".join(lines))
            if people:
                parts.append("**People**: " + ", ".join(people))
            await self.sender.send(
                channel_id,
                "\n\n".join(parts) if parts
                else f"Nothing filed yet. `{prefix}import` a setting file, or just tell me.",
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
                f"`{self.settings.command_prefix}`: setting <premise> | scene <location> | time <HH:MM> | import (attach .md) | notes | playing <name> | undo | cancel | checks | state | stats | reset",
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
        encounter_active=lambda: store.encounter_active,
    )

    if narrator is None:
        narrator = build_narrator(settings, director, store, history_backend)

    return GaryClient(
        settings, director, store, narrator=narrator, clock=clock, lock=lock,
        history_backend=history_backend,
    )


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
        timeout_seconds=settings.gemini_timeout_seconds,
        fallback_model=settings.gemini_fallback_model,
        coins_command=settings.coins_command,
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
