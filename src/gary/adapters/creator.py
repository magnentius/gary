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

from ..gear import ENHANCEMENTS, SIGNATURE_BASES, SIGNATURE_RANGES
from ..sheets import (
    CLASSES, MAX_NAME_CHARS, STAT_NAMES, STATS, Draft, SheetError, alignments_of, describe, with_gear,
)

_SIGNATURE_PROMPTS = {
    "base": "Your signature weapon. Choose a base description (all are 2 weight).",
    "range": "Choose the range that best fits your weapon.",
    "enhancements": "Choose two enhancements.",
    "extra_range": "Versatile: choose an additional range.",
}

#: Long enough to read every class and think; short enough that an abandoned form does
#: not answer a click tomorrow.
FORM_TIMEOUT_SECONDS = 15 * 60

#: Called with the finished sheet. Returns an error to show the player, or None when the
#: character was created.
Finish = Callable[["discord.Interaction", dict], Awaitable[Optional[str]]]


class GearButton(discord.ui.View):
    """`?dm gear`: the public button that opens the gear form for an existing character.
    Like StartButton, only its owner can use it."""

    def __init__(self, owner_id: int, owner_name: str, sheet: dict, finish: Finish) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.owner_id, self.owner_name, self.sheet, self.finish = owner_id, owner_name, sheet, finish

    @discord.ui.button(label="Choose gear", emoji="🛡️", style=discord.ButtonStyle.primary)
    async def start(self, interaction: "discord.Interaction", _button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                f"This one is {self.owner_name}'s. `?dm gear` for your own.", ephemeral=True
            )
            return
        form = CharacterCreator(self.finish, sheet=self.sheet)
        await interaction.response.send_message(form.prompt(), view=form, ephemeral=True)


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

    def __init__(self, finish: Finish, sheet: Optional[dict] = None) -> None:
        """With ``sheet``, the form only gives gear to that existing character: class,
        race, stats and name are theirs already, and nothing else about them changes."""
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self._sheet = sheet
        self.draft = self._fresh()
        self._finish = finish
        self._build()

    def _fresh(self) -> Draft:
        if self._sheet is None:
            return Draft()
        return Draft(
            class_key=self._sheet["class"].lower(),
            race=self._sheet["race"],
            stats=dict(self._sheet["stats"]),
            name=self._sheet["name"],
        )

    def _result(self) -> dict:
        if self._sheet is None:
            return self.draft.build()
        return with_gear(self._sheet, self.draft.gear_items())

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
            # In the rules' order, not the dict's: a sheet read back from the store has
            # its keys sorted, which put CHA first.
            lines.append(" · ".join(
                f"{STAT_NAMES[s][:3].upper()} {d.stats[s]}" for s in STATS if s in d.stats
            ))

        if d.class_key is None:
            lines.append("Choose a class.")
        elif d.race is None:
            lines.append("Choose a race.")
        elif d.unassigned_stats():
            lines.append(f"Where does your **{d.next_score()}** go?")
        elif d.next_gear_group() is not None:
            group = d.class_gear().groups[d.next_gear_group()]
            lines.append(f"{group.prompt}.")
        elif d.signature_step() is not None:
            lines.append(_SIGNATURE_PROMPTS[d.signature_step()])
        elif d.alignment is None and self._sheet is None:
            lines.append("Choose your alignment — you mark XP at the end of a session when you "
                         "have acted on it.")
        elif self._sheet is None and d.class_choices().step() is not None:
            lines.append(d.class_choices().step().prompt)
        elif d.name is None:
            lines.append("Name your character.")
        else:
            lines = ([f"⚠️ {error}"] if error else []) + [
                describe(self._result()),
                "Take this gear?" if self._sheet else "Create this character?",
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

        if d.next_gear_group() is not None:
            index = d.next_gear_group()
            group = d.class_gear().groups[index]
            self._select(
                group.prompt,
                [discord.SelectOption(label=label[:100], value=str(i))
                 for i, (label, _) in enumerate(group.options)],
                lambda interaction, values: self._on_gear(interaction, index, values),
                count=group.count,
            )
            if d.gear_picks:
                self._button("Start gear over", self._on_reset_gear)
            return

        step = d.signature_step()
        if step == "base":
            self._select("Base", [discord.SelectOption(label=b, value=b) for b in SIGNATURE_BASES],
                         self._on_signature_base)
            return
        if step in ("range", "extra_range"):
            self._select(
                "Range",
                [discord.SelectOption(label=r.title(), value=r)
                 for r in SIGNATURE_RANGES if r not in d.signature_ranges],
                self._on_signature_range,
            )
            return
        if step == "enhancements":
            self._select(
                "Two enhancements",
                [discord.SelectOption(label=e.text[:100], value=str(i))
                 for i, e in enumerate(ENHANCEMENTS)],
                self._on_signature_enhancements,
                count=2,
            )
            return

        if d.alignment is None and self._sheet is None:
            self._select("Alignment", [discord.SelectOption(label=a, value=a, description=t[:100])
                                       for a, t in d.alignments().items()], self._on_alignment)
            return

        step = d.class_choices().step() if self._sheet is None else None
        if step is not None:
            add_choice_step(self, step, lambda value: self._apply_choice(value))
            return

        if d.name is None:
            self._button("Name your character", self._on_name, style=discord.ButtonStyle.primary)
            self._button("Start gear over", self._on_reset_gear)
            return

        if self._sheet is not None:
            self._button("Take this gear", self._on_create, style=discord.ButtonStyle.success)
            self._button("Start gear over", self._on_reset_gear)
            return
        self._button("Create character", self._on_create, style=discord.ButtonStyle.success)
        self._button("Rename", self._on_name)
        self._button("Start over", self._on_restart)

    def _select(self, placeholder: str, options, handler, count: int = 1) -> None:
        """A dropdown. With ``count`` above one it takes exactly that many, and the
        handler gets them all; otherwise it gets the single value."""
        select = discord.ui.Select(
            placeholder=placeholder, options=options, min_values=count, max_values=count
        )

        async def callback(interaction: "discord.Interaction") -> None:
            await handler(interaction, list(select.values) if count > 1 else select.values[0])

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

    async def _on_gear(self, interaction, group: int, values) -> None:
        picks = values if isinstance(values, list) else [values]
        await self._apply(interaction, lambda: self.draft.choose_gear(group, [int(v) for v in picks]))

    async def _on_reset_gear(self, interaction) -> None:
        await self._apply(interaction, self.draft.reset_gear)

    async def _on_signature_base(self, interaction, value: str) -> None:
        await self._apply(interaction, lambda: self.draft.choose_signature_base(value))

    async def _on_signature_range(self, interaction, value: str) -> None:
        await self._apply(interaction, lambda: self.draft.choose_signature_range(value))

    async def _on_signature_enhancements(self, interaction, values) -> None:
        await self._apply(
            interaction, lambda: self.draft.choose_signature_enhancements([int(v) for v in values])
        )

    async def _apply_choice(self, value: str):
        """For add_choice_step: returns a coroutine function taking the interaction."""
        return lambda interaction: self._apply(interaction, lambda: self.draft.choose_class_choice(value))

    async def _on_alignment(self, interaction, value: str) -> None:
        await self._apply(interaction, lambda: self.draft.choose_alignment(value))

    async def _on_reset_stats(self, interaction) -> None:
        await self._apply(interaction, self.draft.reset_stats)

    async def _on_restart(self, interaction) -> None:
        self.draft = self._fresh()
        await self.refresh(interaction)

    async def _on_name(self, interaction) -> None:
        await interaction.response.send_modal(NameModal(self))

    async def _on_create(self, interaction) -> None:
        try:
            sheet = self._result()
        except SheetError as exc:
            await self.refresh(interaction, str(exc))
            return
        error = await self._finish(interaction, sheet)
        if error:
            await self.refresh(interaction, error)
            return
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ {'Geared up' if self._sheet else 'Created'}. {describe(sheet)}", view=None
        )


