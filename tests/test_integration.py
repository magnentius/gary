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
    # Posted ahead of the narration it belongs to, not after it.
    assert any('`!check "sleight of hand"`' in t for _, t in transport.sent)
    assert transport.sent[0][1].startswith("**Kethra**")

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


def test_only_avrae_edits_are_reprocessed():
    """Avrae EDITS the initiative tracker rather than reposting it, so the bot must watch
    edits — but re-running every edit would make a player fixing a typo cost another model
    call, and an edited roll embed would re-enter resolution."""
    import asyncio

    from gary.adapters.discord_bot import GaryClient

    seen = []

    class _Settings:
        avrae_bot_id = 261302296103747584

    class _Client:
        settings = _Settings()

        async def on_message(self, message):
            seen.append(message)

    client = _Client()
    edit = GaryClient.on_message_edit.__get__(client, _Client)

    class _Msg:
        def __init__(self, author_id):
            self.author = type("A", (), {"id": author_id})()

    avrae, player = _Msg(261302296103747584), _Msg(999)
    asyncio.get_event_loop().run_until_complete(edit(avrae, avrae))
    asyncio.get_event_loop().run_until_complete(edit(player, player))

    assert seen == [avrae]


def test_raw_edits_are_fetched_when_the_message_was_never_cached():
    """on_message_edit only fires for messages discord.py has cached — those seen since
    this process connected. The initiative tracker is pinned and long-lived, so after a
    restart mid-combat every `!init next` edits it silently."""
    import asyncio

    from gary.adapters.discord_bot import GaryClient

    seen = []
    AVRAE = 261302296103747584

    class _Msg:
        def __init__(self, author_id):
            self.author = type("A", (), {"id": author_id})()

    class _Channel:
        def __init__(self, message):
            self._message = message

        async def fetch_message(self, _id):
            return self._message

    class _Payload:
        def __init__(self, cached, fetched):
            self.cached_message = cached
            self.channel_id = 1
            self.message_id = 2
            self._fetched = fetched

    class _Client:
        settings = type("S", (), {"avrae_bot_id": AVRAE})()

        def __init__(self, fetched):
            self._fetched = fetched

        def get_channel(self, _id):
            return _Channel(self._fetched)

        async def on_message(self, message):
            seen.append(message)

    run = asyncio.get_event_loop().run_until_complete

    tracker = _Msg(AVRAE)
    client = _Client(tracker)
    raw = GaryClient.on_raw_message_edit.__get__(client, _Client)

    run(raw(_Payload(cached=None, fetched=tracker)))          # uncached Avrae edit: fetched
    assert seen == [tracker]

    run(raw(_Payload(cached=tracker, fetched=tracker)))       # cached: on_message_edit has it
    assert seen == [tracker]

    seen.clear()
    player = _Msg(999)
    client = _Client(player)
    raw = GaryClient.on_raw_message_edit.__get__(client, _Client)
    run(raw(_Payload(cached=None, fetched=player)))           # not Avrae: ignored
    assert seen == []


def test_a_sheet_registers_whoever_asked_for_it():
    """`!sheet` names the character but not the person -- it is a prefix command and
    carries no interaction metadata. Remembering who asked is the only way to connect the
    two without a separate registration step."""
    import asyncio, time

    from gary.adapters.discord_bot import GaryClient
    from gary.fsm import Director
    from gary.registry import PendingCheckRegistry
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    now = [1000.0]
    store = CampaignStore(backend=InMemoryStateBackend(), clock=lambda: now[0])
    director = Director(registry=PendingCheckRegistry(clock=lambda: now[0]),
                        clock=lambda: now[0], game_channels={7})
    sent = []

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Client:
        SHEET_CLAIM_SECONDS = GaryClient.SHEET_CLAIM_SECONDS
        def __init__(self):
            self.store, self.director, self.sender = store, director, _Sender()
            self.clock = lambda: now[0]
            self._sheet_asked_by = {}

    client = _Client()
    claim = GaryClient._claim_sheet.__get__(client, _Client)
    run = asyncio.get_event_loop().run_until_complete

    # nobody asked: nothing is claimed
    run(claim(7, "Caspian Varr"))
    assert director.players == {}

    # John ran !sheet a moment ago
    client._sheet_asked_by[7] = (111, "John K", now[0])
    run(claim(7, "Caspian Varr"))
    assert director.players == {111: "Caspian Varr"}
    assert store.state()["players"] == {"111": "Caspian Varr"}
    assert "John K" in sent[0] and "Caspian Varr" in sent[0]

    # a stale request is not claimed by a later sheet
    client._sheet_asked_by[7] = (222, "Someone", now[0])
    now[0] += GaryClient.SHEET_CLAIM_SECONDS + 1
    run(claim(7, "Kethra"))
    assert 222 not in director.players


