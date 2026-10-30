"""The Discord adapter (ARCHITECTURE.md §1, §6.3, §11).

Everything that touches discord.py lives here, and it is deliberately thin: routing,
rolling and resolution all happen in the core, which this module feeds and then obeys.

**Message Content is a privileged intent.** It must be enabled in the Discord Developer
Portal *and* set in code. Enabling only one of the two produces the same silent failure —
``message.content`` arrives empty, every player says nothing, and Gary looks alive while
answering no one. ``GaryClient.on_ready`` checks for this and complains loudly.

Run it:

    export DISCORD_TOKEN=...
    export GARY_GAME_CHANNELS=123456789
    python -m gary.adapters.discord_bot
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Dict, List, Optional, Tuple

import discord

from ..config import Settings
from ..context import chunk_document, parse_proposal
from ..fsm import ChoicePrompt, DamagePrompt, Director, PromptModel, RollPrompt
from ..moves import MOVES
from ..models import CancelReason, Mutation, MutationKind
from ..ports import EchoNarrator, Narrator
from ..registry import PendingCheckRegistry
from ..state.backend import FileCheckBackend, FileStateBackend
from ..state.history import FileHistoryBackend
from ..state.lock import CampaignLock, lock_path
from ..state.schema import format_world_time, parse_world_time
from ..state.store import CampaignStore, StateError
from ..sheets import describe
from .creator import StartButton
from .routing import Route, route_message
from .runner import EffectRunner
from .sender import ChannelSender

log = logging.getLogger("gary.discord")


class RollButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:roll:(?P<check_id>[0-9A-Z]+)",
):
    """The button a player clicks to roll their check.

    A ``DynamicItem`` rather than a View held in memory: the check id rides in the
    custom id, and discord.py rebuilds the button from it on any click. Pending checks
    survive a restart (§10), so their buttons have to as well -- a View would have died
    with the process and left every open check unrollable.

    Discord shows a button to everyone in the channel. Whether the person clicking may
    roll is decided by the Director, from who Discord says clicked.
    """

    def __init__(self, check_id: str, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label="Rolled" if disabled else "Roll",
                emoji="🎲",
                style=discord.ButtonStyle.secondary if disabled else discord.ButtonStyle.primary,
                custom_id=f"gary:roll:{check_id}",
                disabled=disabled,
            )
        )
        self.check_id = check_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "RollButton":
        return cls(match["check_id"])

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_roll(interaction, self.check_id)


#: Discord's limit on a button label.
MAX_LABEL = 80


class PickButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:pick:(?P<check_id>[0-9A-Z]+):(?P<index>\d+)",
):
    """One option of a pick-one choice. Like RollButton, it survives a restart: the
    check and the option ride in the custom id, and a held move survives one too."""

    def __init__(self, check_id: str, index: int, label: str = "") -> None:
        if len(label) > MAX_LABEL:
            label = label[: MAX_LABEL - 1] + "…"
        super().__init__(
            discord.ui.Button(
                label=label or f"Option {index + 1}",
                style=discord.ButtonStyle.primary,
                custom_id=f"gary:pick:{check_id}:{index}",
            )
        )
        self.check_id = check_id
        self.index = index

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "PickButton":
        return cls(match["check_id"], int(match["index"]))

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_choose(interaction, self.check_id, [self.index])


class ChooseSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"gary:choose:(?P<check_id>[0-9A-Z]+):(?P<move>[a-z_]+):(?P<band>strong|weak|miss)",
):
    """A pick-several choice -- Discern Realities' three questions. A dropdown that takes
    exactly ``count``, because three buttons toggled on and off is a worse way to say
    the same thing. The move and band ride in the custom id so the options can be rebuilt
    after a restart; Discord sends the picks themselves."""

    def __init__(self, check_id: str, move: str, band: str) -> None:
        choice = MOVES[move].band(band).choice
        super().__init__(
            discord.ui.Select(
                placeholder=f"Choose {choice.count}",
                min_values=choice.count,
                max_values=choice.count,
                options=[
                    discord.SelectOption(label=o.text[:100], value=str(i))
                    for i, o in enumerate(choice.options)
                ],
                custom_id=f"gary:choose:{check_id}:{move}:{band}",
            )
        )
        self.check_id = check_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "ChooseSelect":
        return cls(match["check_id"], match["move"], match["band"])

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_choose(
            interaction, self.check_id, [int(v) for v in self.item.values]
        )


def choice_view(prompt: ChoicePrompt) -> "discord.ui.View":
    view = discord.ui.View(timeout=None)
    if prompt.count == 1:
        for i, text in enumerate(prompt.options):
            view.add_item(PickButton(prompt.check_id, i, text))
    else:
        view.add_item(ChooseSelect(prompt.check_id, prompt.move, prompt.band))
    return view


class DamageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:damage:(?P<check_id>[0-9A-Z]+)",
):
    """The roller's damage roll, after a hit. A separate click, as the table chose: one
    to hit, one to see how hard. Survives a restart like the other buttons."""

    def __init__(self, check_id: str, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label="Damage rolled" if disabled else "Roll damage",
                emoji="⚔️",
                style=discord.ButtonStyle.secondary if disabled else discord.ButtonStyle.danger,
                custom_id=f"gary:damage:{check_id}",
                disabled=disabled,
            )
        )
        self.check_id = check_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "DamageButton":
        return cls(match["check_id"])

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_damage(interaction, self.check_id)


def damage_view(check_id: str, disabled: bool = False) -> "discord.ui.View":
    view = discord.ui.View(timeout=None)
    view.add_item(DamageButton(check_id, disabled=disabled))
    return view


def roll_view(check_id: str, disabled: bool = False) -> "discord.ui.View":
    # timeout=None: a check can wait minutes for its roll, and a View that times out
    # stops answering clicks while the check is still open.
    view = discord.ui.View(timeout=None)
    view.add_item(RollButton(check_id, disabled=disabled))
    return view


class DiscordTransport:
    """MessageTransport over discord.py."""

    def __init__(self, client: "discord.Client") -> None:
        self._client = client

    async def send(self, channel_id: int, text: str, prompt: Optional[object] = None) -> None:
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except discord.HTTPException:
                log.error("cannot reach channel %d; dropping message", channel_id)
                return
        if isinstance(prompt, RollPrompt):
            await channel.send(text, view=roll_view(prompt.check_id))
        elif isinstance(prompt, ChoicePrompt):
            await channel.send(text, view=choice_view(prompt))
        elif isinstance(prompt, DamagePrompt):
            await channel.send(text, view=damage_view(prompt.check_id))
        else:
            await channel.send(text)


#: Grouped by the moment you need them, because the commands that matter are not the
#: ones used most often -- they are the four that open a campaign, and a DM meeting Gary
#: for the first time has no way to tell those apart from the ones for mid-session repair.
HELP = """**Gary — `{bare}` commands**

