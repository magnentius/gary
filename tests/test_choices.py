"""Creation choices: read from the book's move texts, made in forms, two of them wired."""

from __future__ import annotations

import asyncio

import pytest

from gary import choices
from gary.choices import Choices, steps
from gary.fsm import Director, SendMessage, UpdateSheet
from gary.sheets import Draft, SheetError

CHANNEL, ALICE = 555, 111


def test_each_class_chooses_from_the_books_lists():
    cleric = {s.key: s for s in steps("Cleric", "Dwarf")}
    assert cleric["deity"].written
    assert "Bloody Conquest" in cleric["domain"].options
    assert cleric["precept"].options[3] == "Petition: Personal Victory"
    assert cleric["precept"].descriptions[3] == "Your religion believes in trial by combat"
    assert all(len(o) <= 40 for s in cleric.values() for o in s.options)
    assert len({s.key: s for s in steps("Druid", "Elf")}["land"].options) == 11
    assert steps("Bard", "Human")[0].options[0] == "Spells and Magicks"
    poison = steps("Thief", "Halfling")[0]
    assert poison.options == ("Oil of Tagit", "Bloodweed", "Goldenroot", "Serpent’s Tears")
    assert poison.descriptions[1] == "touch: The target deals -1d4 damage ongoing until cured"
    assert [s.key for s in steps("Fighter", "Human")] == ["look"]
    assert [s.key for s in steps("Fighter", "Elf")] == ["look", "precise_weapon"]
    assert steps("Ranger", "Elf") == []


def test_choosing_checks_each_step():
    c = Choices("Cleric", "Dwarf")
    with pytest.raises(SheetError, match="Write something"):
        c.choose("   ")
    c.choose("Krugon the Bleak")
    with pytest.raises(SheetError, match="not one of the choices"):
        c.choose("Fishing")
    c.choose("What Lies Beneath")
    c.choose(steps("Cleric", "Dwarf")[2].options[2])
    assert c.complete and c.made["deity"] == "Krugon the Bleak"


def test_a_thief_starts_with_three_uses_and_the_model_sees_it():
    sheet = choices.on_sheet({"class": "Thief"}, {"poison": "Goldenroot"})
    assert sheet["poison_uses"] == 3
    assert choices.describe(sheet).startswith("Choices — poison: Goldenroot (applied: The target treats")
    again = choices.on_sheet({**sheet, "poison_uses": 1}, {"poison": "Goldenroot"})
    assert again["poison_uses"] == 1  # not refilled by choosing again


def test_a_cleric_built_in_the_draft_carries_their_choices():
    d = Draft()
    d.choose_class("cleric")
    d.choose_race("Dwarf")
    assert d.class_choices().step().key == "deity"
    d.choose_class_choice("Sucellus")
    d.choose_race("Human")  # a new race clears them
    assert d.choices == {}


@pytest.mark.parametrize("chosen,weapon,precise", [
    ("Hammer", "Warhammer", True), ("Sword", "Signature sword", True), ("Sword", "Long Sword", True),
    ("Dagger", "Throwing Daggers", True), ("Axe", "Staff", False), ("Bow", "Crossbow", True),
])
def test_an_elf_fighters_weapon_type(chosen, weapon, precise):
    assert choices.precise_for({"choices": {"precise_weapon": chosen}}, weapon) is precise


# -- in play ------------------------------------------------------------------------------

THIEF = {"name": "Nim", "class": "Thief", "race": "Halfling", "level": 1, "hp": 12, "max_hp": 12,
         "stats": {"str": 8, "dex": 16, "con": 12, "int": 13, "wis": 9, "cha": 15}, "armor": 1,
         "damage": "d8", "choices": {"poison": "Bloodweed"}, "poison_uses": 1}


@pytest.fixture
def sheets():
    return {"Nim": dict(THIEF)}


@pytest.fixture
def scene():
    return {}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Nim"}, sheet_for=sheets.get, monsters=lambda: scene)


