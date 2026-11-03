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