__Opening a campaign__
`{p}import` *(attach a `.md` or `.txt`)* — read a whole setting document and file its people as NPCs, its places, factions and lore as notes
`{p}setting <premise>` — the standing premise, ≤600 chars. Shown to the DM every turn
`{p}scene` — suggest a scene · `{p}scene <location> | <what is true while you are there>` — set one
`{p}accept [setting|scene]` — take a suggestion Gary has offered
`{p}time 18:30` · `{p}time 4 Harvest Moon 1026 06:00` — the clock only moves forward

__At the table__
`{p}new` — make a character (a private form; it is yours to roll for)
`{p}sheet` — your character's sheet
`{p}playing <character name>` — claim a character someone else made
`{p}checks` — what Gary is waiting to be rolled
`{p}cancel` — drop your own pending check
`{p}undo` — reverse the last change to the world

__Looking in__
`{p}state` — clock, scene, party, and what is stored
`{p}notes` — what has been filed, by subject
`{p}stats` — messages sent, turns narrated, model failures
`{p}help` — this list. So does `{bare}` on its own

__Starting over__
`{p}reset` — **deletes the campaign**, the journal and every channel's memory. Two-step, and the one thing here with no undo."""


def render_help(prefix: str) -> str:
    """`?dm help`, `?dm` on its own, and anything unrecognised all land here."""
    return HELP.format(p=prefix, bare=prefix.strip())


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
        # (channel_id, "setting" | "scene") -> the line the model proposed. Held rather
        # than written: a proposal the DM never saw is not a decision the DM made.
        self._pending_proposals: Dict[Tuple[int, str], str] = {}
        # Same key -> lines already offered and passed over. A proposal is an ordinary
        # turn in history, so without this the model reads its own last suggestion and
        # rewords it: asking twice produced the same vault twice.
        self._declined_proposals: Dict[Tuple[int, str], List[str]] = {}
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
        # Before the Gateway connects, so a click on a button posted before a restart is
        # answered from the first moment.
        self.add_dynamic_items(RollButton, PickButton, ChooseSelect, DamageButton)
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def on_ready(self) -> None:
        log.info("connected as %s (%s)", self.user, getattr(self.user, "id", "?"))
        log.info(
            "watching %d channel(s); %d pending check(s) restored",
            len(self.settings.game_channels),
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
                "GARY_PLAYERS is not set. Until players run ?dm playing, the model is told "
                "only Discord display names, and a check goes to whoever spoke."
            )

        if not self.intents.message_content:
            log.error(
                "MESSAGE CONTENT INTENT IS OFF. Player messages will arrive empty and "
                "Gary will answer nobody. Enable it in the Discord Developer Portal."
            )

    async def close(self) -> None:
        # Reachable twice now -- once from the signal handler, once as `async with`
        # unwinds -- and the closing snapshot is not something to write twice.
        if getattr(self, "_closing", False):
            return await super().close()
        self._closing = True
        if self._sweeper is not None:
            self._sweeper.cancel()
        # Fold the journal into a snapshot so the next boot replays less.
        try:
            self.store.snapshot()
        except Exception:  # noqa: BLE001 - never block shutdown on this
            log.exception("final snapshot failed; journal replay will cover it")
        if self.lock is not None:
            # Released last, so nothing else can take the campaign while we are still
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

    #: How many rejected suggestions are named back to the model. Enough for a few
    #: re-rolls to keep moving; bounded because every one of them rides in the prompt.
    MAX_DECLINED_PROPOSALS = 5

    #: The setting rides on every turn, so it is capped. Detail belongs in
    #: notes, which surface only when their subject comes up.
    MAX_SETTING_CHARS = 600

    #: An imported setting file is read whole into memory and then into prompts. These
    #: bound what one `?dm import` can cost before it starts, not after.
    MAX_IMPORT_BYTES = 200_000
    IMPORT_CHUNK_CHARS = 6000
    MAX_IMPORT_CHUNKS = 12
    IMPORT_SUFFIXES = (".md", ".markdown", ".txt")

    @property
    def _has_model(self) -> bool:
        """Whether there is a model behind the runner at all.

        ``EffectRunner`` keeps its narrator private, and an earlier version of the import
        guard read ``runner.narrator`` -- always None, so the guard never fired and an
        import under ``EchoNarrator`` reported success while filing nothing. Read the
        real name, in one place, so the next caller cannot repeat it.
        """
        return not isinstance(getattr(self.runner, "_narrator", None), EchoNarrator)

    def _apply_setting(self, raw: str, who: str) -> str:
        self.store.apply(
            [Mutation(MutationKind.SETTING, "", {"text": raw})],
            reason=f"setting by {who}",
        )
        return f"Setting noted.\n> {raw}"

    def _apply_scene(self, raw: str, who: str) -> str:
        location, _, summary = raw.partition("|")
        # Both fields are set, always. Setting a location while leaving a summary that
        # described somewhere else is worse than setting no summary at all.
        self.store.apply(
            [
                Mutation(
                    MutationKind.SCENE,
                    "",
                    {"location": location.strip(), "summary": summary.strip()},
                )
            ],
            reason=f"scene set by {who}",
        )
        return f"Scene set: **{location.strip()}**" + (
            f" — {summary.strip()}" if summary.strip() else ""
        )

    def _forget_proposals(self, channel_id: int, kind: str) -> None:
        """End a proposal thread: the offer on the table and the record of what was
        turned down to reach it. The rejects only mean anything while the DM is still
        looking for a line; once one is settled, naming them back would push the *next*
        campaign's suggestions away from perfectly good ground."""
        self._pending_proposals.pop((channel_id, kind), None)
        self._declined_proposals.pop((channel_id, kind), None)

    async def _propose(self, channel_id: int, kind: str) -> bool:
        """Ask the model for a setting or a scene and hold it for the DM to accept.

        Nothing is written here. The model has state-mutating tools in hand on every
        call, so the one thing this must not do is take its own suggestion -- the line
        goes into ``_pending_proposals`` and stays there until somebody types accept.
        """
        prefix = self.settings.command_prefix
        key = (channel_id, kind)
        # Whatever is still pending was also offered and not taken -- the DM asking again
        # *is* the rejection, so it counts alongside the ones before it.
        offered = list(self._declined_proposals.get(key, ()))
        superseded = self._pending_proposals.get(key)
        if superseded:
            offered.append(superseded)

        line = parse_proposal(
            await self.runner.ask(
                PromptModel(
                    channel_id=channel_id,
                    kind=f"propose_{kind}",
                    payload={"already_offered": offered[-self.MAX_DECLINED_PROPOSALS:]},
                )
            )
        )
        if not line:
            await self.sender.send(
                channel_id,
                f"I couldn't put a {kind} into words just now. Try again, or write one "
                f"yourself with `{prefix}{kind} ...`.",
            )
            return False

        if superseded:
            self._declined_proposals[key] = (
                self._declined_proposals.get(key, []) + [superseded]
            )[-self.MAX_DECLINED_PROPOSALS:]
        self._pending_proposals[key] = line
        if kind == "setting":
            preview = f"> {line}"
            own = f"`{prefix}setting <premise>` to write your own"
        else:
            location, _, summary = line.partition("|")
            preview = f"> **{location.strip()}**" + (
                f" — {summary.strip()}" if summary.strip() else ""
            )
            own = f"`{prefix}scene <location> | <standing state>` to write your own"
        await self.sender.send(
            channel_id,
            f"Suggested {kind}:\n{preview}\n"
            f"`{prefix}accept` to take it, {own}. Nothing is written until you do.",
        )
        return True

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

        if not self._has_model:
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

        # The premise is the one thing an import cannot file, because it is not in the
        # document as a sentence -- it is what the whole document is about. The model has
        # just read all of it and will not have it that fresh again, so ask now.
        if not (self.store.state().get("setting") or "").strip():
            await self._propose(channel_id, "setting")

    async def on_message(self, message: "discord.Message") -> None:
        route = route_message(
            author_id=message.author.id,
            author_is_bot=message.author.bot,
            channel_id=message.channel.id,
            content=message.content or "",
            bot_user_id=getattr(self.user, "id", None),
            game_channels=self.settings.game_channels,
            command_prefix=self.settings.command_prefix,
        )

        try:
            if route is Route.PLAYER:
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

    async def on_roll(self, interaction: "discord.Interaction", check_id: str) -> None:
        """A Roll button was clicked.

        Discord gives three seconds to answer an interaction, and narrating takes longer.
        So the click is answered first -- the button is disabled, which is also how the
        table sees the roll was taken -- and the dice and narration follow as ordinary
        messages.

        A click that rolls nothing is answered privately: telling the whole table that
        Bob pressed Alice's button helps nobody.
        """
        channel_id = interaction.channel_id
        user_id = interaction.user.id
        effects = self.director.roll(
            channel_id=channel_id, user_id=user_id, check_id=check_id
        )
        if not effects:
            await interaction.response.send_message(
                self._why_no_roll(channel_id, check_id), ephemeral=True
            )
            return

        try:
            await interaction.response.edit_message(view=roll_view(check_id, disabled=True))
        except discord.HTTPException:
            # The roll is already made and must be narrated regardless; a button left
            # enabled only costs a click that will now say it has been rolled.
            log.warning("could not disable roll button for %s", check_id)
        await self.runner.run(effects)

    async def _finish_character(
        self, interaction: "discord.Interaction", sheet: dict, channel_id: int
    ) -> Optional[str]:
        """Store a finished sheet and claim it for whoever made it.

        One apply, so the sheet and the claim land together or not at all -- a sheet
        nobody plays, or a claim on a character with no sheet, is exactly the half-state
        a crash between two writes would leave.

        Refuses a name another player's character already has: checks are attributed by
        character, and two Kaels at one table cannot both be the one rolling.
        """
        user_id = interaction.user.id
        name = sheet["name"]
        owner = next(
            (uid for uid, actor in self.director.players.items() if actor == name), None
        )
        if owner is not None and owner != user_id:
            return f"Someone at this table already plays a character named {name}."

        diff = self.store.apply(
            [
                Mutation(MutationKind.SHEET, name, {"sheet": sheet}),
                Mutation(MutationKind.PLAYER_CHARACTER, str(user_id), {"actor": name}),
            ],
            reason=f"{interaction.user.display_name} creates {name}",
        )
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        self.director.set_player_character(user_id, name)
        await self.sender.send(channel_id, f"**{name}** joins the party — {describe(sheet, with_name=False)}")
        return None

    async def on_choose(
        self, interaction: "discord.Interaction", check_id: str, picks: List[int]
    ) -> None:
        """An option was picked after a roll. Answered the same way as a roll: first by
        taking the options away, then with the choice and the narration."""
        channel_id = interaction.channel_id
        effects = self.director.choose(
            channel_id=channel_id, user_id=interaction.user.id, check_id=check_id, picks=picks
        )
        if not effects:
            await interaction.response.send_message(
                self._why_no_roll(channel_id, check_id, verb="choice"), ephemeral=True
            )
            return

        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            log.warning("could not clear the choice for %s", check_id)
        await self.runner.run(effects)

    async def on_damage(self, interaction: "discord.Interaction", check_id: str) -> None:
        """Roll damage was clicked. Answered like the Roll button: disable it first, then
        the damage, any blow back, and the narration."""
        channel_id = interaction.channel_id
        effects = self.director.roll_damage(
            channel_id=channel_id, user_id=interaction.user.id, check_id=check_id
        )
        if not effects:
            await interaction.response.send_message(
                self._why_no_roll(channel_id, check_id, verb="damage roll"), ephemeral=True
            )
            return

        try:
            await interaction.response.edit_message(view=damage_view(check_id, disabled=True))
        except discord.HTTPException:
            log.warning("could not disable damage button for %s", check_id)
        await self.runner.run(effects)

    def _why_no_roll(self, channel_id: int, check_id: str, verb: str = "roll") -> str:
        pending = next(
            (c for c in self.director.registry.open_checks(channel_id) if c.check_id == check_id),
            None,
        )
        if pending is not None:
            return f"That {verb} is **{pending.actor}**'s to make."
        return f"That {verb} has already been made, has expired, or was replaced by a newer one."

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
            self._forget_proposals(channel_id, "setting")
            await self.sender.send(
                channel_id, self._apply_setting(raw, message.author.display_name)
            )
            return

        if command == "import":
            await self._import_document(message, channel_id)
            return

        if command == "scene":
            raw = body[len("scene"):].strip()
            if not raw:
                # Bare `?dm scene` used to print its own usage. Proposing one is the more
                # useful answer to the same question and costs a single model call -- and
                # the proposal names the manual form anyway, so the usage is not lost.
                if self._has_model:
                    await self._propose(channel_id, "scene")
                    return
                await self.sender.send(
                    channel_id,
                    f"`{self.settings.command_prefix}scene <location> | <standing state>`"
                    " — the summary is optional, and is re-read every turn, so write what"
                    " is *true while you are there*, not what happens on arrival.\n"
                    "Good: `The Rusty Anchor | Half-empty common room, rain on the"
                    " shutters. Barkeep watches the door.`",
                )
                return
            self._forget_proposals(channel_id, "scene")
            await self.sender.send(
                channel_id, self._apply_scene(raw, message.author.display_name)
            )
            return

        if command == "accept":
            wanted = parts[1].lower() if len(parts) > 1 else ""
            if wanted and wanted not in ("setting", "scene"):
                await self.sender.send(
                    channel_id,
                    f"`{prefix}accept`, or `{prefix}accept setting` / "
                    f"`{prefix}accept scene` to take just one of them.",
                )
                return
            kinds = [wanted] if wanted else ["setting", "scene"]
            taken = [
                (kind, self._pending_proposals[(channel_id, kind)])
                for kind in kinds
                if (channel_id, kind) in self._pending_proposals
            ]
            for kind, _ in taken:
                self._forget_proposals(channel_id, kind)
            if not taken:
                await self.sender.send(
                    channel_id,
                    f"Nothing to accept — I haven't suggested "
                    + (f"a {wanted}" if wanted else "anything")
                    + f" here. `{prefix}import` a setting file, or `{prefix}scene` with "
                    "no arguments, and I'll offer one.",
                )
                return
            who = message.author.display_name
            for kind, line in taken:
                confirmation = (
                    self._apply_setting(line, who) if kind == "setting"
                    else self._apply_scene(line, who)
                )
                await self.sender.send(channel_id, confirmation)
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
                    "Everything goes, including every character sheet and who is playing whom.\n"
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
            self._pending_proposals.clear()
            self._declined_proposals.clear()
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
                "Characters went with it: everyone run "
                f"`{self.settings.command_prefix}new` before the first roll.",
            )
            return

        if command == "new":
            view = StartButton(
                message.author.id,
                message.author.display_name,
                lambda interaction, sheet: self._finish_character(interaction, sheet, channel_id),
            )
            # Sent directly rather than through the sender: it carries a form, and a
            # one-line reply to a command has no pacing to protect.
            await message.channel.send(
                f"**{message.author.display_name}** — make your character:", view=view
            )
            return

        if command == "sheet":
            actor = self.director.players.get(message.author.id)
            sheet = (self.store.state().get("sheets") or {}).get(actor) if actor else None
            await self.sender.send(
                channel_id,
                describe(sheet) if sheet
                else f"You have no sheet yet. `{prefix}new` makes one.",
            )
            return

        if command == "playing":
            actor = body[len("playing"):].strip()
            if not actor:
                await self.sender.send(
                    channel_id,
                    f"`{self.settings.command_prefix}playing <character name>`.",
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
                    channel_id, f"Cancelled {mine[0].actor}'s {MOVES[mine[0].move].name}."
                )

        elif command == "checks":
            open_checks = list(self.director.registry.open_checks(channel_id))
            if not open_checks:
                await self.sender.send(channel_id, "No pending checks.")
            else:
                now = self.clock()
                lines = [
                    f"- **{c.actor}** {MOVES[c.move].name} — "
                    + ("rolled, waiting on a choice" if c.rolled else "waiting on the roll")
                    + f" ({max(0, int(c.expires_at - now))}s left)"
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
                f"outcomes_recorded={s.outcomes_recorded}",
            )

        elif command == "help":
            await self.sender.send(channel_id, render_help(prefix))

        else:
            await self.sender.send(
                channel_id, f"I don't know `{prefix}{command}`.\n\n" + render_help(prefix)
            )


