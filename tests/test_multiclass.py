"""Multiclass Dabbler, Initiate and Master: one move from another class each."""

from __future__ import annotations


from gary import advanced, class_moves, multiclass
from gary.fsm import Director, SendMessage
from gary.levelling import Advance

CHANNEL, ALICE = 555, 111

DORN = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 3, "xp": 10, "hp": 20, "max_hp": 20,
        "stats": {"str": 16, "dex": 15, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 0,
        "damage": "d10"}


def test_the_level_counts_as_one_lower():
    at_two = [m["name"] for m in multiclass.options(DORN, "thief", 2)]
    assert "Backstab" in at_two and "Cheap Shot" not in at_two  # effective level 1: starting moves only
    at_four = [m["name"] for m in multiclass.options(DORN, "thief", 4)]
    assert "Cheap Shot" in at_four and "Dirty Fighter" not in at_four  # 6-10 needs an effective 6
    cleric = [m["name"] for m in multiclass.options(DORN, "cleric", 4)]
    assert "Commune" not in cleric and "Cast a Spell" not in cleric and "Turn Undead" in cleric


def test_dabbling_on_the_level_up_form_records_the_move():
    a = Advance(DORN)
    a.choose_stat("dex")
    a.choose_move("Multiclass Dabbler")
    assert a.step() == "multi_class"
    assert "fighter" not in a.multi_class_options()
    a.choose_multi_class("thief")
    assert a.step() == "multi_move"
    a.choose_multi_move("Backstab")
    sheet = a.result()
    assert sheet["multiclass_moves"] == [{"class": "thief", "name": "Backstab"}]
    assert a.summary() == "level 4, Dexterity +1, Multiclass Dabbler (Backstab, from the Thief)"


def test_a_dabbled_move_can_be_rolled_and_is_shown_to_the_model(registry, clock, dice):
    sheet = dict(DORN, multiclass_moves=[{"class": "thief", "name": "Backstab"},
                                         {"class": "fighter", "name": "Iron Hide"}])
    director = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                        players={ALICE: "Dorn"}, sheet_for={"Dorn": sheet}.get,
                        monsters=lambda: {"Orc 1": {"kind": "Orc", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d6"}})
    effects = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="backstab", target="Orc 1")
    assert any(isinstance(e, SendMessage) and "Backstab" in e.text for e in effects)
    text = class_moves.render({"Dorn": sheet})
    assert "Backstab (from the Thief, by Multiclass):" in text and "move=backstab" in text
    assert advanced.has(sheet, "Iron Hide")


def test_without_the_move_the_class_gate_holds(registry, clock, dice):
    director = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                        players={ALICE: "Dorn"}, sheet_for={"Dorn": dict(DORN)}.get, monsters=lambda: {})
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="backstab", target="x")
    assert "Backstab is the Thief's move" in note.payload["error"]


ANNA = {"name": "Anna", "class": "Paladin", "race": "Human", "level": 3, "xp": 10, "hp": 20, "max_hp": 20,
        "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 14, "cha": 13}, "armor": 0, "damage": "d10"}


def test_divine_favor_makes_a_paladin_a_level_one_cleric_that_grows():
    from gary import choices, spells

    a = Advance(ANNA)
    a.choose_stat("wis")
    a.choose_move("Divine Favor")
    sheet = a.result()
    assert sheet["favor_from"] == 4
    assert spells.caster_class(sheet) == "cleric" and spells.caster_level(sheet) == 1
    assert spells.budget(sheet) == 2
    assert max(s.level for s in spells.preparable({**sheet, "spells": []})) == 1
    assert spells.caster_level({**sheet, "level": 6}) == 3
    assert choices.missing(sheet)  # the deity they are dedicated to
    assert [s.key for s in choices.Choices("Paladin", "Human", None, spells.favored(sheet)).steps] == ["deity"]
    assert spells.Preparation({**sheet}).step() == "prepare"  # no Human Cleric's borrowed spell


def test_a_favored_ranger_casts_with_wis(registry, clock, dice):
    from gary import spells

    rhea = {"name": "Rhea", "class": "Ranger", "race": "Elf", "level": 4, "hp": 15, "max_hp": 15,
            "stats": {"str": 10, "dex": 16, "con": 12, "int": 9, "wis": 15, "cha": 8}, "armor": 1,
            "damage": "d8", "advanced_moves": ["God Amidst the Wastes"], "favor_from": 4,
            "spells": ["Bless"], "lost": [], "cast_penalty": 0}
    director = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                        players={ALICE: "Rhea"}, sheet_for={"Rhea": rhea}.get, monsters=lambda: {})
    effects = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Rhea", move="cast_a_spell",
                                 spell="Bless", target="Rhea")
    (ask,) = [e.text for e in effects if isinstance(e, SendMessage)]
    assert "Cast a Spell" in ask and "+Wis +1" in ask
    assert "cleric level 1" in class_moves.render({"Rhea": rhea})
    assert spells.favored(rhea) and not spells.favored({**rhea, "class": "Fighter"})
