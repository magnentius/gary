"""Creation choices: read from the book's move texts, made in forms, two of them wired."""

from __future__ import annotations

import asyncio

import pytest

from gary import choices
from gary.choices import Choices, look_of, look_steps, steps
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
    def own(cls, race):
        return [s.key for s in steps(cls, race) if not s.key.startswith("look_")]

    assert own("Fighter", "Human") == ["look"]  # the signature weapon's look
    assert own("Fighter", "Elf") == ["look", "precise_weapon"]
    assert own("Ranger", "Elf") == []


def test_every_class_has_a_look_from_the_book():
    fighter = look_steps("Fighter")
    assert fighter[0].options == ("Hard Eyes", "Dead Eyes", "Eager Eyes") and len(fighter) == 5
    assert fighter[-1].optional and fighter[-1].written
    assert look_steps("Barbarian")[2].options == ("Strange tattoos", "Unusual jewelry", "Unmarred by decoration")
    assert look_steps("Druid")[-2].options[1] == "Practical Leathers"  # three lists, then the free line
    c = Choices("Ranger", "Elf")
    for v in ("Wild Eyes", "Bald", "Cape", "Lithe Body", ""):
        c.choose(v)
    assert c.complete and look_of({"choices": c.made}) == "Wild Eyes, Bald, Cape, Lithe Body"
    assert look_of({"choices": {**c.made, "look_own": "a hawk feather behind one ear"}}).endswith(
        "; a hawk feather behind one ear")


def test_choosing_checks_each_step():
    c = Choices("Cleric", "Dwarf")
    with pytest.raises(SheetError, match="Write something"):
        c.choose("   ")
    c.choose("Krugon the Bleak")
    with pytest.raises(SheetError, match="not one of the choices"):
        c.choose("Fishing")
    c.choose("What Lies Beneath")
    c.choose(steps("Cleric", "Dwarf")[2].options[2])
    assert c.step().key == "look_0"  # then the look
    for v in ("Kind Eyes", "Bald", "Habit", "Thin Body", ""):
        c.choose(v)
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
    assert UpdateSheet(CHANNEL, "Nim", {"poison_uses": 0, "poisons_used": ["Bloodweed"]}) in effects
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
        assert "Your look (1 of 3)" in edits[-1][0]  # the Druid's book lists no body
        for look in ("Wise Eyes", "Braided Hair", "Weathered Hides", ""):
            await (await form._choose(look))(type("I", (), {"response": _Resp()})())

    asyncio.run(run())
    assert saved == [{"land": "The Great Forests", "tell": "antlers of birch", "look_0": "Wise Eyes",
                      "look_1": "Braided Hair", "look_2": "Weathered Hides", "look_own": ""}]
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
        for value in ("Testgod", "Civilization", "Petition: Offering", "Kind Eyes", "Tonsure", "Habit",
                      "Thin Body", ""):
            d.choose_class_choice(value)
        d.choose_name("Formtest")
        return view.prompt()

    text = asyncio.run(run())
    assert "Alignment — Good: Endanger yourself to heal another." in text
    assert "serves Testgod, god of Civilization (Petition: Offering)" in text
    assert text.endswith("Create this character?")


# -- the Thief's poison moves -------------------------------------------------------------

def _dose(name):
    from gary.gear import item_for

    return item_for(name)[0].to_dict()


def test_a_bought_serpents_tears_is_recognised_however_its_spelt(director, sheets):
    sheets["Nim"] = {**THIEF, "choices": {}, "gear": [_dose("Serpent's Tears")]}
    _, result = director.use_poison(channel_id=CHANNEL, character="Nim", poison="serpent's tears")
    assert result["ok"] and result["poison"] == "Serpent’s Tears" and "Dangerous" in result["note"]


def test_poison_master_used_once_is_no_longer_dangerous(director, sheets):
    sheets["Nim"] = {**THIEF, "advanced_moves": ["Poison Master"], "poisons_used": ["Goldenroot"],
                     "gear": [_dose("Goldenroot"), _dose("Oil of Tagit")]}
    _, known = director.use_poison(channel_id=CHANNEL, character="Nim", poison="Goldenroot")
    _, new = director.use_poison(channel_id=CHANNEL, character="Nim", poison="Oil of Tagit")
    assert "Poison Master" in known["note"] and new["note"].startswith("Dangerous")


