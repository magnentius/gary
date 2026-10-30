"""End of Session over Discord components (`?dm endsession`).

The book's three questions, answered by the table: each is a toggle anyone at the table
can flip, and Award XP gives everyone 1 XP per "yes". Alignment is each player's own to
say: a toggle per character, which only that character's player can flip, worth 1 XP to
them. A player may resolve one bond: they pick it privately, and the companion it names
must agree (a button only that companion's player can press); it is worth 1 XP, and the
bond is cleared to be rewritten. No model is involved -- XP is the table's reward to
decide, like the numbers on a sheet.

Held in memory, like the character form. A restart mid-way costs typing the command
again, and the toggles are not worth a durable record.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Dict, List, Optional, Set

import discord

from .. import bonds as bond_rules

QUESTIONS = (
    "Did we learn something new and important about the world?",
    "Did we overcome a notable monster or enemy?",
    "Did we loot a memorable treasure?",
)

#: Long enough to talk each question over; short enough that a forgotten one does not
#: award XP tomorrow.
SESSION_TIMEOUT_SECONDS = 30 * 60

#: Called with the number of "yes" answers, the characters who fulfilled their alignment,
#: and the bonds resolved (character -> bond line), once Award XP is clicked. Returns the
#: line to post, or None if it could not be saved.
Award = Callable[[int, Set[str], Dict[str, str]], Awaitable[Optional[str]]]

#: Discord's limits: five rows, five buttons in a row.
_ROWS, _PER_ROW = 5, 5


class EndOfSession(discord.ui.View):
    def __init__(
        self, award: Award, characters: Optional[Dict[str, int]] = None,
        players: Optional[Dict[int, str]] = None, bonds: Optional[Dict[str, list]] = None,
    ) -> None:
        """``characters``: each character with an alignment -> the Discord user who plays
        them, the only one who may say they fulfilled it. ``players``: Discord user ->
        character, for everyone at the table. ``bonds``: character -> their bonds."""
        super().__init__(timeout=SESSION_TIMEOUT_SECONDS)
        self.answers: List[bool] = [False] * len(QUESTIONS)
        self.players = dict(players or {})
        self.bonds = {a: list(b) for a, b in (bonds or {}).items() if b}
        # The Award button takes one place, and Resolve a bond another when anyone has one.
        room = (_ROWS - len(QUESTIONS)) * _PER_ROW - 1 - (1 if self.bonds else 0)
        self.characters = dict(list((characters or {}).items())[:room])
        self.fulfilled: Set[str] = set()
        self.resolved: Dict[str, str] = {}  # character -> the bond line the companion agreed to
        self.message: Optional["discord.Message"] = None  # set by whoever posts the view
        self._award = award
        self._build()

    def prompt(self) -> str:
        lines = ["**End of session.** Answer as a group — each yes is 1 XP for everyone:"]
        lines += [f"{'✅' if yes else '⬜'} {q}" for q, yes in zip(QUESTIONS, self.answers)]
        if self.characters:
            lines.append("Each player: did your character act on their alignment? (+1 XP to them)")
            lines += [f"{'✅' if a in self.fulfilled else '⬜'} {a}" for a in self.characters]
        for actor, line in self.resolved.items():
            bond = next(b for b in self.bonds[actor] if b["line"] == line)
            lines.append(f"🤝 {actor} resolves “{bond_rules.written(bond)}” (+1 XP to them)")
        return "\n".join(lines)

    def _build(self) -> None:
        self.clear_items()
        for i, question in enumerate(QUESTIONS):
            button = discord.ui.Button(
                label=("Yes: " if self.answers[i] else "No: ") + question[len("Did we "):-1],
                style=discord.ButtonStyle.success if self.answers[i] else discord.ButtonStyle.secondary,
                row=i,
            )
            button.callback = self._toggler(i)
            self.add_item(button)
        for i, actor in enumerate(self.characters):
            done = actor in self.fulfilled
            button = discord.ui.Button(
                label=f"{actor}: {'acted on alignment' if done else 'alignment?'}"[:80],
                style=discord.ButtonStyle.success if done else discord.ButtonStyle.secondary,
                row=len(QUESTIONS) + i // _PER_ROW,
            )
            button.callback = self._alignment_toggler(actor)
            self.add_item(button)
        if self.bonds:
            resolve = discord.ui.Button(label="Resolve a bond", emoji="🤝",
                                        style=discord.ButtonStyle.secondary, row=_ROWS - 1)
            resolve.callback = self._on_resolve
            self.add_item(resolve)
        award = discord.ui.Button(label=f"Award {sum(self.answers)} XP each", emoji="⭐",
                                  style=discord.ButtonStyle.primary, row=_ROWS - 1)
        award.callback = self._on_award
        self.add_item(award)

    def _toggler(self, index: int):
        async def toggle(interaction: "discord.Interaction") -> None:
            self.answers[index] = not self.answers[index]
            self._build()
            await interaction.response.edit_message(content=self.prompt(), view=self)

        return toggle

    def _alignment_toggler(self, actor: str):
        async def toggle(interaction: "discord.Interaction") -> None:
            if interaction.user.id != self.characters[actor]:
                await interaction.response.send_message(
                    f"Whether {actor} acted on their alignment is their player's to say.", ephemeral=True)
                return
            self.fulfilled ^= {actor}
            self._build()
            await interaction.response.edit_message(content=self.prompt(), view=self)

        return toggle

    async def _on_resolve(self, interaction: "discord.Interaction") -> None:
        """Privately: which of your bonds is resolved? Then the companion is asked."""
        actor = self.players.get(interaction.user.id)
        if actor not in self.bonds:
            await interaction.response.send_message("You have no bonds to resolve.", ephemeral=True)
            return
        if actor in self.resolved:
            await interaction.response.send_message("You have resolved a bond this session already.",
                                                    ephemeral=True)
            return
        picker = discord.ui.View(timeout=SESSION_TIMEOUT_SECONDS)
        select = discord.ui.Select(placeholder="The bond that is resolved", options=[
            discord.SelectOption(label=bond_rules.written(b)[:100], value=str(i))
            for i, b in enumerate(self.bonds[actor])])

        async def chosen(pick: "discord.Interaction") -> None:
            bond = self.bonds[actor][int(select.values[0])]
            await pick.response.edit_message(content=f"Asking {bond['with']}'s player.", view=None)
            await self.ask_companion(pick.channel, actor, bond)

        select.callback = chosen
        picker.add_item(select)
        await interaction.response.send_message(
            "A bond is resolved when it's completely explored or no longer relevant. Which one?",
            view=picker, ephemeral=True)

    async def ask_companion(self, channel, actor: str, bond: dict) -> None:
        companion = bond["with"]
        owner = next((uid for uid, a in self.players.items() if a == companion), None)
        agree = discord.ui.View(timeout=SESSION_TIMEOUT_SECONDS)
        button = discord.ui.Button(label=f"{companion} agrees"[:80], emoji="🤝",
                                   style=discord.ButtonStyle.success)

        async def agreed(interaction: "discord.Interaction") -> None:
            if interaction.user.id != owner:
                await interaction.response.send_message(f"That's {companion}'s player to say.",
                                                        ephemeral=True)
                return
            if self.is_finished():
                await interaction.response.send_message("XP is already awarded.", ephemeral=True)
                return
            self.resolved[actor] = bond["line"]
            await interaction.response.edit_message(
                content=f"🤝 {companion} agrees: “{bond_rules.written(bond)}” is resolved.", view=None)
            if self.message is not None:
                await self.message.edit(content=self.prompt(), view=self)

        button.callback = agreed
        agree.add_item(button)
        await channel.send(f"**{actor}** feels this bond is resolved: “{bond_rules.written(bond)}”. "
                           f"**{companion}**, do you agree?", view=agree)

    async def _on_award(self, interaction: "discord.Interaction") -> None:
        # Stop first: two people clicking Award at once must not award twice.
        if self.is_finished():
            await interaction.response.send_message("XP is already awarded.", ephemeral=True)
            return
        self.stop()
        said = await self._award(sum(self.answers), set(self.fulfilled), dict(self.resolved))
        await interaction.response.edit_message(
            content=self.prompt() + ("\n" + said if said else "\nThat could not be saved."), view=None
        )
