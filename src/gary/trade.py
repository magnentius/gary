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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .gear import armor_for, item_for, items_of, price_of

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
    return {**sheet, "gear": gear, "armor": armor_for(sheet, gear), "ammo": max(0, ammo),
            "rations": max(0, rations), "coins": max(0, coins)}


def _counts(sheet: Dict[str, Any]) -> Tuple[int, int, int]:
    from .gear import rations_of

    gear = sheet.get("gear") or []
    rations = sheet["rations"] if "rations" in sheet else rations_of(gear)
    return int(sheet.get("ammo", 0)), int(rations), int(sheet.get("coins", 0))


def _rations_for(sheet: Dict[str, Any], item) -> int:
    """The meals an item gives this character. "Requires Dwarf": Dwarven Hardtack feeds no one
    else -- "Everyone else says it tastes like home, if home is a hog farm, and on fire"."""
    needs = next((t.split(" ", 1)[1] for t in item.tags if t.lower().startswith("requires ")), None)
    if needs and str(sheet.get("race", "")).lower() != needs.lower():
        return 0
    return item.rations


_CUSTOM = re.compile(r"^(?:an? )?(custom|repair(?:s|ing)?(?: to| of)?|mend(?:ing)?)\s+(?:an? |the |my )?(.+)$",
                     re.IGNORECASE)