# -- ?dm import --------------------------------------------------------------------------


class _Attachment:
    def __init__(self, filename="setting.md", body=b"# Leafmoor\n\nA drowned fen.", size=None):
        self.filename = filename
        self._body = body
        self.size = len(body) if size is None else size

    async def read(self):
        return self._body


class _ImportMsg:
    def __init__(self, attachments=()):
        self.attachments = list(attachments)


class _Runner:
    """Stands in for EffectRunner. `_narrator` is private on the real one, and a guard
    that reads the wrong attribute name is a guard that never fires."""

    def __init__(self, narrator):
        self._narrator = narrator
        self.effects = []

    async def run(self, effects):
        self.effects.extend(effects)


def _import_client(narrator, sent):
    from gary.adapters.discord_bot import GaryClient

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Settings:
        command_prefix = "?dm "

    class _Client:
        settings = _Settings()
        sender = _Sender()
        runner = _Runner(narrator)
        MAX_IMPORT_BYTES = GaryClient.MAX_IMPORT_BYTES
        IMPORT_CHUNK_CHARS = GaryClient.IMPORT_CHUNK_CHARS
        MAX_IMPORT_CHUNKS = GaryClient.MAX_IMPORT_CHUNKS
        IMPORT_SUFFIXES = GaryClient.IMPORT_SUFFIXES

    client = _Client()
    return client, GaryClient._import_document.__get__(client, _Client)


class _Narrator:
    async def narrate(self, *a, **k):  # pragma: no cover - never reached
        raise AssertionError("import must not narrate through this stub")


def test_import_refuses_before_spending_anything():
    """Every refusal has to land before the file is read into prompts. An import that
    validates halfway has already cost model calls for the half it accepted."""
    import asyncio

    cases = [
        (_ImportMsg(), "needs a file attached"),
        (_ImportMsg([_Attachment(filename="setting.pdf")]), "only read"),
        (_ImportMsg([_Attachment(size=999_999)]), "limit is"),
        (_ImportMsg([_Attachment(body=b"   ")]), "is empty"),
    ]
    for message, expected in cases:
        sent = []
        client, do_import = _import_client(_Narrator(), sent)
        asyncio.get_event_loop().run_until_complete(do_import(message, 7))
        assert expected in " ".join(sent), (expected, sent)
        assert client.runner.effects == []


def test_import_says_so_when_no_model_is_attached():
    """Regression: this guard read `runner.narrator`, but EffectRunner stores `_narrator`.
    getattr returned None, the isinstance was never true, and an import under EchoNarrator
    ran every chunk through a narrator that cannot call a tool -- reporting success while
    filing nothing."""
    import asyncio

    from gary.ports import EchoNarrator

    sent = []
    client, do_import = _import_client(EchoNarrator(), sent)
    asyncio.get_event_loop().run_until_complete(
        do_import(_ImportMsg([_Attachment()]), 7)
    )
    assert "No model is attached" in " ".join(sent)
    assert client.runner.effects == []


def test_import_fans_a_document_into_one_prompt_per_part():
    """Parts are numbered because the model is told it is reading part n of m -- a
    document that arrives unlabelled gets summarised as if it were the whole world."""
    import asyncio

    body = ("# Leafmoor\n\n" + "The fen drowns the road.\n" * 600).encode()
    sent = []
    client, do_import = _import_client(_Narrator(), sent)
    asyncio.get_event_loop().run_until_complete(
        do_import(_ImportMsg([_Attachment(filename="leaf-node.md", body=body)]), 7)
    )

    effects = client.runner.effects
    assert len(effects) > 1
    assert {e.kind for e in effects} == {"import_document"}
    assert [e.payload["part"] for e in effects] == list(range(1, len(effects) + 1))
    assert all(e.payload["total"] == len(effects) for e in effects)
    assert all(e.payload["filename"] == "leaf-node.md" for e in effects)
    assert all(e.channel_id == 7 for e in effects)
    # Nothing is dropped between chunks.
    assert "".join(e.payload["chunk"] for e in effects).count("drowns") == 600


