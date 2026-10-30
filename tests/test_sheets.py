"""Dungeon World sheets: a sheet that reaches the store is valid by construction."""

from __future__ import annotations

import pytest

from gary.sheets import CLASSES, STAT_ARRAY, STATS, Draft, SheetError, describe, modifier


def fighter(name="Kael", order=("str", "con", "dex", "wis", "cha")) -> Draft:
    """A Dwarf Fighter in chainmail with a serrated, sharp signature sword."""
    d = Draft()
    d.choose_class("fighter")
    d.choose_race("Dwarf")
    for stat in order:
        d.assign(stat, d.next_score())
    d.choose_gear(0, [0])        # chainmail and adventuring gear
    d.choose_gear(1, [0, 3])     # 2 healing potions, 22 coins
    d.choose_signature_base("Sword")
    d.choose_signature_range("close")
    d.choose_signature_enhancements([1, 3])  # sharp, serrated
    d.choose_name(name)
    return d


# -- the data ----------------------------------------------------------------------------


def test_every_class_is_extracted_with_what_a_sheet_needs():
    """Pinned against the Sagelt source, so a bad re-extraction fails here."""
    assert {k: (c["base_hp"], c["damage"]) for k, c in CLASSES.items()} == {
        "bard": (6, "d6"), "cleric": (8, "d6"), "druid": (6, "d6"), "fighter": (10, "d10"),
        "paladin": (10, "d10"), "ranger": (8, "d8"), "thief": (6, "d8"), "wizard": (4, "d4"),
    }
    assert CLASSES["paladin"]["races"] == ["Human"]
    assert all("Human" in c["races"] for c in CLASSES.values())


@pytest.mark.parametrize("score,mod", [
    (1, -3), (3, -3), (4, -2), (5, -2), (6, -1), (8, -1), (9, 0), (12, 0),
    (13, 1), (15, 1), (16, 2), (17, 2), (18, 3),
])
def test_modifiers_follow_the_table(score, mod):
    assert modifier(score) == mod


# -- building one ------------------------------------------------------------------------


def test_a_finished_sheet_carries_what_rolls_need():
    sheet = fighter().build()
    assert sheet["class"] == "Fighter" and sheet["race"] == "Dwarf"
    assert sheet["stats"] == {"str": 16, "dex": 13, "con": 15, "int": 8, "wis": 12, "cha": 9}
    assert sheet["damage"] == "d10"
    assert (sheet["level"], sheet["xp"], sheet["armor"]) == (1, 0, 1)  # chainmail


def test_max_hp_is_base_plus_the_constitution_score_not_its_modifier():
    """Character Creation, step 7: "base HP+Constitution score"."""
    sheet = fighter().build()
    assert sheet["max_hp"] == 10 + 15
    assert sheet["hp"] == sheet["max_hp"]


def test_the_array_is_assigned_highest_first_and_the_last_score_places_itself():
    d = Draft()
    d.choose_class("wizard")
    d.choose_race("Elf")
    assert d.next_score() == 16
    for stat in ("int", "wis", "dex", "con", "cha"):
        d.assign(stat, d.next_score())
    assert d.stats["str"] == 8  # nothing left to ask
    assert sorted(d.stats.values(), reverse=True) == list(STAT_ARRAY)
    assert d.next_score() is None


def test_a_score_cannot_be_used_twice():
    d = Draft()
    d.choose_class("thief")
    d.assign("dex", 16)
    with pytest.raises(SheetError):
        d.assign("cha", 16)


def test_a_stat_cannot_take_two_scores():
    d = Draft()
    d.choose_class("thief")
    d.assign("dex", 16)
    with pytest.raises(SheetError):
        d.assign("dex", 15)


def test_stats_can_be_started_over():
    d = Draft()
    d.choose_class("thief")
    d.assign("dex", 16)
    d.reset_stats()
    assert d.unassigned_stats() == list(STATS)
    assert d.next_score() == 16


def test_a_race_the_class_does_not_offer_is_refused():
    d = Draft()
    d.choose_class("paladin")
    with pytest.raises(SheetError, match="Human"):
        d.choose_race("Elf")


