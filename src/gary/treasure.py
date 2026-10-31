"""Treasure (Dungeon World, "Monsters" -> Treasure), and the book's magic items as ideas.

"If the monster has accumulated some wealth you can roll that randomly. Start with the
monster's damage die, modified if the monster is":

- Hoarder: roll the damage die twice, take the higher.
- Far from home: at least one ration.
- Magical: some strange item, possibly magical. Divine: a sign of a deity. Planar:
  something not of this earth.
- Lord over others: +1d4. Ancient and noteworthy: +1d4.

The total reads the book's table, 1 to 18 (18 and up is a hoard); three results say "and
roll again". Gary rolls the die and every amount the table names, so a chest really holds
3d6×100 coins; the coins and the valuables (worth coins, sold for what they are worth) go
to whoever searched. What the rest is -- the useful item, the information, the portal,
the magic -- is the GM's to say, and for anything magical the GM is shown a few of the
book's magic items for ideas, as the book offers them: "magic items are ultimately for
you to decide".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .damage import parse

#: Tags in the bestiary that change a treasure roll.
_TAG_FLAGS = {"hoarder": "hoarder", "magical": "magical", "divine": "divine", "planar": "planar"}

#: How many times "roll again" may chain before the table stops.
MAX_REROLLS = 3

#: (what it is, coin dice, multiplier, valuable dice, multiplier, valuable weight, kind)
#: kind: "coins", "valuable", "gm" (the GM says what), "magic", "again".
_TABLE = {
    1: ("A few coins", "2d8", 1, None, 0, 0, "coins"),
    2: ("An item useful to the current situation", None, 0, None, 0, 0, "gm"),
    3: ("Several coins", "4d10", 1, None, 0, 0, "coins"),
    4: ("A small item (gem, art) of considerable value", None, 0, "2d10", 10, 0, "valuable"),
    5: ("Some minor magical trinket", None, 0, None, 0, 0, "magic"),
    6: ("Useful information (clues, notes)", None, 0, None, 0, 0, "gm"),
    7: ("A bag of coins", "1d4", 100, None, 0, 0, "coins"),
    8: ("A very valuable small item (gem, art)", None, 0, "2d6", 100, 0, "valuable"),
    9: ("A chest of coins and other small valuables", None, 0, "3d6", 100, 1, "valuable"),
    10: ("A magical item or magical effect", None, 0, None, 0, 0, "magic"),
    11: ("Many bags of coins", "2d4", 100, None, 0, 0, "coins"),
    12: ("A sign of office (crown, banner)", None, 0, "3d4", 100, 1, "valuable"),
    13: ("A large art item", None, 0, "4d4", 100, 1, "valuable"),
    14: ("A unique item", None, 0, "5d4", 100, 1, "valuable"),
    15: ("All the information needed to learn a new spell", None, 0, None, 0, 0, "again"),
    16: ("A portal or secret path, or directions to one", None, 0, None, 0, 0, "again"),
    17: ("Something relating to one of the characters", None, 0, None, 0, 0, "again"),
    18: ("A hoard", "1d10", 1000, None, 0, 0, "hoard"),
}


def _load() -> List[Dict[str, Any]]:
    path = Path(__file__).parent / "data" / "magic_items.json"
    return json.loads(path.read_text(encoding="utf-8"))["items"]


MAGIC_ITEMS = _load()


@dataclass
class Find:
    roll: int  # the total that read the table
    rolled: str  # how, for the table: "d8: 3, hoarder d8: 6 → 6, lord +1d4: 2 = 8"
    entries: List[str] = field(default_factory=list)  # what the table said, in order
    coins: int = 0
    valuables: List[Dict[str, Any]] = field(default_factory=list)  # {"name", "value", "weight"}
    rations: int = 0
    for_gm: List[str] = field(default_factory=list)  # what the GM must decide and narrate
    ideas: List[Dict[str, Any]] = field(default_factory=list)  # magic items from the book


def flags_of(record: Optional[Dict[str, Any]]) -> List[str]:
    """The treasure flags a bestiary creature's tags give it."""
    return [_TAG_FLAGS[t.lower()] for t in (record or {}).get("tags") or () if t.lower() in _TAG_FLAGS]


#: Rolls dice like "3d6" and returns the total: Gary's dice, so treasure is as fair -- and as
#: testable -- as every other roll.
Die = Callable[[str], int]


def roll(die: Die, damage: str, flags: Sequence[str] = (), sample: Callable = None, _depth: int = 0) -> Find:
    """Roll a creature's treasure from its damage expression and flags. ``sample(items, k)``
    picks the magic item ideas (random.sample by default)."""
    if sample is None:
        import random

        sample = random.SystemRandom().sample
    sides = parse(damage).sides
    first = die(f"1d{sides}")
    total, shown, added = first, f"d{sides}: {first}", False
    if "hoarder" in flags:
        second = die(f"1d{sides}")
        total = max(first, second)
        shown = f"d{sides} twice (hoarder): {first}, {second} → {total}"
    for flag in ("lord", "ancient"):
        if flag in flags:
            more = die("1d4")
            total += more
            added = True
            shown += f", +1d4 {'lord over others' if flag == 'lord' else 'ancient'}: {more}"
    if added:
        shown += f" = {total}"
    found = Find(roll=total, rolled=shown)
    _read(die, sample, found, min(max(total, 1), 18), damage, flags, _depth)
    if "far from home" in flags:
        found.rations += 1
    for flag, what in (("magical", "some strange item, possibly magical"),
                       ("divine", "a sign of a deity (or deities)"),
                       ("planar", "something not of this earth")):
        if flag in flags:
            found.for_gm.append(f"Also, as it is {flag}: {what}.")
            if flag == "magical" and not found.ideas:
                found.ideas = sample(MAGIC_ITEMS, 3)
    return found


def _read(die: Die, sample: Callable, found: Find, index: int, damage: str, flags: Sequence[str],
          depth: int) -> None:
    text, coins, coin_mult, worth, worth_mult, weight, kind = _TABLE[index]
    if kind == "coins":
        amount = die(coins) * coin_mult
        found.coins += amount
        found.entries.append(f"{text}: {amount} coins")
    elif kind == "valuable":
        value = die(worth) * worth_mult
        found.valuables.append({"name": text.split(" (")[0], "value": value, "weight": weight})
        found.entries.append(f"{text}, worth {value} coins")
        found.for_gm.append(f"Name the {text.split(' (')[0].lower()} (worth {value} coins) -- what it is, "
                            "what it looks like. Its worth is fixed.")
    elif kind == "hoard":
        coins_found = die("1d10") * 1000
        gems = die("1d10") * 10
        each = die("2d6") * 100
        found.coins += coins_found
        found.valuables.append({"name": f"{gems} gems", "value": gems * each, "weight": 0})
        found.entries.append(f"A hoard: {coins_found} coins and {gems} gems worth {each} each")
    else:
        found.entries.append(text)
        found.for_gm.append(f"{text}: you say what it is.")
        if kind == "magic" and not found.ideas:
            found.ideas = sample(MAGIC_ITEMS, 3)
        if kind == "again" and depth < MAX_REROLLS:
            again = roll(die, damage, [f for f in flags if f in ("hoarder", "lord", "ancient")], sample, depth + 1)
            found.entries += [f"and rolling again ({again.rolled}): {e}" if i == 0 else e
                              for i, e in enumerate(again.entries)]
            found.coins += again.coins
            found.valuables += again.valuables
            found.for_gm += again.for_gm
            found.ideas = found.ideas or again.ideas