class AlignmentForm(discord.ui.View):
    """`?dm alignment`: choose or change an existing character's alignment. Private, and
    the save writes only the alignment."""

    def __init__(self, sheet: dict, finish: Callable[[str], Awaitable[Optional[str]]]) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self._finish = finish
        options = alignments_of(sheet["class"])
        select = discord.ui.Select(placeholder="Alignment", options=[
            discord.SelectOption(label=a, value=a, description=t[:100], default=a == sheet.get("alignment"))
            for a, t in options.items()])

        async def chosen(interaction: "discord.Interaction") -> None:
            error = await self._finish(select.values[0])
            if error:
                await interaction.response.edit_message(content=f"⚠️ {error}", view=self)
                return
            self.stop()
            await interaction.response.edit_message(
                content=f"✅ {select.values[0]}: {options[select.values[0]]}", view=None)

        select.callback = chosen
        self.add_item(select)

    @staticmethod
    def prompt() -> str:
        return "Choose your alignment — you mark XP at the end of a session when you have acted on it."


def add_choice_step(view: "discord.ui.View", step, choose) -> None:
    """One creation choice on ``view``: a dropdown for a list, a button and a text box for
    something written (a god's name, a tell). ``choose(value)`` is awaited and returns a
    handler taking the interaction."""
    if step.written:
        button = discord.ui.Button(label="Write it", style=discord.ButtonStyle.primary)

        async def open_modal(interaction: "discord.Interaction") -> None:
            await interaction.response.send_modal(ChoiceModal(step.prompt, choose))

        button.callback = open_modal
        view.add_item(button)
        return
    select = discord.ui.Select(placeholder=step.prompt[:150], options=[
        discord.SelectOption(label=o[:100], value=o[:100], description=(d or None) and d[:100])
        for o, d in zip(step.options, step.descriptions or ("",) * len(step.options))])

    async def chosen(interaction: "discord.Interaction") -> None:
        handler = await choose(select.values[0])
        await handler(interaction)

    select.callback = chosen
    view.add_item(select)


