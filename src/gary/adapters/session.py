"""End of Session over Discord components (`?dm endsession`).

The book's three questions, answered by the table: each is a toggle anyone at the table
can flip, and Award XP gives everyone 1 XP per "yes". No model is involved -- XP is the
table's reward to decide, like the numbers on a sheet.

Held in memory, like the character form. A restart mid-way costs typing the command
again, and the toggles are not worth a durable record.

Bonds and alignment, the book's other End of Session XP, come when those are tracked.
"""

from __future__ import annotations

from typing import Awaitable, Callable, List, Optional

import discord

QUESTIONS = (
    "Did we learn something new and important about the world?",
    "Did we overcome a notable monster or enemy?",
    "Did we loot a memorable treasure?",
)

#: Long enough to talk each question over; short enough that a forgotten one does not
#: award XP tomorrow.
SESSION_TIMEOUT_SECONDS = 30 * 60

#: Called with the number of "yes" answers once Award XP is clicked. Returns the line to
#: post, or None if it could not be saved.
Award = Callable[[int], Awaitable[Optional[str]]]


class EndOfSession(discord.ui.View):
    def __init__(self, award: Award) -> None:
        super().__init__(timeout=SESSION_TIMEOUT_SECONDS)
        self.answers: List[bool] = [False] * len(QUESTIONS)
        self._award = award
        self._build()

    def prompt(self) -> str:
        lines = ["**End of session.** Answer as a group — each yes is 1 XP for everyone:"]
        lines += [f"{'✅' if yes else '⬜'} {q}" for q, yes in zip(QUESTIONS, self.answers)]
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
        award = discord.ui.Button(label=f"Award {sum(self.answers)} XP", emoji="⭐",
                                  style=discord.ButtonStyle.primary, row=len(QUESTIONS))
        award.callback = self._on_award
        self.add_item(award)

    def _toggler(self, index: int):
        async def toggle(interaction: "discord.Interaction") -> None:
            self.answers[index] = not self.answers[index]
            self._build()
            await interaction.response.edit_message(content=self.prompt(), view=self)

        return toggle

    async def _on_award(self, interaction: "discord.Interaction") -> None:
        # Stop first: two people clicking Award at once must not award twice.
        if self.is_finished():
            await interaction.response.send_message("XP is already awarded.", ephemeral=True)
            return
        self.stop()
        said = await self._award(sum(self.answers))
        await interaction.response.edit_message(
            content=self.prompt() + ("\n" + said if said else "\nThat could not be saved."), view=None
        )
