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
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

import discord

from ..config import Settings
from ..steadings import here
from ..fsm import (
    ChoicePrompt, CurePrompt, DamagePrompt, Director, OverloadPrompt, PromptModel, ReactPrompt, RollPrompt,
    SpellsPrompt,
)
from ..moves import MOVES
from ..models import CancelReason, Mutation, MutationKind
from ..ports import EchoNarrator, Narrator
from ..registry import PendingCheckRegistry
from ..state.backend import FileCheckBackend, FileStateBackend
from ..state.history import FileHistoryBackend
from ..state.lock import CampaignLock, lock_path
from ..state.schema import format_world_time
from ..state.store import CampaignStore
from ..levelling import ready
from ..sheets import LEVEL_UP_HINT, SheetError, alignment_line, carrying_line, describe, xp_line, xp_needed
from .. import bonds, choices, companion as companions, hirelings, spells
from .creator import FORM_TIMEOUT_SECONDS, AlignmentForm, CharacterCreator, ChoicesForm, StartButton
from .bonds_form import BondsForm
from .companion_form import CompanionForm
from .levelup import LevelUpForm
from .session import EndOfSession
from .spellbook import SpellPicker
from .routing import Route, route_message
from .runner import EffectRunner
from .sender import ChannelSender

log = logging.getLogger("gary.discord")



NICK_MAX = 32  # Discord's limit on a server nickname
_TAG = re.compile(r"\s*\([^()]*\)\s*$")


def tagged_nickname(current: str, actor: str) -> str:
    """``current`` with its character tag set to ``actor``: "Magnentius (Slymin)". An old
    tag is replaced rather than stacked, and the name is cut, not the tag, to fit 32."""
    base = _TAG.sub("", current or "").strip()
    tag = f" ({actor})"[:NICK_MAX]
    return (base[:NICK_MAX - len(tag)].rstrip() + tag).strip()


async def tag_nickname(member: Any, actor: str) -> None:
    """Show who plays whom in the member list by setting the player's server nickname.

    Best effort: it needs Manage Nicknames and a role above the player's, and Discord never
    lets a bot rename the server owner, so a failure is logged and play goes on.
    """
    guild = getattr(member, "guild", None)
    if guild is None or not hasattr(member, "edit"):
        return  # a DM, or a user rather than a member
    if getattr(guild, "owner_id", None) == member.id:
        log.info("not tagging %s: Discord does not let bots rename the server owner", member)
        return
    wanted = tagged_nickname(member.nick or member.display_name, actor)
    if wanted == member.nick:
        return
    try:
        await member.edit(nick=wanted, reason=f"playing {actor}")
    except discord.HTTPException as exc:  # Forbidden included: no permission, or role too low
        log.warning("could not set %s's nickname to %r: %s", member, wanted, exc)

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
                    wrapped_option(o.text, str(i))
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


class SpellsButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:spells:(?P<user_id>\d+)",
):
    """Choose spells: opens the private spell form for this player's caster. Durable like
    the other buttons -- an hour of prayer survives a restart -- and whether there is
    anything to choose is the sheet's to say, not the button's."""

    def __init__(self, user_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Choose spells", emoji="📖", style=discord.ButtonStyle.primary,
                custom_id=f"gary:spells:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "SpellsButton":
        return cls(int(match["user_id"]))

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_spells(interaction, self.user_id)


class DropButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:drop:(?P<check_id>[0-9A-Z]+):(?P<index>\d+)",
):
    """Overloaded: drop this item (by its place in the gear) and roll at -1."""

    def __init__(self, check_id: str, index: int, label: str = "") -> None:
        super().__init__(discord.ui.Button(
            label=f"Drop {label}"[:MAX_LABEL] if label else f"Drop item {index + 1}",
            style=discord.ButtonStyle.primary, custom_id=f"gary:drop:{check_id}:{index}",
        ))
        self.check_id, self.index = check_id, index

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "DropButton":
        return cls(match["check_id"], int(match["index"]))

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_overload(interaction, self.check_id, self.index)


class FailButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:fail:(?P<check_id>[0-9A-Z]+)",
):
    """Overloaded: keep everything and fail the move."""

    def __init__(self, check_id: str) -> None:
        super().__init__(discord.ui.Button(
            label="Keep it all and fail", style=discord.ButtonStyle.danger,
            custom_id=f"gary:fail:{check_id}",
        ))
        self.check_id = check_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "FailButton":
        return cls(match["check_id"])

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_overload(interaction, self.check_id, None)


def overload_view(prompt: OverloadPrompt) -> "discord.ui.View":
    view = discord.ui.View(timeout=None)
    for index, name in prompt.items[:24]:  # 25 components at most, and Fail is one
        view.add_item(DropButton(prompt.check_id, index, name))
    view.add_item(FailButton(prompt.check_id))
    return view


class CureButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:cure:(?P<user_id>\d+):(?P<debility>[a-z]+)",
):
    """One debility Recover may clear, for its player to pick. Durable like the others;
    whether one is still owed is the sheet's to say."""

    def __init__(self, user_id: int, debility: str) -> None:
        super().__init__(discord.ui.Button(
            label=f"Shed {debility.title()}", style=discord.ButtonStyle.success,
            custom_id=f"gary:cure:{user_id}:{debility}",
        ))
        self.user_id, self.debility = user_id, debility

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "CureButton":
        return cls(int(match["user_id"]), match["debility"])

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_cure(interaction, self.user_id, self.debility)


def wrapped_option(text: str, value: str, width: int = 45) -> "discord.SelectOption":
    """A select option that shows all of a long text: Discord cuts the label off at one line (about
    45 characters), so the rest runs on in the grey description line under it."""
    text = text.strip()
    if len(text) <= width:
        return discord.SelectOption(label=text, value=value)
    cut = text.rfind(" ", 0, width + 1)
    cut = cut if cut > 0 else width
    return discord.SelectOption(label=text[:cut].rstrip()[:100], value=value,
                                description=text[cut:].strip()[:100] or None)


@dataclass(frozen=True)
class PremisePrompt:
    """Gemini's premises, to choose one from (/begin): activities[i] happens at places[i]."""

    places: Tuple[str, ...]
    activities: Tuple[str, ...]


class PremiseView(discord.ui.View):
    """Pick a premise -- a place and its activity, offered together -- or write your own, or ask
    for others, then Accept. Only whoever ran /begin may choose. Not durable: after a restart,
    /begin offers again."""

    def __init__(self, prompt: PremisePrompt) -> None:
        super().__init__(timeout=3600)
        self.place = self.activity = ""
        self._premise = discord.ui.Select(placeholder="Choose a premise", row=0, options=[
            discord.SelectOption(label=p[:100], value=str(i), description=a[:100] or None)
            for i, (p, a) in enumerate(zip(prompt.places, prompt.activities))])

        def keep(v: str) -> None:
            self.place, self.activity = prompt.places[int(v)], prompt.activities[int(v)]

        self._premise.callback = self._chose(keep)
        accept = discord.ui.Button(label="Accept", emoji="✅", style=discord.ButtonStyle.success, row=1)
        own = discord.ui.Button(label="Write my own", emoji="✍️", style=discord.ButtonStyle.secondary, row=1)
        more = discord.ui.Button(label="Other options", emoji="🎲", style=discord.ButtonStyle.secondary, row=1)
        accept.callback, own.callback, more.callback = self._accept, self._own, self._more
        for item in (self._premise, accept, own, more):
            self.add_item(item)

    def _chose(self, keep):
        async def chose(interaction: "discord.Interaction") -> None:
            if not await interaction.client.may_run_opening(interaction):
                return
            keep(interaction.data["values"][0])
            await interaction.response.defer()

        return chose

    async def _accept(self, interaction: "discord.Interaction") -> None:
        if not await interaction.client.may_run_opening(interaction):
            return
        if not (self.place and self.activity):
            await interaction.response.send_message("Choose a premise first.", ephemeral=True)
            return
        self.stop()
        await interaction.client.on_premise(interaction, self.place, self.activity)

    async def _own(self, interaction: "discord.Interaction") -> None:
        if not await interaction.client.may_run_opening(interaction):
            return
        view = self

        class Own(discord.ui.Modal, title="Your premise"):
            place = discord.ui.TextInput(label="A fantastic place", max_length=100, default=view.place or None)
            activity = discord.ui.TextInput(label="A grabby activity", max_length=100,
                                            default=view.activity or None)

            async def on_submit(self, submitted: "discord.Interaction") -> None:
                view.stop()
                await submitted.client.on_premise(submitted, str(self.place), str(self.activity))

        await interaction.response.send_modal(Own())

    async def _more(self, interaction: "discord.Interaction") -> None:
        if not await interaction.client.may_run_opening(interaction):
            return
        self.stop()
        await interaction.response.edit_message(view=None)
        await interaction.client.offer_premise(interaction.channel_id, again=True)


class OpeningButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:begin:(?P<step>introductions|questions|bonds|playing|mybonds|carryon|finale|campaign|oneshot"
             r"|scene|break|epilogue|continue)",
):
    """The next step of /begin, for whoever ran it -- or, "mybonds", anyone's own Bonds form.
    Durable: the stage is in state, so a click after a restart still finds it."""

    def __init__(self, step: str) -> None:
        from ..opening import NEXT_LABEL

        mine = step == "mybonds"
        label = "Write my bonds" if mine else NEXT_LABEL[step] if step in (
            "carryon", "finale", "campaign", "oneshot", "continue", "scene") else f"Next: {NEXT_LABEL[step]}"
        super().__init__(discord.ui.Button(
            label=label, emoji="🤝" if mine else "📖" if step == "finale" else "▶️",
            style=discord.ButtonStyle.secondary if mine else discord.ButtonStyle.primary,
            custom_id=f"gary:begin:{step}",
        ))
        self.step = step

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "OpeningButton":
        return cls(match["step"])

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_opening_step(interaction, self.step)


@dataclass(frozen=True)
class OpeningPrompt:
    """The buttons under a /begin step: the next step, and for bonds, everyone's form."""

    steps: Tuple[str, ...]


class ReactButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"gary:react:(?P<hit_id>[0-9a-z]+)",
):
    """Reduce damage, on a hit someone at the table has a move to answer. Anyone may press
    it; each sees only what they can do (a Cleric's deity can spare an ally)."""

    def __init__(self, hit_id: str) -> None:
        super().__init__(discord.ui.Button(
            label="Reduce damage", style=discord.ButtonStyle.secondary, emoji="🛡️",
            custom_id=f"gary:react:{hit_id}",
        ))
        self.hit_id = hit_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "ReactButton":
        return cls(match["hit_id"])

    async def callback(self, interaction: "discord.Interaction") -> None:
        await interaction.client.on_react(interaction, self.hit_id)


def spells_view(user_id: int) -> "discord.ui.View":
    view = discord.ui.View(timeout=None)
    view.add_item(SpellsButton(user_id))
    return view


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
        elif isinstance(prompt, SpellsPrompt):
            await channel.send(text, view=spells_view(prompt.user_id))
        elif isinstance(prompt, OverloadPrompt):
            await channel.send(text, view=overload_view(prompt))
        elif isinstance(prompt, PremisePrompt):
            await channel.send(text, view=PremiseView(prompt))
        elif isinstance(prompt, OpeningPrompt):
            view = discord.ui.View(timeout=None)
            for step in prompt.steps:
                view.add_item(OpeningButton(step))
            await channel.send(text, view=view)
        elif isinstance(prompt, ReactPrompt):
            view = discord.ui.View(timeout=None)
            view.add_item(ReactButton(prompt.hit_id))
            await channel.send(text, view=view)
        elif isinstance(prompt, CurePrompt):
            view = discord.ui.View(timeout=None)
            for debility in prompt.debilities:
                view.add_item(CureButton(prompt.user_id, debility))
            await channel.send(text, view=view)
        else:
            await channel.send(text)


#: Internal commands that open one of the sheet's buttons.
_SHEET_ACTIONS = ("spells", "companion", "bonds", "moves", "alignment", "choices", "gear", "levelup")

