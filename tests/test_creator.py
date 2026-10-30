"""`?dm new`: the private character form, and what happens when it finishes.

Discord is faked at the interaction boundary; the form, the Draft, the store and the
Director are real. A click is a call to the component's callback with an interaction that
records how it was answered.
"""

from __future__ import annotations

import pytest

from gary.adapters.creator import CharacterCreator, NameModal, StartButton
from gary.fsm import Director
from gary.registry import PendingCheckRegistry
from gary.state.backend import InMemoryStateBackend
from gary.state.store import CampaignStore

CHANNEL = 7
ALICE = 111
BOB = 222


class _Response:
    def __init__(self):
        self.edits = []      # (content, view) the form was edited to
        self.private = []    # (content, view) sent ephemerally
        self.modals = []

    async def edit_message(self, content=None, view=None):
        self.edits.append((content, view))

    async def send_message(self, content=None, view=None, ephemeral=False):
        assert ephemeral, "nothing about character creation is posted publicly from a click"
        self.private.append((content, view))

    async def send_modal(self, modal):
        self.modals.append(modal)


class _User:
    def __init__(self, user_id, name="Alice"):
        self.id = user_id
        self.display_name = name


class _Interaction:
    def __init__(self, user_id=ALICE, name="Alice"):
        self.user = _User(user_id, name)
        self.channel_id = CHANNEL
        self.response = _Response()


def _component(view, kind, label=None):
    """The view's select, or its button with this label."""
    import discord

    for item in view.children:
        if kind == "select" and isinstance(item, discord.ui.Select):
            return item
        if kind == "button" and isinstance(item, discord.ui.Button) and item.label == label:
            return item
    raise AssertionError(f"no {kind} {label or ''} in {[getattr(i, 'label', i) for i in view.children]}")


async def choose(view, value):
    select = _component(view, "select")
    assert value in [o.value for o in select.options], f"{value} not offered"
    select._values = [value]  # what discord.py sets from the click payload
    interaction = _Interaction()
    await select.callback(interaction)
    return interaction


async def press(view, label, interaction=None):
    interaction = interaction or _Interaction()
    await _component(view, "button", label).callback(interaction)
    return interaction


async def make_kael(view):
    await choose(view, "fighter")
    await choose(view, "Dwarf")
    for stat in ("str", "con", "dex", "wis", "cha"):
        await choose(view, stat)
    interaction = await press(view, "Name your character")
    (modal,) = interaction.response.modals
    modal.name._value = "Kael"
    await modal.on_submit(_Interaction())


# -- the form -----------------------------------------------------------------------------


async def test_the_form_walks_class_race_stats_name_then_asks_to_confirm():
    finished = []

    async def finish(interaction, sheet):
        finished.append(sheet)
        return None

    view = CharacterCreator(finish)
    assert view.prompt() == "Choose a class."
    await make_kael(view)

    assert view.prompt().endswith("Create this character?")
    assert "Kael — Dwarf Fighter 1, HP 25/25" in view.prompt()

    done = await press(view, "Create character")
    (sheet,) = finished
    assert sheet["stats"]["str"] == 16 and sheet["stats"]["int"] == 8
    content, final_view = done.response.edits[-1]
    assert content.startswith("✅ Created.") and final_view is None


async def test_only_the_classes_races_are_offered():
    view = CharacterCreator(None)
    await choose(view, "paladin")
    assert [o.value for o in _component(view, "select").options] == ["Human"]


async def test_each_stat_step_offers_only_the_stats_left():
    view = CharacterCreator(None)
    await choose(view, "thief")
    await choose(view, "Halfling")
    assert "Where does your **16** go?" in view.prompt()
    await choose(view, "dex")
    offered = [o.value for o in _component(view, "select").options]
    assert "dex" not in offered and len(offered) == 5
    assert "Where does your **15** go?" in view.prompt()


async def test_a_bad_name_is_shown_and_the_form_waits():
    view = CharacterCreator(None)
    await choose(view, "wizard")
    await choose(view, "Elf")
    for stat in ("int", "wis", "dex", "con", "cha"):
        await choose(view, stat)
    modal = NameModal(view)
    modal.name._value = "   "
    submitted = _Interaction()
    await modal.on_submit(submitted)

    content, _ = submitted.response.edits[-1]
    assert "needs a name" in content
    assert view.draft.name is None


async def test_a_refusal_from_saving_is_shown_and_nothing_is_lost():
    async def finish(interaction, sheet):
        return "Someone at this table already plays a character named Kael."

    view = CharacterCreator(finish)
    await make_kael(view)
    refused = await press(view, "Create character")

    content, still = refused.response.edits[-1]
    assert "already plays a character named Kael" in content
    assert still is view and view.draft.complete  # rename and try again


