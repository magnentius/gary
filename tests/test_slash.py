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
    def __init__(self, channel_id=ROOM):
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
    assert len(names) == 17
    assert {n for n in names if f"`/{n}" not in HELP} == set()


def test_a_command_runs_its_dm_handler_and_clears_the_placeholder(client_and_tree):
    client, tree = client_and_tree
    i = _Interaction()
    call(tree, "scene", i, location="The Eel-Runs", summary="fever-mist on the ditches")
    assert i.response.deferred and i.deleted
    assert client.ran == [("?dm scene The Eel-Runs | fever-mist on the ditches", 11, ROOM, [])]
    call(tree, "dice", _Interaction(), character=None)
    assert client.ran[-1][0] == "?dm dice"


def test_an_attachment_reaches_import(client_and_tree):
    client, tree = client_and_tree
    doc = object()
    call(tree, "import", _Interaction(), document=doc)
    assert client.ran[-1][0] == "?dm import" and client.ran[-1][3] == [doc]


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
    i = _Interaction()
    call(tree, "fronts", i)
    assert i.response.sent == [("No fronts yet.", None, True)]


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
