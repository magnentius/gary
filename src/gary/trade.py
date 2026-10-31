"""Carrying, finding, dropping, buying and selling: changes to a sheet's gear and coin.

Pure: each function takes a sheet and returns the new one with what happened, or raises
TradeError with a message for the model. Nothing here writes; the caller journals the
new sheet, so every change undoes.

Two counters are not recounted from gear: ammo and rations, because they are spent in
play. Picking up a bundle of arrows adds its 3 to what is left, rather than resetting the
count to every arrow ever carried; dropping one takes its 3 away, never below nothing.
Armor has no such history and is recomputed from what is carried.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

from .gear import armor_of, item_for, items_of, price_of

#: The most coin one call can grant or a sale of an unlisted item can fetch. Treasure
#: past this is a DM's call, not a model's: the store's bounds exist for the same reason.
MAX_COINS_PER_CALL = 500
MAX_UNLISTED_SALE = 100
MAX_ITEMS_PER_CALL = 5
#: Supply, 7-9: "you'll have to pay more" -- at most this many times the book's price.
MAX_MARKUP = 2
#: Supply, for something the book does not price (a magic blade): the most one costs.
MAX_SPECIAL_PRICE = 500

_COINS = re.compile(r"^\s*(\d+)\s+coins?\s*$", re.IGNORECASE)


class TradeError(ValueError):
    """Refused. The message goes back to the model as is."""


def _quantity(name: str) -> Tuple[int, str]:
    """"3 healing potions" -> (3, "healing potions"). Coins are not a quantity of items."""
    m = re.match(r"^\s*(\d+)\s+(.+)$", str(name or ""))
    if m and not _COINS.match(name):
        return min(int(m[1]), MAX_ITEMS_PER_CALL), m[2]
    return 1, str(name or "")


def _recount(sheet: Dict[str, Any], gear: List[Dict[str, Any]], ammo: int, rations: int,
             coins: int) -> Dict[str, Any]:
    return {**sheet, "gear": gear, "armor": armor_of(gear), "ammo": max(0, ammo),
            "rations": max(0, rations), "coins": max(0, coins)}


def _counts(sheet: Dict[str, Any]) -> Tuple[int, int, int]:
    from .gear import rations_of

    gear = sheet.get("gear") or []
    rations = sheet["rations"] if "rations" in sheet else rations_of(gear)
    return int(sheet.get("ammo", 0)), int(rations), int(sheet.get("coins", 0))


def add_items(sheet: Dict[str, Any], names: Sequence[str]) -> Tuple[Dict[str, Any], List[str]]:
    """Give items to a character. Returns (new sheet, what they got, as the table reads it).

    A name the book lists comes with its real stats -- a long sword hits +1 harder, plate
    is 3 armor, arrows are ammo. Anything else is carried, and does nothing to the rules.
    "12 coins" is coin, not an item.
    """
    if not names:
        raise TradeError("Name what they get.")
    gear = list(sheet.get("gear") or [])
    ammo, rations, coins = _counts(sheet)
    got: List[str] = []
    count = 0
    for raw in names:
        if m := _COINS.match(str(raw)):
            coins += int(m[1])
            got.append(f"{int(m[1])} coins")
            continue
        qty, name = _quantity(raw)
        count += qty
        if count > MAX_ITEMS_PER_CALL:
            raise TradeError(f"At most {MAX_ITEMS_PER_CALL} items per call.")
        item, known = item_for(name)
        for _ in range(qty):
            gear.append(item.to_dict())
            ammo += item.ammo
            rations += item.rations
        got.append(item.label() if qty == 1 else f"{qty} × {item.label()}")
    return _recount(sheet, gear, ammo, rations, coins), got


def remove_items(sheet: Dict[str, Any], names: Sequence[str]) -> Tuple[Dict[str, Any], List[str]]:
    """Take items away -- dropped, lost, stolen, used up. Refuses one they do not carry,
    so the model narrates the truth rather than a loss that never happened."""
    gear = list(sheet.get("gear") or [])
    ammo, rations, coins = _counts(sheet)
    lost: List[str] = []
    for raw in names:
        qty, name = _quantity(raw)
        wanted, _ = item_for(name)
        # Starting kits name some things their own way ("Throwing daggers", "Worn bow"),
        # so the name as given counts as much as the book's.
        said = " ".join(str(name).split()).lower()
        names = {wanted.name.lower(), said, said.rstrip("s")}
        for _ in range(qty):
            index = next((i for i, g in enumerate(gear) if g["name"].lower() in names
                          or g["name"].lower().rstrip("s") in names), None)
            if index is None:
                carried = ", ".join(sorted({g["name"] for g in gear})) or "nothing"
                raise TradeError(f"{sheet['name']} carries no {wanted.name}. They carry: {carried}.")
            item = items_of([gear.pop(index)])[0]
            ammo -= item.ammo
            rations -= item.rations
            lost.append(item.name)
    return _recount(sheet, gear, ammo, rations, coins), lost


def give_coins(sheet: Dict[str, Any], amount: int) -> Dict[str, Any]:
    """Coin gained or lost outside a sale -- treasure, a bribe, a fine. Never below 0."""
    amount = int(amount)
    if abs(amount) > MAX_COINS_PER_CALL:
        raise TradeError(f"{abs(amount)} coins is past the {MAX_COINS_PER_CALL} a call can "
                         "move; split it, or have the DM grant it.")
    ammo, rations, coins = _counts(sheet)
    if coins + amount < 0:
        raise TradeError(f"{sheet['name']} has only {coins} coins.")
    return _recount(sheet, list(sheet.get("gear") or []), ammo, rations, coins + amount)


def buy(sheet: Dict[str, Any], name: str, price: int = None) -> Tuple[Dict[str, Any], str, int]:
    """At the book's price -- or, after Supply, at the price the GM names: never below the
    book's, at most twice it, and for something the book does not price at most
    MAX_SPECIAL_PRICE. Returns (new sheet, item label, total cost)."""
    qty, item_name = _quantity(name)
    book = price_of(item_name)
    if price is None:
        if book is None:
            raise TradeError(f"{item_name!r} has no price in the book. If it is something special "
                             "and they found it with Supply, give the price you name.")
        price = book
    else:
        price = int(price)
        if book is not None and not book <= price <= book * MAX_MARKUP:
            raise TradeError(f"{item_name} is {book} coins in the book; a Supply price must be "
                             f"{book} to {book * MAX_MARKUP}.")
        if book is None and not 1 <= price <= MAX_SPECIAL_PRICE:
            raise TradeError(f"Price something special at 1 to {MAX_SPECIAL_PRICE} coins.")
    cost = price * qty
    _, _, coins = _counts(sheet)
    if cost > coins:
        raise TradeError(f"{item_name} costs {cost} coins; {sheet['name']} has {coins}.")
    sheet, got = add_items(give_coins(sheet, -cost), [name])
    return sheet, got[0], cost


def sell(sheet: Dict[str, Any], name: str, offer: int = None) -> Tuple[Dict[str, Any], str, int]:
    """Half the book price, or less if the buyer offers less -- never more. An item the
    book does not price sells for what the model offers, up to a cap. Returns (new sheet,
    item name, coins paid)."""
    _, item_name = _quantity(name)
    price = price_of(item_name)
    if price is not None:
        fair = price // 2
        paid = fair if offer is None else max(0, min(int(offer), fair))
    else:
        if offer is None:
            raise TradeError(f"{item_name!r} has no price in the book: say what the buyer "
                             f"offers (offer), up to {MAX_UNLISTED_SALE} coins.")
        paid = max(0, min(int(offer), MAX_UNLISTED_SALE))
    sheet, lost = remove_items(sheet, [item_name])
    return give_coins(sheet, paid), lost[0], paid
