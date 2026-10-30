"""End-to-end through every layer except the Gateway socket itself.

Player speaks → Director → make_move → player asked to roll → player clicks Roll → Gary
rolls the dice → band → (player chooses) → narrate. The unit tests prove each piece;
this proves they are wired to each other, which is where a system like this usually
breaks.

Only Discord's transport is faked, and the dice are scripted. The registry does real
attribution and the resolver does real arithmetic.
"""

from __future__ import annotations

import inspect

import pytest

from gary.adapters.runner import EffectRunner
from gary.adapters.sender import ChannelSender
from gary.fsm import ChannelState, ChoicePrompt, DamagePrompt, Director, RollPrompt
from gary.ports import EchoNarrator, NarrationResult, ScriptedDice
from gary.registry import PendingCheckRegistry
from gary.state.backend import FileCheckBackend

CHANNEL = 555
ALICE = 111
BOB = 222

KETHRA = {"name": "Kethra", "stats": {"str": 8, "dex": 16, "con": 13, "int": 12, "wis": 15, "cha": 9},
          "hp": 19, "max_hp": 19, "armor": 1, "damage": "d8"}
GOBLIN = {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6",
          "piercing": 0, "ignores_armor": False, "srd": True}


class FakeTransport:
    def __init__(self) -> None:
        self.sent = []
        self.prompts = []  # (text, prompt) for each message that carried something to click

    async def send(self, channel_id: int, text: str, prompt=None) -> None:
        self.sent.append((channel_id, text))
        if prompt is not None:
            self.prompts.append((text, prompt))


class DirectorNarrator:
    """Stands in for the Gemini adapter.

    Its only job here is what the real one will do: translate a decision to call
    ``make_move`` into a Director call, and return the resulting effects.
    """

    def __init__(self, director: Director) -> None:
        self.director = director
        self.move_next = None
        self.seen = []

    async def narrate(self, *, channel_id, kind, payload):
        self.seen.append((kind, payload))
        if kind == "player_action" and self.move_next is not None:
            spec, self.move_next = self.move_next, None
            return NarrationResult(
                text="You draw and loose in one motion.",
                effects=self.director.open_move(
                    channel_id=channel_id, user_id=payload["speaker_user_id"], **spec
                ),
            )
        if kind == "check_outcome":
            return NarrationResult(text=f"[{payload['result']}] {payload['what_it_means']}")
        return NarrationResult(text="...")


def build(clock, path, dice):
    registry = PendingCheckRegistry(clock=clock, backend=FileCheckBackend(path))
    scene = {"Goblin 1": dict(GOBLIN)}
    director = Director(
        registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
        sheet_for={"Kethra": KETHRA}.get, monsters=lambda: scene,
    )
    narrator = DirectorNarrator(director)
    transport = FakeTransport()
    runner = EffectRunner(
        ChannelSender(transport, interval_seconds=0.0, clock=lambda: 0.0), narrator, max_depth=4
    )
    return director, narrator, runner, transport


@pytest.fixture
def rig(clock, tmp_path):
    dice = ScriptedDice()
    return (*build(clock, tmp_path / "checks.json", dice), dice)


async def declare(director, runner, content="I shoot the warden", user_id=ALICE):
    await runner.run(
        director.on_player_message(
            channel_id=CHANNEL, user_id=user_id, display_name="Alice",
            content=content, mentions_bot=True,
        )
    )


async def test_full_turn_from_move_to_narrated_outcome(rig):
    director, narrator, runner, transport, dice = rig
    narrator.move_next = dict(actor="Kethra", move="defy_danger", stat="dex")

    # 1. Player acts; the model calls the move.
    await declare(director, runner, "I dive under the falling portcullis")
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL
    # Posted ahead of the narration it belongs to, not after it -- with the button.
    assert transport.sent[0][1] == "**Kethra** — Defy Danger, roll +Dex +2."
    ((_, prompt),) = transport.prompts
    assert isinstance(prompt, RollPrompt)

    # 2. The player clicks Roll. 4 + 4 + 2 = 10.
    dice.queue(4, 4)
    await runner.run(director.roll(channel_id=CHANNEL, user_id=ALICE, check_id=prompt.check_id))

    # 3. The band was decided by arithmetic and handed to the model as fact.
    assert [k for k, _ in narrator.seen] == ["player_action", "check_outcome"]
    assert narrator.seen[-1][1]["result"] == "10+ (strong hit)"
    assert [t for _, t in transport.sent[-2:]] == [
        "🎲 **Kethra** rolls 4 + 4 +2 = 10 — strong hit.",
        "[10+ (strong hit)] You do what you set out to, the threat doesn’t come to bear.",
    ]
    assert director.state(CHANNEL) is ChannelState.IDLE