class ChoiceModal(discord.ui.Modal, title="Your choice"):
    answer = discord.ui.TextInput(label="Answer", max_length=MAX_NAME_CHARS * 2)

    def __init__(self, prompt: str, choose) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.answer.label = prompt[:45]
        self._choose = choose

    async def on_submit(self, interaction: "discord.Interaction") -> None:
        handler = await self._choose(self.answer.value)
        await handler(interaction)


class ChoicesForm(discord.ui.View):
    """`?dm choices`: the class's creation choices for a character made before them.
    Private; the save writes only the choices (and a Thief's starting poison)."""

    def __init__(self, sheet: dict, finish: Callable[[dict], Awaitable[Optional[str]]]) -> None:
        from ..choices import Choices

        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self._finish = finish
        self.choices = Choices(sheet["class"], sheet.get("race", ""), sheet.get("choices"))
        self._build()

    def prompt(self, error: Optional[str] = None) -> str:
        step = self.choices.step()
        lines = [f"⚠️ {error}"] if error else []
        lines.append(step.prompt if step else "All chosen.")
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        step = self.choices.step()
        if step is not None:
            add_choice_step(self, step, self._choose)

    async def _choose(self, value: str):
        async def handle(interaction: "discord.Interaction") -> None:
            try:
                self.choices.choose(value)
            except SheetError as exc:
                self._build()
                await interaction.response.edit_message(content=self.prompt(str(exc)), view=self)
                return
            if self.choices.complete:
                error = await self._finish(self.choices.made)
                if error:
                    await interaction.response.edit_message(content=f"⚠️ {error}", view=self)
                    return
                self.stop()
                await interaction.response.edit_message(content="✅ Chosen.", view=None)
                return
            self._build()
            await interaction.response.edit_message(content=self.prompt(), view=self)

        return handle


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