def test_envenom_makes_a_safe_poison_on_a_weapon_touch(director, sheets):
    sheets["Nim"] = {**THIEF, "advanced_moves": ["Envenom"]}
    _, result = director.use_poison(channel_id=CHANNEL, character="Nim", target="the guard", on_weapon=True)
    assert result["note"].startswith("Envenom")


def test_brewer_makes_three_doses_of_a_poison_used_before(director, sheets):
    sheets["Nim"] = {**THIEF, "advanced_moves": ["Brewer"], "poisons_used": ["Goldenroot"], "gear": []}
    effects, result = director.brew_poison(channel_id=CHANNEL, character="Nim", reason="a locked cellar",
                                           poison="goldenroot")
    assert result["ok"] and [g["name"] for g in effects[1].changes["gear"]] == ["Goldenroot"] * 3
    _, refused = director.brew_poison(channel_id=CHANNEL, character="Nim", reason="a locked cellar",
                                      poison="Oil of Tagit")
    assert "never used Oil of Tagit" in refused["error"]


def test_alchemist_designs_a_poison_with_caveats(director, sheets):
    sheets["Nim"] = {**THIEF, "advanced_moves": ["Alchemist"], "gear": []}
    _, refused = director.brew_poison(channel_id=CHANNEL, character="Nim", reason="a locked cellar",
                                      poison="Widow's Kiss", effect="paralyses for a minute")
    assert "caveats" in refused["error"]
    effects, made = director.brew_poison(channel_id=CHANNEL, character="Nim", reason="a locked cellar",
                                         poison="Widow's Kiss", effect="paralyses for a minute",
                                         caveats=["only in the dark"])
    assert made["ok"] and "only in the dark" in made["effect"]
    sheets["Nim"].update(effects[1].changes)
    _, used = director.use_poison(channel_id=CHANNEL, character="Nim", poison="Widow's Kiss")
    assert used["ok"] and "paralyses" in used["effect"]


def test_poisons_on_a_creature_mark_it_and_show(director, sheets, scene):
    from gary.fsm import AfflictMonster, ConditionMonster
    from gary.monsters import describe as describe_monster

    scene["Guard 1"] = {"kind": "Guard", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d6"}
    for name in ("Oil of Tagit", "Goldenroot"):
        sheets["Nim"] = {**THIEF, "choices": {}, "gear": [_dose(name)]}
        effects, _ = director.use_poison(channel_id=CHANNEL, character="Nim", target="guard 1", poison=name)
        (mark,) = [e for e in effects if isinstance(e, ConditionMonster)]
        assert mark.conditions[-1]["kind"] == ("asleep" if name == "Oil of Tagit" else "trusting")
    sheets["Nim"] = dict(THIEF)  # back to her mastered Bloodweed
    effects, _ = director.use_poison(channel_id=CHANNEL, character="Nim", target="Guard 1")
    (weed,) = [e for e in effects if isinstance(e, AfflictMonster)]
    scene["Guard 1"]["afflictions"] = list(weed.afflictions)
    assert "poisoned: Bloodweed" in describe_monster("Guard 1", scene["Guard 1"])


def test_poison_does_nothing_to_the_undead(director, sheets, scene):
    from gary.fsm import AfflictMonster, ConditionMonster

    scene["Skeleton 1"] = {"kind": "Skeleton", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d6"}
    effects, result = director.use_poison(channel_id=CHANNEL, character="Nim", target="Skeleton 1")
    assert result["uses_left"] == 0  # the dose is spent
    assert not [e for e in effects if isinstance(e, (AfflictMonster, ConditionMonster))]
    assert "the dead are past poison" in result["note"]


def test_poison_does_nothing_to_a_construct(director, sheets, scene):
    from gary.fsm import AfflictMonster, construct

    scene["Iron Golem 1"] = {"kind": "Iron Golem", "hp": 20, "max_hp": 20, "armor": 3, "damage": "d10+3"}
    effects, result = director.use_poison(channel_id=CHANNEL, character="Nim", target="Iron Golem 1")
    assert not [e for e in effects if isinstance(e, AfflictMonster)]
    assert "a made thing has no blood" in result["note"]
    assert construct({"kind": "Clockwork Sentinel"}) and not construct({"kind": "Goblin"})