async def test_a_weak_volley_waits_for_the_players_choice_then_narrates_it(rig):
    director, narrator, runner, transport, dice = rig
    narrator.move_next = dict(actor="Kethra", move="volley", target="Goblin 1")
    await declare(director, runner)
    ((_, roll_prompt),) = transport.prompts

    dice.queue(3, 2)  # 7: weak hit
    await runner.run(director.roll(channel_id=CHANNEL, user_id=ALICE, check_id=roll_prompt.check_id))
    assert [k for k, _ in narrator.seen] == ["player_action"]  # nothing narrated yet
    (_, choice) = transport.prompts[-1]
    assert isinstance(choice, ChoicePrompt) and choice.count == 1

    await runner.run(director.choose(
        channel_id=CHANNEL, user_id=ALICE, check_id=choice.check_id, picks=[2]
    ))
    assert [k for k, _ in narrator.seen] == ["player_action"]  # damage still to roll
    (_, damage_prompt) = transport.prompts[-1]
    assert isinstance(damage_prompt, DamagePrompt)

    dice.queue(5)
    await runner.run(director.roll_damage(
        channel_id=CHANNEL, user_id=ALICE, check_id=damage_prompt.check_id
    ))
    outcome = narrator.seen[-1][1]
    assert outcome["player_chose"] == ["Take several shots, reducing your ammo by one."]
    assert outcome["damage"]["dealt"] == 4 and outcome["damage"]["defeated"] is True
    assert director.state(CHANNEL) is ChannelState.IDLE


async def test_someone_elses_roll_costs_nothing(rig):
    """Bob asking to roll Alice's move: no dice, no model call, no message."""
    director, narrator, runner, transport, dice = rig
    narrator.move_next = dict(actor="Kethra", move="volley", target="Goblin 1")
    await declare(director, runner)
    before = (list(transport.sent), runner.stats.narrated)

    await runner.run(director.roll(channel_id=CHANNEL, user_id=BOB))

    assert (transport.sent, runner.stats.narrated) == before
    assert dice.rolls == []
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


async def test_expiry_narrates_a_pivot(rig, clock):
    director, narrator, runner, transport, _ = rig
    narrator.move_next = dict(actor="Kethra", move="volley", target="Goblin 1", ttl_seconds=60)
    await declare(director, runner)

    clock.advance(61)
    await runner.run(director.sweep())

    assert narrator.seen[-1][0] == "check_expired"
    assert director.state(CHANNEL) is ChannelState.IDLE


async def test_a_restart_mid_move_still_resolves(rig, clock, tmp_path):
    """Gary dies after asking for the roll and before the player makes it."""
    director, narrator, runner, transport, _ = rig
    narrator.move_next = dict(actor="Kethra", move="defy_danger", stat="dex")
    await declare(director, runner)

    # ... process restarts: everything below is rebuilt from storage ...
    revived, revived_narrator, revived_runner, revived_transport = build(
        clock, tmp_path / "checks.json", ScriptedDice([4, 4])
    )
    assert revived.state(CHANNEL) is ChannelState.AWAITING_ROLL

    await revived_runner.run(revived.roll(channel_id=CHANNEL, user_id=ALICE))

    outcome = revived_narrator.seen[-1][1]
    assert outcome["roll_total"] == 10  # the +2 committed before the crash
    assert outcome["result"] == "10+ (strong hit)"