# ---------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------


def build_client(
    settings: Settings, narrator: Optional[Narrator] = None, clock=time.time
) -> GaryClient:
    """Assemble the object graph from configuration.

    Pending checks are restored here, before the Gateway connects, so a restart between
    "roll for it" and the player rolling still resolves against the committed DC.
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

    # Gary is the live writer, so it takes the campaign lock (§7.3): a second process
    # that finds it held runs read-only rather than interleaving writes into a journal we
    # are appending to. Failing to acquire is a warning, not a refusal -- a stale lock
    # must never stop a game from starting.
    stranded = settings.stranded_campaign()
    if stranded is not None:
        log.warning(
            "found a campaign at %s, but this one is at %s. The default moved out of the "
            "working directory so a `git clone` cannot destroy it. If %s is the campaign "
            "you want, move it or set GARY_CAMPAIGN_ROOT -- nothing here will touch it.",
            stranded, settings.campaign_root, stranded,
        )

    lock = CampaignLock(lock_path(settings.campaign_root), owner="gary")
    if not lock.acquire():
        log.warning(
            "campaign lock held by %s. Another Gary may be writing to this campaign. "
            "Continuing, but concurrent writes can corrupt the journal.",
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
        sheet_for=lambda actor: (store.state().get("sheets") or {}).get(actor),
        monsters=lambda: store.state().get("monsters") or {},
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
    )


async def serve(client: "GaryClient", token: str) -> None:  # pragma: no cover
    """Run until told to stop, then shut down through `close`.

    `Client.run` catches KeyboardInterrupt and nothing else. That is fine at a terminal
    and wrong in a container: `docker stop` sends SIGTERM, and **a process running as PID
    1 discards any signal it has no handler for** -- the kernel does not apply default
    dispositions there. So SIGTERM did nothing, Docker waited out its grace period, and
    the process was SIGKILLed. Stopping took 30 seconds and `close` never ran, so the
    closing snapshot was skipped and the lock file was left behind reading `pid=1`.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(_stop(client, s))
            )
        except NotImplementedError:  # no signal handlers on this platform
            log.warning("cannot install a %s handler here", sig)
    async with client:
        await client.start(token)


async def _stop(client: "GaryClient", sig: int) -> None:  # pragma: no cover
    log.info("%s received; closing cleanly", signal.Signals(sig).name)
    await client.close()


def main() -> None:  # pragma: no cover - operator entry point
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    settings = Settings.from_env()
    client = build_client(settings)
    asyncio.run(serve(client, settings.discord_token))


if __name__ == "__main__":  # pragma: no cover
    main()
