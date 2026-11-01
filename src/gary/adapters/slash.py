"""Gary's slash commands: /begin, /new, /sheet and the rest, in Discord's command picker.

Each runs Gary's command handler -- one dispatcher, tested once -- through a stand-in message
built from the interaction: its author, channel, text and attachments.
The interaction is acknowledged at once (Discord allows three seconds) and the "thinking"
placeholder removed when the handler has posted; what it posts appears in the channel as
it always has.

Four are answered privately instead, which is what a slash command can do and a typed one
cannot: /new opens the character form, /sheet shows your sheet with its buttons, /fronts
shows the GM's threats, and /help lists the commands -- each only to whoever asked.

Commands are registered per server where Gary has a game channel (instant), not globally
(up to an hour). They are the only commands: a typed `?dm` is pointed here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

import discord
from discord import app_commands

if TYPE_CHECKING:
    from .discord_bot import GaryClient

log = logging.getLogger(__name__)

#: What /help lists. Kept beside the commands so the two cannot drift.
HELP = """**Gary's commands**

__Starting__
`/begin` — start the adventure: Gary offers a premise, then runs introductions, hook questions, bonds and the first scene
`/new` — make a character (a private form) · `/playing character:` — claim one someone else made

__At the table__
`/sheet` — your sheet, privately, with buttons for spells, bonds, companion, alignment and level-up
`/npcs` · `/npcs name:` — the people you've met or heard of
`/dice` · `/dice character:` — the dice so far, against fair 2d6
`/checks` — what Gary is waiting on, with a button to cancel your own
`/undo` — reverse the last change to the world
`/endsession` — the end-of-session questions and XP · `/questions` — set this adventure's own

