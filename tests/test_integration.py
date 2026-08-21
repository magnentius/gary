"""End-to-end through every layer except the Gateway socket itself.

Player speaks → Director → open_check → command posted → Avrae answers → parse →
correlate → resolve → narrate. The unit tests prove each piece; this proves they are
wired to each other, which is where a system like this usually breaks.

Only Discord's transport is faked. The parser sees a real fixture payload, the registry
does real correlation, and the resolver does real arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gary.adapters.discord_bot import message_to_dict
from gary.adapters.runner import EffectRunner
from gary.adapters.sender import ChannelSender
from gary.fsm import ChannelState, Director
from gary.models import CheckKind
from gary.parser.avrae import parse_embed
from gary.ports import EchoNarrator, NarrationResult
from gary.registry import PendingCheckRegistry
from gary.state.backend import FileCheckBackend

FIXTURES = Path(__file__).parent / "fixtures" / "avrae"
CHANNEL = 555
ALICE = 111


class FakeTransport:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, channel_id: int, text: str) -> None:
        self.sent.append((channel_id, text))


class DirectorNarrator:
    """Stands in for the Gemini adapter.

    Its only job here is what the real one will do: translate a decision to call
    ``open_check`` into a Director call, and return the resulting effects.
    """

    def __init__(self, director: Director) -> None:
        self.director = director
        self.open_next = None
        self.seen = []

    async def narrate(self, *, channel_id, kind, payload):
        self.seen.append((kind, payload))
        if kind == "player_action" and self.open_next is not None:
            spec, self.open_next = self.open_next, None
            return NarrationResult(
                text="You crouch by the lock, picks in hand.",
                effects=self.director.open_check(
                    channel_id=channel_id, user_id=payload["speaker_user_id"], **spec
                ),
            )
        if kind == "check_outcome":
            return NarrationResult(text=payload["established_consequence"])
        return NarrationResult(text="...")


@pytest.fixture
def rig(clock, tmp_path):
    registry = PendingCheckRegistry(
        clock=clock, backend=FileCheckBackend(tmp_path / "checks.json")
    )
    director = Director(registry=registry, clock=clock, game_channels={CHANNEL})
    narrator = DirectorNarrator(director)
    transport = FakeTransport()
    sender = ChannelSender(transport, interval_seconds=0.0, clock=lambda: 0.0)
    runner = EffectRunner(sender, narrator, max_depth=4)
    return director, narrator, runner, transport, registry


def fixture_message(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())["message"]


async def test_full_turn_from_declaration_to_narrated_outcome(rig, clock):
    director, narrator, runner, transport, _ = rig

    narrator.open_next = dict(
        actor="Kethra",
        kind=CheckKind.CHECK,
        key="sleight of hand",
        dc=15,
        on_success="The tumblers give. The door swings inward.",
        on_failure="The pick snaps.",
    )

    # 1. Player declares a risky action.
    await runner.run(
        director.on_player_message(
            channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
            content="I try to pick the lock", mentions_bot=True,
        )
    )

    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL
    assert '`!check "sleight of hand"`' in transport.sent[-1][1]

    # 2. Avrae answers, from a real fixture payload. Total 20 vs DC 15.
    parsed = parse_embed(fixture_message("check_success.json"))
    await runner.run(director.on_avrae_message(channel_id=CHANNEL, parsed=parsed))

    # 3. The outcome was decided by arithmetic and handed to the model as fact.
    kinds = [k for k, _ in narrator.seen]
    assert kinds == ["player_action", "check_outcome"]
    outcome = narrator.seen[-1][1]
    assert outcome["succeeded"] is True
    assert outcome["dc"] == 15 and outcome["roll_total"] == 20

    assert transport.sent[-1][1] == "The tumblers give. The door swings inward."
    assert director.state(CHANNEL) is ChannelState.IDLE


async def test_failed_roll_narrates_the_committed_failure_line(rig):
    director, narrator, runner, transport, _ = rig
    narrator.open_next = dict(
        actor="Kethra", kind=CheckKind.CHECK, key="sleight of hand", dc=25,
        on_success="The tumblers give.", on_failure="The pick snaps.",
    )

    await runner.run(
        director.on_player_message(
            channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
            content="I pick the lock", mentions_bot=True,
        )
    )
    parsed = parse_embed(fixture_message("check_success.json"))  # total 20 vs DC 25
    await runner.run(director.on_avrae_message(channel_id=CHANNEL, parsed=parsed))

    assert narrator.seen[-1][1]["succeeded"] is False
    assert transport.sent[-1][1] == "The pick snaps."


async def test_help_embed_costs_nothing(rig):
    """The v1 spam case, end to end: no model call, no message, no state change."""
    director, narrator, runner, transport, _ = rig

    parsed = parse_embed(fixture_message("help_text.json"))
    await runner.run(director.on_avrae_message(channel_id=CHANNEL, parsed=parsed))

    assert narrator.seen == []
    assert transport.sent == []
    assert runner.stats.narrated == 0


async def test_initiative_embed_never_resolves_a_pending_check(rig):
    director, narrator, runner, transport, _ = rig
    narrator.open_next = dict(
        actor="Kethra", kind=CheckKind.CHECK, key="stealth", dc=15,
        on_success="unseen", on_failure="spotted",
    )
    await runner.run(
        director.on_player_message(
            channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
            content="I sneak", mentions_bot=True,
        )
    )

    parsed = parse_embed(fixture_message("initiative_dying.json"))
    await runner.run(director.on_avrae_message(channel_id=CHANNEL, parsed=parsed))

    assert [k for k, _ in narrator.seen] == ["player_action"]
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


async def test_unreadable_embed_asks_instead_of_guessing(rig):
    director, narrator, runner, transport, _ = rig
    narrator.open_next = dict(
        actor="Kethra", kind=CheckKind.CHECK, key="stealth", dc=15,
        on_success="unseen", on_failure="spotted",
    )
    await runner.run(
        director.on_player_message(
            channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
            content="I sneak", mentions_bot=True,
        )
    )

    parsed = parse_embed(fixture_message("unreadable_embed.json"))
    await runner.run(director.on_avrae_message(channel_id=CHANNEL, parsed=parsed))

    assert "couldn't read that roll" in transport.sent[-1][1]
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL
    assert runner.stats.drops_by_status.get("unknown") == 1


async def test_expiry_narrates_a_pivot(rig, clock):
    director, narrator, runner, transport, _ = rig
    narrator.open_next = dict(
        actor="Kethra", kind=CheckKind.CHECK, key="stealth", dc=15,
        on_success="unseen", on_failure="spotted", ttl_seconds=60,
    )
    await runner.run(
        director.on_player_message(
            channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
            content="I sneak", mentions_bot=True,
        )
    )

    clock.advance(61)
    await runner.run(director.sweep())

    assert narrator.seen[-1][0] == "check_expired"
    assert director.state(CHANNEL) is ChannelState.IDLE


async def test_a_restart_mid_check_still_resolves(rig, clock, tmp_path):
    """Gary dies after posting the command and before the player rolls."""
    director, narrator, runner, transport, _ = rig
    narrator.open_next = dict(
        actor="Kethra", kind=CheckKind.CHECK, key="sleight of hand", dc=15,
        on_success="The tumblers give.", on_failure="The pick snaps.",
    )
    await runner.run(
        director.on_player_message(
            channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
            content="I pick the lock", mentions_bot=True,
        )
    )

    # ... process restarts: everything below is rebuilt from storage ...
    revived_registry = PendingCheckRegistry(
        clock=clock, backend=FileCheckBackend(tmp_path / "checks.json")
    )
    revived_director = Director(
        registry=revived_registry, clock=clock, game_channels={CHANNEL}
    )
    revived_narrator = DirectorNarrator(revived_director)
    revived_transport = FakeTransport()
    revived_runner = EffectRunner(
        ChannelSender(revived_transport, interval_seconds=0.0, clock=lambda: 0.0),
        revived_narrator,
    )

    assert revived_director.state(CHANNEL) is ChannelState.AWAITING_ROLL

    parsed = parse_embed(fixture_message("check_success.json"))
    await revived_runner.run(
        revived_director.on_avrae_message(channel_id=CHANNEL, parsed=parsed)
    )

    outcome = revived_narrator.seen[-1][1]
    assert outcome["succeeded"] is True
    assert outcome["dc"] == 15  # the DC committed before the crash
    assert revived_transport.sent[-1][1] == "The tumblers give."


# -- the discord.py boundary conversion --------------------------------------------------------


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Embed:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


def test_message_to_dict_extracts_the_slash_command_invoker():
    """The strongest correlation signal available (§6.2)."""
    msg = _Obj(
        id=1,
        content="",
        embeds=[_Embed({"title": "Kethra makes a Stealth check!"})],
        interaction_metadata=_Obj(user=_Obj(id=111)),
        reference=None,
    )
    out = message_to_dict(msg)
    assert out["interaction"]["user"]["id"] == "111"
    assert out["embeds"][0]["title"] == "Kethra makes a Stealth check!"


def test_message_to_dict_falls_back_to_the_legacy_interaction_attribute():
    """discord.py renamed this; a bump must not silently drop correlation."""
    msg = _Obj(
        id=1, content="", embeds=[],
        interaction_metadata=None, interaction=_Obj(user=_Obj(id=222)), reference=None,
    )
    assert message_to_dict(msg)["interaction"]["user"]["id"] == "222"


def test_message_to_dict_extracts_a_resolved_reply_author():
    msg = _Obj(
        id=1, content="", embeds=[], interaction_metadata=None, interaction=None,
        reference=_Obj(resolved=_Obj(author=_Obj(id=333))),
    )
    assert message_to_dict(msg)["referenced_message"]["author"]["id"] == "333"


def test_message_to_dict_tolerates_everything_absent():
    msg = _Obj(
        id=1, content="hi", embeds=[],
        interaction_metadata=None, interaction=None, reference=None,
    )
    out = message_to_dict(msg)
    assert out["content"] == "hi"
    assert "interaction" not in out and "referenced_message" not in out


def test_message_to_dict_output_is_parseable():
    """The conversion feeds the parser directly, so it must round-trip through JSON."""
    msg = _Obj(
        id=1, content="",
        embeds=[_Embed({"title": "Kethra makes a Stealth check!",
                        "description": "**1d20 (13) + 7** = `20`"})],
        interaction_metadata=None, interaction=None, reference=None,
    )
    parsed = parse_embed(json.loads(json.dumps(message_to_dict(msg))))
    assert parsed.roll.total == 20
    assert parsed.roll.natural == 13


class _Tags:
    def __init__(self, bot_id):
        self.bot_id = bot_id


class _Role:
    def __init__(self, bot_id):
        self.tags = _Tags(bot_id)


class _Msg:
    def __init__(self, mentions=(), role_mentions=()):
        self.mentions = list(mentions)
        self.role_mentions = list(role_mentions)


class _Client:
    """Just enough of GaryClient to exercise _addressed_directly."""

    def __init__(self, user):
        self.user = user

    _addressed_directly = None  # bound below


def test_bot_role_mention_counts_as_addressing_gary():
    """Discord auto-creates a role named after the bot, so the picker shows two
    identical '@Gary' entries. Picking the role put the mention in role_mentions and the
    message read as unaddressed -- silently, while looking addressed at the table."""
    from gary.adapters.discord_bot import GaryClient

    class _User:
        """discord.py compares users by id, not identity."""
        id = 42

        def __eq__(self, other):
            return getattr(other, "id", None) == self.id

        def __hash__(self):
            return hash(self.id)

    client = _Client(_User())
    addressed = GaryClient._addressed_directly.__get__(client, _Client)

    assert addressed(_Msg(mentions=[_User()])) is True
    assert addressed(_Msg(role_mentions=[_Role(bot_id=42)])) is True
    assert addressed(_Msg(role_mentions=[_Role(bot_id=999)])) is False
    assert addressed(_Msg()) is False
