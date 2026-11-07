"""Finding, dropping, buying and selling: gear and coin on the sheet."""

from __future__ import annotations

import pytest

from gary import trade
from gary.gear import (
    ARROWS, CHAINMAIL, EQUIPMENT, RATIONS, THROWING_DAGGERS, WARHAMMER, WORN_BOW, item_for,
    price_of,
)
from gary.sheets import with_gear


def thorin(coins=20):
    sheet = with_gear({"name": "Thorin", "hp": 18, "max_hp": 21}, [CHAINMAIL, WARHAMMER, RATIONS])
    return {**sheet, "coins": coins}


# -- the book's list ----------------------------------------------------------------------


def test_the_equipment_list_is_extracted_and_pinned():
    assert len(EQUIPMENT) == 45
    sword = EQUIPMENT["long sword"]
    assert (sword["price"], sword["damage"], sword["tags"]) == (15, 1, ["close", "+1 damage"])
    assert (EQUIPMENT["plate"]["armor"], EQUIPMENT["plate"]["price"]) == (3, 350)
    assert EQUIPMENT["dwarven hardtack"]["rations"] == 7
    assert "heal yourself of 10" in EQUIPMENT["healing potion"]["description"]


@pytest.mark.parametrize("said,book", [
    ("a long sword", "Long Sword"),
    ("Long Swords", "Long Sword"),
    ("bundles of arrows", "Bundle of Arrows"),
    ("healing potions", "Healing Potion"),
    ("Hunter’s bow", "Hunter's Bow"),
])
def test_items_are_found_however_they_are_said(said, book):
    item, known = item_for(said)
    assert known and item.name == book


def test_an_item_the_book_does_not_list_is_carried_without_rules():
    item, known = item_for("a silver locket")
    assert not known and item.name == "Silver locket" and not item.ranges and item.armor == 0
    assert price_of("a silver locket") is None


# -- finding and dropping ------------------------------------------------------------------


def test_found_items_bring_their_stats():
    sheet, got = trade.add_items(thorin(), ["plate", "2 bundles of arrows", "a long sword"])
    assert sheet["armor"] == 3  # the best worn armor, not chainmail plus plate
    assert sheet["ammo"] == 6
    assert got == ["Plate (3 armor, worn, clumsy)", "2 × Bundle of Arrows (3 ammo)",
                   "Long Sword (close, +1 damage)"]


def test_coins_in_loot_are_coin():
    sheet, got = trade.add_items(thorin(coins=0), ["12 coins"])
    assert sheet["coins"] == 12 and got == ["12 coins"]
    assert not any(g["name"].endswith("coins") for g in sheet["gear"])


def test_picking_up_arrows_adds_to_what_is_left_not_to_what_was_ever_carried():
    sheet = {**with_gear({"name": "Kethra"}, [WORN_BOW, ARROWS]), "ammo": 1}  # two spent
    sheet, _ = trade.add_items(sheet, ["bundle of arrows"])
    assert sheet["ammo"] == 4


def test_dropping_changes_the_rules_at_once():
    sheet, lost = trade.remove_items(thorin(), ["chainmail"])
    assert lost == ["Chainmail"] and sheet["armor"] == 0


def test_dropping_what_you_do_not_carry_is_refused_with_what_you_do():
    with pytest.raises(trade.TradeError, match="carries no Plate. They carry: Chainmail"):
        trade.remove_items(thorin(), ["plate"])


def test_a_starting_kits_own_names_can_be_dropped():
    sheet = with_gear({"name": "Liriel"}, [THROWING_DAGGERS, WORN_BOW, ARROWS])
    sheet, lost = trade.remove_items(sheet, ["throwing daggers", "worn bow"])
    assert lost == ["Throwing daggers", "Worn bow"] and sheet["ammo"] == 3


def test_there_is_a_limit_per_call():
    with pytest.raises(trade.TradeError, match="At most 5"):
        trade.add_items(thorin(), ["4 torches", "3 healing potions"])