def test_changing_class_clears_a_race_the_new_class_may_not_offer():
    d = Draft()
    d.choose_class("fighter")
    d.choose_race("Dwarf")
    d.choose_class("paladin")
    assert d.race is None


def test_an_unknown_class_is_refused():
    with pytest.raises(SheetError):
        Draft().choose_class("artificer")


@pytest.mark.parametrize("name", ["", "   ", "x" * 41])
def test_names_must_be_present_and_short(name):
    with pytest.raises(SheetError):
        Draft().choose_name(name)


def test_names_are_tidied():
    d = Draft()
    d.choose_name("  Kael   the  Bold ")
    assert d.name == "Kael the Bold"


def test_an_incomplete_draft_will_not_build_and_says_what_is_missing():
    d = Draft()
    d.choose_class("fighter")
    with pytest.raises(SheetError, match="a race, stats, gear, a name"):
        d.build()


def test_the_description_can_leave_the_name_off():
    assert describe(fighter().build(), with_name=False).startswith("Dwarf Fighter 1, HP 25/25")


def test_the_description_shows_scores_and_modifiers():
    line = describe(fighter().build())
    assert line.startswith("Kael — Dwarf Fighter 1, HP 25/25, armor 1, damage d10")
    assert "STR 16 (+2)" in line and "INT 8 (-1)" in line
    assert line.endswith("wields Signature sword (close, +1 damage, 2 piercing)")


# -- gear --------------------------------------------------------------------------------


def test_every_class_can_be_fully_equipped_with_its_first_options():
    from gary.gear import CLASS_GEAR

    for key in CLASS_GEAR:
        d = Draft()
        d.choose_class(key)
        while (g := d.next_gear_group()) is not None:
            d.choose_gear(g, list(range(d.class_gear().groups[g].count)))
        if d.needs_signature():
            d.choose_signature_base("Axe")
            d.choose_signature_range("close")
            d.choose_signature_enhancements([0, 1])
        assert d.gear_complete, key
        assert d.gear_items(), key


@pytest.mark.parametrize("cls,picks,armor", [
    ("cleric", {0: [0]}, 1),          # chainmail
    ("cleric", {0: [1]}, 1),          # shield alone
    ("paladin", {0: [1]}, 3),         # fixed scale (2) + long sword and shield (+1)
    ("wizard", {0: [1]}, 0),          # books and potions, no armor
])
def test_armor_is_the_best_worn_plus_shields(cls, picks, armor):
    d = Draft()
    d.choose_class(cls)
    for group, p in picks.items():
        d.choose_gear(group, p)
    while (g := d.next_gear_group()) is not None:
        d.choose_gear(g, [0])
    from gary.gear import armor_of

    assert armor_of([i.to_dict() for i in d.gear_items()]) == armor


def test_a_gear_choice_takes_exactly_its_count():
    d = Draft()
    d.choose_class("fighter")
    d.choose_gear(0, [0])
    with pytest.raises(SheetError, match="pick 2"):
        d.choose_gear(1, [0])


def test_changing_class_clears_gear():
    d = Draft()
    d.choose_class("cleric")
    d.choose_gear(0, [0])
    d.choose_class("thief")
    assert d.gear_picks == {}


def test_versatile_asks_for_a_second_range():
    d = Draft()
    d.choose_class("fighter")
    d.choose_signature_base("Spear")
    d.choose_signature_range("reach")
    d.choose_signature_enhancements([6, 7])  # versatile, well-crafted
    assert d.signature_step() == "extra_range"
    d.choose_signature_range("close")
    assert d.signature_step() is None


def test_gear_can_be_given_to_a_character_made_before_gear_existed():
    """Thorin: HP and everything else stay; gear, armor and ammo arrive."""
    from gary.sheets import with_gear
    from gary.gear import CHAINMAIL, MACE

    old = {"name": "Thorin", "hp": 18, "max_hp": 21, "armor": 0, "class": "Cleric"}
    new = with_gear(old, [CHAINMAIL, MACE])
    assert (new["hp"], new["max_hp"], new["armor"], new["ammo"]) == (18, 21, 1, 0)
    assert [g["name"] for g in new["gear"]] == ["Chainmail", "Mace"]
