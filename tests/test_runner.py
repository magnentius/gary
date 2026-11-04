"""Performing effects (ARCHITECTURE.md §4, §10).

Covers the two things that only go wrong mid-session: the model looping on its own
output, and a model failure leaving the channel silent.
"""

from __future__ import annotations

import pytest

from gary.adapters.runner import STALL_MESSAGE, EffectRunner
from gary.fsm import PromptModel, RollPrompt, SendMessage
from gary.ports import EchoNarrator, NarrationResult

CHANNEL = 555


class FakeSender:
    def __init__(self) -> None:
        self.sent = []
        self.buttons = []

    async def send(self, channel_id: int, text: str, prompt=None) -> None:
        self.sent.append((channel_id, text))
        if prompt is not None:
            self.buttons.append((text, prompt))


class ScriptedNarrator:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = []

    async def narrate(self, *, channel_id, kind, payload):
        self.calls.append((kind, payload))
        if self.results:
            return self.results.pop(0)
        return NarrationResult(text="...")


class ExplodingNarrator:
    async def narrate(self, *, channel_id, kind, payload):
        raise RuntimeError("model unavailable")


def runner(sender=None, narrator=None, max_depth=4) -> EffectRunner:
    return EffectRunner(sender or FakeSender(), narrator or EchoNarrator(), max_depth)


# -- basic dispatch --------------------------------------------------------------------------


async def test_send_message_reaches_the_channel():
    sender = FakeSender()
    await runner(sender).run([SendMessage(CHANNEL, "The door opens.")])
    assert sender.sent == [(CHANNEL, "The door opens.")]


async def test_a_roll_request_keeps_its_button():
    sender = FakeSender()
    prompt = RollPrompt("C1")
    await runner(sender).run([SendMessage(CHANNEL, "**Kethra** — roll", prompt=prompt)])
    assert sender.buttons == [("**Kethra** — roll", prompt)]


async def test_a_hoisted_roll_request_keeps_its_button_too():
    """open_check's request is hoisted ahead of the prose; the button must come with it."""
    sender = FakeSender()
    narrator = ScriptedNarrator(NarrationResult(
        text="You crouch by the lock.",
        effects=[SendMessage(CHANNEL, "**Kethra** — roll", prompt=RollPrompt("C1"))],
    ))
    await runner(sender, narrator).run([PromptModel(CHANNEL, "player_action", {})])
    assert sender.buttons == [("**Kethra** — roll", RollPrompt("C1"))]


async def test_unknown_effect_type_is_logged_not_fatal():
    class Weird:
        pass

    await runner().run([Weird()])  # must not raise


# -- narration -------------------------------------------------------------------------------


async def test_narration_is_sent():
    sender = FakeSender()
    narrator = ScriptedNarrator(NarrationResult(text="The lock gives."))
    await runner(sender, narrator).run(
        [PromptModel(CHANNEL, "check_outcome", {"succeeded": True})]
    )
    assert sender.sent == [(CHANNEL, "The lock gives.")]


async def test_payload_reaches_the_narrator_unchanged():
    narrator = ScriptedNarrator(NarrationResult(text="ok"))
    payload = {"succeeded": True, "dc": 15, "margin": 2}
    await runner(narrator=narrator).run([PromptModel(CHANNEL, "check_outcome", payload)])
    assert narrator.calls == [("check_outcome", payload)]


async def test_model_failure_never_leaves_the_channel_silent():
    """A dead-looking bot is worse than an awkward line of prose."""
    sender = FakeSender()
    r = runner(sender, ExplodingNarrator())
    await r.run([PromptModel(CHANNEL, "player_action", {})])

    assert sender.sent == [(CHANNEL, STALL_MESSAGE)]
    assert r.stats.narrator_failures == 1


async def test_empty_narration_with_no_effects_still_says_something():
    sender = FakeSender()
    await runner(sender, ScriptedNarrator(NarrationResult(text="  "))).run(
        [PromptModel(CHANNEL, "player_action", {})]
    )
    assert sender.sent == [(CHANNEL, STALL_MESSAGE)]