# -- coin -----------------------------------------------------------------------------------


def test_starting_kit_coin_becomes_coin():
    from gary.gear import Item

    sheet = with_gear({"name": "Kael"}, [Item("22 coins"), RATIONS])
    assert sheet["coins"] == 22 and [g["name"] for g in sheet["gear"]] == ["Dungeon rations"]


def test_treasure_and_spending_are_bounded():
    assert trade.give_coins(thorin(coins=20), 30)["coins"] == 50
    with pytest.raises(trade.TradeError, match="only 20 coins"):
        trade.give_coins(thorin(coins=20), -21)
    with pytest.raises(trade.TradeError, match="past the 500"):
        trade.give_coins(thorin(), 5000)


def test_buying_pays_the_book_price_for_the_books_item():
    sheet, label, cost = trade.buy(thorin(coins=20), "shield")
    assert (label, cost, sheet["coins"], sheet["armor"]) == ("Shield (+1 armor)", 15, 5, 2)


def test_buying_what_you_cannot_afford_changes_nothing():
    before = thorin(coins=20)
    with pytest.raises(trade.TradeError, match="costs 350 coins; Thorin has 20"):
        trade.buy(before, "plate")
    assert before["coins"] == 20


def test_buying_something_the_book_does_not_price_is_refused_with_what_to_do():
    with pytest.raises(trade.TradeError, match="found it with Supply"):
        trade.buy(thorin(), "a silver locket")


def test_selling_pays_half_and_an_offer_can_only_lower_it():
    assert trade.sell(thorin(coins=0), "warhammer")[2] == 4          # half of 8
    assert trade.sell(thorin(coins=0), "warhammer", offer=2)[2] == 2  # a stingy buyer
    assert trade.sell(thorin(coins=0), "warhammer", offer=999)[2] == 4  # never more


def test_an_unlisted_item_sells_for_an_offer_up_to_the_cap():
    sheet, _ = trade.add_items(thorin(coins=0), ["a silver locket"])
    with pytest.raises(trade.TradeError, match="say what the buyer offers"):
        trade.sell(sheet, "silver locket")
    assert trade.sell(sheet, "silver locket", offer=40)[2] == 40
    assert trade.sell(sheet, "silver locket", offer=5000)[2] == 100


def test_you_cannot_sell_what_you_do_not_carry():
    with pytest.raises(trade.TradeError, match="carries no Plate"):
        trade.sell(thorin(), "plate")


def test_a_supply_price_is_bounded():
    sheet = thorin(coins=100)
    new, label, cost = trade.buy(sheet, "long sword", price=20)  # book 15
    assert cost == 20
    with pytest.raises(trade.TradeError, match="must be 15 to 30"):
        trade.buy(sheet, "long sword", price=10)  # never below the book
    with pytest.raises(trade.TradeError, match="must be 15 to 30"):
        trade.buy(sheet, "long sword", price=31)
    new, label, cost = trade.buy(thorin(coins=400), "Moonlit blade", price=300)
    assert cost == 300 and label == "Moonlit blade"
    with pytest.raises(trade.TradeError, match="1 to 500"):
        trade.buy(thorin(coins=900), "Moonlit blade", price=800)


def test_a_loss_whose_reason_names_more_than_was_taken_says_what_is_still_carried():
    """take_items got only the arrows while its reason read "Ragged bow and quiver knocked into
    the water": the narration lost the bow and the sheet kept it (2026-11-07)."""
    sheet = {"gear": [{"name": "Dagger"}, {"name": "Short sword"}, {"name": "Ragged bow"}]}
    assert trade.named_but_kept(sheet, "Ragged bow and quiver knocked into the cellar water") == ["Ragged bow"]
    assert trade.named_but_kept(sheet, "his bows fall away") == ["Ragged bow"]
    assert trade.named_but_kept(sheet, "arrows spent in the fight") == []