#: What a typed `?dm` command gets now: the commands are slash commands.
ROLE_MENTION_HINT = (
    "I couldn't read that one — Discord sent it to me blank. The **@Gary** you picked was my *role*: the picker "
    "shows two. Pick the one with my picture, not the one that says \"Notify users with this role\" — or reply "
    "to one of my messages, or use `/do`."
)
SLASH_ONLY = "Gary uses slash commands now: type `/` and pick one (`/help` lists them)."


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
        # PRIVILEGED, so off unless asked for: without it Discord still sends the text of messages
        # that mention Gary or reply to him, and /do needs no intent at all.
        intents.message_content = settings.message_content
        intents.guilds = True
        super().__init__(intents=intents)

        self.settings = settings
        self.director = director
        self.store = store
        self.clock = clock
        self.lock = lock
        from .pictures import Painter

        self.painter = Painter(getattr(settings, "gemini_api_key", ""), getattr(settings, "gemini_image_model", ""),
                               Path(getattr(settings, "campaign_root", ".")) / "pictures",
                               int(getattr(settings, "images_per_day", 0)),
                               describe_model=getattr(settings, "gemini_summary_model", ""))
        self.history_backend = history_backend
        self._reset_token: Optional[str] = None
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
        self.add_dynamic_items(RollButton, PickButton, ChooseSelect, DamageButton, SpellsButton, CureButton, ReactButton, OpeningButton,
                               DropButton, FailButton)
        self._sweeper = asyncio.create_task(self._sweep_loop())
        from .slash import register

        self.tree = register(self)
        self._slash_synced = False

    async def on_ready(self) -> None:
        log.info("connected as %s (%s)", self.user, getattr(self.user, "id", "?"))
        if not getattr(self, "_slash_synced", True):
            from .slash import sync

            self._slash_synced = True
            await sync(self)
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
                "GARY_PLAYERS is not set. Until players run /playing, the model is told "
                "only Discord display names, and a check goes to whoever spoke."
            )

        if not self.intents.message_content:
            log.info("Message Content intent off (not privileged): players use @Gary, a reply to Gary, or /do.")
            if self.settings.freeform_channels:
                log.warning(
                    "GARY_FREEFORM_CHANNELS is set, but without the Message Content intent Discord sends "
                    "unaddressed messages with no text: only @Gary, replies and /do reach Gary there. Set "
                    "GARY_MESSAGE_CONTENT=1 and enable the intent in the Developer Portal for freeform play."
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

    #: The setting rides on every turn, so it is capped. Detail belongs in
    #: notes, which surface only when their subject comes up.
    MAX_SETTING_CHARS = 600

    @property
    def _has_model(self) -> bool:
        """Whether there is a model behind the runner at all.

        ``EffectRunner`` keeps its narrator private, and an earlier version of the import
        guard read ``runner.narrator`` -- always None, so the guard never fired and an
        import under ``EchoNarrator`` reported success while filing nothing. Read the
        real name, in one place, so the next caller cannot repeat it.
        """
        return not isinstance(getattr(self.runner, "_narrator", None), EchoNarrator)

    async def _session_questions(self, message, channel_id: int, raw: str) -> None:
        """The adventure's own end-of-session questions (Advanced Delving: "When doing this
        it's key that you show the players the new End of Session move") -- posted for all."""
        from .session import QUESTIONS

        current = self.store.state().get("session_questions") or []
        if not raw:
            shown = current or list(QUESTIONS)
            source = "this adventure's" if current else "the book's"
            await self.sender.send(channel_id, f"End of session asks {source} questions:\n"
                                   + "\n".join(f"• {q}" for q in shown)
                                   + "\nSet your own with `/questions first: second: third:`, or "
                                     "`/questions default:True` to restore the book's.")
            return
        questions = [] if raw.lower() == "default" else [" ".join(q.split()) for q in raw.split("|") if q.strip()]
        if not 0 <= len(questions) <= 3 or any(len(q) > 200 for q in questions):
            await self.sender.send(channel_id, "One to three questions, separated by |, each under 200 characters.")
            return
        diff = self.store.apply([Mutation(MutationKind.SESSION_QUESTIONS, "", {"questions": questions})],
                                reason=f"{message.author.display_name} sets the end-of-session questions")
        if not diff.ok:
            await self.sender.send(channel_id, "That could not be saved: " + "; ".join(r for _, r in diff.rejected))
            return
        shown = questions or list(QUESTIONS)
        await self.sender.send(channel_id, ("📜 **End of session** now asks, for this adventure:\n" if questions
                                            else "📜 **End of session** asks the book's questions again:\n")
                               + "\n".join(f"• {q}" for q in shown) + "\nEach yes is 1 XP for everyone.")

    def _keep_a_copy(self) -> Optional[Path]:
        """Before /reset deletes a campaign, copy it into ``kept/<date-time>`` inside the campaign folder
        -- inside, because in the container that folder is the only one that outlives a restart -- so a
        finished campaign is not simply gone. The file backend only; None when nothing was copied."""
        import shutil
        from datetime import datetime

        root = Path(getattr(self.settings, "campaign_root", "") or "")
        if getattr(self.settings, "backend", "file") != "file" or not root.is_dir() or not any(root.iterdir()):
            return None
        stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"
        target, n = root / "kept" / stamp, 1
        while target.exists():  # two resets in one second
            n += 1
            target = root / "kept" / f"{stamp}-{n}"
        try:
            shutil.copytree(root, target, ignore=shutil.ignore_patterns("*.lock", "kept"))
        except OSError:
            log.exception("could not keep a copy of the campaign before reset")
            return None
        return target

    def _remember_portrait(self, kind: str, name: str, described: str) -> None:
        """What a new portrait shows goes on the record, so the GM narrates the person as drawn and
        the next picture of them starts from it."""
        if not described:
            return
        state = self.store.state()
        if kind == "character" and name in (state.get("sheets") or {}):
            self.store.apply([Mutation(MutationKind.SHEET, name, {"sheet": {**state["sheets"][name],
                                                                            "portrait": described}})],
                             reason=f"portrait of {name}")
        elif kind == "npc":
            key = next((k for k, n in (state.get("npcs") or {}).items() if str(n.get("name")) == name), None)
            if key is not None:
                self.store.apply([Mutation(MutationKind.NPC_RELATION, key, {"portrait": described})],
                                 reason=f"portrait of {name}")

    def map_text(self, name: Optional[str] = None) -> str:
        """/map: the dungeon they're in (or one named), or the campaign map, as the players know it."""
        from .. import dungeons, worldmap

        state = self.store.state()
        known = state.get("dungeons") or {}
        wants_world = str(name or "").strip().lower() in ("world", "the world", "campaign")
        record = None if wants_world else (known.get(dungeons.slug(name)) if name else dungeons.current(state))
        if record is None:
            if name and not wants_world:
                listed = ", ".join(d["name"] for d in known.values())
                return f"No map of {name!r}." + (f" Maps: World, {listed}." if listed else "")
            world = worldmap.render(state.get("world"), for_players=True)
            if not world:
                return "No campaign map yet: it's drawn as you travel and hear of places."
            text = "🗺️ **The campaign map**\n" + world + (
                f"\nDungeon maps: {', '.join(d['name'] for d in known.values())} (`/map dungeon:`)." if known else "")
            return text if len(text) <= 1990 else text[:1980].rsplit("\n", 1)[0] + "\n…"
        text = "🗺️ " + dungeons.render(record, for_players=True)
        blanks = dungeons.blanks(record)
        text += f"\n{blanks} way{'s' if blanks != 1 else ''} not yet taken." if blanks else ""
        return text if len(text) <= 1990 else text[:1980].rsplit("\n", 1)[0] + "\n…"

    def own_sheet(self, user_id: int) -> Optional[dict]:
        actor = self.director.players.get(user_id)
        return (self.store.state().get("sheets") or {}).get(actor) if actor else None

    async def show_moves(self, interaction: "discord.Interaction", move: Optional[str] = None) -> None:
        """/moves, and the Moves button on /sheet: privately, in as many messages as it takes."""
        from .. import movelist

        sheet = self.own_sheet(interaction.user.id)
        if move:
            found = movelist.find(sheet or {}, move)
            text = (f"**{found[0]}** — {' '.join(found[1].split())}"[:1990] if found
                    else f"No move called {move!r}. `/moves` lists them.")
            await interaction.response.send_message(text, ephemeral=True)
            return
        if sheet is None:
            pages = ["You have no character yet: `/new` makes one. Everyone's moves:\n"
                     + "\n".join(f"• **{n}** — {t}" for n, t in movelist.basic())[:1800]]
        else:
            pages = movelist.pages(sheet)
        await interaction.response.send_message(pages[0], ephemeral=True)
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True)

    def _state_text(self) -> str:
        """`/state`: what the model is told every turn -- fronts and all, so it is the server
        managers' alone, and private. Notes and NPCs surface only when their subject comes up
        (§8), so they are counted outside the block rather than left looking missing."""
        from ..context import render_live_state

        state = self.store.state()
        body = "```\n" + render_live_state(state) + "\n```"
        notes, npcs = len(state.get("notes") or []), len(state.get("npcs") or {})
        if notes or npcs:
            body += (f"\nAlso stored: **{notes} note(s)**, **{npcs} NPC(s)** — retrieved when their subject "
                     "is mentioned, so not in the block above. `/notes` lists them.")
        body += "\n" + self._stats_line()
        return body if len(body) <= 1990 else body[:1900].rsplit("\n", 1)[0] + "\n…```"

    def _fronts_text(self) -> str:
        """Every front, for the DM's eyes only -- or that there are none yet."""
        from .. import fronts

        known = self.store.state().get("fronts") or {}
        if not known:
            return ("No fronts yet. In a campaign's first session the GM only notes threats; the fronts "
                    "are written after the first `/end`.")
        text = "\n\n".join(fronts.describe(f) for f in known.values())
        if len(text) > 1900:
            text = text[:1900].rsplit("\n", 1)[0] + "\n…"
        return text

    def _stats_line(self) -> str:
        """Since Gary started: messages sent, turns narrated, model failures, moves journalled."""
        s = self.runner.stats
        return (f"Since start: {s.sent} sent · {s.narrated} narrated · {s.narrator_failures} model "
                f"failure{'s' if s.narrator_failures != 1 else ''} · {s.outcomes_recorded} moves journalled")

    def _cancel_mine(self, channel_id: int, user_id: int) -> str:
        """Drop this player's own pending move."""
        mine = [c for c in self.director.registry.open_checks(channel_id) if c.user_id == user_id]
        if not mine:
            return "You have no pending check."
        self.director.registry.cancel(mine[0].check_id, CancelReason.MANUAL)
        return f"Cancelled {mine[0].actor}'s {MOVES[mine[0].move].name}."

    # -- /begin: starting the adventure (opening.py) -------------------------------------

    def _opening(self) -> dict:
        return dict(self.store.state().get("opening") or {})

    def _set_opening(self, who: str, **fields) -> None:
        self.store.apply([Mutation(MutationKind.OPENING, "", fields)], reason=f"opening: {who}")

    def _party_sheets(self) -> List[dict]:
        sheets = self.store.state().get("sheets") or {}
        return [sheets[a] for a in sorted(set(self.director.players.values()))
                if a in sheets and not sheets[a].get("dead")]

    async def may_run_opening(self, interaction: "discord.Interaction") -> bool:
        """Only whoever ran /begin moves it on; anyone else is told so, privately."""
        starter = self._opening().get("starter")
        if starter and interaction.user.id != starter:
            await interaction.response.send_message(
                f"<@{starter}> is running the opening; they move it on.", ephemeral=True)
            return False
        return True

    async def _begin(self, message, channel_id: int) -> None:
        """`/begin`: start the adventure -- or show where the opening has got to."""
        from .. import opening

        current = self._opening()
        stage = current.get("stage")
        if stage in (None, "", "premise"):
            if not self._has_model:
                await self.sender.send(channel_id, "/begin needs the model: set GEMINI_API_KEY.")
                return
            if stage != "premise":
                self._set_opening(message.author.display_name, stage="premise", starter=message.author.id, session=1)
            if not current.get("mode"):
                await self.sender.send(
                    channel_id, "**Let's begin.** A campaign, played over many sessions, or a one-shot: one session of "
                                "about four hours, three scenes and an ending?",
                    prompt=OpeningPrompt(("campaign", "oneshot")))
                return
            await self.offer_premise(channel_id)
            return
        if stage in ("playing", "session", "epilogue"):
            number = int(self._opening().get("session") or 1)
            await self.sender.send(channel_id, f"Session {number} is under way. `/end` finishes it.")
            return
        if stage == "finished" and current.get("mode") == "oneshot" and opening.wiped(opening.table(self.store.state())):
            await self.sender.send(channel_id, "This one-shot is finished, and nobody survived it. `/reset` starts "
                                               "something new; Gary keeps a copy of this one first.")
            return
        if stage == "finished" and current.get("mode") == "oneshot":
            await self.sender.send(channel_id, "This one-shot is finished. Keep going with these characters as a "
                                               "campaign, or `/reset` to start something new.",
                                   prompt=OpeningPrompt(("continue",)))
            return
        if stage == "finished":
            await self.sender.send(channel_id, "This campaign is finished. `/reset` starts a new one; Gary keeps a "
                                               "copy of this one first.")
            return
        if stage == "done" and self._opening().get("campaign_over"):
            await self.sender.send(
                channel_id, "📜 The campaign front is over: the threat behind everything has been met. Carry on "
                            "in this world (the GM writes a new campaign front after the next session), or close "
                            "the campaign with an epilogue? Anyone at the table may choose.",
                prompt=OpeningPrompt(("carryon", "finale")))
            return
        if stage == "done":
            await self._next_session(message.author.display_name, channel_id)
            return
        nxt = opening.STAGES[opening.STAGES.index(stage) + 1]
        if stage == "characters" and current.get("mode") == "oneshot":
            nxt = "questions"  # a one-shot's characters already travel together
        await self.sender.send(channel_id, f"The opening is at **{stage}**. When the table is ready:",
                               prompt=OpeningPrompt(("mybonds", nxt) if stage == "bonds" else (nxt,)))

    async def _next_session(self, who: str, channel_id: int) -> None:
        """Every session after the first: a recap, anyone ready to level up, the Outsider's
        question, and the scene reframed."""
        from .. import opening

        number = int(self._opening().get("session") or 1) + 1
        self._set_opening(who, stage="session", session=number)
        party = self._party_sheets()
        ready = [s["name"] for s in party if int(s.get("xp", 0)) >= xp_needed(s) and int(s.get("level", 1)) < 10]
        await self.sender.send(channel_id, f"🎲 **Session {number}.**"
                               + (f" {', '.join(ready)}: you can level up from `/sheet` before we start."
                                  if ready else ""))
        await self.runner.run([PromptModel(channel_id, "rules_event",
                                           opening.with_doom(opening.session_event(number, party), party))])

    async def _oneshot_step(self, interaction: "discord.Interaction", step: str) -> None:
        """The mode at the start, and a one-shot's pacing: the next scene, the level-up break before
        the finale, the epilogue -- and, when it is over, carrying on as a campaign."""
        from .. import opening

        channel_id, who = interaction.channel_id, interaction.user.display_name
        current = self._opening()
        stage, scene = current.get("stage"), int(current.get("scene") or 0)
        allowed = {
            "campaign": stage == "premise" and not current.get("mode"),
            "oneshot": stage == "premise" and not current.get("mode"),
            "scene": stage == "playing" and current.get("mode") == "oneshot"
                     and (scene == 1 or (scene == 2 and current.get("break_done"))),
            "break": stage == "playing" and scene == 2 and not current.get("break_done"),
            "epilogue": stage == "playing" and scene == opening.ONESHOT_SCENES,
            "continue": stage == "finished" and current.get("mode") == "oneshot",
        }[step]
        if not allowed:
            await interaction.response.send_message("That step isn't open now: `/begin` shows where things are.",
                                                    ephemeral=True)
            return
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            await interaction.response.defer()
        if step in ("campaign", "oneshot"):
            self._set_opening(who, mode=step)
            await self.offer_premise(channel_id)
        elif step == "scene":
            self._set_opening(who, scene=scene + 1)
            await self.runner.run([PromptModel(channel_id, "rules_event",
                                               opening.with_doom(opening.scene_event(scene + 1), self._party_sheets()))])
            if scene + 1 == 2:
                await self.sender.send(channel_id, "When this scene is done, take a break and level up:",
                                       prompt=OpeningPrompt(("break",)))
            else:
                await self.sender.send(channel_id, "When the dust settles:", prompt=OpeningPrompt(("epilogue",)))
        elif step == "break":
            # "XP is irrelevant": everyone gains a level at the break, whatever they have.
            changes = []
            for s in self._party_sheets():
                if int(s.get("level", 1)) < 10 and int(s.get("xp", 0)) < xp_needed(s):
                    changes.append(Mutation(MutationKind.SHEET, s["name"], {"sheet": {**s, "xp": xp_needed(s)}}))
            if changes:
                self.store.apply(changes, reason="one-shot level-up break")
            self._set_opening(who, break_done=True)
            await self.sender.send(channel_id, "☕ **Break.** Everyone gains a level: press **Level up** on `/sheet` "
                                               "now. When you're back:", prompt=OpeningPrompt(("scene",)))
        elif step == "epilogue":
            self._set_opening(who, stage="epilogue")
            await self.sender.send(channel_id, "📖 **The epilogue.** When everyone has told their ending, `/end` "
                                               "closes the game.")
            await self.runner.run([PromptModel(channel_id, "rules_event", opening.with_doom(
                opening.epilogue_event(self._party_sheets(), oneshot=True), self._party_sheets()))])
        else:  # continue as a campaign: the next session is the campaign's first; fronts come after it
            number = int(current.get("session") or 1) + 1
            self._set_opening(who, mode="campaign", stage="playing", session=number)
            await self.sender.send(channel_id, f"🎲 **Session {number}: the story goes on as a campaign.**")
            await self.runner.run([PromptModel(channel_id, "rules_event",
                                               opening.session_event(number, self._party_sheets()))])

    async def _campaign_choice(self, interaction: "discord.Interaction", step: str) -> None:
        """The campaign front is over: carry on in this world, or close with an epilogue."""
        from .. import opening

        current = self._opening()
        if current.get("stage") != "done" or not current.get("campaign_over"):
            await interaction.response.send_message("That choice has been made.", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            await interaction.response.defer()
        who = interaction.user.display_name
        if step == "carryon":
            self._set_opening(who, campaign_over=False, new_campaign_front=True)
            await self.sender.send(interaction.channel_id, f"▶️ {who}: the story goes on.")
            await self._next_session(who, interaction.channel_id)
            return
        number = int(current.get("session") or 1) + 1
        self._set_opening(who, stage="epilogue", session=number)
        await self.sender.send(interaction.channel_id,
                               f"📖 **Session {number}: the epilogue.** When everyone has told their character's "
                               "ending, `/end` closes the campaign.")
        await self.runner.run([PromptModel(interaction.channel_id, "rules_event",
                                           opening.epilogue_event(self._party_sheets()))])

    async def offer_premise(self, channel_id: int, again: bool = False) -> None:
        from .. import opening

        offered = list(self._opening().get("offered") or [])
        text = await self.runner.ask(PromptModel(channel_id=channel_id, kind="propose_premise",
                                                 payload={"already_offered": offered}))
        places, activities = opening.parse_offer(text)
        if not places or not activities:
            await self.sender.send(channel_id, "I couldn't come up with a premise just now. `/begin` to try again.")
            return
        self._set_opening("offered premises",
                          offered=(offered + [f"{p} | {a}" for p, a in zip(places, activities)])[-8:])
        await self.sender.send(
            channel_id,
            ("Some others. " if again else
             "A one-shot starts from a premise: a grabby thing to do, and the fantastic place where it ends -- the "
             "final destination. " if self._opening().get("mode") == "oneshot" else
             "Every adventure starts from a premise: a fantastic place, and something grabby to do there. ")
            + "Choose one, write your own, or ask for other options.",
            prompt=PremisePrompt(tuple(places), tuple(activities)))

    async def on_premise(self, interaction: "discord.Interaction", place: str, activity: str) -> None:
        from .. import opening

        oneshot = self._opening().get("mode") == "oneshot"
        text = opening.premise(place, activity, oneshot)[:self.MAX_SETTING_CHARS]
        who = interaction.user.display_name
        self.store.apply([Mutation(MutationKind.SETTING, "", {"text": text})], reason=f"premise by {who}")
        self._set_opening(who, stage="characters", premise=text)
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            await interaction.response.defer()
        await self.sender.send(
            interaction.channel_id,
            f"🗺️ **{activity.strip()}** — at **{place.strip()}**.\n"
            "Everyone, make your character with `/new` (or `/playing` for one already made). When the whole "
            "table is in, " + interaction.user.mention
            + (" moves on to questions: your characters already know each other and travel together."
               if oneshot else " moves on to introductions."),
            prompt=OpeningPrompt(("questions",) if oneshot else ("introductions",)))

    async def on_opening_step(self, interaction: "discord.Interaction", step: str) -> None:
        from .. import opening

        channel_id = interaction.channel_id
        if step == "mybonds":
            actor = self.director.players.get(interaction.user.id)
            sheet = (self.store.state().get("sheets") or {}).get(actor) if actor else None
            if sheet is None:
                await interaction.response.send_message("You have no character yet: `/new` makes one.",
                                                        ephemeral=True)
                return
            action = self._sheet_actions(channel_id, interaction.user.id, actor, sheet,
                                         self.settings.command_prefix)["bonds"]
            if isinstance(action, str):
                await interaction.response.send_message(action, ephemeral=True)
                return
            await action[2](interaction)
            return
        if step in ("carryon", "finale"):
            await self._campaign_choice(interaction, step)
            return
        if not await self.may_run_opening(interaction):
            return
        if step in ("campaign", "oneshot", "scene", "break", "epilogue", "continue"):
            await self._oneshot_step(interaction, step)
            return
        stage = self._opening().get("stage")
        oneshot = self._opening().get("mode") == "oneshot"
        expected = {"introductions": "characters", "questions": "characters" if oneshot else "introductions",
                    "bonds": "questions", "playing": "bonds"}[step]
        if stage != expected:
            await interaction.response.send_message(f"The opening is at {stage or 'nothing yet'}, not that step.",
                                                    ephemeral=True)
            return
        party = self._party_sheets()
        if step == "introductions" and not party:
            await interaction.response.send_message("Nobody has a character yet: everyone runs `/new` first.",
                                                    ephemeral=True)
            return
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            await interaction.response.defer()
        who = interaction.user.display_name
        self._set_opening(who, stage=step)
        premise = self._opening().get("premise") or self.store.state().get("setting") or ""
        if step == "introductions":
            await self.sender.send(channel_id, opening.introductions(party), prompt=OpeningPrompt(("questions",)))
        elif step == "questions":
            await self.runner.run([PromptModel(channel_id, "rules_event",
                                               opening.questions_event(premise, party, oneshot))])
            await self.sender.send(channel_id, "Answer by talking to Gary (@-mention him). When the premise is clear:",
                                   prompt=OpeningPrompt(("bonds",)))
        elif step == "bonds":
            await self.sender.send(channel_id, opening.bonds_text(), prompt=OpeningPrompt(("mybonds", "playing")))
        elif oneshot:
            self._set_opening(who, scene=1)
            await self.runner.run([PromptModel(channel_id, "rules_event",
                                               opening.opening_event(premise, party, oneshot=True))])
            await self.sender.send(channel_id, "When this scene is done:", prompt=OpeningPrompt(("scene",)))
        else:
            await self.runner.run([PromptModel(channel_id, "rules_event", opening.opening_event(premise, party))])

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
                # The commands are slash commands now; a typed one gets pointed there rather
                # than read out to the model as something said at the table.
                await self.sender.send(message.channel.id, SLASH_ONLY)
            elif route is Route.NOT_A_GAME_CHANNEL and not message.author.bot:
                # §4.2 calls an unregistered channel "silence, not an error", which is
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
        current = self.store.state().get("opening") or {}
        if current.get("mode") == "oneshot" and current.get("stage") in ("playing", "epilogue", "finished"):
            # A one-shot's cast is made before the first scene; a character who falls stays fallen.
            return "This is a one-shot, already under way: there are no new characters once play has begun."
        sheets_now = self.store.state().get("sheets") or {}
        fallen = [a for uid, a in self.director.players.items()
                  if uid == user_id and (sheets_now.get(a) or {}).get("dead")]
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
        await tag_nickname(interaction.user, name)
        if fallen and current.get("stage") in ("playing", "session", "done"):
            from .. import opening

            await self.runner.run([PromptModel(channel_id, "rules_event", opening.newcomer_event(name, fallen))])
        if spells.needs_setup(sheet):
            await self._offer_spells(channel_id, user_id, name)
        if sheet.get("class") == "Ranger":
            await self.sender.send(channel_id, f"**{name}**, your animal companion awaits: "
                                               "`/sheet` has the button.")
        return None

    async def _offer_spells(self, channel_id: int, user_id: int, actor: str) -> None:
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        await channel.send(f"**{actor}**, choose your spells:", view=spells_view(user_id))

    async def on_overload(self, interaction: "discord.Interaction", check_id: str, index: Optional[int]) -> None:
        """Overloaded: drop an item and roll, or fail. Answered like a roll."""
        channel_id = interaction.channel_id
        if index is None:
            effects = self.director.fail_overloaded(channel_id=channel_id, user_id=interaction.user.id,
                                                    check_id=check_id)
        else:
            effects = self.director.drop_and_roll(channel_id=channel_id, user_id=interaction.user.id,
                                                  check_id=check_id, index=index)
        if not effects:
            await interaction.response.send_message(self._why_no_roll(channel_id, check_id), ephemeral=True)
            return
        try:
            await interaction.response.edit_message(view=None)
        except discord.HTTPException:
            log.warning("could not clear the overload choice for %s", check_id)
        await self.runner.run(effects)

    async def on_cure(self, interaction: "discord.Interaction", owner_id: int, debility: str) -> None:
        """A debility picked for Recover to clear. Only by its owner, only while one is owed."""
        if interaction.user.id != owner_id:
            await interaction.response.send_message("That choice is someone else's.", ephemeral=True)
            return
        effects = self.director.cure(channel_id=interaction.channel_id, user_id=owner_id, debility=debility)
        if not effects:
            await interaction.response.send_message("Nothing left to shed.", ephemeral=True)
            return
        after = next(e.changes for e in effects if hasattr(e, "changes"))
        view = None
        if after.get("cures_owed"):
            view = discord.ui.View(timeout=None)
            for left in after["debilities"]:
                view.add_item(CureButton(owner_id, left))
        try:
            await interaction.response.edit_message(view=view)
        except discord.HTTPException:
            log.warning("could not update the debility buttons")
        await self.runner.run(effects)

    async def on_react(self, interaction: "discord.Interaction", hit_id: str) -> None:
        """Reduce damage was pressed: show this player, privately, what they can do about
        the blow; their pick is applied at once."""
        found, why = self.director.reactions(user_id=interaction.user.id, hit_id=hit_id)
        if not found:
            await interaction.response.send_message(why, ephemeral=True)
            return
        view = discord.ui.View(timeout=300)
        for reaction in found[:25]:
            button = discord.ui.Button(label=reaction.label[:MAX_LABEL], style=discord.ButtonStyle.primary)

            async def picked(inner: "discord.Interaction", key: str = reaction.key) -> None:
                effects = self.director.react(channel_id=inner.channel_id, user_id=inner.user.id,
                                              hit_id=hit_id, key=key)
                await inner.response.edit_message(
                    content="Done." if effects else "That is no longer on offer.", view=None)
                await self.runner.run(effects)

            button.callback = picked
            view.add_item(button)
        await interaction.response.send_message("What do you do about the blow?", view=view, ephemeral=True)

    async def on_spells(self, interaction: "discord.Interaction", owner_id: int) -> None:
        """Choose spells was clicked. Only its owner, and only when there is something to
        choose: after Commune or Prepare Spells, or a caster's first time."""
        if interaction.user.id != owner_id:
            await interaction.response.send_message("Those spells are someone else's to choose.", ephemeral=True)
            return
        actor = self.director.players.get(owner_id)
        sheet = (self.store.state().get("sheets") or {}).get(actor) if actor else None
        if sheet is None or spells.caster_class(sheet) is None:
            await interaction.response.send_message("You have no spellcaster to choose for.", ephemeral=True)
            return
        if not (sheet.get("preparing") or spells.needs_setup(sheet)):
            cls = spells.caster_class(sheet)
            await interaction.response.send_message(
                f"Your spells are chosen. New ones come with {spells.PREPARE_NAMES[cls]} — an "
                "undisturbed hour, in the story.", ephemeral=True,
            )
            return
        channel_id = interaction.channel_id
        picker = SpellPicker(sheet, lambda i, changes: self._finish_spells(actor, changes, channel_id))
        await interaction.response.send_message(picker.prompt(), view=picker, ephemeral=True)

    async def _end_session(
        self, yeses: int, fulfilled: frozenset = frozenset(), resolved: Optional[dict] = None
    ) -> Optional[str]:
        """Everyone at the table marks XP for each "yes", those who acted on their alignment
        one more, and those who resolved a bond one more, the bond cleared to be rewritten.
        One apply for the party."""
        resolved = resolved or {}
        sheets = self.store.state().get("sheets") or {}
        party = [sheets[a] for a in sorted(set(self.director.players.values()))
                 if a in sheets and not sheets[a].get("dead")]
        if not party:
            if any(a in sheets for a in self.director.players.values()):
                return "Nobody at the table is left alive to mark XP."
            return "Nobody at the table has a character sheet."
        from .session import BEST

        gains = {s["name"]: yeses + (s["name"] in fulfilled) + (s["name"] in resolved)
                 + (f"{s['name']}{BEST}" in fulfilled) for s in party}
        if not any(gains.values()):
            return "No XP this session."
        updated = []
        for s in party:
            if not gains[s["name"]]:
                continue
            if s["name"] in resolved:
                s = bonds.resolve(s, resolved[s["name"]])
            updated.append({**s, "xp": int(s.get("xp", 0)) + gains[s["name"]]})
        diff = self.store.apply(
            [Mutation(MutationKind.SHEET, s["name"], {"sheet": s}) for s in updated],
            reason=f"end of session: +{yeses} XP each, alignment for {sorted(fulfilled) or 'nobody'}",
        )
        if not diff.ok:
            return None
        line = "⭐ End of session — " + "; ".join(
            f"**{s['name']}** +{gains[s['name']]} ({xp_line(s)})" for s in updated) + "."
        if resolved:
            line += (f" {', '.join(sorted(resolved))}: write a new bond from `/sheet` "
                     "(the Bonds button).")
        if any(int(s["xp"]) >= xp_needed(s) for s in updated):
            line += LEVEL_UP_HINT
        return line

    async def _end_session_then_fronts(self, channel_id: int, yeses: int, fulfilled, resolved) -> Optional[str]:
        """End of session -- and after the first one of an adventure started with /begin, the
        fronts, written from what play turned up (the book's advice for the first session)."""
        line = await self._end_session(yeses, fulfilled, resolved)
        if not line:
            return line
        from .. import opening

        threats = [f"{n['subject']}: {n['body']}" for n in self.store.state().get("notes") or ()
                   if n.get("kind") == "threat"][-20:]
        current = self._opening()
        stage = current.get("stage")
        if stage == "playing" and current.get("mode") == "oneshot":
            stage = "epilogue"  # ended early: no epilogue round, but still closed with its chronicle
        if stage == "epilogue":
            # The last session: no more front work, just the record of what was.
            self._set_opening("campaign closed", stage="finished")
            if current.get("mode") != "oneshot":
                closing = "The campaign is closed. `/reset` starts a new one; Gary keeps a copy of this one first."
            elif opening.wiped(opening.table(self.store.state())):
                closing = "The one-shot is over. `/reset` starts something new, keeping a copy of this one first."
            else:
                closing = ("The one-shot is over. `/begin` can carry these characters on as a campaign; `/reset` "
                           "starts something new, keeping a copy of this one first.")
            asyncio.create_task(self.sender.send(channel_id, opening.chronicle(self.store.state()) + "\n\n" + closing))
            return line
        if stage == "playing":
            self._set_opening("first session over", stage="done")
            event = opening.fronts_event(threats, self.store.state().get("world"))
        else:
            if stage == "session":
                self._set_opening("session over", stage="done")
            new_front = bool(current.get("new_campaign_front"))
            if new_front:
                self._set_opening("asking for a new campaign front", new_campaign_front=False)
            changed, checked = self._update_steadings()
            event = opening.review_event(threats, new_front, changed, checked, self.store.state().get("world"))
        asyncio.create_task(self._front_work(channel_id, event))
        return line

    def _update_steadings(self) -> Tuple[List[str], List[str]]:
        """The book's between-session steading conditions (steadings.between_sessions): apply what the
        tags decide, journalled, and return (what changed, what the GM must check)."""
        from .. import steadings, worldmap

        state = self.store.state()
        known = state.get("steadings") or {}
        if not known:
            return [], []
        world = state.get("world")
        changed, done, ask = steadings.between_sessions(known, lambda a, b: worldmap.distance(world, a, b))
        if changed:
            self.store.apply([Mutation(MutationKind.STEADING, k, {"steading": v}) for k, v in changed.items()],
                             reason="steadings between sessions")
        return done, ask

    async def _front_work(self, channel_id: int, event: dict) -> None:
        """Between sessions: "Fronts are built outside of active play." create_front is open only
        while this runs."""
        self._set_opening("front work", front_work=True)
        try:
            await self.runner.run([PromptModel(channel_id, "rules_event", event)])
        finally:
            self._set_opening("front work done", front_work=False)

    def _sheet_actions(self, channel_id: int, owner: int, actor: str, sheet: dict, prefix: str) -> dict:
        """What this character can open from their sheet: name -> (label, emoji, opener), or
        the reason it does not apply. Each form is built when its button is clicked, from the
        sheet as it is then."""
        sheets = self.store.state().get("sheets") or {}
        out: dict = {}

        async def show(interaction, form, prompt=None):
            await interaction.response.send_message(prompt or form.prompt(), view=form, ephemeral=True)

        if spells.caster_class(sheet) is None:
            out["spells"] = f"**{actor}** casts no spells."
        elif sheet.get("preparing") or spells.needs_setup(sheet):
            out["spells"] = ("Choose spells", "✨", lambda i: self.on_spells(i, owner))
        else:
            out["spells"] = f"**{actor}** — {spells.describe(sheet)}"
        if sheet.get("class") != "Ranger":
            out["companion"] = f"Only a Ranger has an animal companion; {actor} is a {sheet.get('class')}."
        elif sheet.get("companion"):
            out["companion"] = f"**{actor}** — {companions.describe(sheet)}."
        else:
            out["companion"] = ("Choose companion", "🐺", lambda i: show(i, CompanionForm(
                actor, lambda pet: self._finish_companion(actor, pet, channel_id))))
        others = sorted(a for a in set(self.director.players.values())
                        if a != actor and a in sheets and not sheets[a].get("dead")) + bonds.partners(sheet)
        out["bonds"] = ("Bonds", "🤝", lambda i: show(i, BondsForm(
            sheets.get(actor, sheet), others, lambda edited: self._finish_bonds(edited, channel_id)))) if others else (
            "Bonds are with your companions, and nobody else at the table has a character yet.")
        out["moves"] = ("Moves", "📖", lambda i: self.show_moves(i))
        out["alignment"] = ("Alignment", "⚖️", lambda i: show(i, AlignmentForm(
            (self.store.state().get("sheets") or {}).get(actor, sheet),
            lambda chosen: self._finish_alignment(actor, chosen, channel_id))))
        out["choices"] = ("Choices and look", "📜", lambda i: show(i, ChoicesForm(
            sheet, lambda made: self._finish_choices(actor, made, channel_id)))) if choices.missing(sheet) else (
            f"**{actor}** — {choices.describe(sheet) or 'nothing to choose'}.")
        out["gear"] = (f"**{actor}** already has their gear. `/sheet` shows it." if "gear" in sheet else
                       ("Choose gear", "🛡️", lambda i: show(i, CharacterCreator(
                           lambda _i, geared: self._finish_gear(geared, channel_id), sheet=sheet))))

        async def level_up(interaction):
            busy = self.director.unsettled(channel_id, "level up")
            if busy:
                await interaction.response.send_message(f"{busy} Levelling up takes downtime.", ephemeral=True)
                return
            current = (self.store.state().get("sheets") or {}).get(actor, sheet)
            try:
                from .. import beyond
                from .levelup import BeyondForm

                form = (BeyondForm(current, lambda after: self._finish_beyond(after, channel_id))
                        if beyond.ready(current) else
                        LevelUpForm(current, lambda levelled: self._finish_level_up(levelled, channel_id)))
            except SheetError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await show(interaction, form)

        from ..beyond import ready as beyond_ready

        if ready(sheet) or beyond_ready(sheet):
            out["levelup"] = ("Level up", "⭐", level_up)
        else:
            try:
                LevelUpForm(sheet, lambda _: None)
            except SheetError as exc:
                out["levelup"] = str(exc)
        return out

    async def _away(self, message: Any, channel_id: int, command: str, named: str) -> None:
        """`/away` and `/back`: a character whose player is not at the table is out of the story
        -- in no scene, addressed by nobody, rolling nothing -- until they are back."""
        sheets = self.store.state().get("sheets") or {}
        actor = (next((a for a in sheets if a.lower() == named.lower()), None) if named
                 else self.director.players.get(message.author.id))
        if actor is None or actor not in sheets:
            await self.sender.send(channel_id, f"No character {named!r}." if named
                                   else f"`/{command} character:` — whose? You haven't claimed one with `/playing`.")
            return
        away = command == "away"
        if bool(sheets[actor].get("away")) == away:
            await self.sender.send(channel_id, f"**{actor}** is already {'away' if away else 'here'}.")
            return
        changes = [Mutation(MutationKind.SHEET, actor, {"sheet": {**sheets[actor], "away": away}})]
        apart = dict((self.store.state().get("scene") or {}).get("apart") or {})
        if actor in apart:  # away is out of every scene, and back is back with the party
            apart.pop(actor)
            changes.append(Mutation(MutationKind.SCENE, "", {"apart": apart}))
        self.store.apply(changes, reason=f"{actor} is {'away' if away else 'back'}")
        await self.sender.send(channel_id, (
            f"**{actor}** is away. Gary leaves them out of the story until `/back`." if away
            else f"**{actor}** is back with the party."))

    async def _sheet_hub(self, message, channel_id: int, prefix: str, only: Optional[str] = None,
                         reply=None) -> None:
        """`/sheet`: the sheet, with a button for everything this character can open --
        spells, companion, bonds, alignment, class choices, gear, a level. `/spells` and
        the rest are the same buttons one at a time, kept for habit's sake."""
        actor = self.director.players.get(message.author.id)
        sheet = (self.store.state().get("sheets") or {}).get(actor) if actor else None
        if sheet is None:
            if reply is not None:
                await reply("You have no sheet yet. `/new` makes one.", None)
            else:
                await self.sender.send(channel_id, "You have no sheet yet. `/new` makes one.")
            return
        actions = self._sheet_actions(channel_id, message.author.id, actor, sheet, prefix)
        if only is not None:
            action = actions[only]
            busy = self.director.unsettled(channel_id, "level up") if only == "levelup" else ""
            if busy or isinstance(action, str):
                await self.sender.send(channel_id, f"{busy} Levelling up takes downtime." if busy else action)
                return
            buttons = {only: action}
            text = (f"**{actor}** — {spells.describe(sheet)}" if only == "spells" else
                    f"**{actor}** is ready for level {int(sheet.get('level', 1)) + 1}:" if only == "levelup" else
                    f"**{actor}**:")
        else:
            from ..magic import describe as describe_magic

            extra = [line for line in (alignment_line(sheet), choices.describe(sheet), describe_magic(sheet),
                                       carrying_line(sheet),
                                       spells.describe(sheet), companions.describe(sheet), bonds.describe(sheet),
                                       hirelings.describe(sheet)) if line]
            # Bonds and alignment always; the rest only while there is something to do.
            buttons = {k: a for k, a in actions.items() if not isinstance(a, str)}
            text = "\n".join([describe(sheet), *extra])
        owner = message.author.id
        view = discord.ui.View(timeout=FORM_TIMEOUT_SECONDS)
        for label, emoji, opener in buttons.values():
            button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary)

            async def clicked(interaction, opener=opener):
                if interaction.user.id != owner:
                    await interaction.response.send_message(f"This is {actor}'s sheet. `/sheet` for yours.",
                                                            ephemeral=True)
                    return
                await opener(interaction)

            button.callback = clicked
            view.add_item(button)
        await (reply or (lambda t, v: message.channel.send(t, view=v)))(text, view if buttons else None)

    async def _private_button(self, message, text: str, label: str, emoji: str, form, prompt: str) -> None:
        """Post a button only the asker can use, opening ``form`` privately. A typed command
        cannot be answered privately; a click can."""
        view = discord.ui.View(timeout=form.timeout)
        button = discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.primary)
        owner = message.author.id

        async def open_form(interaction: "discord.Interaction") -> None:
            if interaction.user.id != owner:
                await interaction.response.send_message("This one is someone else's.", ephemeral=True)
                return
            await interaction.response.send_message(prompt, view=form, ephemeral=True)

        button.callback = open_form
        view.add_item(button)
        await message.channel.send(text, view=view)

    async def _finish_choices(self, actor: str, made: dict, channel_id: int) -> Optional[str]:
        sheet = (self.store.state().get("sheets") or {}).get(actor)
        if sheet is None:
            return f"{actor} has no sheet any more."
        updated = choices.on_sheet(sheet, made)
        diff = self.store.apply([Mutation(MutationKind.SHEET, actor, {"sheet": updated})],
                                reason=f"{actor} makes their class's choices")
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        await self.sender.send(channel_id, f"📜 **{actor}** — {choices.describe(updated)}.")
        return None

    async def _finish_companion(self, actor: str, pet: dict, channel_id: int) -> Optional[str]:
        sheet = (self.store.state().get("sheets") or {}).get(actor)
        if sheet is None:
            return f"{actor} has no sheet any more."
        if sheet.get("companion"):
            return f"{actor} already has {sheet['companion']['name']}."
        diff = self.store.apply([Mutation(MutationKind.SHEET, actor, {"sheet": {**sheet, "companion": pet}})],
                                reason=f"{actor} is joined by {pet['name']}")
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        await self.sender.send(channel_id, f"🐺 **{actor}** — {companions.describe({**sheet, 'companion': pet})}.")
        return None

    async def _finish_alignment(self, actor: str, chosen: str, channel_id: int) -> Optional[str]:
        sheet = (self.store.state().get("sheets") or {}).get(actor)
        if sheet is None:
            return f"{actor} has no sheet any more."
        diff = self.store.apply([Mutation(MutationKind.SHEET, actor, {"sheet": {**sheet, "alignment": chosen}})],
                                reason=f"{actor} is {chosen}")
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        await self.sender.send(channel_id, f"⚖️ **{actor}** — {alignment_line({**sheet, 'alignment': chosen})}")
        return None

    async def _finish_bonds(self, edited: dict, channel_id: int) -> Optional[str]:
        """Store bonds. Only the bonds: anything else on the sheet may have changed since the
        form opened -- HP in a fight, say -- and must not be put back."""
        actor = edited["name"]
        sheet = (self.store.state().get("sheets") or {}).get(actor)
        if sheet is None:
            return f"{actor} has no sheet any more."
        diff = self.store.apply(
            [Mutation(MutationKind.SHEET, actor, {"sheet": {**sheet, "bonds": edited.get("bonds") or []}})],
            reason=f"{actor} writes their bonds",
        )
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        await self.sender.send(channel_id, f"🤝 **{actor}** — {bonds.describe(edited) or 'no bonds.'}")
        return None

    async def _finish_level_up(self, levelled: dict, channel_id: int) -> Optional[str]:
        """Store a level gained. Checked again: XP could have been spent from another
        window, or a fight started, since the form opened."""
        actor = levelled["name"]
        sheet = (self.store.state().get("sheets") or {}).get(actor)
        if sheet is None or not ready(sheet) or int(sheet.get("level", 1)) + 1 != levelled["level"]:
            return "That level was already taken."
        busy = self.director.unsettled(channel_id, "level up")
        if busy:
            return busy
        diff = self.store.apply([Mutation(MutationKind.SHEET, actor, {"sheet": levelled})],
                                reason=f"{actor} reaches level {levelled['level']}")
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        moves = levelled.get("advanced_moves") or []
        await self.sender.send(channel_id, f"⭐ **{actor}** reaches level {levelled['level']} — "
                                           f"{moves[-1] if moves else ''}. {describe(levelled, with_name=False)}")
        if moves and moves[-1] in spells.FAVOR_MOVES:
            await self.sender.send(channel_id, f"🙏 **{actor}** is a cleric of level 1 for spells now: "
                                               "`/sheet` has the buttons to name "
                                               "your deity and choose your spells.")
        return None

    async def _finish_beyond(self, after: dict, channel_id: int) -> Optional[str]:
        """Beyond 10th Level, stored. Checked again, as a level is."""
        from .. import beyond

        actor = after["name"]
        sheet = (self.store.state().get("sheets") or {}).get(actor)
        if sheet is None or not beyond.ready(sheet):
            return "That was already chosen."
        busy = self.director.unsettled(channel_id, "level up")
        if busy:
            return busy
        diff = self.store.apply([Mutation(MutationKind.SHEET, actor, {"sheet": after})],
                                reason=f"{actor} goes beyond 10th level")
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        if after.get("retired"):
            line = (f"🏡 **{actor}** retires to safety, a legend of 10th level. Make your next character with "
                    "`/new`; the GM will ask where the old one ended up.")
        elif after.get("mentor"):
            line = (f"🧙 **{actor}** takes on an apprentice. Make the apprentice with `/new`: they're yours to "
                    f"play now, and {actor} travels with them as their mentor, played by the GM, gaining no XP.")
        else:
            line = (f"🔄 **{actor}** becomes a **{after['class']}**: same stats, race and HP, new class moves. "
                    "`/sheet` has the buttons for anything the new class chooses.")
        await self.sender.send(channel_id, line)
        return None

    async def _finish_spells(self, actor: str, changes: dict, channel_id: int) -> Optional[str]:
        """Store chosen spells. Checked again here: the form was opened from a sheet that
        may since have chosen already, in another window."""
        sheet = (self.store.state().get("sheets") or {}).get(actor)
        if sheet is None or not (sheet.get("preparing") or spells.needs_setup(sheet)):
            return "Those spells were already chosen."
        diff = self.store.apply(
            [Mutation(MutationKind.SHEET, actor, {"sheet": {**sheet, **changes}})],
            reason=f"{actor} chooses spells",
        )
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        word = "is granted" if spells.caster_class(sheet) == "cleric" else "prepares"
        await self.sender.send(channel_id, f"📖 **{actor}** {word} {', '.join(changes['spells'])}.")
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

    async def _finish_gear(self, sheet: dict, channel_id: int) -> Optional[str]:
        """Store a character's chosen gear. One journalled change, so it undoes."""
        diff = self.store.apply(
            [Mutation(MutationKind.SHEET, sheet["name"], {"sheet": sheet})],
            reason=f"{sheet['name']} takes their gear",
        )
        if not diff.ok:
            return "That could not be saved: " + "; ".join(r for _, r in diff.rejected)
        await self.sender.send(channel_id, f"**{sheet['name']}** gears up — {describe(sheet, with_name=False)}")
        return None

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

    async def on_do(self, interaction: "discord.Interaction", text: str) -> None:
        """/do: what your character does or says, as if typed to Gary -- no privileged intent needed.
        It's posted to the table under the character's name, then played like any message to Gary."""
        text = " ".join(str(text or "").split())[:1500]
        who = self.director.players.get(interaction.user.id) or interaction.user.display_name
        await interaction.response.send_message(f"**{who}:** {text}")
        effects = self.director.on_player_message(
            channel_id=interaction.channel_id, user_id=interaction.user.id,
            display_name=interaction.user.display_name, content=text, mentions_bot=True,
        )
        await self.runner.run(effects)

    async def _on_player(self, message: "discord.Message") -> None:
        if not (message.content or "").strip():
            # Without the Message Content intent an unaddressed message comes with no text; there is
            # nothing to hear, and saying so to the model would be noise.
            log.info("player message %s arrived without text (no Message Content intent); ignored", message.id)
            if self._addressed_directly(message):
                # Addressed but blank: the "@Gary" picked was his role, not him. Discord sends a
                # message's text only for a mention of the bot user, and the two look identical.
                await self.sender.send(message.channel.id, ROLE_MENTION_HINT)
            return
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

    # -- commands (run by the slash commands) -------------------------------------------

    async def _on_dm_command(self, message: "discord.Message") -> None:
        body = message.content[len(self.settings.command_prefix) :].strip()
        parts = body.split()
        command = parts[0].lower() if parts else "help"
        channel_id = message.channel.id
        prefix = self.settings.command_prefix

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
                    f"To confirm: `/reset code:{self._reset_token}`"
                )
                return
            if token != self._reset_token:
                await self.sender.send(
                    channel_id,
                    "That code is not current. Run `/reset` "
                    "again to get a fresh one.",
                )
                return

            self._reset_token = None
            kept = self._keep_a_copy()
            import shutil

            shutil.rmtree(self.painter.folder, ignore_errors=True)  # the new campaign draws its own
            if (self.store.state().get("sheets") or self.store.state().get("setting")) and \
                    (self._opening().get("stage") != "finished"):  # a finished one has posted its chronicle
                from .. import opening

                await self.sender.send(channel_id, opening.chronicle(self.store.state()))
            for check in list(self.director.registry.open_checks()):
                self.director.registry.cancel(check.check_id, CancelReason.MANUAL)
            narrator = getattr(self.runner, "_narrator", None)
            if hasattr(narrator, "forget"):
                narrator.forget()  # first: no summary in flight may land after the wipe
            if self.history_backend is not None:
                self.history_backend.clear_all()
            self.store.reset_campaign()
            self.director.forget_everything()
            log.warning("campaign reset by %s in channel %d", message.author, channel_id)
            await self.sender.send(
                channel_id,
                f"Campaign reset. It is {format_world_time(self.store.state())}. "
                + (f"A copy of the old campaign is kept on Gary's machine (kept/{kept.name}). " if kept else "")
                + "`/begin` starts the next adventure.",
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

        if command == "questions":
            await self._session_questions(message, channel_id, body[len("questions"):].strip())
            return

        if command == "sheet" or command in _SHEET_ACTIONS:
            await self._sheet_hub(message, channel_id, prefix, only=None if command == "sheet" else command)
            return

        if command == "begin":
            await self._begin(message, channel_id)
            return

        if command == "end":
            from ..advanced import has as advanced_has

            sheets = self.store.state().get("sheets") or {}
            aligned = {actor: uid for uid, actor in sorted(self.director.players.items(), key=lambda p: p[1])
                       if actor in sheets and sheets[actor].get("alignment") and not sheets[actor].get("dead")}
            living = {uid: a for uid, a in self.director.players.items()
                      if a in sheets and not sheets[a].get("dead")}
            if not living and any(a in sheets for a in self.director.players.values()):
                # Everyone at the table has died: no XP to ask about, so close the session now.
                line = await self._end_session_then_fronts(channel_id, 0, frozenset(), {})
                if line and self._opening().get("stage") != "finished":
                    await self.sender.send(channel_id, line)
                return
            view = EndOfSession(
                lambda yeses, fulfilled, resolved: self._end_session_then_fronts(channel_id, yeses, fulfilled,
                                                                                 resolved),
                aligned, living, {a: sheets[a].get("bonds") or [] for a in living.values()},
                questions=self.store.state().get("session_questions") or None,
                best={a: uid for uid, a in living.items()
                      if advanced_has(sheets[a], "What Is Best In Life")},
            )
            view.message = await message.channel.send(view.prompt(), view=view)
            return

        if command == "dice":
            from ..dicestats import summary

            who = body[len("dice"):].strip() or None
            await self.sender.send(channel_id, summary(self.store.journal(), who))
            return

        if command == "fronts":
            known = self.store.state().get("fronts") or {}
            text = self._fronts_text()
            if not known:
                await self.sender.send(channel_id, text)
                return
            view = discord.ui.View(timeout=15 * 60)
            button = discord.ui.Button(label="Show fronts (only you will see them)", emoji="🗺️",
                                       style=discord.ButtonStyle.secondary)

            async def show(interaction: "discord.Interaction") -> None:
                await interaction.response.send_message(text, ephemeral=True)

            button.callback = show
            view.add_item(button)
            # A button, because only a click can be answered privately: fronts are spoilers.
            await message.channel.send(f"{len(known)} front(s) in play.", view=view)
            return

        if command == "playing":
            actor = body[len("playing"):].strip()
            if not actor:
                await self.sender.send(
                    channel_id,
                    "`/playing character:`.",
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
            await tag_nickname(message.author, actor)
            return

        if command in ("away", "back"):
            await self._away(message, channel_id, command, body[len(command):].strip())
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
            await self.sender.send(channel_id, self._cancel_mine(channel_id, message.author.id))

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
                owner = message.author.id
                if not any(c.user_id == owner for c in open_checks):
                    await self.sender.send(channel_id, "\n".join(lines))
                else:
                    view = discord.ui.View(timeout=FORM_TIMEOUT_SECONDS)
                    button = discord.ui.Button(label="Cancel my move", emoji="✖️", style=discord.ButtonStyle.danger)

                    async def cancel(interaction: "discord.Interaction") -> None:
                        if interaction.user.id != owner:
                            await interaction.response.send_message("That button cancels the asker's own move.",
                                                                    ephemeral=True)
                            return
                        await interaction.response.edit_message(view=None)
                        await self.sender.send(channel_id, self._cancel_mine(channel_id, owner))

                    button.callback = cancel
                    view.add_item(button)
                    await message.channel.send("\n".join(lines), view=view)


        elif command == "notes":
            state = self.store.state()
            lines = []
            for subject in sorted({str(n.get("subject", "")) for n in state.get("notes") or []}):
                count = sum(1 for n in state["notes"] if n.get("subject") == subject)
                lines.append(f"- {subject} ({count})" if count > 1 else f"- {subject}")
            parts = []
            if lines:
                parts.append("**Notes**, by subject — say one to bring it into play:\n"
                             + "\n".join(lines))
            await self.sender.send(
                channel_id,
                "\n\n".join(parts) if parts
                else "Nothing filed yet: it fills as the story goes.",
            )

        elif command == "npcs":
            from .. import npcs as people

            await self.sender.send(channel_id, people.roster(self.store.state(), body[len("npcs"):].strip() or None))

        elif command == "stats":  # the last line of /state
            await self.sender.send(channel_id, self._stats_line())

        elif command == "help":
            from .slash import HELP as SLASH_HELP

            await self.sender.send(channel_id, SLASH_HELP)

        else:
            await self.sender.send(
                channel_id, SLASH_ONLY
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
        steading=lambda: here(store.state()),
        scene=lambda: store.state().get("scene") or {},
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
        # The SDK's own HTTP timeout too (ms), so a stalled socket read gives up underneath us.
        client=genai.Client(api_key=settings.gemini_api_key, http_options=genai.types.HttpOptions(
            timeout=int(settings.gemini_timeout_seconds * 1000))),
        director=director,
        store=store,
        history_backend=history_backend,
        model=settings.gemini_model,
        summary_model=settings.gemini_summary_model,
        window=settings.history_window,
        timeout_seconds=settings.gemini_timeout_seconds,
        fallback_model=settings.gemini_fallback_model,
        thinking_level=settings.gemini_thinking_level,
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


def version() -> str:
    """The installed package's version (pyproject.toml; a release tag is vX.Y.Z of it)."""
    from importlib.metadata import PackageNotFoundError, version as installed

    try:
        return installed("gary")
    except PackageNotFoundError:
        return "unknown"


def main() -> None:  # pragma: no cover - operator entry point
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    log.info("Gary %s starting", version())  # which release the VM is running
    settings = Settings.from_env()
    client = build_client(settings)
    asyncio.run(serve(client, settings.discord_token))


if __name__ == "__main__":  # pragma: no cover
    main()
