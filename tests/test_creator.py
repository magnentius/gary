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
    """Pick from the form's dropdown: one value, or a list for a pick-several step."""
    select = _component(view, "select")
    values = value if isinstance(value, list) else [value]
    offered = [o.value for o in select.options]
    assert all(v in offered for v in values), f"{values} not all offered in {offered}"
    assert select.min_values == select.max_values == len(values)
    select._values = values  # what discord.py sets from the click payload
    interaction = _Interaction()
    await select.callback(interaction)
    return interaction


async def press(view, label, interaction=None):
    interaction = interaction or _Interaction()
    await _component(view, "button", label).callback(interaction)
    return interaction


async def fighter_gear(view):
    """Chainmail and gear; potions and coins; a sharp, serrated signature sword."""
    await choose(view, "0")
    await choose(view, ["0", "3"])
    await choose(view, "Sword")
    await choose(view, "close")
    await choose(view, ["1", "3"])
    await choose(view, "Good")
    await choose(view, "Ancient")


async def make_kael(view):
    await choose(view, "fighter")
    await choose(view, "Dwarf")
    for stat in ("str", "con", "dex", "wis", "cha"):
        await choose(view, stat)
    await fighter_gear(view)
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
    assert "Kael — Dwarf Fighter 1 (0/8 XP), HP 25/25" in view.prompt()

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
    for _ in range(3):  # leather; dagger; healing potion
        await choose(view, "0")
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
    assert sent == ["**Kael** joins the party — Dwarf Fighter 1 (0/8 XP), HP 25/25, armor 1, "
                    "damage d10 · STR 16 (+2) DEX 13 (+1) CON 15 (+1) INT 8 (-1) "
                    "WIS 12 (+0) CHA 9 (+0) · wields Signature sword (close, +1 damage, "
                    "2 piercing) · rations 5 · 22 coins · weight 5/14"]


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
        _finish_gear = GaryClient._finish_gear
        _sheet_hub = GaryClient._sheet_hub
        _sheet_actions = GaryClient._sheet_actions

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
    msg, dispatch, posted = _dispatcher(table)

    await dispatch(msg("?dm sheet"))
    assert sent[-1] == "You have no sheet yet. `?dm new` makes one."

    await save(_Interaction(ALICE), await _kael_sheet(), CHANNEL)
    await dispatch(msg("?dm sheet"))
    text, view = posted[-1]
    assert text.startswith("Kael — Dwarf Fighter 1 (0/8 XP), HP 25/25")
    assert "\nCarrying: Signature sword (close, +1 damage, 2 piercing), Dungeon rations" in text
    assert "2 × Healing Potion" in text
    # Alone at the table, a made Fighter has only alignment to revisit: no bonds yet, no level.
    assert [b.label for b in view.children] == ["Alignment"]



# -- gear for a character made before gear --------------------------------------------------


THORIN = {"name": "Thorin", "class": "Cleric", "race": "Dwarf", "level": 1, "xp": 0,
          "stats": {"str": 15, "dex": 9, "con": 13, "int": 12, "wis": 16, "cha": 8},
          "max_hp": 21, "hp": 18, "damage": "d6", "armor": 0}


async def test_the_gear_form_asks_only_for_gear_and_keeps_everything_else():
    taken = []

    async def finish(interaction, sheet):
        taken.append(sheet)

    view = CharacterCreator(finish, sheet=THORIN)
    assert view.prompt().endswith("Starting gear — choose your defenses.")
    await choose(view, "0")   # chainmail
    await choose(view, "1")   # mace
    await choose(view, "1")   # healing potion
    assert view.prompt().endswith("Take this gear?")
    done = await press(view, "Take this gear")

    (sheet,) = taken
    assert (sheet["hp"], sheet["max_hp"], sheet["armor"]) == (18, 21, 1)
    assert [g["name"] for g in sheet["gear"]][-3:] == ["Chainmail", "Mace", "Healing potion"]
    assert done.response.edits[-1][0].startswith("✅ Geared up.")


async def test_dm_gear_posts_a_button_and_saving_journals_the_sheet(table):
    from gary.models import Mutation, MutationKind
    from gary.sheets import with_gear
    from gary.gear import CHAINMAIL

    save, store, director, sent = table
    store.apply([Mutation(MutationKind.SHEET, "Thorin", {"sheet": THORIN})], reason="old")
    director.set_player_character(ALICE, "Thorin")
    msg, dispatch, posted = _dispatcher(table)

    await dispatch(msg("?dm gear"))
    ((text, view),) = posted
    assert [b.label for b in view.children] == ["Choose gear"] and "Thorin" in text

    await dispatch.__self__._finish_gear(with_gear(THORIN, [CHAINMAIL]), CHANNEL)
    assert store.state()["sheets"]["Thorin"]["armor"] == 1
    assert sent[-1].startswith("**Thorin** gears up — Dwarf Cleric 1 (0/8 XP), HP 18/21, armor 1")
    store.undo_last()
    assert "gear" not in store.state()["sheets"]["Thorin"]

    store.apply([Mutation(MutationKind.SHEET, "Thorin", {"sheet": with_gear(THORIN, [CHAINMAIL])})],
                reason="geared")
    await dispatch(msg("?dm gear"))
    assert "already has their gear" in sent[-1]


async def test_stats_show_in_the_rules_order_even_from_a_sorted_sheet():
    """Read back from the store, a sheet's stats come alphabetical: CHA first."""
    sorted_thorin = {**THORIN, "stats": dict(sorted(THORIN["stats"].items()))}
    view = CharacterCreator(None, sheet=sorted_thorin)
    assert "STR 15 · DEX 9 · CON 13 · INT 12 · WIS 16 · CHA 8" in view.prompt()
