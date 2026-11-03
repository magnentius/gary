"""Two-handed weapons and shields: one shield counts, and a grip decides which is in hand."""

from __future__ import annotations

from gary import gear
from gary.adapters import gemini_tools as T
from gary.models import Mutation, MutationKind
from gary.sheets import describe
from tests.test_gemini import call_response, narrator, player, text_response
from tests.test_gemini import rig as _rig

rig = _rig
HALBERD, SHIELD, SWORD, MAIL = (g.to_dict() for g in (gear.HALBERD, gear.SHIELD, gear.LONG_SWORD, gear.CHAINMAIL))


def test_one_shield_counts():
    assert gear.armor_of([MAIL, SHIELD, gear.WOODEN_SHIELD.to_dict()]) == 2


def test_shield_up_or_both_hands():
    kit = [MAIL, SHIELD, HALBERD, SWORD]
    assert gear.has_grip(kit) and not gear.has_grip([MAIL, SHIELD, SWORD])
    assert gear.armor_of(kit) == 2 and gear.armor_of(kit, "both hands") == 1
    assert gear.weapon_for(kit, ranged=False, ammo=0).name == "Long sword"  # shield up: one-handed
    assert gear.weapon_for(kit, ranged=False, ammo=0, grip="both hands").name == "Halberd"
    # Only the halberd: still the halberd, shield or not -- Gemini should set the grip.
    assert gear.weapon_for([SHIELD, HALBERD], ranged=False, ammo=0).name == "Halberd"
    # No shield: the grip is irrelevant.
    assert gear.armor_of([MAIL, HALBERD], "both hands") == 1


async def test_set_grip_changes_armor_and_shows_on_the_sheet(rig):
    director, store, history = rig
    sheet = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0, "hp": 20, "max_hp": 20,
             "armor": 2, "damage": "d10", "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
             "gear": [MAIL, SHIELD, HALBERD, SWORD]}
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": sheet})], reason="setup")
    assert "armor 2 (shield up)" in describe(sheet)
    n = narrator(rig, call_response(T.SET_GRIP, {"character": "Dorn", "grip": "both hands"}), text_response("."))
    await player(n, "I sling my shield and take up the halberd")
    dorn = store.state()["sheets"]["Dorn"]
    assert dorn["grip"] == "both hands" and dorn["armor"] == 1 and "shield slung" in describe(dorn)


CLOAK = {"name": "Cloak of Warding", "tags": ["+1 armor"], "shield": 1, "armor": 0}
PLATE = gear.item_for("Plate")[0].to_dict()


def test_other_plus_armor_stacks_with_the_shield():
    assert gear.armor_of([MAIL, SHIELD, CLOAK]) == 3  # 1 worn + shield 1 + cloak 1
    assert gear.armor_of([MAIL, SHIELD, gear.WOODEN_SHIELD.to_dict(), CLOAK]) == 3  # still one shield
    assert gear.armor_of([PLATE, MAIL]) == 3  # only the highest n Armor


def test_clumsy_is_worn_armor_and_cumulative():
    from gary.sheets import clumsy_items

    wizard = {"name": "Mae", "class": "Wizard", "gear": [MAIL, PLATE]}  # plate worn, mail in the pack
    assert clumsy_items(wizard) == ["Plate"]
    assert clumsy_items({**wizard, "gear": [MAIL]}) == []  # a spare suit isn't in use
    assert clumsy_items({**wizard, "armor_off": True}) == []
    lumber = {"name": "Mae", "class": "Wizard", "gear": [PLATE, {"name": "Siege ladder", "tags": ["clumsy"]}]}
    assert clumsy_items(lumber) == ["Plate", "Siege ladder"]  # -2: "cumulative"
    assert clumsy_items({**lumber, "class": "Fighter"}) == ["Siege ladder"]  # Armored: armor only


async def test_taking_armor_off(rig):
    director, store, history = rig
    sheet = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 1, "xp": 0, "hp": 20, "max_hp": 20,
             "armor": 3, "damage": "d10", "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
             "gear": [MAIL, SHIELD, CLOAK]}
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": sheet})], reason="setup")
    n = narrator(rig, call_response(T.WEAR_ARMOR, {"character": "Dorn", "on": False}), text_response("."))
    await player(n, "I strip off my mail to swim the river")
    dorn = store.state()["sheets"]["Dorn"]
    assert dorn["armor_off"] and dorn["armor"] == 1 and "(armor off)" in describe(dorn)  # the cloak stays