async def test_empty_narration_with_effects_stays_quiet():
    """The model called a tool instead of speaking; the tool's output is the response."""
    sender = FakeSender()
    narrator = ScriptedNarrator(
        NarrationResult(text="", effects=[SendMessage(CHANNEL, "**Kethra** — roll for stealth (check).")])
    )
    await runner(sender, narrator).run([PromptModel(CHANNEL, "player_action", {})])
    assert sender.sent == [(CHANNEL, "**Kethra** — roll for stealth (check).")]


# -- recursion bound ---------------------------------------------------------------------------


async def test_narration_effects_are_performed():
    """open_check produces a SendMessage asking the player to roll."""
    sender = FakeSender()
    narrator = ScriptedNarrator(
        NarrationResult(text="You crouch by the lock.",
                        effects=[SendMessage(CHANNEL, "**Kethra** — roll for stealth (check).")]),
    )
    await runner(sender, narrator).run([PromptModel(CHANNEL, "player_action", {})])

    # The request to roll goes first: it is a stage direction for the prose that follows, not a
    # footnote to it. See the runner's comment for the rest that made this obvious.
    assert [t for _, t in sender.sent] == [
        "**Kethra** — roll for stealth (check).",
        "You crouch by the lock.",
    ]


async def test_runaway_model_is_cut_off():
    """A model that keeps prompting itself would otherwise burn quota until it ran out."""
    sender = FakeSender()

    class LoopingNarrator:
        def __init__(self):
            self.count = 0

        async def narrate(self, *, channel_id, kind, payload):
            self.count += 1
            return NarrationResult(
                text=f"turn {self.count}",
                effects=[PromptModel(channel_id, "system_note", {})],
            )

    narrator = LoopingNarrator()
    r = EffectRunner(sender, narrator, max_depth=3)
    await r.run([PromptModel(CHANNEL, "player_action", {})])

    assert narrator.count == 4  # depths 0..3, then refused
    assert r.stats.depth_exceeded == 1


async def test_bounded_nesting_completes_normally():
    sender = FakeSender()
    narrator = ScriptedNarrator(
        NarrationResult(text="one", effects=[PromptModel(CHANNEL, "system_note", {})]),
        NarrationResult(text="two"),
    )
    r = EffectRunner(sender, narrator, max_depth=3)
    await r.run([PromptModel(CHANNEL, "player_action", {})])

    assert [t for _, t in sender.sent] == ["one", "two"]
    assert r.stats.depth_exceeded == 0


# -- the echo narrator is a real dev tool, not just a stub --------------------------------------


async def test_echo_narrator_reports_outcomes_without_a_model():
    """Lets the whole Discord path be proven with no API key."""
    sender = FakeSender()
    await runner(sender, EchoNarrator()).run(
        [
            PromptModel(
                CHANNEL,
                "check_outcome",
                {
                    "actor": "Kethra",
                    "move": "Volley",
                    "result": "7-9 (weak hit)",
                    "what_it_means": "Choose one.",
                    "player_chose": ["Take what you can get: -1d6 damage."],
                },
            )
        ]
    )
    assert sender.sent[0][1] == (
        "[7-9 (weak hit)] Kethra, Volley: Choose one.\n"
        "Chose: Take what you can get: -1d6 damage."
    )


@pytest.mark.asyncio
async def test_non_message_effects_keep_their_place():
    """Only channel posts are hoisted. A PromptModel among the effects spawns a nested
    turn, and running it early would put that turn's narration before this one's."""
    sender = FakeSender()
    narrator = ScriptedNarrator(
        NarrationResult(
            text="Nothing happens.",
            effects=[
                SendMessage(CHANNEL, "**Kethra** — roll for stealth (check)."),
                PromptModel(CHANNEL, "system_note", {"error": "bad key"}),
            ],
        ),
        NarrationResult(text="(the DM reconsiders)"),
    )
    await runner(sender, narrator).run([PromptModel(CHANNEL, "player_action", {})])

    assert [t for _, t in sender.sent] == [
        "**Kethra** — roll for stealth (check).",   # hoisted
        "Nothing happens.",                    # this turn's prose
        "(the DM reconsiders)",                # the nested turn, still last
    ]


