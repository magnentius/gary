"""Writing bonds over Discord components (`?dm bonds`): a private form.

Pick one of your class's bond lines, then the companion who fills its blank; remove one
to rewrite it; save. Every change is a ``bonds`` call, so the form cannot write a bond the
rules do not offer. Held in memory like the character form.
"""

from __future__ import annotations

from typing import Awaitable, Callable, List, Optional

import discord

from .. import bonds
from ..sheets import SheetError
from .creator import FORM_TIMEOUT_SECONDS

#: A dropdown shows roughly this much of a label before cutting it off; the rest goes in
#: the option's description, which wraps.
_SHOWN = 45


def long_option(text: str, value: str) -> "discord.SelectOption":
    if len(text) <= _SHOWN:
        return discord.SelectOption(label=text, value=value)
    cut = text.rfind(" ", 0, _SHOWN)
    head = text[:cut if cut > 0 else _SHOWN]
    return discord.SelectOption(label=head + " …", value=value, description=("… " + text[len(head):].strip())[:100])


#: Called with the sheet as edited. Returns an error to show the player, or None when saved.
Finish = Callable[[dict], Awaitable[Optional[str]]]


class BondsForm(discord.ui.View):
    def __init__(self, sheet: dict, companions: List[str], finish: Finish) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.sheet = dict(sheet)
        self.companions = companions
        self.line: Optional[str] = None  # chosen, awaiting a companion
        self._finish = finish
        self._build()

    def prompt(self, error: Optional[str] = None) -> str:
        lines = [f"⚠️ {error}"] if error else []
        written = self.sheet.get("bonds") or []
        lines.append("**Your bonds:**" if written else "**No bonds yet.** Write at least one.")
        lines += [f"• {bonds.written(b)}" for b in written]
        if self.line:
            lines.append(f"Who fills the blank? *{self.line}*")
        return "\n".join(lines)

    def _unused(self) -> List[str]:
        used = {b["line"] for b in self.sheet.get("bonds") or ()}
        return [line for line in bonds.lines_for(self.sheet) if line not in used]

    def _build(self) -> None:
        self.clear_items()
        if self.line is not None:
            self._select("With…", [discord.SelectOption(label=c[:100], value=c) for c in self.companions],
                         self._on_companion)
        elif self._unused():
            self._select("Write a bond", [long_option(line, str(i))
                                          for i, line in enumerate(self._unused())], self._on_line)
        if self.sheet.get("bonds"):
            self._select("Remove a bond", [long_option(bonds.written(b), str(i))
                                           for i, b in enumerate(self.sheet["bonds"])], self._on_remove)
        save = discord.ui.Button(label="Save bonds", style=discord.ButtonStyle.success)
        save.callback = self._on_save
        self.add_item(save)

    def _select(self, placeholder: str, options, handler) -> None:
        select = discord.ui.Select(placeholder=placeholder, options=options)

        async def chosen(interaction: "discord.Interaction") -> None:
            await handler(interaction, select.values[0])

        select.callback = chosen
        self.add_item(select)

    async def _refresh(self, interaction: "discord.Interaction", error: Optional[str] = None) -> None:
        self._build()
        await interaction.response.edit_message(content=self.prompt(error), view=self)

    async def _on_line(self, interaction, value: str) -> None:
        self.line = self._unused()[int(value)]
        await self._refresh(interaction)

    async def _on_companion(self, interaction, companion: str) -> None:
        try:
            self.sheet = bonds.add(self.sheet, self.line, companion, self.companions)
        except SheetError as exc:
            self.line = None
            await self._refresh(interaction, str(exc))
            return
        self.line = None
        await self._refresh(interaction)

    async def _on_remove(self, interaction, value: str) -> None:
        self.sheet = bonds.remove(self.sheet, int(value))
        await self._refresh(interaction)

    async def _on_save(self, interaction) -> None:
        error = await self._finish(self.sheet)
        if error:
            await self._refresh(interaction, error)
            return
        self.stop()
        await interaction.response.edit_message(content="✅ " + self.prompt(), view=None)