async def test_a_restart_between_the_dice_and_the_choice_loses_neither(rig, clock, tmp_path):
    director, narrator, runner, transport, dice = rig
    narrator.move_next = dict(actor="Kethra", move="volley", target="Goblin 1")
    await declare(director, runner)
    dice.queue(3, 2)
    await runner.run(director.roll(channel_id=CHANNEL, user_id=ALICE))
    (_, choice) = transport.prompts[-1]

    revived, revived_narrator, revived_runner, revived_transport = build(
        clock, tmp_path / "checks.json", ScriptedDice([6, 1])
    )
    await revived_runner.run(revived.choose(
        channel_id=CHANNEL, user_id=ALICE, check_id=choice.check_id, picks=[1]
    ))
    await revived_runner.run(revived.roll_damage(
        channel_id=CHANNEL, user_id=ALICE, check_id=choice.check_id
    ))
    outcome = revived_narrator.seen[-1][1]
    assert outcome["roll_total"] == 7
    assert outcome["player_chose"] == ["Take what you can get: -1d6 damage."]
    assert outcome["damage"]["rolled"] == 5  # d8 6, -1d6 1


# -- the buttons ---------------------------------------------------------------------------


class _Response:
    def __init__(self):
        self.private = []   # ephemeral replies
        self.edits = []     # views the clicked message was edited to

    async def send_message(self, text, ephemeral=False):
        assert ephemeral, "a failed click must never be answered in front of the table"
        self.private.append(text)

    async def edit_message(self, view=None):
        self.edits.append(view)


class _Interaction:
    def __init__(self, user_id, channel_id=CHANNEL):
        self.user = type("U", (), {"id": user_id})()
        self.channel_id = channel_id
        self.response = _Response()


def _click_client(director, runner):
    from gary.adapters.discord_bot import GaryClient

    class _Client:
        pass

    client = _Client()
    client.director, client.runner = director, runner
    client._why_no_roll = GaryClient._why_no_roll.__get__(client, _Client)
    return (
        GaryClient.on_roll.__get__(client, _Client),
        GaryClient.on_choose.__get__(client, _Client),
    )


async def _ask_for_a_roll(rig, move="defy_danger", **kw):
    director, narrator, runner, transport, dice = rig
    narrator.move_next = dict(actor="Kethra", move=move, **kw)
    await declare(director, runner)
    ((_, prompt),) = transport.prompts
    return prompt.check_id


async def test_clicking_roll_rolls_narrates_and_disables_the_button(rig):
    director, narrator, runner, transport, dice = rig
    check_id = await _ask_for_a_roll(rig, stat="dex")
    click, _ = _click_client(director, runner)

    dice.queue(5, 5)
    interaction = _Interaction(ALICE)
    await click(interaction, check_id)

    # Answered first, by disabling the button -- Discord allows three seconds.
    (view,) = interaction.response.edits
    (button,) = view.children
    assert button.item.disabled and button.custom_id == f"gary:roll:{check_id}"
    assert interaction.response.private == []
    assert transport.sent[-2][1] == "🎲 **Kethra** rolls 5 + 5 +2 = 12 — strong hit."


async def test_someone_elses_click_is_answered_privately_and_rolls_nothing(rig):
    director, narrator, runner, transport, dice = rig
    check_id = await _ask_for_a_roll(rig, stat="dex")
    click, _ = _click_client(director, runner)
    before = list(transport.sent)

    interaction = _Interaction(BOB)
    await click(interaction, check_id)

    assert interaction.response.private == ["That roll is **Kethra**'s to make."]
    assert interaction.response.edits == []
    assert transport.sent == before and dice.rolls == []
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


async def test_a_second_click_says_the_roll_is_made(rig):
    director, narrator, runner, transport, dice = rig
    check_id = await _ask_for_a_roll(rig, stat="dex")
    click, _ = _click_client(director, runner)
    dice.queue(5, 5)
    await click(_Interaction(ALICE), check_id)

    again = _Interaction(ALICE)
    await click(again, check_id)

    assert "already been made" in again.response.private[0]
    assert len(dice.rolls) == 1