__For the GM__
`/fronts` — the threats in play, shown only to you (spoilers)
`/state` — clock, scene, party, what is stored, and Gary's counters
`/notes` — what has been filed, by subject
`/help` — this list
`/reset` — **deletes the campaign**. Two-step (`/reset code:`), and the one thing here with no undo."""


class _SlashMessage:
    """What the command dispatcher reads from a message, built from an interaction."""

    def __init__(self, interaction: "discord.Interaction", content: str,
                 attachments: Optional[List["discord.Attachment"]] = None) -> None:
        self.content = content
        self.author = interaction.user
        self.channel = interaction.channel
        self.attachments = list(attachments or [])
        self.id = interaction.id


def register(client: "GaryClient") -> app_commands.CommandTree:
    tree = app_commands.CommandTree(client)
    prefix = client.settings.command_prefix

    def playing_here(interaction: "discord.Interaction") -> bool:
        return interaction.channel_id in client.settings.game_channels

    async def not_here(interaction: "discord.Interaction") -> None:
        await interaction.response.send_message("Gary doesn't play in this channel.", ephemeral=True)

    async def run(interaction: "discord.Interaction", body: str,
                  attachments: Optional[List["discord.Attachment"]] = None) -> None:
        """Run a command for this interaction; its posts land in the channel."""
        if not playing_here(interaction):
            await not_here(interaction)
            return
        await interaction.response.defer(thinking=True)
        try:
            await client._on_dm_command(_SlashMessage(interaction, f"{prefix}{body}".rstrip(), attachments))
        except Exception:  # the placeholder must not be left spinning
            log.exception("slash command /%s failed", body.split(" ")[0])
            await interaction.edit_original_response(content="That didn't work — the error is in Gary's log.")
            return
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            log.warning("could not remove the slash command placeholder")

    async def characters(interaction: "discord.Interaction", current: str) -> List[app_commands.Choice[str]]:
        names = sorted((client.store.state().get("sheets") or {}))
        return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]

    # -- private answers -----------------------------------------------------------------------

    @tree.command(name="new", description="Make a character, in a private form")
    async def new(interaction: "discord.Interaction") -> None:
        if not playing_here(interaction):
            await not_here(interaction)
            return
        from .creator import CharacterCreator

        channel_id = interaction.channel_id
        form = CharacterCreator(lambda i, sheet: client._finish_character(i, sheet, channel_id))
        await interaction.response.send_message(form.prompt(), view=form, ephemeral=True)

    @tree.command(name="sheet", description="Your character's sheet, with buttons for everything you can open")
    async def sheet(interaction: "discord.Interaction") -> None:
        if not playing_here(interaction):
            await not_here(interaction)
            return

        async def reply(text: str, view) -> None:
            if view is None:
                await interaction.response.send_message(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, view=view, ephemeral=True)

        await client._sheet_hub(_SlashMessage(interaction, f"{prefix}sheet"), interaction.channel_id, prefix,
                                reply=reply)

    @tree.command(name="fronts", description="The threats the GM is tracking — shown only to you")
    async def fronts(interaction: "discord.Interaction") -> None:
        await interaction.response.send_message(client._fronts_text(), ephemeral=True)

    @tree.command(name="help", description="Gary's commands")
    async def help_(interaction: "discord.Interaction") -> None:
        await interaction.response.send_message(HELP, ephemeral=True)

    # -- the rest, through the command dispatcher ----------------------------------------------

    @tree.command(name="begin", description="Start the adventure: a premise, characters, questions, bonds, play")
    async def begin(interaction: "discord.Interaction") -> None:
        await run(interaction, "begin")





    @tree.command(name="endsession", description="The end-of-session questions, and everyone's XP")
    async def endsession(interaction: "discord.Interaction") -> None:
        await run(interaction, "endsession")

    @tree.command(name="questions", description="Show, set or reset the end-of-session questions")
    @app_commands.describe(first="This adventure's first question", second="A second", third="A third",
                           default="Go back to the book's three")
    async def questions(interaction: "discord.Interaction", first: Optional[str] = None,
                        second: Optional[str] = None, third: Optional[str] = None,
                        default: bool = False) -> None:
        asked = " | ".join(q for q in (first, second, third) if q)
        await run(interaction, "questions default" if default else f"questions {asked}")

    @tree.command(name="dice", description="How the table's dice have fallen, against fair 2d6")
    @app_commands.describe(character="Just this character's rolls")
    @app_commands.autocomplete(character=characters)
    async def dice(interaction: "discord.Interaction", character: Optional[str] = None) -> None:
        await run(interaction, f"dice {character or ''}")

    @tree.command(name="playing", description="Claim a character someone else made")
    @app_commands.describe(character="The character's name")
    @app_commands.autocomplete(character=characters)
    async def playing(interaction: "discord.Interaction", character: str) -> None:
        await run(interaction, f"playing {character}")

    @tree.command(name="checks", description="What Gary is waiting to be rolled")
    async def checks(interaction: "discord.Interaction") -> None:
        await run(interaction, "checks")

    @tree.command(name="undo", description="Reverse the last change to the world")
    async def undo(interaction: "discord.Interaction") -> None:
        await run(interaction, "undo")

    @tree.command(name="state", description="Clock, scene, party, what is stored, and Gary's counters")
    async def state(interaction: "discord.Interaction") -> None:
        await run(interaction, "state")

    async def known_people(interaction: "discord.Interaction", current: str) -> List[app_commands.Choice[str]]:
        from .. import npcs as people

        names = [str(n["name"]) for n in people.known(client.store.state())]
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names if current.lower() in n.lower()][:25]

    @tree.command(name="npcs", description="The people you've met or heard of")
    @app_commands.describe(name="Just this person")
    @app_commands.autocomplete(name=known_people)
    async def npcs(interaction: "discord.Interaction", name: Optional[str] = None) -> None:
        await run(interaction, f"npcs {name or ''}")

    @tree.command(name="notes", description="What has been filed, by subject")
    async def notes(interaction: "discord.Interaction") -> None:
        await run(interaction, "notes")

    @tree.command(name="reset", description="Delete the campaign — two steps, and no undo")
    @app_commands.describe(code="The confirmation code Gary gave you")
    async def reset(interaction: "discord.Interaction", code: Optional[str] = None) -> None:
        await run(interaction, f"reset {code or ''}")

    return tree


async def sync(client: "GaryClient") -> None:
    """Register the commands in every server Gary plays in. Per server, so they appear at once."""
    guilds = {channel.guild for channel in (client.get_channel(c) for c in client.settings.game_channels)
              if channel is not None and getattr(channel, "guild", None) is not None}
    for guild in guilds:
        client.tree.copy_global_to(guild=guild)
        try:
            synced = await client.tree.sync(guild=guild)
            log.info("registered %d slash command(s) in %s", len(synced), guild.name)
        except discord.HTTPException as exc:
            log.error("could not register slash commands in %s: %s -- was Gary invited with the "
                      "applications.commands scope?", guild.name, exc)
