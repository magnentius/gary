"""Healing items, as the book describes them."""

from __future__ import annotations

import pytest

from gary import healing
from gary.gear import BANDAGES, POTION, POULTICES, Item
from gary.sheets import with_gear


def thorin(hp=10, items=(POTION, BANDAGES, POULTICES)):
    return {**with_gear({"name": "Thorin", "max_hp": 21}, list(items)), "hp": hp}


KETHRA = {"name": "Kethra", "hp": 5, "max_hp": 19, "gear": []}


def test_a_potion_heals_the_drinker_10_and_is_gone():
    user, patient, healed, name = healing.use(thorin(), "healing potion", None, fighting=True)
    assert (name, healed, user["hp"]) == ("Healing Potion", 10, 20)
    assert "Healing potion" not in [g["name"] for g in user["gear"]]


def test_a_potion_can_be_given_to_a_fallen_friend():
    user, patient, healed, _ = healing.use(thorin(), "healing potion", KETHRA, fighting=True)
    assert (patient["name"], patient["hp"], user["hp"]) == ("Kethra", 15, 10)


def test_bandages_heal_someone_else_4_and_spend_a_use():
    user, patient, healed, _ = healing.use(thorin(), "bandages", KETHRA, fighting=False)
    assert (healed, patient["hp"]) == (4, 9)
    (bandages,) = [g for g in user["gear"] if g["name"] == "Bandages"]
    assert bandages["uses"] == 2


def test_the_last_use_is_the_last_of_the_item():
    sheet = thorin()
    for _ in range(2):
        sheet, _, _, _ = healing.use(sheet, "poultices and herbs", KETHRA, fighting=False)
    assert "Poultices and herbs" not in [g["name"] for g in sheet["gear"]]


def test_poultices_work_on_yourself():
    user, _, healed, _ = healing.use(thorin(), "poultices and herbs", None, fighting=False)
    assert (healed, user["hp"]) == (7, 17)


def test_healing_stops_at_max_hp():
    _, patient, healed, _ = healing.use(thorin(hp=19), "healing potion", None, fighting=False)
    assert (healed, patient["hp"]) == (2, 21)


@pytest.mark.parametrize("item,target,fighting,refusal", [
    ("bandages", None, False, "someone else's wounds, not your own"),
    ("bandages", KETHRA, True, "not while creatures are still standing"),
    ("poultices and herbs", KETHRA, True, "not while creatures are still standing"),
    ("chainmail", None, False, "does not heal"),
    ("plate", None, False, "does not heal"),
])
def test_what_the_book_does_not_allow(item, target, fighting, refusal):
    with pytest.raises(healing.HealingError, match=refusal):
        healing.use(thorin(), item, target, fighting)


def test_you_cannot_use_what_you_do_not_carry():
    with pytest.raises(healing.HealingError, match="has no healing potion"):
        healing.use(thorin(items=()), "healing potion", None, fighting=False)


def test_the_dead_are_beyond_bandages():
    with pytest.raises(healing.HealingError, match="Kethra is dead"):
        healing.use(thorin(), "healing potion", {**KETHRA, "dead": True}, fighting=False)


def test_a_kit_of_2_healing_potions_is_two_potions():
    sheet = with_gear({"name": "Kael"}, [Item("2 healing potions")])
    assert [g["name"] for g in sheet["gear"]] == ["Healing Potion", "Healing Potion"]



def test_a_spent_use_does_not_break_armor_or_weapons_afterwards():
    """Spending a bandage adds "uses" to its gear entry; loading gear must not choke on it."""
    from gary.gear import CHAINMAIL, MACE, armor_of, weapon_for

    sheet = thorin(items=(CHAINMAIL, MACE, BANDAGES))
    sheet, _, _, _ = healing.use(sheet, "bandages", KETHRA, fighting=False)
    assert armor_of(sheet["gear"]) == 1
    assert weapon_for(sheet["gear"], ranged=False, ammo=0).name == "Mace"
