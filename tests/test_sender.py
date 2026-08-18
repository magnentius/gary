"""Message splitting and pacing (ARCHITECTURE.md §10)."""

from __future__ import annotations

import asyncio

import pytest

from gary.adapters.sender import DISCORD_LIMIT, ChannelSender, split_message


class FakeTransport:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, channel_id: int, text: str) -> None:
        self.sent.append((channel_id, text))


# -- splitting ------------------------------------------------------------------------------


def test_short_text_is_one_message():
    assert split_message("A door creaks open.") == ["A door creaks open."]


def test_empty_text_sends_nothing():
    """Discord rejects empty messages; returning [] is how the caller skips."""
    assert split_message("") == []
    assert split_message("   \n  ") == []


def test_every_chunk_respects_the_limit():
    text = " ".join(f"word{i}" for i in range(3000))
    for chunk in split_message(text):
        assert len(chunk) <= DISCORD_LIMIT


def test_nothing_is_lost_in_the_split():
    text = "\n\n".join(f"Paragraph {i}. " + "x" * 300 for i in range(20))
    rejoined = "".join(split_message(text)).replace("\n", "")
    assert rejoined.replace(" ", "") == text.replace("\n", "").replace(" ", "")


def test_paragraphs_are_preferred_break_points():
    a, b = "A" * 1500, "B" * 1000
    chunks = split_message(f"{a}\n\n{b}")
    assert len(chunks) == 2
    assert chunks[0] == a
    assert chunks[1] == b


def test_falls_back_to_lines_then_sentences_then_words():
    lines = split_message("\n".join("L" * 200 for _ in range(20)))
    assert all(len(c) <= DISCORD_LIMIT for c in lines)

    sentences = split_message(". ".join("S" * 150 for _ in range(30)) + ".")
    assert all(len(c) <= DISCORD_LIMIT for c in sentences)

    words = split_message(" ".join("W" * 40 for _ in range(200)))
    assert all(len(c) <= DISCORD_LIMIT for c in words)


def test_unbreakable_run_is_hard_cut():
    """A 5000-character URL has no break point; refusing to send is worse than cutting."""
    chunks = split_message("x" * 5000)
    assert len(chunks) == 3
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)


def test_code_fences_are_closed_and_reopened_across_a_split():
    """An unterminated fence renders the rest of one message as code and the next as text."""
    body = "\n".join(f"line {i} " + "y" * 60 for i in range(60))
    chunks = split_message(f"```\n{body}\n```")

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, "chunk leaves a fence open"
        assert len(chunk) <= DISCORD_LIMIT


def test_inline_backticks_in_a_short_command_are_untouched():
    """The Avrae command post is the most common message Gary sends."""
    text = "**Kethra** — run this:\n`!check sleightOfHand`"
    assert split_message(text) == [text]


# -- pacing ---------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_first_send_is_immediate():
    clock, transport, slept = FakeClock(), FakeTransport(), []

    async def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    sender = ChannelSender(transport, interval_seconds=1.0, clock=clock, sleep=sleep)
    await sender.send(555, "hello")

    assert slept == []
    assert transport.sent == [(555, "hello")]


@pytest.mark.asyncio
async def test_consecutive_sends_are_paced():
    clock, transport, slept = FakeClock(), FakeTransport(), []

    async def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    sender = ChannelSender(transport, interval_seconds=1.0, clock=clock, sleep=sleep)
    await sender.send(555, "one")
    await sender.send(555, "two")
    await sender.send(555, "three")

    assert slept == [1.0, 1.0]
    assert [t for _, t in transport.sent] == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_elapsed_time_counts_against_the_interval():
    clock, transport, slept = FakeClock(), FakeTransport(), []

    async def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    sender = ChannelSender(transport, interval_seconds=1.0, clock=clock, sleep=sleep)
    await sender.send(555, "one")
    clock.now += 0.75  # the turn took a while
    await sender.send(555, "two")

    assert slept == [pytest.approx(0.25)]


@pytest.mark.asyncio
async def test_channels_do_not_pace_each_other():
    """Two tables in two channels must not slow each other down."""
    clock, transport, slept = FakeClock(), FakeTransport(), []

    async def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    sender = ChannelSender(transport, interval_seconds=1.0, clock=clock, sleep=sleep)
    await sender.send(555, "table one")
    await sender.send(666, "table two")

    assert slept == []


@pytest.mark.asyncio
async def test_a_long_narration_is_not_interleaved_with_another_turn():
    """The lock spans every part, so two turns cannot alternate mid-paragraph."""
    clock, transport = FakeClock(), FakeTransport()

    async def sleep(seconds):
        clock.now += seconds

    sender = ChannelSender(transport, interval_seconds=0.0, clock=clock, sleep=sleep)
    long_text = "\n\n".join("A" * 900 for _ in range(3))

    await asyncio.gather(
        sender.send(555, long_text),
        sender.send(555, "SHORT"),
    )

    texts = [t for _, t in transport.sent]
    short_at = texts.index("SHORT")
    assert short_at == 0 or short_at == len(texts) - 1, texts


@pytest.mark.asyncio
async def test_oversized_text_is_split_before_sending():
    clock, transport = FakeClock(), FakeTransport()

    async def sleep(seconds):
        clock.now += seconds

    sender = ChannelSender(transport, interval_seconds=0.0, clock=clock, sleep=sleep)
    await sender.send(555, "\n\n".join("z" * 900 for _ in range(4)))

    assert len(transport.sent) > 1
    assert all(len(t) <= DISCORD_LIMIT for _, t in transport.sent)


@pytest.mark.asyncio
async def test_empty_send_reaches_no_transport():
    sender = ChannelSender(FakeTransport(), interval_seconds=0.0)
    await sender.send(555, "   ")
    assert sender._transport.sent == []
