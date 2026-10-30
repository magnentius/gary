"""Choosing spells over Discord components: the private form behind Choose spells.

Opened after Commune or Prepare Spells, and for a caster who has never chosen any (a new
character, or one made before spells existed). Each step is a ``spells.Preparation``
choice, so the form cannot hand a caster spells the rules forbid -- it only lists what
the Preparation offers, and refuses a pick over the level + 1 total.

Held in memory like the character form: a half-made choice is cheap to start again. The
button that opens it is durable, and the sheet's ``preparing`` flag says whether there is
anything to choose.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

import discord

from ..spells import PREPARE_NAMES, Preparation, SpellError, budget
from .creator import FORM_TIMEOUT_SECONDS

#: Called with the sheet changes. Returns an error to show the player, or None when saved.
Finish = Callable[["discord.Interaction", dict], Awaitable[Optional[str]]]

_PROMPTS = {
    "spellbook": "Your spellbook: choose three first-level spells to start with.",
    "borrowed": "Choose one spell from the other list to cast as your own.",
}


class SpellPicker(discord.ui.View):
    def __init__(self, sheet: dict, finish: Finish) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.sheet = sheet
        self.prep = Preparation(sheet)
        self._finish = finish
        self._build()

    def prompt(self, error: Optional[str] = None) -> str:
        lines = [f"⚠️ {error}"] if error else []
        step = self.prep.step()
        if step in _PROMPTS:
            lines.append(_PROMPTS[step])
        elif step == "prepare":
            word = "granted" if self.prep.cls == "cleric" else "prepared"
            lines.append(
                f"**{PREPARE_NAMES[self.prep.cls]}** — choose the spells to have {word}: "
                f"total levels up to {budget(self.sheet)}, none above level "
                f"{int(self.sheet.get('level', 1))}. Your "
                f"{'rotes' if self.prep.cls == 'cleric' else 'cantrips'} come free."
            )
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        fewest, most = self.prep.counts()
        select = discord.ui.Select(
            placeholder="Choose spells" if most > 1 else "Choose a spell",
            min_values=fewest, max_values=most,
            options=[discord.SelectOption(label=s.label()[:100], value=s.name,
                                          description=s.text[:100])
                     for s in self.prep.options()[:25]],
        )

        async def chosen(interaction: "discord.Interaction") -> None:
            await self._on_choose(interaction, list(select.values))

        select.callback = chosen
        self.add_item(select)

    async def _on_choose(self, interaction: "discord.Interaction", names) -> None:
        try:
            self.prep.choose(names)
        except SpellError as exc:
            self._build()
            await interaction.response.edit_message(content=self.prompt(str(exc)), view=self)
            return
        if self.prep.step() is not None:
            self._build()
            await interaction.response.edit_message(content=self.prompt(), view=self)
            return
        changes = self.prep.changes()
        error = await self._finish(interaction, changes)
        if error:
            self.prep = Preparation(self.sheet)
            self._build()
            await interaction.response.edit_message(content=self.prompt(error), view=self)
            return
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ {', '.join(changes['spells'])}.", view=None
        )