# -- ?dm state and ?dm notes ---------------------------------------------------------------


def _dm_client(state, sent):
    """Runs the real ?dm dispatcher against a stub store.

    These handlers had no test at all, and it showed: `?dm state` referenced a `prefix`
    local that was never bound in the dispatcher, so every invocation raised NameError.
    Exercising the dispatcher itself -- rather than the pieces it calls -- is what catches
    that class of bug.
    """
    from gary.adapters.discord_bot import GaryClient

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Settings:
        command_prefix = "?dm "

    class _Store:
        def state(self):
            return state

    class _Channel:
        id = 7

    class _Client:
        settings = _Settings()
        sender = _Sender()
        store = _Store()

    class _Msg:
        channel = _Channel()

        def __init__(self, content):
            self.content = content

    client = _Client()
    return _Msg, GaryClient._on_dm_command.__get__(client, _Client)


def _seeded_state():
    return {
        "world_minutes": 0,
        "calendar": {"epoch_date": "1026-08-21", "minutes_per_day": 1440},
        "setting": "", "scene": {"location": "", "summary": ""},
        "party": {}, "players": {}, "effects": [], "quests": {}, "encounter": None,
        "npcs": {"liriel": {"name": "Liriel", "disposition": 0, "notes": ["A scribe."]}},
        "notes": [
            {"note_id": "1", "kind": "general", "subject": "The Leaf-Mote", "body": "A hall."},
            {"note_id": "2", "kind": "clue", "subject": "The Leaf-Mote", "body": "Nine tallies."},
            {"note_id": "3", "kind": "general", "subject": "The Rootbound", "body": "Traditionalists."},
        ],
    }


def test_dm_state_does_not_stay_silent_about_stored_memory():
    """An import filed ten notes and four NPCs, and `?dm state` showed a clock and a
    party -- because notes and NPCs are layer 3 and surface only on mention. Correct, and
    indistinguishable on screen from having stored nothing at all."""
    import asyncio

    sent = []
    Msg, dispatch = _dm_client(_seeded_state(), sent)
    asyncio.get_event_loop().run_until_complete(dispatch(Msg("?dm state")))

    body = " ".join(sent)
    assert "3 note(s)" in body and "1 NPC(s)" in body
    assert "21 Sturgeon Moon 1026" in body      # layer 2 still rendered verbatim
    assert "?dm notes" in body                   # and points somewhere that can show them


def test_dm_state_stays_quiet_when_nothing_is_stored():
    import asyncio

    state = _seeded_state()
    state["notes"], state["npcs"] = [], {}
    sent = []
    Msg, dispatch = _dm_client(state, sent)
    asyncio.get_event_loop().run_until_complete(dispatch(Msg("?dm state")))
    assert "Also stored" not in " ".join(sent)


def test_dm_notes_lists_subjects_a_player_would_say():
    """Subjects are the retrieval key (§8 layer 3), so listing them is also the honest
    answer to 'what will Gary remember if I bring it up?'"""
    import asyncio

    sent = []
    Msg, dispatch = _dm_client(_seeded_state(), sent)
    asyncio.get_event_loop().run_until_complete(dispatch(Msg("?dm notes")))

    body = " ".join(sent)
    assert "- The Leaf-Mote (2)" in body      # count only where there is more than one
    assert "- The Rootbound" in body and "The Rootbound (" not in body
    assert "Liriel" in body


def test_dm_notes_on_an_empty_campaign_says_so():
    import asyncio

    state = _seeded_state()
    state["notes"], state["npcs"] = [], {}
    sent = []
    Msg, dispatch = _dm_client(state, sent)
    asyncio.get_event_loop().run_until_complete(dispatch(Msg("?dm notes")))
    assert "Nothing filed yet" in " ".join(sent)
