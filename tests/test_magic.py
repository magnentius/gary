"""Magic items: carried as gear with their text, shown to the GM, never sold."""

from __future__ import annotations

import pytest

from gary import magic, pictures, trade
from gary.context import render_live_state
from gary.gear import armor_of, weapon_for
from gary.sheets import carried

SHEET = {"name": "Anna", "class": "Paladin", "race": "Human", "level": 2, "hp": 20, "max_hp": 20,
         "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
         "armor": 0, "damage": "d10", "gear": [], "coins": 10}


def test_a_book_item_comes_with_the_books_text_and_tags():
    sheet, name = trade.add_magic_item(SHEET, "vorpal sword")
    item = sheet["gear"][-1]
    assert name == "Vorpal Sword" and item["book"] and item["magic"].startswith("Snicker-snack")
    assert (item["piercing"], item["weight"]) == (3, 2) and "close" in item["tags"]
    assert weapon_for(sheet["gear"], ranged=False, ammo=0).name == "Vorpal Sword"  # it can be swung
    assert carried(sheet) == 2


def test_armor_and_a_shield_protect():
    sheet, _ = trade.add_magic_item(SHEET, "Timunn's Armor")
    sheet, _ = trade.add_magic_item(sheet, "Lodestone Shield")
    assert armor_of(sheet["gear"]) == 2 and sheet["armor"] == 2


def test_an_invented_item_needs_its_text():
    with pytest.raises(trade.TradeError, match="say what it does"):
        trade.add_magic_item(SHEET, "the Ember Crown")
    sheet, _ = trade.add_magic_item(SHEET, "the Ember Crown", "While worn, fire speaks to you.", ["1 weight"])
    assert sheet["gear"][-1]["magic"] == "While worn, fire speaks to you." and not sheet["gear"][-1]["book"]


def test_argo_thaan_is_a_d12_for_a_paladin_only():
    sheet, _ = trade.add_magic_item(SHEET, "Argo-Thaan")
    assert magic.damage_die(sheet) == "d12"
    assert magic.damage_die({**sheet, "class": "Fighter"}) is None


def test_the_gm_sees_what_it_does_and_it_is_never_for_sale():
    sheet, _ = trade.add_magic_item(SHEET, "Bag of Holding")
    shown = render_live_state({"players": {"1": "Anna"}, "sheets": {"Anna": sheet}})
    assert "Magic items — Bag of Holding (0 weight):" in shown
    with pytest.raises(trade.TradeError, match="isn't for sale"):
        trade.buy(SHEET, "Bag of Holding", 50)


def test_a_carried_magic_item_can_be_pictured():
    sheet, _ = trade.add_magic_item(SHEET, "Vorpal Sword")
    state = {"players": {"1": "Anna"}, "sheets": {"Anna": sheet}}
    assert ("item", "Vorpal Sword") in pictures.subjects(state)
    assert "Snicker-snack" in pictures.prompt(state, "item", "Vorpal Sword")


def test_rummaging_marks_off_a_use_and_the_last_empties_the_pack():
    sheet, _ = trade.add_items(SHEET, ["adventuring gear"])
    assert carried(sheet) == 1
    for left in (4, 3, 2, 1):
        sheet, got = trade.rummage(sheet, "a torch")
        assert got == left
    sheet, got = trade.rummage(sheet, "chalk")
    assert got == 0 and carried(sheet) == 0  # its weight goes with it
    with pytest.raises(trade.TradeError, match="no adventuring gear left"):
        trade.rummage(sheet, "a torch")
    with pytest.raises(trade.TradeError, match="Say what they pull out"):
        trade.rummage(SHEET, "")