async def test_clicking_an_option_resolves_and_clears_the_options(rig):
    director, narrator, runner, transport, dice = rig
    check_id = await _ask_for_a_roll(rig, move="volley", target="Goblin 1")
    click, pick = _click_client(director, runner)
    dice.queue(3, 2)
    await click(_Interaction(ALICE), check_id)

    bob = _Interaction(BOB)
    await pick(bob, check_id, [1])
    assert bob.response.private == ["That choice is **Kethra**'s to make."]

    alice = _Interaction(ALICE)
    await pick(alice, check_id, [1])
    assert alice.response.edits == [None]  # the options are taken away
    assert transport.sent[-1][1] == "**Kethra**, roll your damage (d8 -1d6) against Goblin 1."


# -- addressing ---------------------------------------------------------------------------------


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

    def __init__(self, narrator, answer=""):
        self._narrator = narrator
        self.effects = []
        self.asked = []
        self._answer = answer

    async def run(self, effects):
        self.effects.extend(effects)

    async def ask(self, effect):
        self.asked.append(effect)
        return self._answer


def _import_client(narrator, sent, state=None, answer=""):
    from gary.adapters.discord_bot import GaryClient

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Settings:
        command_prefix = "?dm "

    class _Store:
        def __init__(self):
            self.applied = []

        def state(self):
            return state if state is not None else {"setting": ""}

        def apply(self, mutations, reason=""):
            self.applied.append((mutations, reason))

    class _Client:
        settings = _Settings()
        sender = _Sender()
        runner = _Runner(narrator, answer)
        store = _Store()
        _pending_proposals = {}
        _declined_proposals = {}
        _has_model = GaryClient._has_model
        _propose = GaryClient._propose
        _forget_proposals = GaryClient._forget_proposals
        MAX_DECLINED_PROPOSALS = GaryClient.MAX_DECLINED_PROPOSALS
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
        asyncio.run(do_import(message, 7))
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
    asyncio.run(
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
    asyncio.run(
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


def _dm_client(state, sent, narrator=None, answer=""):
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
        applied = []

        def state(self):
            return state

        def apply(self, mutations, reason=""):
            self.applied.append((mutations, reason))

    class _Channel:
        id = 7

    class _Author:
        display_name = "John"
        id = 11

    class _Client:
        settings = _Settings()
        sender = _Sender()
        store = _Store()
        runner = _Runner(narrator or EchoNarrator(), answer)
        _pending_proposals = {}
        _declined_proposals = {}
        _has_model = GaryClient._has_model
        _propose = GaryClient._propose
        _forget_proposals = GaryClient._forget_proposals
        MAX_DECLINED_PROPOSALS = GaryClient.MAX_DECLINED_PROPOSALS
        _apply_setting = GaryClient._apply_setting
        _apply_scene = GaryClient._apply_scene
        MAX_SETTING_CHARS = GaryClient.MAX_SETTING_CHARS

    class _Msg:
        channel = _Channel()
        author = _Author()

        def __init__(self, content):
            self.content = content

    client = _Client()
    return _Msg, GaryClient._on_dm_command.__get__(client, _Client), client


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
    Msg, dispatch, _ = _dm_client(_seeded_state(), sent)
    asyncio.run(dispatch(Msg("?dm state")))

    body = " ".join(sent)
    assert "3 note(s)" in body and "1 NPC(s)" in body
    assert "21 Sturgeon Moon 1026" in body      # layer 2 still rendered verbatim
    assert "?dm notes" in body                   # and points somewhere that can show them


def test_dm_state_stays_quiet_when_nothing_is_stored():
    import asyncio

    state = _seeded_state()
    state["notes"], state["npcs"] = [], {}
    sent = []
    Msg, dispatch, _ = _dm_client(state, sent)
    asyncio.run(dispatch(Msg("?dm state")))
    assert "Also stored" not in " ".join(sent)


def test_dm_notes_lists_subjects_a_player_would_say():
    """Subjects are the retrieval key (§8 layer 3), so listing them is also the honest
    answer to 'what will Gary remember if I bring it up?'"""
    import asyncio

    sent = []
    Msg, dispatch, _ = _dm_client(_seeded_state(), sent)
    asyncio.run(dispatch(Msg("?dm notes")))

    body = " ".join(sent)
    assert "- The Leaf-Mote (2)" in body      # count only where there is more than one
    assert "- The Rootbound" in body and "The Rootbound (" not in body
    assert "Liriel" in body


def test_dm_notes_on_an_empty_campaign_says_so():
    import asyncio

    state = _seeded_state()
    state["notes"], state["npcs"] = [], {}
    sent = []
    Msg, dispatch, _ = _dm_client(state, sent)
    asyncio.run(dispatch(Msg("?dm notes")))
    assert "Nothing filed yet" in " ".join(sent)


# -- proposals: ?dm accept, and the two places a proposal comes from -----------------------


def test_import_proposes_a_premise_it_could_not_have_filed():
    """A premise is the one thing an import cannot record, because it is not a sentence in
    the document -- it is what the document is *about*. So it is asked for at the only
    moment the model has read the whole thing."""
    import asyncio

    sent = []
    client, do_import = _import_client(
        _Narrator(), sent, state={"setting": ""}, answer="The Fen Marches, drowned and feudal."
    )
    asyncio.run(
        do_import(_ImportMsg([_Attachment()]), 7)
    )

    assert [e.kind for e in client.runner.asked] == ["propose_setting"]
    assert client.runner.asked[0].channel_id == 7
    assert client._pending_proposals[(7, "setting")] == "The Fen Marches, drowned and feudal."
    # Proposed, not written: the store saw nothing.
    assert client.store.applied == []
    assert "?dm accept" in " ".join(sent)


def test_import_leaves_an_established_premise_alone():
    """Re-importing a second sourcebook into a running campaign must not offer to
    overwrite the premise the table has been playing under for weeks."""
    import asyncio

    sent = []
    client, do_import = _import_client(
        _Narrator(), sent, state={"setting": "The Fen Marches."}, answer="Something else."
    )
    asyncio.run(
        do_import(_ImportMsg([_Attachment()]), 7)
    )
    assert client.runner.asked == []
    assert "Suggested" not in " ".join(sent)


def test_bare_dm_scene_proposes_one_instead_of_printing_its_own_usage():
    import asyncio

    sent = []
    Msg, dispatch, client = _dm_client(
        _seeded_state(), sent, narrator=object(), answer="The Rusty Anchor | Rain on the shutters."
    )
    asyncio.run(dispatch(Msg("?dm scene")))

    assert [e.kind for e in client.runner.asked] == ["propose_scene"]
    assert client._pending_proposals[(7, "scene")] == "The Rusty Anchor | Rain on the shutters."
    assert client.store.applied == []
    body = " ".join(sent)
    assert "The Rusty Anchor" in body and "Rain on the shutters." in body
    # The manual form is still reachable from the proposal, so nothing is lost by
    # replacing the usage text with an offer.
    assert "?dm scene <location> | <standing state>" in body


def test_bare_dm_scene_falls_back_to_usage_with_no_model_attached():
    """`EchoNarrator` cannot propose anything. Spending a turn to find that out and then
    saying 'I couldn't put a scene into words' would be a worse answer than the usage."""
    import asyncio

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent)
    asyncio.run(dispatch(Msg("?dm scene")))

    assert client.runner.asked == []
    assert "?dm scene <location> | <standing state>" in " ".join(sent)


def test_accept_writes_the_held_line_and_spends_it():
    import asyncio

    from gary.models import MutationKind

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent)
    client._pending_proposals[(7, "scene")] = "The Rusty Anchor | Rain on the shutters."
    asyncio.run(dispatch(Msg("?dm accept")))

    (mutations, reason), = client.store.applied
    assert mutations[0].kind is MutationKind.SCENE
    assert mutations[0].payload == {
        "location": "The Rusty Anchor",
        "summary": "Rain on the shutters.",
    }
    assert "John" in reason
    assert "Scene set" in " ".join(sent)
    # Accepting consumes it, so a second accept cannot write it twice.
    assert client._pending_proposals == {}


