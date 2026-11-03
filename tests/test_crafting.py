"""Ritual kept on the sheet, Blacksmith's graft, custom items and repairs."""

from __future__ import annotations

import pytest

from gary import crafting, trade
from gary.sheets import SheetError

FIGHTER = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 6, "coins": 200,
           "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}, "armor": 0, "damage": "d10",
           "advanced_moves": ["Blacksmith"],
           "gear": [{"name": "Signature sword", "tags": ["close"], "piercing": 0, "weight": 2}]}


def test_a_ritual_is_kept_condition_by_condition():
    r = crafting.start_ritual("bind the storm into a bottle", ["it will take two weeks", "help from the hermit"])
    assert not crafting.done(r)
    r = crafting.meet(crafting.meet(r, "1"), "hermit")
    assert crafting.done(r) and "☒ help from the hermit" in crafting.describe({"ritual": r})
    with pytest.raises(SheetError, match="one to four"):
        crafting.start_ritual("x", [])


def test_blacksmith_grafts_a_magic_weapon_onto_the_signature():
    sheet, _ = trade.add_magic_item(FIGHTER, "Vorpal Sword")
    gear, signature, destroyed = crafting.graft(sheet, "vorpal")
    assert (signature, destroyed) == ("Signature sword", "Vorpal Sword") and len(gear) == 1
    assert gear[0]["piercing"] == 3 and "Grafted from Vorpal Sword" in gear[0]["magic"]
    with pytest.raises(SheetError, match="no magic weapon"):
        crafting.graft(FIGHTER, "vorpal")


def test_custom_items_and_repairs_are_priced_from_the_item():
    sheet, label, cost = trade.pay_for(FIGHTER, "a custom long sword")
    assert cost == 15 + 50 and "custom Long Sword" in label and sheet["gear"][-1]["name"].lower() == "long sword"
    _, label, cost = trade.pay_for(FIGHTER, "repairs to my plate")
    assert cost == 88 and label == "repairs to their plate"  # a quarter of 350, rounded up
