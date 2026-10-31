"""Level Up over Discord components (`/levelup`): a private form.

A stat to raise, an advanced move, and for a Wizard a spell for the spellbook -- each a
``levelling.Advance`` choice, so the form cannot level a character in a way the rules
forbid. Held in memory like the character form.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

import discord

from ..levelling import SPELL_MOVES, Advance, move_options
from ..sheets import STAT_NAMES, SheetError
from .creator import FORM_TIMEOUT_SECONDS

#: Called with the levelled sheet. Returns an error to show the player, or None when saved.
Finish = Callable[[dict], Awaitable[Optional[str]]]


class LevelUpForm(discord.ui.View):
    def __init__(self, sheet: dict, finish: Finish) -> None:
        super().__init__(timeout=FORM_TIMEOUT_SECONDS)
        self.sheet = sheet
        self.advance = Advance(sheet)
        self._finish = finish
        self._build()

    def prompt(self, error: Optional[str] = None) -> str:
        a = self.advance
        lines = [f"⚠️ {error}"] if error else []
        lines.append(f"**{self.sheet['name']}** reaches level {a.level}.")
        step = a.step()
        if step == "stat":
            lines.append("Raise one stat by 1 (no higher than 18).")
        elif step == "move":
            lines.append(f"Choose an advanced move. {_moves_text(self.sheet, a.level)}")
        elif step == "move_spell":
            lines.append(f"{a.move}: " + ("add a spell from any class's list to your spellbook."
                                          if SPELL_MOVES[a.move] == "spellbook" else
                                          "choose the spell you will have as if it were one level lower "
                                          "(a first-level one becomes a rote or cantrip)."))
        elif step == "move_choice":
            lines.append(f"{a.move}: choose the part of your body that bears the mark of the wraith lands.")
        elif step == "multi_class":
            lines.append(f"{a.move}: choose a class to take a move from (your level counts as {a.level - 1}).")
        elif step == "multi_move":
            lines.append(f"Choose a {a.multi_class.title()} move.")
        elif step == "spell":
            lines.append(f"Add a spell of level {a.level} or lower to your spellbook.")
        else:
            lines.append(f"Take {a.summary()}?")
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        a = self.advance
        step = a.step()
        if step == "stat":
            self._select("Raise…", [discord.SelectOption(
                label=f"{STAT_NAMES[s]} {self.sheet['stats'][s]} → {int(self.sheet['stats'][s]) + 1}", value=s)
                for s in a.stat_options()], a.choose_stat)
        elif step == "move":
            self._select("Advanced move", [discord.SelectOption(
                label=(m["name"] + (f" ({m['compendium']})" if m.get("compendium") else ""))[:100],
                value=m["name"], description=m["text"][:100])
                for m in move_options(self.sheet, a.level)[:25]], a.choose_move)
        elif step == "move_spell":
            self._spell_selects(a.move_spell_options(), a.choose_move_spell, with_class=True)
        elif step == "move_choice":
            self._select("Mark", [discord.SelectOption(label=c, value=c) for c in a.move_choice_options()],
                         a.choose_move_choice)
        elif step == "multi_class":
            self._select("Class", [discord.SelectOption(label=c.title(), value=c)
                                   for c in a.multi_class_options()], a.choose_multi_class)
        elif step == "multi_move":
            offered = a.multi_move_options()
            for i in range(0, min(len(offered), 100), 25):
                self._select("Move" if len(offered) <= 25 else f"Move ({i // 25 + 1})", [discord.SelectOption(
                    label=m["name"][:100], value=m["name"], description=m["text"][:100])
                    for m in offered[i:i + 25]], a.choose_multi_move)
        elif step == "spell":
            self._spell_selects(a.spell_options(), a.choose_spell)
        else:
            confirm = discord.ui.Button(label="Level up", emoji="⭐", style=discord.ButtonStyle.success)
            confirm.callback = self._on_confirm
            self.add_item(confirm)
        if step != "stat":
            again = discord.ui.Button(label="Start over", style=discord.ButtonStyle.secondary)
            again.callback = self._on_restart
            self.add_item(again)

    def _spell_selects(self, offered, choose, with_class: bool = False) -> None:
        """25 to a dropdown, and a spell list can run past that (the Cleric's has 30;
        Expanded Spellbook offers every list), so as many dropdowns as it takes, up to four."""
        chunks = [offered[i:i + 25] for i in range(0, min(len(offered), 100), 25)]
        for i, chunk in enumerate(chunks):
            self._select(f"Spell ({i + 1}/{len(chunks)})" if len(chunks) > 1 else "Spell",
                         [discord.SelectOption(label=(f"{sp.label()} ({sp.cls})" if with_class else sp.label())[:100],
                                               value=sp.name, description=sp.text[:100]) for sp in chunk],
                         choose)

    def _select(self, placeholder: str, options, choose) -> None:
        select = discord.ui.Select(placeholder=placeholder, options=options)

        async def chosen(interaction: "discord.Interaction") -> None:
            try:
                choose(select.values[0])
            except SheetError as exc:
                await self._refresh(interaction, str(exc))
                return
            await self._refresh(interaction)

        select.callback = chosen
        self.add_item(select)

    async def _refresh(self, interaction: "discord.Interaction", error: Optional[str] = None) -> None:
        self._build()
        await interaction.response.edit_message(content=self.prompt(error), view=self)

    async def _on_restart(self, interaction: "discord.Interaction") -> None:
        self.advance = Advance(self.sheet)
        await self._refresh(interaction)

    async def _on_confirm(self, interaction: "discord.Interaction") -> None:
        error = await self._finish(self.advance.result())
        if error:
            await self._refresh(interaction, error)
            return
        self.stop()
        await interaction.response.edit_message(content=f"✅ {self.advance.summary()}.", view=None)


def _moves_text(sheet: dict, level: int) -> str:
    """The moves' text is too long for the dropdown, so the form spells out as many as a
    Discord message holds; the dropdown's descriptions start the rest."""
    lines, left = [], 1700
    options = move_options(sheet, level)
    for m in options:
        line = f"**{m['name']}**: {' '.join(m['text'].split())}"
        if len(line) + 1 > left:
            break
        lines.append(line)
        left -= len(line) + 1
    if len(lines) < len(options):
        lines.append(f"…and {len(options) - len(lines)} more, shown in the list.")
    return "\n" + "\n".join(lines)