def test_accept_can_take_one_kind_while_the_other_keeps_waiting():
    import asyncio

    from gary.models import MutationKind

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent)
    client._pending_proposals[(7, "setting")] = "The Fen Marches."
    client._pending_proposals[(7, "scene")] = "The Rusty Anchor | Rain."
    asyncio.run(dispatch(Msg("?dm accept setting")))

    (mutations, _), = client.store.applied
    assert mutations[0].kind is MutationKind.SETTING
    assert list(client._pending_proposals) == [(7, "scene")]


def test_accept_with_nothing_pending_says_where_proposals_come_from():
    import asyncio

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent)
    asyncio.run(dispatch(Msg("?dm accept")))

    body = " ".join(sent)
    assert "Nothing to accept" in body
    assert "?dm import" in body and "?dm scene" in body
    assert client.store.applied == []


def test_writing_a_setting_by_hand_drops_the_suggestion_it_overrules():
    """Otherwise a later bare `?dm accept` -- meant for a scene, or typed by somebody who
    missed the exchange -- would silently reinstate a premise the DM had already replaced."""
    import asyncio

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent)
    client._pending_proposals[(7, "setting")] = "The Fen Marches."
    asyncio.run(
        dispatch(Msg("?dm setting A dry upland barony."))
    )
    assert client._pending_proposals == {}
    (mutations, _), = client.store.applied
    assert mutations[0].payload == {"text": "A dry upland barony."}