async def test_start_button_opens_the_form_privately_for_its_owner_only():
    view = StartButton(ALICE, "Alice", finish=None)
    button = _component(view, "button", "Create character")

    bob = _Interaction(BOB, "Bob")
    await button.callback(bob)
    ((refusal, refusal_view),) = bob.response.private
    assert "Alice's" in refusal and refusal_view is None

    alice = _Interaction(ALICE)
    await button.callback(alice)
    ((prompt, form),) = alice.response.private
    assert prompt == "Choose a class." and isinstance(form, CharacterCreator)


# -- saving ----------------------------------------------------------------------------------


@pytest.fixture
def table(clock):
    from gary.adapters.discord_bot import GaryClient

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock,
                        game_channels={CHANNEL})
    sent = []

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Client:
        pass

    client = _Client()
    client.store, client.director, client.sender = store, director, _Sender()
    save = GaryClient._finish_character.__get__(client, _Client)
    return save, store, director, sent


async def _kael_sheet():
    sheets = []

    async def finish(interaction, sheet):
        sheets.append(sheet)

    view = CharacterCreator(finish)
    await make_kael(view)
    await press(view, "Create character")
    return sheets[0]


async def test_saving_stores_the_sheet_claims_it_and_tells_the_table(table):
    save, store, director, sent = table
    sheet = await _kael_sheet()

    assert await save(_Interaction(ALICE), sheet, CHANNEL) is None
    state = store.state()
    assert state["sheets"]["Kael"] == sheet
    assert state["players"] == {str(ALICE): "Kael"}
    assert director.players == {ALICE: "Kael"}  # checks for Kael are Alice's to roll
    assert sent == [f"**Kael** joins the party — Kael — Dwarf Fighter 1, HP 25/25, armor 0, "
                    f"damage d10 · STR 16 (+2) DEX 13 (+1) CON 15 (+1) INT 8 (-1) "
                    f"WIS 12 (+0) CHA 9 (+0)"]


async def test_the_sheet_and_the_claim_undo_together(table):
    """One journal entry per mutation; undoing both leaves neither."""
    save, store, director, _ = table
    await save(_Interaction(ALICE), await _kael_sheet(), CHANNEL)
    store.undo_last()
    store.undo_last()
    assert store.state()["sheets"] == {}


async def test_a_name_another_player_has_is_refused(table):
    save, store, director, sent = table
    director.set_player_character(BOB, "Kael")

    error = await save(_Interaction(ALICE), await _kael_sheet(), CHANNEL)

    assert "already plays a character named Kael" in error
    assert store.state()["sheets"] == {} and sent == []
    assert director.players == {BOB: "Kael"}


async def test_remaking_your_own_character_is_allowed(table):
    save, store, director, _ = table
    sheet = await _kael_sheet()
    await save(_Interaction(ALICE), sheet, CHANNEL)
    assert await save(_Interaction(ALICE), sheet, CHANNEL) is None


# -- the commands -----------------------------------------------------------------------------


def _dispatcher(table):
    from gary.adapters.discord_bot import GaryClient

    save, store, director, sent = table
    posted = []

    class _Settings:
        command_prefix = "?dm "

    class _Channel:
        id = CHANNEL

        async def send(self, text, view=None):
            posted.append((text, view))

    class _Msg:
        channel = _Channel()

        def __init__(self, content, user_id=ALICE):
            self.content = content
            self.author = _User(user_id)

    class _Client:
        settings = _Settings()
        _finish_character = GaryClient._finish_character

    client = _Client()
    client.store, client.director, client.sender = store, director, save.__self__.sender
    return _Msg, GaryClient._on_dm_command.__get__(client, _Client), posted


async def test_dm_new_posts_a_start_button_for_whoever_asked(table):
    msg, dispatch, posted = _dispatcher(table)
    await dispatch(msg("?dm new"))

    ((text, view),) = posted
    assert "Alice" in text
    assert isinstance(view, StartButton) and view.owner_id == ALICE


async def test_dm_sheet_shows_your_sheet_or_says_how_to_make_one(table):
    save, store, director, sent = table
    msg, dispatch, _ = _dispatcher(table)

    await dispatch(msg("?dm sheet"))
    assert sent[-1] == "You have no sheet yet. `?dm new` makes one."

    await save(_Interaction(ALICE), await _kael_sheet(), CHANNEL)
    await dispatch(msg("?dm sheet"))
    assert sent[-1].startswith("Kael — Dwarf Fighter 1, HP 25/25")
