"""Level Up: the choices, and the sheet they come to."""

from __future__ import annotations

import pytest

from gary.class_moves import TEXTS, render
from gary.levelling import Advance, advanced_moves, move_options, ready
from gary.sheets import SheetError

EVA = {"name": "Eva", "class": "Wizard", "race": "Human", "level": 1, "xp": 9, "hp": 10, "max_hp": 17,
       "stats": {"str": 8, "dex": 12, "con": 13, "int": 18, "wis": 15, "cha": 9},
       "spellbook": ["Magic Missile", "Alarm", "Sleep"]}
THORIN = {"name": "Thorin", "class": "Cleric", "race": "Dwarf", "level": 1, "xp": 8, "hp": 14, "max_hp": 21,
          "stats": {"str": 15, "dex": 9, "con": 13, "int": 12, "wis": 16, "cha": 8}}


def test_every_class_has_both_tiers_of_advanced_moves():
    assert sum(len(c["advanced"]) for c in TEXTS.values()) == 157
    cleric = {m["name"]: m for m in TEXTS["cleric"]["advanced"]}
    assert cleric["Empower"]["tier"] == "2-5"
    assert cleric["Greater Empower"]["replaces"] == "Empower"
    assert cleric["Anointed"]["requires"] == "Chosen One"
    assert "The spell’s targets are doubled" in cleric["Empower"]["text"]


def test_ready_at_level_plus_seven():
    assert ready(THORIN) and not ready({**THORIN, "xp": 7})
    assert not ready({**THORIN, "level": 10, "xp": 99})


def test_not_ready_is_refused_with_the_numbers():
    with pytest.raises(SheetError, match="has 3 XP and needs 8"):
        Advance({**THORIN, "xp": 3})


def test_levelling_raises_a_stat_takes_a_move_and_spends_the_xp():
    a = Advance(THORIN)
    a.choose_stat("con")
    assert a.step() == "move"
    a.choose_move("Divine Protection")
    assert a.step() is None  # not a wizard
    sheet = a.result()
    assert (sheet["level"], sheet["xp"], sheet["stats"]["con"]) == (2, 0, 14)
    assert (sheet["max_hp"], sheet["hp"]) == (22, 15)  # Con raises max and current HP
    assert sheet["advanced_moves"] == ["Divine Protection"]


def test_no_stat_above_18():
    a = Advance(EVA)
    assert "int" not in a.stat_options()
    with pytest.raises(SheetError, match="can't go higher than 18"):
        a.choose_stat("int")


def test_the_6_to_10_list_opens_at_level_6_and_requirements_apply():
    names = [m["name"] for m in move_options(THORIN, 2)]
    assert "Empower" in names and "Greater Empower" not in names and "Anointed" not in names
    names = [m["name"] for m in move_options({**THORIN, "advanced_moves": ["Chosen One"]}, 6)]
    assert "Anointed" in names and "Greater Empower" in names and "Chosen One" not in names


def test_a_replacing_move_takes_the_old_ones_place():
    sheet = {**THORIN, "level": 5, "xp": 12, "advanced_moves": ["Empower"]}
    a = Advance(sheet)
    a.choose_stat("wis")
    a.choose_move("Greater Empower")
    assert a.result()["advanced_moves"] == ["Greater Empower"]
    assert "Empower" not in [m["name"] for m in move_options(a.result(), 7)]


def test_a_wizard_adds_a_spell_of_their_new_level_or_lower():
    a = Advance(EVA)
    a.choose_stat("wis")
    a.choose_move("Prodigy")
    assert a.step() == "spell"
    offered = [s.name for s in a.spell_options()]
    assert "Magic Missile" not in offered and "Fireball" not in offered and "Charm Person" in offered
    a.choose_spell("Charm Person")
    assert a.result()["spellbook"][-1] == "Charm Person"
    assert a.summary() == "level 2, Wisdom +1, Prodigy, Charm Person in the spellbook"


def test_the_model_sees_advanced_moves_in_the_books_words():
    sheet = {**THORIN, "advanced_moves": ["Divine Protection"]}
    assert advanced_moves(sheet)[0]["name"] == "Divine Protection"
    assert "- Divine Protection (advanced): When you wear no armor or shield you get 2 armor." \
        in render({"Thorin": sheet})
