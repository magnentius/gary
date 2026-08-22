"""Performing effects (ARCHITECTURE.md §4, §10).

Covers the two things that only go wrong mid-session: the model looping on its own
output, and a model failure leaving the channel silent.
"""

from __future__ import annotations

import pytest

from gary.adapters.runner import STALL_MESSAGE, EffectRunner
from gary.fsm import AskPlayer, LogDrop, PromptModel, SendMessage
from gary.ports import EchoNarrator, NarrationResult

CHANNEL = 555


class FakeSender:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, channel_id: int, text: str) -> None:
        self.sent.append((channel_id, text))


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


async def test_ask_player_is_sent_like_any_message():
    sender = FakeSender()
    await runner(sender).run([AskPlayer(CHANNEL, "What was the total?")])
    assert sender.sent == [(CHANNEL, "What was the total?")]


async def test_log_drop_sends_nothing():
    """Dropping is silent at the table by design -- it is the common case."""
    sender = FakeSender()
    r = runner(sender)
    await r.run([LogDrop(CHANNEL, "irrelevant", "help text")])
    assert sender.sent == []
    assert r.stats.drops_by_status == {"irrelevant": 1}


async def test_drop_counters_track_parse_failures():
    """A rising 'unknown' rate is how an Avrae format change announces itself (§10)."""
    r = runner()
    await r.run(
        [
            LogDrop(CHANNEL, "unknown", "no total"),
            LogDrop(CHANNEL, "unknown", "no total"),
            LogDrop(CHANNEL, "irrelevant", "help"),
        ]
    )
    assert r.stats.drops_by_status == {"unknown": 2, "irrelevant": 1}


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
        NarrationResult(text="", effects=[SendMessage(CHANNEL, "`!check stealth`")])
    )
    await runner(sender, narrator).run([PromptModel(CHANNEL, "player_action", {})])
    assert sender.sent == [(CHANNEL, "`!check stealth`")]


# -- recursion bound ---------------------------------------------------------------------------


async def test_narration_effects_are_performed():
    """open_check produces a SendMessage carrying the Avrae command."""
    sender = FakeSender()
    narrator = ScriptedNarrator(
        NarrationResult(text="You crouch by the lock.",
                        effects=[SendMessage(CHANNEL, "**Kethra** — run `!check stealth`")]),
    )
    await runner(sender, narrator).run([PromptModel(CHANNEL, "player_action", {})])

    # The command goes first: it is a stage direction for the prose that follows, not a
    # footnote to it. See the runner's comment for the rest that made this obvious.
    assert [t for _, t in sender.sent] == [
        "**Kethra** — run `!check stealth`",
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
    """Lets the whole Discord path be proven against live Avrae with no API key."""
    sender = FakeSender()
    await runner(sender, EchoNarrator()).run(
        [
            PromptModel(
                CHANNEL,
                "check_outcome",
                {
                    "succeeded": True,
                    "actor": "Kethra",
                    "roll_total": 17,
                    "dc": 15,
                    "established_consequence": "The tumblers give.",
                },
            )
        ]
    )
    text = sender.sent[0][1]
    assert "SUCCESS" in text and "17" in text and "15" in text
    assert "The tumblers give." in text


@pytest.mark.asyncio
async def test_non_message_effects_keep_their_place():
    """Only channel posts are hoisted. A PromptModel among the effects spawns a nested
    turn, and running it early would put that turn's narration before this one's."""
    sender = FakeSender()
    narrator = ScriptedNarrator(
        NarrationResult(
            text="Nothing happens.",
            effects=[
                SendMessage(CHANNEL, "**Kethra** — run `!check stealth`"),
                PromptModel(CHANNEL, "system_note", {"error": "bad key"}),
            ],
        ),
        NarrationResult(text="(the DM reconsiders)"),
    )
    await runner(sender, narrator).run([PromptModel(CHANNEL, "player_action", {})])

    assert [t for _, t in sender.sent] == [
        "**Kethra** — run `!check stealth`",   # hoisted
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
    answer = asyncio.get_event_loop().run_until_complete(
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
    answer = asyncio.get_event_loop().run_until_complete(
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
    answer = asyncio.get_event_loop().run_until_complete(
        runner.ask(PromptModel(channel_id=7, kind="propose_setting", payload={}))
    )
    assert answer == ""
    assert sent == []
    assert runner.stats.narrator_failures == 1
