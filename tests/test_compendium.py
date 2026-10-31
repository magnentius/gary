"""Compendium classes: qualify in the fiction, take the starting move at the next level-up,
then its moves count as class moves."""

from __future__ import annotations


from gary import class_moves, compendium
from gary.fsm import Director, SendMessage, UpdateSheet
from gary.levelling import Advance, move_options
from gary.sheets import stat_modifier

CHANNEL, ALICE = 555, 111
DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 3, "xp": 10, "hp": 20, "max_hp": 20,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 0, "damage": "d10"}


def test_eight_classes_from_the_core_book_and_the_player_compendium():
    assert list(compendium.CLASSES) == ["Awakened Mind", "Bearer", "Blob", "Dragonslayer", "Flux",
                                        "Landed Gentry", "Wraithlander", "Divine Bond"]
    dragon = compendium.CLASSES["Dragonslayer"]
    assert dragon["requirement"].startswith("When you drink the still-warm heart-blood of a dragon")
    assert dragon["starting"]["name"] == "Dovahkiin" and len(dragon["moves"]) == 6
    assert compendium.move("Hunger")["class"] == "Bearer"


def test_qualifying_records_it_and_says_so(registry, clock, dice):
    sheets = {"Dorn": dict(DORN)}
    d = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice, players={ALICE: "Dorn"},
                 sheet_for=sheets.get)
    effects, result = d.qualify_compendium(channel_id=CHANNEL, character="dorn", compendium_class="dragonslayer",
                                           reason="he drank the red wyrm's heart-blood")
    assert result["ok"] and UpdateSheet(CHANNEL, "Dorn", {"compendium_qualified": ["Dragonslayer"]}) in effects
    assert "**Dovahkiin** (Dragonslayer)" in [e.text for e in effects if isinstance(e, SendMessage)][0]


def test_the_starting_move_is_offered_at_the_next_level_and_then_lapses():
    sheet = {**DORN, "compendium_qualified": ["Dragonslayer"]}
    assert "Dovahkiin" in [m["name"] for m in move_options(sheet, 4)]
    a = Advance(sheet)
    a.choose_stat("str")
    a.choose_move("Dovahkiin")
    after = a.result()
    assert "Dovahkiin" in after["advanced_moves"] and after["compendium_qualified"] == []
    # From now on its moves count as class moves.
    names = [m["name"] for m in move_options(after, 5)]
    assert {"Fury", "Tireless Blood", "Dragon’s Breath"} <= set(names) and "Dovahkiin" not in names
    # Skipped, the chance is gone.
    other = Advance({**DORN, "compendium_qualified": ["Bearer"]})
    other.choose_stat("str")
    other.choose_move("Iron Hide")
    assert other.result()["compendium_qualified"] == []


def test_wraithlander_chooses_a_mark_and_the_arm_pierces_the_living(registry, clock, dice):
    a = Advance({**DORN, "compendium_qualified": ["Wraithlander"]})
    a.choose_stat("str")
    a.choose_move("From The Forbidden Land Returned")
    assert a.step() == "move_choice" and a.move_choice_options() == ["Eye", "Tongue", "Arm", "Legs"]
    a.choose_move_choice("Arm")
    sheet = a.result()
    assert sheet["wraith_marks"] == ["Arm"]
    sheet.pop("gear", None)
    scene = {"Orc 1": {"kind": "Orc Berserker", "hp": 20, "max_hp": 20, "armor": 3, "damage": "d10"},
             "Skeleton 1": {"kind": "Zombie", "hp": 20, "max_hp": 20, "armor": 3, "damage": "d6"}}
    d = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice, players={ALICE: "Dorn"},
                 sheet_for={"Dorn": sheet}.get, monsters=lambda: scene)
    dice.queue(5)
    _, alive = d.deal_damage(channel_id=CHANNEL, character="Dorn", target="Orc 1", reason="x")
    dice.queue(5)
    _, dead = d.deal_damage(channel_id=CHANNEL, character="Dorn", target="Skeleton 1", reason="x")
    assert alive["dealt"] == 4 and dead["dealt"] == 2  # 5 vs armor 3: 2 piercing for the living only


def test_tireless_blood_ignores_sick():
    sick = {**DORN, "debilities": ["sick"]}
    assert stat_modifier(sick, "con") == 0  # Con 15 (+1) -1 sick
    assert stat_modifier({**sick, "advanced_moves": ["Dovahkiin", "Tireless Blood"]}, "con") == 1


def test_compendium_moves_reach_the_model_and_bind_rolls(registry, clock, dice):
    sheet = {**DORN, "advanced_moves": ["Bind"]}
    text = class_moves.render({"Dorn": sheet})
    assert "Bind (Flux, compendium):" in text and "move=bind" in text
    d = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice, players={ALICE: "Dorn"},
                 sheet_for={"Dorn": sheet}.get)
    (ask,) = [e.text for e in d.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="bind")
              if isinstance(e, SendMessage)]
    assert "Bind" in ask and "+Con +1" in ask


def test_the_level_up_form_labels_compendium_moves_and_asks_for_the_mark():
    import asyncio

    from gary.adapters.levelup import LevelUpForm

    async def run():
        form = LevelUpForm({**DORN, "compendium_qualified": ["Wraithlander"]}, lambda *a: None)
        form.advance.choose_stat("str")
        form._build()
        (select,) = [c for c in form.children if hasattr(c, "options")]
        assert "From The Forbidden Land Returned (Wraithlander)" in [o.label for o in select.options]
        form.advance.choose_move("From The Forbidden Land Returned")
        form._build()
        (select,) = [c for c in form.children if hasattr(c, "options")]
        assert [o.label for o in select.options] == ["Eye", "Tongue", "Arm", "Legs"]
        assert "bears the mark" in form.prompt()

    asyncio.run(run())