def _made_or_mended(sheet: Dict[str, Any], what: str, discount: int):
    """The services priced from an item: "A custom item from a blacksmith: Base Item + 50 coins";
    "Repairs to a mundane item: 25% of the item's cost". None if it isn't one of those."""
    m = _CUSTOM.match(" ".join(str(what or "").split()))
    if not m:
        return None
    item = m[2].removesuffix(" from a blacksmith").strip()
    base = price_of(item)
    if base is None:
        raise TradeError(f"{item!r} has no price in the book to work from: name a price and use give_coins.")
    custom = m[1].lower() == "custom"
    cost = max(1, (base + 50 if custom else -(-base // 4)) - int(discount or 0))
    _, _, coins = _counts(sheet)
    if cost > coins:
        raise TradeError(f"That costs {cost} coins; {sheet['name']} has {coins}.")
    sheet = give_coins(sheet, -cost)
    if custom:
        sheet, got = add_items(sheet, [item])
        return sheet, f"a custom {got[0]} from a blacksmith", cost
    return sheet, f"repairs to their {item}", cost


def pay_for(sheet: Dict[str, Any], what: str, times: int = 1, discount: int = 0) -> Tuple[Dict[str, Any], str, int]:
    """A service, a meal, transport, land, a bribe or a gift, at the book's price (services.py):
    (new sheet, what was paid for, total cost). What is bought to keep goes on the sheet."""
    from .services import find, options, price
    from .sheets import stat_modifier

    made = _made_or_mended(sheet, what, discount)
    if made is not None:
        return made
    service, night = find(what)
    if service is None:
        raise TradeError(f"{what!r} isn't on the book's price list. Listed: " + "; ".join(options()[:12])
                         + "... For anything else, name a price in the fiction and use give_coins.")
    times = max(1, min(int(times or 1), 30))
    cha = stat_modifier(sheet, "cha") if "stats" in sheet else 0
    each = price(service, cha, night, discount)
    cost = each * times
    _, _, coins = _counts(sheet)
    label = (f"a night at {service['name'].split(' at ', 1)[1]}" if night else service["name"]) + (
        f" × {times}" if times > 1 else "")
    if cost > coins:
        raise TradeError(f"{label} costs {cost} coins; {sheet['name']} has {coins}.")
    sheet = give_coins(sheet, -cost)
    if service.get("kept"):
        tags = [f"load {service['load']}"] if service.get("load") else []
        gear = list(sheet.get("gear") or []) + [{"name": service["name"].removeprefix("A ").removeprefix("An "),
                                                  "tags": tags, "weight": 0, "armor": 0, "shield": 0, "damage": 0,
                                                  "piercing": 0, "ammo": 0, "needs_ammo": False, "rations": 0,
                                                  "possession": True} for _ in range(times)]
        ammo, rations, coins_now = _counts(sheet)
        sheet = _recount(sheet, gear, ammo, rations, coins_now)
    return sheet, label, cost


def mount(sheet: Dict[str, Any], name: str, with_party: bool) -> Tuple[Dict[str, Any], str]:
    """A mount or beast of burden left behind (stabled, tied at the dungeon's mouth) or back with
    them: its load counts toward theirs only while it's with them. (new sheet, its name)."""
    wanted = " ".join(str(name or "").split()).lower()
    gear = list(sheet.get("gear") or [])
    index = next((i for i, g in enumerate(gear) if g.get("possession")
                  and any(str(t).startswith("load ") for t in g.get("tags") or ())
                  and (str(g["name"]).lower() == wanted or wanted in str(g["name"]).lower())), None)
    if index is None:
        raise TradeError(f"{sheet['name']} has no mount or beast of burden called {name!r}.")
    gear[index] = {**gear[index], "with_party": bool(with_party)}
    ammo, rations, coins = _counts(sheet)
    return _recount(sheet, gear, ammo, rations, coins), str(gear[index]["name"])


def rummage(sheet: Dict[str, Any], what: str) -> Tuple[Dict[str, Any], int]:
    """Adventuring gear: "When you rummage through your adventuring gear for some useful mundane
    item, you find what you need and mark off a use." (new sheet, uses left). The last use takes
    the gear itself, and its weight with it."""
    what = " ".join(str(what or "").split())
    if not what:
        raise TradeError("Say what they pull out (a torch, a rope, chalk, spikes...).")
    return use_up(sheet, "Adventuring gear")


def use_up(sheet: Dict[str, Any], item_name: str, uses: int = 1) -> Tuple[Dict[str, Any], int]:
    """Mark off ``uses`` of an item with uses (a bag of books, pipeleaf, adventuring gear): (new
    sheet, uses left). The last use takes the item, and its weight with it; an item without uses
    (a keg) is used up whole."""
    gear = list(sheet.get("gear") or [])
    index = next((i for i, g in enumerate(gear) if str(g.get("name", "")).lower() == item_name.lower()), None)
    if index is None:
        raise TradeError(f"{sheet['name']} has no {item_name.lower()} left: they don't have one to hand.")
    item = dict(gear[index])
    have = int(item["uses"]) if "uses" in item else next(
        (int(m[1]) for t in item.get("tags") or () if (m := re.fullmatch(r"(\d+) uses?", str(t)))), 1)
    if have < uses:
        raise TradeError(f"{sheet['name']}'s {item_name.lower()} has only {have} use{'s' if have != 1 else ''} left; "
                         f"this takes {uses}.")
    left = have - uses
    if left <= 0:
        gear.pop(index)
    else:
        gear[index] = {**item, "uses": left}
    ammo, rations, coins = _counts(sheet)
    return _recount(sheet, gear, ammo, rations, coins), max(0, left)


def add_magic_item(sheet: Dict[str, Any], name: str, text: str = "", tags: Sequence[str] = ()) -> Tuple[Dict[str, Any], str]:
    """Give a magic item: carried as gear, with its text (magic.py)."""
    from .magic import entry

    try:
        item = entry(name, text, tags)
    except ValueError as exc:
        raise TradeError(str(exc)) from exc
    gear = list(sheet.get("gear") or []) + [item]
    ammo, rations, coins = _counts(sheet)
    return _recount(sheet, gear, ammo + int(item.get("ammo", 0)), rations, coins), item["name"]


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
    thrown = [dict(t) for t in sheet.get("thrown") or []]
    got: List[str] = []
    count = 0
    for raw in names:
        if m := _COINS.match(str(raw)):
            coins += int(m[1])
            got.append(f"{int(m[1])} coins")
            continue
        back = recovered(thrown, str(raw))
        if back is not None:  # the very weapon they threw: its magic and all
            thrown.remove(back)
            if back.get("item"):
                gear.append(dict(back["item"]))
            ammo += int(back.get("ammo", 0))
            got.append(f"{back['name']} (recovered)")
            continue
        qty, name = _quantity(raw)
        count += qty
        if count > MAX_ITEMS_PER_CALL:
            raise TradeError(f"At most {MAX_ITEMS_PER_CALL} items per call.")
        item, known = item_for(name)
        for _ in range(qty):
            gear.append(item.to_dict())
            ammo += item.ammo
            rations += _rations_for(sheet, item)
        got.append(item.label() if qty == 1 else f"{qty} × {item.label()}")
    out = _recount(sheet, gear, ammo, rations, coins)
    if "thrown" in sheet:
        out["thrown"] = thrown
    return out, got


def recovered(thrown: Sequence[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """The thrown weapon this name picks up ("my spear", "the spear", "Spear of Kantos"), if any."""
    wanted = re.sub(r"^(my|the|his|her|their|a|an)\s+", "", " ".join(str(name).split()).lower())
    wanted = re.sub(r"^\d+\s+", "", wanted)
    return next((t for t in thrown if t["name"].lower() == wanted), None) or next(
        (t for t in thrown if wanted and (wanted in t["name"].lower() or t["name"].lower() in wanted)), None)


def named_but_kept(sheet: Dict[str, Any], text: str) -> List[str]:
    """What they still carry that ``text`` names -- by its whole name, or its last word ("bow" for
    "Ragged bow"). take_items was called with only the arrows while its reason said "Ragged bow
    and quiver knocked into the water": the narration lost the bow and the sheet kept it."""
    said = " ".join(str(text or "").split()).lower()
    out: List[str] = []
    for g in sheet.get("gear") or ():
        name = str(g.get("name", ""))
        last = name.lower().split()[-1] if name.split() else ""
        if name.lower() in said or (len(last) >= 3 and re.search(rf"\b{re.escape(last)}s?\b", said)):
            if name not in out:
                out.append(name)
    return out


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
            rations -= _rations_for(sheet, item)
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


def buy(sheet: Dict[str, Any], name: str, price: int = None, discount: int = 0) -> Tuple[Dict[str, Any], str, int]:
    """At the book's price -- or, after Supply, at the price the GM names: never below the
    book's, at most twice it, and for something the book does not price at most
    MAX_SPECIAL_PRICE. Returns (new sheet, item label, total cost)."""
    from .magic import book_item

    if book_item(name) is not None:
        # "It'll buy you mundane stuff... The special stuff, like magic weapons, isn't for sale."
        raise TradeError(f"{name} isn't for sale -- magic items aren't, not for coin. It is found, earned or made.")
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
    # Minstrel, A Hero's Welcome: "subtract the minstrel's skill from all prices in town."
    cost = max(1, price - int(discount or 0)) * qty
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
    carried = next((g for g in sheet.get("gear") or () if g["name"].lower() == item_name.strip().lower()), None)
    price = price_of(item_name)
    if carried is not None and carried.get("value"):
        # A found valuable -- a gem, a crown -- is worth what the treasure roll said, in full.
        worth = int(carried["value"])
        paid = worth if offer is None else max(0, min(int(offer), worth))
        gear = list(sheet["gear"])
        gear.remove(carried)
        ammo, rations, coins = _counts(sheet)
        return _recount(sheet, gear, ammo, rations, coins + paid), carried["name"], paid
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