def test_using_poison_spends_a_use_until_none_are_left(director, sheets):
    effects, result = director.use_poison(channel_id=CHANNEL, character="nim", target="the guard")
    assert result["ok"] and result["uses_left"] == 0 and "-1d4 damage" in result["effect"]
    assert UpdateSheet(CHANNEL, "Nim", {"poison_uses": 0}) in effects
    sheets["Nim"]["poison_uses"] = 0
    _, result = director.use_poison(channel_id=CHANNEL, character="Nim")
    assert "no Bloodweed left" in result["error"]


def test_brewing_needs_a_quiet_place(director, sheets, scene):
    scene["Goblin 1"] = {"hp": 3, "armor": 1, "damage": "d6"}
    _, result = director.brew_poison(channel_id=CHANNEL, character="Nim", reason="a cellar")
    assert "Goblin 1 still standing" in result["error"]
    scene.clear()
    effects, result = director.brew_poison(channel_id=CHANNEL, character="Nim", reason="a locked cellar")
    assert result == {"ok": True, "poison": "Bloodweed", "uses": 4}


def test_no_poison_no_using_it(director, sheets):
    sheets["Nim"]["choices"] = {}
    _, result = director.use_poison(channel_id=CHANNEL, character="Nim")
    assert "mastered no poison" in result["error"]


def test_an_elf_fighter_hacks_with_dex_using_their_chosen_weapon(director, sheets, scene):
    from gary.gear import Item

    scene["Goblin 1"] = {"hp": 3, "armor": 1, "damage": "d6"}
    sheets["Nim"] = {**THIEF, "class": "Fighter", "race": "Elf", "choices": {"precise_weapon": "Sword"},
                     "gear": [Item("Long Sword", tags=("close", "+1 damage"), damage=1).to_dict()]}
    effects = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Nim", move="hack_and_slash",
                                 target="Goblin 1")
    (ask,) = [e.text for e in effects if isinstance(e, SendMessage)]
    assert ask == "**Nim** — Hack and Slash at Goblin 1, roll +Dex +2."


def test_the_choices_form_saves_when_all_are_made():
    from gary.adapters.creator import ChoicesForm

    saved, edits = [], []

    async def finish(made):
        saved.append(made)

    class _Resp:
        async def edit_message(self, content=None, view=None):
            edits.append((content, view))

    async def run():
        form = ChoicesForm({"class": "Druid", "race": "Elf"}, finish)
        assert "land to which you are attuned" in form.prompt()
        await (await form._choose("The Great Forests"))(type("I", (), {"response": _Resp()})())
        assert "Your tell" in edits[-1][0]
        await (await form._choose("antlers of birch"))(type("I", (), {"response": _Resp()})())

    asyncio.run(run())
    assert saved == [{"land": "The Great Forests", "tell": "antlers of birch"}]
    assert edits[-1] == ("✅ Chosen.", None)


def test_written_choices_have_labels_discord_will_not_cut():
    for cls, race in (("Cleric", "Dwarf"), ("Druid", "Elf")):
        for step in steps(cls, race):
            if step.written:
                assert step.label and len(step.label) <= 45 and len(step.placeholder) <= 100


def test_the_confirmation_shows_alignment_and_class_choices():
    from gary.adapters.creator import CharacterCreator

    async def run():
        view = CharacterCreator(lambda i, s: None)
        d = view.draft
        d.choose_class("cleric")
        d.choose_race("Dwarf")
        for stat in ("wis", "con", "str", "cha", "dex"):
            d.assign(stat, d.next_score())
        d.choose_gear(0, [0])
        d.choose_gear(1, [0])
        d.choose_gear(2, [0])
        d.choose_alignment("Good")
        for value in ("Testgod", "Civilization", "Petition: Offering"):
            d.choose_class_choice(value)
        d.choose_name("Formtest")
        return view.prompt()

    text = asyncio.run(run())
    assert "Alignment — Good: Endanger yourself to heal another." in text
    assert "serves Testgod, god of Civilization (Petition: Offering)" in text
    assert text.endswith("Create this character?")
