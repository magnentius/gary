"""Character creation over Discord components (`?dm new`).

A private, step-by-step form: class, race, the stat array one score at a time, a name,
then a summary to confirm. Every step is a ``sheets.Draft`` setter, so the form cannot
produce a sheet the rules forbid -- it only shows the choices the Draft has left.

Held in memory, unlike the Roll button. A half-made character is cheap to start again,
and a form that survived a restart would be resuming choices the player may not
remember making.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

import discord

from ..sheets import CLASSES, MAX_NAME_CHARS, STAT_NAMES, Draft, SheetError, describe

#: Long enough to read every class and think; short enough that an abandoned form does
#: not answer a click tomorrow.
FORM_TIMEOUT_SECONDS = 15 * 60

#: Called with the finished sheet. Returns an error to show the player, or None when the
#: character was created.
Finish = Callable[["discord.Interaction", dict], Awaitable[Optional[str]]]


class StartButton(discord.ui.View):
    """The public button `?dm new` posts. Only the person who asked can use it.

    Needed because a typed command cannot be answered privately -- only a click can --
    and stats are nobody else's business until the character exists.
    """

    def __init__(self, owner_id: int, owner_name: str, finish: Finish) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.finish = finish

    @discord.ui.button(label="Create character", emoji="📜", style=discord.ButtonStyle.primary)
    async def start(self, interaction: "discord.Interaction", _button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"This one is {self.owner_name}'s. `?dm new` to start your own.",
                ephemeral=True,
            )
            return
        creator = CharacterCreator(self.finish)
        await interaction.response.send_message(creator.prompt(), view=creator, ephemeral=True)


class CharacterCreator(discord.ui.View):
    """The private form. Rebuilds its components for whichever step the Draft is at."""

    def __init__(self, finish: Finish) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.draft = Draft()
        self._finish = finish
        self._build()

    # -- what the player sees ------------------------------------------------------------

    def prompt(self, error: Optional[str] = None) -> str:
        d = self.draft
        lines = []
        if error:
            lines.append(f"⚠️ {error}")
        if d.class_key:
            chosen = CLASSES[d.class_key]["name"] + (f", {d.race}" if d.race else "")
            lines.append(f"**{chosen}**")
        if d.stats:
            lines.append(" · ".join(
                f"{STAT_NAMES[s][:3].upper()} {v}" for s, v in d.stats.items()
            ))

        if d.class_key is None:
            lines.append("Choose a class.")
        elif d.race is None:
            lines.append("Choose a race.")
        elif d.unassigned_stats():
            lines.append(f"Where does your **{d.next_score()}** go?")
        elif d.name is None:
            lines.append("Name your character.")
        else:
            lines = ([f"⚠️ {error}"] if error else []) + [
                describe(d.build()), "Create this character?"
            ]
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        d = self.draft

        if d.class_key is None:
            self._select(
                "Class",
                [
                    discord.SelectOption(
                        label=c["name"], value=key,
                        description=f"HP {c['base_hp']}+Constitution, damage {c['damage']}",
                    )
                    for key, c in CLASSES.items()
                ],
                self._on_class,
            )
            return

        if d.race is None:
            self._select(
                "Race", [discord.SelectOption(label=r, value=r) for r in d.races()], self._on_race
            )
            self._button("Change class", self._on_restart)
            return

        if d.unassigned_stats():
            self._select(
                f"Put {d.next_score()} in…",
                [discord.SelectOption(label=STAT_NAMES[s], value=s) for s in d.unassigned_stats()],
                self._on_stat,
            )
            if d.stats:
                self._button("Start stats over", self._on_reset_stats)
            return

        if d.name is None:
            self._button("Name your character", self._on_name, style=discord.ButtonStyle.primary)
            self._button("Start stats over", self._on_reset_stats)
            return

        self._button("Create character", self._on_create, style=discord.ButtonStyle.success)
        self._button("Rename", self._on_name)
        self._button("Start over", self._on_restart)

    def _select(self, placeholder: str, options, handler) -> None:
        select = discord.ui.Select(placeholder=placeholder, options=options)

        async def callback(interaction: "discord.Interaction") -> None:
            await handler(interaction, select.values[0])

        select.callback = callback
        self.add_item(select)

    def _button(self, label: str, handler, style=discord.ButtonStyle.secondary) -> None:
        button = discord.ui.Button(label=label, style=style)

        async def callback(interaction: "discord.Interaction") -> None:
            await handler(interaction)

        button.callback = callback
        self.add_item(button)

    async def refresh(self, interaction: "discord.Interaction", error: Optional[str] = None) -> None:
        self._build()
        await interaction.response.edit_message(content=self.prompt(error), view=self)

    # -- steps ---------------------------------------------------------------------------

    async def _apply(self, interaction, step: Callable[[], None]) -> None:
        try:
            step()
        except SheetError as exc:
            await self.refresh(interaction, str(exc))
            return
        await self.refresh(interaction)

    async def _on_class(self, interaction, value: str) -> None:
        await self._apply(interaction, lambda: self.draft.choose_class(value))

    async def _on_race(self, interaction, value: str) -> None:
        await self._apply(interaction, lambda: self.draft.choose_race(value))

    async def _on_stat(self, interaction, value: str) -> None:
        score = self.draft.next_score()
        await self._apply(interaction, lambda: self.draft.assign(value, score))

    async def _on_reset_stats(self, interaction) -> None:
        await self._apply(interaction, self.draft.reset_stats)

    async def _on_restart(self, interaction) -> None:
        self.draft = Draft()
        await self.refresh(interaction)

    async def _on_name(self, interaction) -> None:
        await interaction.response.send_modal(NameModal(self))

    async def _on_create(self, interaction) -> None:
        try:
            sheet = self.draft.build()
        except SheetError as exc:
            await self.refresh(interaction, str(exc))
            return
        error = await self._finish(interaction, sheet)
        if error:
            await self.refresh(interaction, error)
            return
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ Created. {describe(sheet)}", view=None
        )


class NameModal(discord.ui.Modal, title="Name your character"):
    name = discord.ui.TextInput(label="Name", max_length=MAX_NAME_CHARS)

    def __init__(self, creator: CharacterCreator) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.creator = creator
        if creator.draft.name:
            self.name.default = creator.draft.name

    async def on_submit(self, interaction: "discord.Interaction") -> None:
        # Submitted from a button on the form, so the reply edits the form in place.
        await self.creator._apply(interaction, lambda: self.creator.draft.choose_name(self.name.value))