def test_a_proposal_the_model_fumbled_is_reported_not_stored():
    import asyncio

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent, narrator=object(), answer="```\n```")
    asyncio.run(dispatch(Msg("?dm scene")))

    assert client._pending_proposals == {}
    assert client.store.applied == []
    assert "couldn't put a scene into words" in " ".join(sent)


def test_dm_help_covers_every_command_the_dispatcher_answers():
    """Help that has drifted from the dispatcher is worse than no help: it sends the DM
    looking for a command that was renamed. Pin it to the branches themselves."""
    import re

    from gary.adapters.discord_bot import GaryClient, render_help

    source = inspect.getsource(GaryClient._on_dm_command)
    answered = set(re.findall(r'command == "(\w+)"', source))
    assert len(answered) > 10, "the regex stopped matching the dispatcher"
    text = render_help("?dm ")
    missing = {c for c in answered if f"?dm {c}" not in text} - {"help"}
    assert not missing, missing


def test_dm_help_is_one_discord_message():
    from gary.adapters.discord_bot import render_help

    assert len(render_help("?dm ")) < 2000


def test_dm_help_respects_a_custom_prefix():
    from gary.adapters.discord_bot import render_help

    text = render_help("!gm ")
    assert "!gm setting <premise>" in text
    assert "?dm" not in text


def test_an_unknown_dm_command_says_so_and_then_helps():
    import asyncio

    sent = []
    Msg, dispatch, _ = _dm_client(_seeded_state(), sent)
    asyncio.run(dispatch(Msg("?dm sitting")))

    body = " ".join(sent)
    assert "I don't know `?dm sitting`" in body
    assert "Opening a campaign" in body


def test_bare_dm_is_help():
    import asyncio

    sent = []
    Msg, dispatch, _ = _dm_client(_seeded_state(), sent)
    asyncio.run(dispatch(Msg("?dm")))
    assert "Opening a campaign" in " ".join(sent)


def test_asking_again_tells_the_model_what_it_already_offered():
    """The bug this exists for: `?dm scene` twice returned the same vault under a
    different description, because the first suggestion was sitting in history for the
    second call to read. Asking again is itself the rejection."""
    import asyncio

    sent = []
    Msg, dispatch, client = _dm_client(
        _seeded_state(), sent, narrator=object(), answer="The Unsealed Vault | Scribes."
    )
    run = asyncio.run

    run(dispatch(Msg("?dm scene")))
    assert client.runner.asked[0].payload["already_offered"] == []

    run(dispatch(Msg("?dm scene")))
    assert client.runner.asked[1].payload["already_offered"] == [
        "The Unsealed Vault | Scribes."
    ]
    assert client._declined_proposals[(7, "scene")] == ["The Unsealed Vault | Scribes."]


