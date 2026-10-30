"""Damage dice, armor, and the monsters they land on."""

from __future__ import annotations

import pytest

from gary import damage, monsters
from gary.ports import ScriptedDice


# -- dice ----------------------------------------------------------------------------------


@pytest.mark.parametrize("expr,sides,mode,mod", [
    ("d6", 6, "single", 0),
    ("d10+3", 10, "single", 3),
    ("d6-2", 6, "single", -2),
    ("b[2d10]+4", 10, "best", 4),
    ("w[2d8]", 8, "worst", 0),
    ("w[2d8-2]", 8, "worst", -2),   # modifier inside the brackets, as the source writes it
])
def test_every_shape_of_damage_parses(expr, sides, mode, mod):
    spec = damage.parse(expr)
    assert (spec.sides, spec.mode, spec.modifier) == (sides, mode, mod)


def test_every_damage_in_the_bestiary_parses():
    for name, m in monsters.BESTIARY.items():
        if m["damage"]:
            damage.parse(m["damage"])  # raises on anything unreadable


@pytest.mark.parametrize("bad", ["", "2d6", "d", "b[3d6]", "d6+x"])
def test_anything_else_is_refused(bad):
    with pytest.raises(ValueError):
        damage.parse(bad)


def test_best_and_worst_keep_the_right_die():
    assert damage.roll(ScriptedDice([3, 7]), damage.parse("b[2d8]")).kept == 7
    assert damage.roll(ScriptedDice([3, 7]), damage.parse("w[2d8]")).kept == 3


def test_a_move_can_add_or_take_away_a_d6():
    up = damage.roll(ScriptedDice([6, 4]), damage.parse("d10"), "+1d6")
    down = damage.roll(ScriptedDice([6, 4]), damage.parse("d10"), "-1d6")
    assert (up.total, down.total) == (10, 2)


def test_damage_never_goes_below_zero():
    assert damage.roll(ScriptedDice([1]), damage.parse("d6-2")).total == 0
    assert damage.roll(ScriptedDice([2, 6]), damage.parse("d6"), "-1d6").total == 0


@pytest.mark.parametrize("expr,faces,extra,shown", [
    ("d6", [5], None, "d6: 5"),
    ("d10+3", [7], None, "d10+3: 7 +3 = 10"),
    ("b[2d8]", [3, 7], None, "b[2d8]: 3, 7 → 7"),
    ("d8", [7, 2], "-1d6", "d8 -1d6: 7 - 2 = 5"),
])
def test_rolls_are_shown_face_by_face(expr, faces, extra, shown):
    assert damage.roll(ScriptedDice(faces), damage.parse(expr), extra).render() == shown


@pytest.mark.parametrize("total,armor,piercing,ignores,dealt", [
    (5, 1, 0, False, 4),
    (5, 3, 1, False, 3),   # 1 piercing ignores 1 of the 3
    (5, 1, 3, False, 5),   # more piercing than armor is just no armor
    (5, 4, 0, True, 5),
    (1, 3, 0, False, 0),   # armor never heals
])
def test_armor(total, armor, piercing, ignores, dealt):
    assert damage.through_armor(total, armor, piercing, ignores) == dealt


# -- the bestiary ---------------------------------------------------------------------------


def test_the_bestiary_is_all_there_and_pinned_against_the_source():
    assert len(monsters.BESTIARY) == 154
    assert sum(1 for m in monsters.BESTIARY.values() if m["hp"] is not None) == 142
    goblin = monsters.lookup("Goblin")
    assert (goblin["hp"], goblin["armor"], goblin["damage"]) == (3, 1, "d6")
    rat = monsters.lookup("cave rat")
    assert (rat["damage"], rat["piercing"]) == ("d6", 1)


def test_known_creatures_are_looked_up_not_taken_from_the_model():
    """The model offering a 50 HP goblin gets the book's goblin."""
    added, errors = monsters.prepare([{"name": "goblin", "hp": 50, "damage": "d12"}], [])
    assert errors == []
    assert added["Goblin 1"]["hp"] == 3 and added["Goblin 1"]["srd"] is True


def test_instances_are_numbered_and_numbering_continues():
    added, _ = monsters.prepare([{"name": "Goblin", "count": 2}], ["Goblin 1", "Goblin 3"])
    assert list(added) == ["Goblin 2", "Goblin 4"]
    assert all(r["max_hp"] == r["hp"] for r in added.values())


def test_a_creature_of_the_models_own_needs_its_stats():
    _, errors = monsters.prepare([{"name": "Dock Thug"}], [])
    assert "not in the Dungeon World bestiary" in errors[0]


def test_a_creature_of_the_models_own_is_checked_against_the_rules():
    added, errors = monsters.prepare([
        {"name": "Dock Thug", "hp": 6, "armor": 1, "damage": "d6+1"},
        {"name": "Kraken Lord", "hp": 200, "armor": 1, "damage": "d6"},
        {"name": "Tin Man", "hp": 6, "armor": 9, "damage": "d6"},
        {"name": "Odd", "hp": 6, "armor": 1, "damage": "d7"},
    ], [])
    assert list(added) == ["Dock Thug 1"] and added["Dock Thug 1"]["srd"] is False
    assert len(errors) == 3


def test_a_creature_that_is_not_fought_says_so():
    _, errors = monsters.prepare([{"name": "Acolyte"}], [])
    assert "not something you fight" in errors[0]


def test_there_is_a_limit_per_call():
    added, errors = monsters.prepare([{"name": "Goblin", "count": 20}], [])
    assert added == {} and "At most" in errors[0]


def test_a_description_shows_what_attacks_need():
    rat = {**monsters.stats_for({"name": "Cave Rat"}), "max_hp": 7}
    assert monsters.describe("Cave Rat 1", rat) == (
        "Cave Rat 1 (HP 7/7, armor 1, damage d6, 1 piercing)"
    )
    assert monsters.describe("Cave Rat 1", {**rat, "hp": 0}) == "Cave Rat 1 (defeated)"
