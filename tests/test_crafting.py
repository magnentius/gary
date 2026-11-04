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


# -- the place of power and the passage of time ------------------------------------------

DAY = 1440


def test_a_span_of_time_is_read_from_the_condition():
    assert crafting.duration("It's going to take three weeks") == 21 * DAY
    assert crafting.duration("a month of fasting") == 30 * DAY
    assert crafting.duration("it will take days") == 2 * DAY
    assert crafting.duration("help from the hermit") == 0


def test_the_clock_meets_a_time_condition_and_nothing_else_can():
    r = crafting.start_ritual("bind the wight", ["it will take two weeks", "help from a priest"], now=100,
                              where="the Nine Stones", when=lambda m: f"minute {m}")
    weeks = r["conditions"][0]
    assert weeks["due"] == 100 + 14 * DAY and weeks["due_label"] == f"minute {100 + 14 * DAY}"
    with pytest.raises(SheetError, match="the clock's to meet — 14 days to go"):
        crafting.meet(r, "weeks", now=100)
    r = crafting.meet(r, "priest", now=200)
    assert not crafting.done(r, now=200) and crafting.done(r, now=100 + 14 * DAY)
    assert "Ritual under way at the Nine Stones" in crafting.describe({"ritual": r})
    assert "(time: until minute" in crafting.describe({"ritual": r})


def _wizard():
    from gary.fsm import Director
    from gary.registry import PendingCheckRegistry

    sheet = {"name": "Mae", "class": "Wizard", "race": "Human", "level": 5, "hp": 15, "max_hp": 15, "armor": 0,
             "damage": "d4", "stats": {"str": 8, "dex": 13, "con": 12, "int": 16, "wis": 10, "cha": 9},
             "choices": {}}
    director = Director(registry=PendingCheckRegistry(clock=lambda: 0.0), clock=lambda: 0.0, game_channels={1},
                        players={1: "Mae"}, sheet_for={"Mae": sheet}.get)
    return director, sheet


STONES = {"name": "The Nine Stones", "kind": "landmark", "note": "", "power": True}


def test_a_ritual_needs_a_place_of_power_and_finishes_there_when_the_weeks_are_up():
    from gary.fsm import UpdateSheet

    director, sheet = _wizard()
    _, refused = director.ritual(channel_id=1, character="Mae", action="start", goal="bind the wight",
                                 conditions=["three weeks"], now=0,
                                 place={"name": "Fenwick", "kind": "steading"}, location="Fenwick")
    assert "needs a place of power" in refused["error"] and "Fenwick isn't one" in refused["error"]
    effects, ok = director.ritual(channel_id=1, character="Mae", action="start", goal="bind the wight",
                                  conditions=["it will take three weeks"], now=0, place=STONES,
                                  location="The Nine Stones")
    assert ok["ok"]
    sheet.update(next(e for e in effects if isinstance(e, UpdateSheet)).changes)
    _, early = director.ritual(channel_id=1, character="Mae", action="finish", now=7 * DAY,
                               location="The Nine Stones")
    assert "14 days to go" in early["error"]
    _, away = director.ritual(channel_id=1, character="Mae", action="finish", now=21 * DAY, location="Fenwick")
    assert "must be back there" in away["error"]
    _, done = director.ritual(channel_id=1, character="Mae", action="finish", now=21 * DAY,
                              location="the circle of the Nine Stones")
    assert done["ok"]


def test_places_of_power_on_the_map():
    from gary import worldmap

    w = worldmap.place(None, "The Nine Stones", "landmark", "a ring on the moor", power=True)
    assert "[landmark, place of power]" in worldmap.render(w)