def test_ask_returns_the_text_instead_of_posting_it():
    """A proposal is shown with an offer attached and written only if it is taken, so the
    one thing `ask` must not do is what `run` does -- put it in the channel as narration."""
    import asyncio

    from gary.adapters.runner import EffectRunner
    from gary.fsm import PromptModel
    from gary.ports import NarrationResult

    sent = []

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Narrator:
        async def narrate(self, *, channel_id, kind, payload):
            return NarrationResult(text="  The Fen Marches.  ")

    runner = EffectRunner(_Sender(), _Narrator())
    answer = asyncio.run(
        runner.ask(PromptModel(channel_id=7, kind="propose_setting", payload={}))
    )
    assert answer == "The Fen Marches."
    assert sent == []
    assert runner.stats.narrated == 1
    assert runner.stats.sent == 0


def test_ask_drops_effects_rather_than_deciding_while_asking():
    import asyncio

    from gary.adapters.runner import EffectRunner
    from gary.fsm import PromptModel, SendMessage
    from gary.ports import NarrationResult

    sent = []

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Narrator:
        async def narrate(self, *, channel_id, kind, payload):
            return NarrationResult(
                text="The Fen Marches.",
                effects=[SendMessage(channel_id=7, text="!game coins +5")],
            )

    runner = EffectRunner(_Sender(), _Narrator())
    answer = asyncio.run(
        runner.ask(PromptModel(channel_id=7, kind="propose_setting", payload={}))
    )
    assert answer == "The Fen Marches."
    assert sent == []


def test_ask_answers_empty_when_the_model_fails_and_stays_silent():
    """`_narrate` posts a stall on failure because a silent channel reads as a dead bot.
    `ask` must not: its caller has a better thing to say, and two messages would be one
    apology too many."""
    import asyncio

    from gary.adapters.runner import EffectRunner
    from gary.fsm import PromptModel

    sent = []

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Narrator:
        async def narrate(self, *, channel_id, kind, payload):
            raise RuntimeError("503")

    runner = EffectRunner(_Sender(), _Narrator())
    answer = asyncio.run(
        runner.ask(PromptModel(channel_id=7, kind="propose_setting", payload={}))
    )
    assert answer == ""
    assert sent == []
    assert runner.stats.narrator_failures == 1


async def test_hp_changes_are_journalled_so_they_undo():
    from gary.fsm import ChangeHp
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=lambda: 0.0)
    store.apply([
        Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {"Goblin 1": {"hp": 3, "max_hp": 3}}}),
        Mutation(MutationKind.SHEET, "Kethra", {"sheet": {"name": "Kethra", "hp": 19, "max_hp": 19}}),
    ], reason="setup")
    r = EffectRunner(FakeSender(), EchoNarrator(), store=store)

    await r.run([ChangeHp(CHANNEL, "monster", "Goblin 1", -1),
                 ChangeHp(CHANNEL, "character", "Kethra", 16)])
    state = store.state()
    assert state["monsters"]["Goblin 1"]["hp"] == -1
    assert state["sheets"]["Kethra"] == {"name": "Kethra", "hp": 16, "max_hp": 19}

    store.undo_last()
    assert store.state()["sheets"]["Kethra"]["hp"] == 19


async def test_spending_ammo_is_journalled():
    from gary.fsm import SpendAmmo
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=lambda: 0.0)
    store.apply([Mutation(MutationKind.SHEET, "Kethra", {"sheet": {"name": "Kethra", "ammo": 3}})],
                reason="setup")
    await EffectRunner(FakeSender(), EchoNarrator(), store=store).run([SpendAmmo(CHANNEL, "Kethra", 2)])
    assert store.state()["sheets"]["Kethra"]["ammo"] == 2
    store.undo_last()
    assert store.state()["sheets"]["Kethra"]["ammo"] == 3


