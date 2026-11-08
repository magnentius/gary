"""Slash commands: each runs its ?dm handler, or answers privately."""

from __future__ import annotations

import asyncio

import discord
import pytest

from gary.adapters.slash import HELP, register

ROOM = 7


class _Response:
    def __init__(self):
        self.deferred, self.sent = False, []

    async def defer(self, thinking=False):
        self.deferred = True

    async def send_message(self, text, view=None, ephemeral=False):
        self.sent.append((text, view, ephemeral))


class _Interaction:
    def __init__(self, channel_id=ROOM, manager=False):
        self.permissions = type("P", (), {"manage_guild": manager, "administrator": False})()
        self.user = type("U", (), {"id": 11, "display_name": "John", "bot": False})()
        self.channel = type("C", (), {"id": channel_id})()
        self.channel_id = channel_id
        self.id = 99
        self.response = _Response()
        self.deleted = False
        self.edited = None

    async def delete_original_response(self):
        self.deleted = True

    async def edit_original_response(self, content=None):
        self.edited = content


class _Client(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.settings = type("S", (), {"command_prefix": "?dm ", "game_channels": {ROOM}})()
        self.store = type("St", (), {"state": lambda self: {"sheets": {"Thorin": {}, "Dorn": {}}}})()
        self.ran = []
        self.fail = False

    async def _on_dm_command(self, message):
        if self.fail:
            raise RuntimeError("boom")
        self.ran.append((message.content, message.author.id, message.channel.id, message.attachments))

    def _fronts_text(self):
        return "No fronts yet."

    def _state_text(self):
        return "the model's view"


def call(tree, name, interaction, **kw):
    return asyncio.run(tree.get_command(name).callback(interaction, **kw))


@pytest.fixture
def client_and_tree():
    async def make():
        client = _Client()
        return client, register(client)
    return asyncio.run(make())


def test_every_command_is_in_help(client_and_tree):
    _, tree = client_and_tree
    names = {c.name for c in tree.get_commands()}
    assert len(names) == 21
    assert not {"setting", "import", "scene", "time"} & names  # /begin replaced them
    assert {n for n in names if f"`/{n}" not in HELP} == set()


def test_a_command_runs_its_dm_handler_and_clears_the_placeholder(client_and_tree):
    client, tree = client_and_tree
    i = _Interaction()
    call(tree, "begin", i)
    assert i.response.deferred and i.deleted
    assert client.ran == [("?dm begin", 11, ROOM, [])]
    call(tree, "dice", _Interaction(), character=None)
    assert client.ran[-1][0] == "?dm dice"


def test_a_failure_replaces_the_placeholder_instead_of_leaving_it_spinning(client_and_tree):
    client, tree = client_and_tree
    client.fail = True
    i = _Interaction()
    call(tree, "undo", i)
    assert i.edited == "That didn't work — the error is in Gary's log." and not i.deleted


def test_outside_a_game_channel_gary_says_so_privately(client_and_tree):
    client, tree = client_and_tree
    i = _Interaction(channel_id=123)
    call(tree, "checks", i)
    assert i.response.sent == [("Gary doesn't play in this channel.", None, True)] and client.ran == []


def test_help_and_fronts_answer_privately(client_and_tree):
    _, tree = client_and_tree
    i = _Interaction()
    call(tree, "help", i)
    assert i.response.sent[0][0] == HELP and i.response.sent[0][2] is True
    i = _Interaction(manager=True)
    call(tree, "fronts", i)
    assert i.response.sent == [("No fronts yet.", None, True)]


def test_fronts_and_state_are_for_server_managers_only(client_and_tree):
    """With Gemini as the GM, a player who could open /fronts could read the plot."""
    _, tree = client_and_tree
    for name in ("fronts", "state"):
        player = _Interaction()
        call(tree, name, player)
        (text, _, private), = player.response.sent
        assert private and "Manage Server" in text
        assert tree.get_command(name).default_permissions.manage_guild
    manager = _Interaction(manager=True)
    call(tree, "state", manager)
    assert manager.response.sent == [("the model's view", None, True)]


def test_character_names_autocomplete(client_and_tree):
    _, tree = client_and_tree
    choices = asyncio.run(tree.get_command("playing")._params["character"].autocomplete(_Interaction(), "tho"))
    assert [c.name for c in choices] == ["Thorin"]


def test_new_opens_the_character_form_privately(client_and_tree):
    from gary.adapters.creator import CharacterCreator

    client, tree = client_and_tree
    client._finish_character = lambda i, sheet, channel_id: None
    i = _Interaction()
    call(tree, "new", i)
    ((text, view, ephemeral),) = i.response.sent
    assert isinstance(view, CharacterCreator) and ephemeral


def test_sheet_answers_privately(client_and_tree):
    from gary.adapters.discord_bot import GaryClient

    client, tree = client_and_tree
    client._sheet_hub = GaryClient._sheet_hub.__get__(client)
    client.director = type("D", (), {"players": {}})()
    i = _Interaction()
    call(tree, "sheet", i)
    assert i.response.sent == [("You have no sheet yet. `/new` makes one.", None, True)]


def test_questions_join_their_options(client_and_tree):
    client, tree = client_and_tree
    call(tree, "questions", _Interaction(), first="Did we win?", second="Did we lose?")
    assert client.ran[-1][0] == "?dm questions Did we win? | Did we lose?"
    call(tree, "questions", _Interaction(), default=True)
    assert client.ran[-1][0] == "?dm questions default"


def test_a_typed_dm_command_is_pointed_at_the_slash_commands():
    from gary.adapters.discord_bot import SLASH_ONLY, GaryClient

    sent, ran = [], []

    class _Sender:
        async def send(self, channel_id, text, prompt=None):
            sent.append(text)

    class _Stub:
        settings = type("S", (), {"command_prefix": "?dm ", "game_channels": {ROOM}, "freeform_channels": set()})()
        sender = _Sender()
        user = type("U", (), {"id": 1})()

        async def _on_dm_command(self, message):
            ran.append(message.content)

    author = type("A", (), {"id": 11, "bot": False, "display_name": "John"})()
    message = type("M", (), {"author": author, "channel": type("C", (), {"id": ROOM})(), "content": "?dm sheet"})()
    asyncio.run(GaryClient.on_message(_Stub(), message))
    assert sent == [SLASH_ONLY] and ran == []


def test_do_posts_the_action_and_plays_it_as_a_message_to_gary():
    import asyncio

    from gary.adapters.discord_bot import GaryClient

    seen, ran, said = [], [], []

    class _Director:
        players = {7: "Dorn"}

        def on_player_message(self, **kw):
            seen.append(kw)
            return ["narrate"]

    class _Runner:
        async def run(self, effects):
            ran.extend(effects)

    class _Response:
        async def send_message(self, text):
            said.append(text)

    class _User:
        id, display_name = 7, "alice"

    class _Interaction:
        user, channel_id, response = _User(), 42, _Response()

    client = type("C", (), {"director": _Director(), "runner": _Runner(), "on_do": GaryClient.on_do})()
    asyncio.run(client.on_do(_Interaction(), "  I hurl my   spear at the archer "))
    assert said == ["**Dorn:** I hurl my spear at the archer"]
    assert seen[0]["mentions_bot"] is True and seen[0]["content"] == "I hurl my spear at the archer"
    assert ran == ["narrate"]
