"""End of Session over Discord components (`?dm endsession`).

The book's three questions, answered by the table: each is a toggle anyone at the table
can flip, and Award XP gives everyone 1 XP per "yes". Alignment is each player's own to
say: a toggle per character, which only that character's player can flip, worth 1 XP to
them. No model is involved -- XP is the table's reward to decide, like the numbers on a
sheet.

Held in memory, like the character form. A restart mid-way costs typing the command
again, and the toggles are not worth a durable record.

Resolving a bond, the book's other End of Session XP, is not here yet.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Dict, List, Optional, Set

import discord

QUESTIONS = (
    "Did we learn something new and important about the world?",
    "Did we overcome a notable monster or enemy?",
    "Did we loot a memorable treasure?",
)

#: Long enough to talk each question over; short enough that a forgotten one does not
#: award XP tomorrow.
SESSION_TIMEOUT_SECONDS = 30 * 60

#: Called with the number of "yes" answers and the characters who fulfilled their alignment,
#: once Award XP is clicked. Returns the line to post, or None if it could not be saved.
Award = Callable[[int, Set[str]], Awaitable[Optional[str]]]

#: Discord's limits: five rows, five buttons in a row.
_ROWS, _PER_ROW = 5, 5


class EndOfSession(discord.ui.View):
    def __init__(self, award: Award, characters: Optional[Dict[str, int]] = None) -> None:
        """``characters``: each character with an alignment -> the Discord user who plays
        them, the only one who may say they fulfilled it."""
        super().__init__(timeout=SESSION_TIMEOUT_SECONDS)
        self.answers: List[bool] = [False] * len(QUESTIONS)
        room = (_ROWS - len(QUESTIONS)) * _PER_ROW - 1  # the Award button takes one
        self.characters = dict(list((characters or {}).items())[:room])
        self.fulfilled: Set[str] = set()
        self._award = award
        self._build()

    def prompt(self) -> str:
        lines = ["**End of session.** Answer as a group — each yes is 1 XP for everyone:"]
        lines += [f"{'✅' if yes else '⬜'} {q}" for q, yes in zip(QUESTIONS, self.answers)]
        if self.characters:
            lines.append("Each player: did your character act on their alignment? (+1 XP to them)")
            lines += [f"{'✅' if a in self.fulfilled else '⬜'} {a}" for a in self.characters]
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

    async def _on_award(self, interaction: "discord.Interaction") -> None:
        # Stop first: two people clicking Award at once must not award twice.
        if self.is_finished():
            await interaction.response.send_message("XP is already awarded.", ephemeral=True)
            return
        self.stop()
        said = await self._award(sum(self.answers), set(self.fulfilled))
        await interaction.response.edit_message(
            content=self.prompt() + ("\n" + said if said else "\nThat could not be saved."), view=None
        )