def test_the_list_of_rejects_is_bounded():
    """Every reject rides in the prompt, so the list cannot grow with the session."""
    import asyncio

    from gary.adapters.discord_bot import GaryClient

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent, narrator=object())
    run = asyncio.run

    cap = GaryClient.MAX_DECLINED_PROPOSALS
    for i in range(cap + 3):
        client.runner._answer = f"Place {i} | Something true."
        run(dispatch(Msg("?dm scene")))

    assert len(client._declined_proposals[(7, "scene")]) == cap
    assert len(client.runner.asked[-1].payload["already_offered"]) == cap
    # The oldest fall off the front, not the newest.
    assert client.runner.asked[-1].payload["already_offered"][-1] == f"Place {cap + 1} | Something true."


def test_accepting_forgets_the_rejects_too():
    """Rejects only mean 'not this one' while the DM is still looking. Carrying them past
    a settled line would push later suggestions off perfectly good ground."""
    import asyncio

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent)
    client._pending_proposals[(7, "scene")] = "The Vault | Scribes."
    client._declined_proposals[(7, "scene")] = ["The Fen | Mist."]

    asyncio.run(dispatch(Msg("?dm accept")))
    assert client._pending_proposals == {}
    assert client._declined_proposals == {}


def test_writing_a_scene_by_hand_forgets_the_rejects_too():
    import asyncio

    sent = []
    Msg, dispatch, client = _dm_client(_seeded_state(), sent)
    client._pending_proposals[(7, "scene")] = "The Vault | Scribes."
    client._declined_proposals[(7, "scene")] = ["The Fen | Mist."]

    asyncio.run(
        dispatch(Msg("?dm scene The Eel-Runs | Fever-mist off the ditches."))
    )
    assert client._pending_proposals == {}
    assert client._declined_proposals == {}


# -- how prompts are drawn -------------------------------------------------------------------


async def test_a_pick_one_choice_is_a_button_per_option_in_rules_order():
    from gary.adapters.discord_bot import PickButton, choice_view

    view = choice_view(ChoicePrompt("01AB", "volley", "weak", 1, ("a", "b", "c")))
    assert [type(i) for i in view.children] == [PickButton] * 3
    assert [i.custom_id for i in view.children] == [f"gary:pick:01AB:{n}" for n in range(3)]
    assert [i.item.label for i in view.children] == ["a", "b", "c"]
    assert view.timeout is None  # a choice can wait as long as its move can


async def test_a_pick_several_choice_is_one_dropdown_that_takes_exactly_that_many():
    from gary.adapters.discord_bot import ChooseSelect, choice_view
    from gary.moves import MOVES

    options = tuple(o.text for o in MOVES["discern_realities"].strong.choice.options)
    view = choice_view(ChoicePrompt("01AB", "discern_realities", "strong", 3, options))
    (select,) = view.children
    assert isinstance(select, ChooseSelect)
    assert (select.item.min_values, select.item.max_values) == (3, 3)
    assert [o.label for o in select.item.options] == list(options)
    assert select.custom_id == "gary:choose:01AB:discern_realities:strong"


async def test_choice_items_rebuild_from_their_custom_ids_after_a_restart():
    import re

    from gary.adapters.discord_bot import ChooseSelect, PickButton

    pick = re.fullmatch(PickButton.__discord_ui_compiled_template__, "gary:pick:01AB:2")
    rebuilt = await PickButton.from_custom_id(None, None, pick)
    assert (rebuilt.check_id, rebuilt.index) == ("01AB", 2)

    choose = re.fullmatch(
        ChooseSelect.__discord_ui_compiled_template__, "gary:choose:01AB:discern_realities:weak"
    )
    rebuilt = await ChooseSelect.from_custom_id(None, None, choose)
    assert rebuilt.check_id == "01AB" and rebuilt.item.max_values == 1


async def test_a_long_option_is_cut_to_fit_a_button():
    from gary.adapters.discord_bot import MAX_LABEL, PickButton

    assert len(PickButton("01AB", 0, "x" * 200).item.label) == MAX_LABEL