async def test_sheet_updates_are_journalled():
    from gary.fsm import UpdateSheet
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=lambda: 0.0)
    store.apply([Mutation(MutationKind.SHEET, "Thorin", {"sheet": {"name": "Thorin", "hp": 0}})],
                reason="setup")
    await EffectRunner(FakeSender(), EchoNarrator(), store=store).run(
        [UpdateSheet(CHANNEL, "Thorin", {"dead": True})])
    assert store.state()["sheets"]["Thorin"] == {"name": "Thorin", "hp": 0, "dead": True}
    store.undo_last()
    assert "dead" not in store.state()["sheets"]["Thorin"]


async def test_a_blow_wakes_a_sleeper_and_a_summon_at_zero_is_gone(clock):
    """The runner applies what a blow breaks: Sleep ends on pain; a summoned creature at 0 HP is dismissed."""
    from gary.fsm import ChangeHp, ConditionMonster
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {"Goblin 1": {
        "kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"}}}),
        Mutation(MutationKind.SHEET, "Mae", {"sheet": {"name": "Mae", "hp": 10, "ongoing": [
            {"spell": "Summon Monster", "target": "Vex"}]}}),
        Mutation(MutationKind.SHEET, "Vex", {"sheet": {"name": "Vex", "hp": 1, "summoned_by": "Mae",
                                                       "summoned_spell": "Summon Monster"}})], reason="setup")
    sender = FakeSender()
    r = EffectRunner(sender, EchoNarrator(), store=store)
    await r.run([ConditionMonster(CHANNEL, "Goblin 1", ({"kind": "asleep", "spell": "Sleep", "caster": "Mae"},)),
                 ChangeHp(CHANNEL, "monster", "Goblin 1", 2), ChangeHp(CHANNEL, "character", "Vex", 0)])
    state = store.state()
    assert state["monsters"]["Goblin 1"]["conditions"] == []
    assert "Vex" not in state["sheets"] and state["sheets"]["Mae"]["ongoing"] == []
    assert any("no longer asleep" in text for _, text in sender.sent)


async def test_bless_ends_with_the_battle(clock):
    """"+1 ongoing so long as battle continues": once the last creature falls the Cleric stops paying."""
    from gary.fsm import ChangeHp
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {
        "Goblin 1": {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"},
        "Goblin 2": {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"}}}),
        Mutation(MutationKind.SHEET, "Thorin", {"sheet": {"name": "Thorin", "hp": 10, "ongoing": [
            {"spell": "Bless", "target": "Dorn"}]}})], reason="setup")
    sender = FakeSender()
    r = EffectRunner(sender, EchoNarrator(), store=store)
    await r.run([ChangeHp(CHANNEL, "monster", "Goblin 1", 0)])
    assert store.state()["sheets"]["Thorin"]["ongoing"] == [{"spell": "Bless", "target": "Dorn"}]  # one still up
    await r.run([ChangeHp(CHANNEL, "monster", "Goblin 2", -1)])
    assert store.state()["sheets"]["Thorin"]["ongoing"] == []
    assert any("The battle is over, and Thorin's Bless with it." in text for _, text in sender.sent)


async def test_a_creatures_armor_can_be_lowered_and_undone(clock):
    from gary.fsm import ArmorMonster
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {
        "Orc 1": {"kind": "Orc", "hp": 6, "max_hp": 6, "armor": 2, "damage": "d8"}}})], reason="setup")
    await EffectRunner(FakeSender(), EchoNarrator(), store=store).run([ArmorMonster(CHANNEL, "Orc 1", 1)])
    assert store.state()["monsters"]["Orc 1"]["armor"] == 1
    store.undo_last()
    assert store.state()["monsters"]["Orc 1"]["armor"] == 2


async def test_a_barbarians_challenge_ends_with_the_battle(clock):
    from gary.fsm import ChangeHp
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {
        "Goblin 1": {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"}}}),
        Mutation(MutationKind.SHEET, "Gorm", {"sheet": {"name": "Gorm", "hp": 20, "challenged": ["Goblin 1"]}})],
        reason="setup")
    await EffectRunner(FakeSender(), EchoNarrator(), store=store).run([ChangeHp(CHANNEL, "monster", "Goblin 1", 0)])
    assert store.state()["sheets"]["Gorm"]["challenged"] == []
