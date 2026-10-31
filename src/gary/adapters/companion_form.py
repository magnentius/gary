"""Making a Ranger's animal companion over Discord components (`/companion`).

Species, base, strengths, trainings, weaknesses, then a name: each a ``CompanionDraft``
step, so the form only offers what the book allows, as many as the base says. Held in
memory like the character form.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

import discord

from ..companion import BASES, CompanionDraft, base_label, describe
from ..sheets import MAX_NAME_CHARS, SheetError
from .creator import FORM_TIMEOUT_SECONDS

#: Called with the finished companion. Returns an error to show the player, or None.
Finish = Callable[[dict], Awaitable[Optional[str]]]

_PROMPTS = {
    "species": "Choose your companion's species.",
    "base": "Choose a base.",
    "strengths": "Choose its strengths — as many as its ferocity ({n}).",
    "trainings": "It is trained to fight humanoids. Choose more trainings — as many as its cunning ({n}).",
    "weaknesses": "Choose its weaknesses — as many as its instinct ({n}).",
    "name": "Name your companion.",
}


class CompanionForm(discord.ui.View):
    def __init__(self, owner: str, finish: Finish) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.owner = owner
        self.draft = CompanionDraft()
        self._finish = finish
        self._build()

    def prompt(self, error: Optional[str] = None) -> str:
        lines = [f"⚠️ {error}"] if error else []
        step = self.draft.step()
        if step is None:
            lines.append(describe({"name": self.owner, "companion": self.draft.build()}))
            lines.append("Take this companion?")
        else:
            lines.append(_PROMPTS[step].format(n=self.draft.counts()))
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        d = self.draft
        step = d.step()
        if step == "species":
            self._select("Species", [discord.SelectOption(label=s, value=s) for s in d.options()],
                         lambda values: d.choose_species(values[0]))
        elif step == "base":
            self._select("Base", [discord.SelectOption(label=base_label(b), value=str(i))
                                  for i, b in enumerate(BASES)],
                         lambda values: d.choose_base(int(values[0])))
        elif step in ("strengths", "trainings", "weaknesses"):
            self._select(step.title(), [discord.SelectOption(label=o, value=o) for o in d.options()],
                         d.choose, count=d.counts())
        elif step == "name":
            self._button("Name your companion", self._on_name, discord.ButtonStyle.primary)
        else:
            self._button("Take this companion", self._on_done, discord.ButtonStyle.success)
        if step != "species":
            self._button("Start over", self._on_restart, discord.ButtonStyle.secondary)

    def _select(self, placeholder: str, options, choose, count: int = 1) -> None:
        select = discord.ui.Select(placeholder=placeholder, options=options,
                                   min_values=count, max_values=count)

        async def chosen(interaction: "discord.Interaction") -> None:
            await self.apply(interaction, lambda: choose(list(select.values)))

        select.callback = chosen
        self.add_item(select)

    def _button(self, label: str, handler, style) -> None:
        button = discord.ui.Button(label=label, style=style)
        button.callback = handler
        self.add_item(button)

    async def apply(self, interaction: "discord.Interaction", step) -> None:
        try:
            step()
        except SheetError as exc:
            self._build()
            await interaction.response.edit_message(content=self.prompt(str(exc)), view=self)
            return
        self._build()
        await interaction.response.edit_message(content=self.prompt(), view=self)

    async def _on_restart(self, interaction: "discord.Interaction") -> None:
        await self.apply(interaction, lambda: setattr(self, "draft", CompanionDraft()))

    async def _on_name(self, interaction: "discord.Interaction") -> None:
        await interaction.response.send_modal(CompanionNameModal(self))

    async def _on_done(self, interaction: "discord.Interaction") -> None:
        error = await self._finish(self.draft.build())
        if error:
            self._build()
            await interaction.response.edit_message(content=self.prompt(error), view=self)
            return
        self.stop()
        await interaction.response.edit_message(content="✅ " + self.prompt().rsplit("\n", 1)[0], view=None)


class CompanionNameModal(discord.ui.Modal, title="Name your companion"):
    name = discord.ui.TextInput(label="Name", max_length=MAX_NAME_CHARS)

    def __init__(self, form: CompanionForm) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.form = form

    async def on_submit(self, interaction: "discord.Interaction") -> None:
        await self.form.apply(interaction, lambda: self.form.draft.choose_name(self.name.value))