async def test_clicking_roll_damage_lands_the_blow_and_disables_the_button(rig):
    from gary.adapters.discord_bot import DamageButton, GaryClient

    director, narrator, runner, transport, dice = rig
    check_id = await _ask_for_a_roll(rig, move="volley", target="Goblin 1")
    click, _ = _click_client(director, runner)
    dice.queue(6, 6)
    await click(_Interaction(ALICE), check_id)
    assert isinstance(transport.prompts[-1][1], DamagePrompt)

    class _Client:
        pass

    client = _Client()
    client.director, client.runner = director, runner
    client._why_no_roll = GaryClient._why_no_roll.__get__(client, _Client)
    hit = GaryClient.on_damage.__get__(client, _Client)

    bob = _Interaction(BOB)
    await hit(bob, check_id)
    assert bob.response.private == ["That damage roll is **Kethra**'s to make."]

    dice.queue(5)
    alice = _Interaction(ALICE)
    await hit(alice, check_id)
    (view,) = alice.response.edits
    (button,) = view.children
    assert isinstance(button, DamageButton) and button.item.disabled
    assert "**Goblin 1** takes **4 damage** (armor 1), HP 3 → 0 — **defeated**." in transport.sent[-2][1]


# -- spells -----------------------------------------------------------------------------------

_THORIN = {"name": "Thorin", "class": "Cleric", "race": "Dwarf", "level": 1, "hp": 20, "max_hp": 20,
           "stats": {"str": 12, "dex": 8, "con": 15, "int": 9, "wis": 16, "cha": 13},
           "armor": 2, "damage": "d6"}


def test_dm_spells_shows_them_and_offers_the_choice_to_a_caster_who_never_chose():
    import asyncio

    sent, offered = [], []
    Msg, dispatch, client = _dm_client({**_seeded_state(), "sheets": {"Thorin": dict(_THORIN)}}, sent)
    client.director = type("D", (), {"players": {11: "Thorin"}})()

    async def offer(channel_id, user_id, actor):
        offered.append((user_id, actor))

    client._offer_spells = offer
    asyncio.run(dispatch(Msg("?dm spells")))
    assert sent == ["**Thorin** — Spells — Granted: none · rotes: Light, Sanctify, Guidance, "
                    "Words of the Unspeaking (stone)."]
    assert offered == [(11, "Thorin")]


def test_choosing_spells_is_the_owners_and_saves_once():
    import asyncio

    from gary.adapters.discord_bot import GaryClient
    from gary.adapters.spellbook import SpellPicker

    sheets = {"Thorin": {**_THORIN, "preparing": True, "spells": []}}
    saved, sent, private = [], [], []

    class _Store:
        def state(self):
            return {"sheets": sheets}

        def apply(self, mutations, reason=""):
            for m in mutations:
                sheets[m.target] = m.payload["sheet"]
                saved.append(m.payload["sheet"])
            return type("Diff", (), {"ok": True, "rejected": ()})()

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Client:
        store, sender = _Store(), _Sender()
        director = type("D", (), {"players": {ALICE: "Thorin"}})()

    class _Resp:
        async def send_message(self, text, view=None, ephemeral=False):
            assert ephemeral
            private.append((text, view))

    def interaction(user_id):
        i = type("I", (), {})()
        i.user, i.channel_id, i.response = type("U", (), {"id": user_id})(), CHANNEL, _Resp()
        return i

    client = _Client()
    on_spells = GaryClient.on_spells.__get__(client, _Client)
    client._finish_spells = GaryClient._finish_spells.__get__(client, _Client)

    asyncio.run(on_spells(interaction(BOB), ALICE))
    assert private[-1][0] == "Those spells are someone else's to choose."

    asyncio.run(on_spells(interaction(ALICE), ALICE))
    text, picker = private[-1]
    assert isinstance(picker, SpellPicker) and "**Commune**" in text

    changes = {"spells": ["Bless"], "lost": [], "cast_penalty": 0, "preparing": False}
    assert asyncio.run(client._finish_spells("Thorin", changes, CHANNEL)) is None
    assert saved[-1]["spells"] == ["Bless"] and saved[-1]["preparing"] is False
    assert sent == ["📖 **Thorin** is granted Bless."]
    assert asyncio.run(client._finish_spells("Thorin", changes, CHANNEL)) == "Those spells were already chosen."

    asyncio.run(on_spells(interaction(ALICE), ALICE))
    assert private[-1][0].startswith("Your spells are chosen.")
