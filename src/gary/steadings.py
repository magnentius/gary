"""Steadings (Dungeon World, "The World"): villages, towns, keeps and cities, described by tags.

"All steadings have tags indicating prosperity, population, and defenses. Many will have
tags to illustrate their more unusual properties." The three ranked categories move up and
down a step at a time (+Prosperity, -Defenses); the rest are tags, some naming something
(Resource (fish), Oath (Castonshire)).

The model makes one with the book's template for its kind -- a village is by default Poor,
Steady, Militia, Resource and an Oath, then one option and one problem -- and updates its
tags as the fiction changes them. Gary keeps them as journalled state and applies the tags
that change a number it runs: what is for sale (prosperity, Market, Exotic, Lawless), what
it costs (Resource, Need), Supply (+1 with Market) and Recruit (+1 with Arcane for an adept,
Divine for a priest, a Guild for its kind).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .sheets import SheetError

PROSPERITY = ("dirt", "poor", "moderate", "wealthy", "rich")
POPULATION = ("exodus", "shrinking", "steady", "growing", "booming")
DEFENSES = ("none", "militia", "watch", "guard", "garrison", "battalion", "legion")
RANKED = {"prosperity": PROSPERITY, "population": POPULATION, "defenses": DEFENSES}

#: Tags beyond the three ranked ones; True when the tag names something.
TAGS: Dict[str, bool] = {
    "safe": False, "religion": True, "exotic": True, "resource": True, "need": True, "oath": True,
    "trade": True, "market": False, "enmity": True, "history": True, "arcane": False, "divine": False,
    "guild": True, "personage": True, "dwarven": False, "elven": False, "craft": True, "lawless": False,
    "blight": True, "power": True,
}

#: The book's template per kind: (default ranks, default tags, options, problems). A change is
#: "+prosperity" / "-defenses", or ("tag", detail-or-None) where None means "your choice".
_Change = Any
KINDS: Dict[str, Dict[str, Any]] = {
    "village": {
        "ranks": ("poor", "steady", "militia"),
        "tags": [("resource", None), ("oath", None)],
        "options": [
            ("naturally defended", [("safe", ""), "-defenses"]),
            ("abundant resources that sustain it", ["+prosperity", ("resource", None), ("enmity", None)]),
            ("under the protection of another steading", [("oath", None), "+defenses"]),
            ("on a major road", [("trade", None), "+prosperity"]),
            ("built around a wizard's tower", [("personage", None), ("blight", "arcane creatures")]),
            ("built on a site of religious significance", [("divine", ""), ("history", None)]),
        ],
        "problems": [
            ("in arid or uncultivable land", [("need", "food")]),
            ("dedicated to a deity", [("religion", None), ("enmity", None)]),
            # "-Prosperity if they fought to the end, -Defenses if they lost": the model adjusts those.
            ("recently fought a battle", ["-population"]),
            ("has a monster problem", [("blight", None), ("need", "adventurers")]),
            ("absorbed another village", ["+population", ("lawless", "")]),
            ("remote or unwelcoming", ["-prosperity", ("dwarven or elven", None)]),
        ],
    },
    "town": {
        "ranks": ("moderate", "steady", "watch"),
        "tags": [("trade", None), ("trade", None)],
        "options": [
            ("booming", ["=population:booming", ("lawless", "")]),
            ("stands on a crossroads", [("market", ""), "+prosperity"]),
            ("defended by another steading", [("oath", None), "+defenses"]),
            ("built around a church", [("power", "divine")]),
            ("built around a craft", [("craft", None), ("resource", None)]),
            ("built around a military post", ["+defenses"]),
        ],
        "problems": [
            ("grown too big for an important supply", [("need", None), ("trade", None)]),
            ("offers defense to others", [("oath", None), "-defenses"]),
            ("notorious for an outlaw rumored to live there", [("personage", None), ("enmity", None)]),
            ("cornered the market on a good or service", [("exotic", None), ("enmity", None)]),
            ("has a disease", ["-population"]),
            ("a popular meeting place", ["+population", ("lawless", "")]),
        ],
    },
    "keep": {
        "ranks": ("poor", "shrinking", "guard"),
        "tags": [("need", "supplies"), ("trade", None), ("oath", None)],
        "options": [
            ("belongs to a noble family", ["+prosperity", ("power", "political")]),
            ("run by a skilled commander", [("personage", None), "+defenses"]),
            ("stands watch over a trade road", ["+prosperity", ("guild", "trade")]),
            ("used to train special troops", [("arcane", ""), "-population"]),
            ("surrounded by fertile land", ["remove:need"]),
            ("stands on a border", ["+defenses", ("enmity", None)]),
        ],
        "problems": [
            ("built on a naturally defensible position", [("safe", ""), "-population"]),
            ("a conquest from another power", [("enmity", None)]),
            ("a safe haven for brigands", [("lawless", "")]),
            ("built to defend from a specific threat", [("blight", None)]),
            ("has seen horrible bloody war", [("history", "battle"), ("blight", "restless spirits")]),
            ("given the worst of the worst", [("need", "skilled recruits")]),
        ],
    },
    "city": {
        "ranks": ("moderate", "steady", "guard"),
        "tags": [("market", ""), ("guild", None), ("oath", None), ("oath", None)],
        "options": [
            ("has permanent defenses, like walls", ["+defenses", ("oath", None)]),
            ("ruled by a single individual", [("personage", None), ("power", "political")]),
            ("diverse", [("dwarven or elven", None)]),
            ("a trade hub", [("trade", None), "+prosperity"]),
            ("ancient, built on its own ruins", [("history", None), ("divine", "")]),
            ("a center of learning", [("arcane", ""), ("craft", None), ("power", "arcane")]),
        ],
        "problems": [
            ("outgrown its resources", ["+population", ("need", "food")]),
            ("has designs on nearby territory", [("enmity", None), "+defenses"]),
            ("ruled by a theocracy", ["-defenses", ("power", "divine")]),
            ("ruled by the people", ["-defenses", "+population"]),
            ("has supernatural defenses", ["+defenses", ("blight", None)]),
            ("lies on a place of power", [("arcane", ""), ("personage", None), ("blight", "arcane creatures")]),
        ],
    },
}

#: Recruit: the tag that gives +1 when this kind of hireling is sought.
RECRUIT_BONUS = {"Adept": "arcane", "Priest": "divine"}

MAX_TEXT = 80


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:40]


def _step(record: Dict[str, Any], change: str) -> None:
    sign, category = change[0], change[1:]
    ranks = RANKED[category]
    at = ranks.index(record[category])
    record[category] = ranks[max(0, min(len(ranks) - 1, at + (1 if sign == "+" else -1)))]


def _add(record: Dict[str, Any], tag: str, detail: str) -> None:
    if tag == "dwarven or elven":
        tag, detail = (detail.lower(), "") if detail.lower() in ("dwarven", "elven") else ("dwarven", "")
    if tag not in TAGS:
        raise SheetError(f"No steading tag {tag!r}. Tags: {', '.join(TAGS)}.")
    entry = {"tag": tag, "detail": " ".join(str(detail or "").split())[:MAX_TEXT]}
    if TAGS[tag] and not entry["detail"]:
        raise SheetError(f"{tag.title()} names something: say what (details).")
    if entry not in record["tags"]:
        record["tags"].append(entry)


def make(name: str, kind: str, option: int, problem: int, details: Sequence[str] = ()) -> Dict[str, Any]:
    """A steading built by the book: its kind's defaults, one option and one problem. Every
    "your choice" is filled, in order, from ``details``."""
    name = " ".join(str(name or "").split())[:MAX_TEXT]
    if not name:
        raise SheetError("A steading needs a name.")
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise SheetError(f"A steading is a {', '.join(KINDS)}.")
    book = KINDS[kind]
    if not (0 <= int(option) < len(book["options"]) and 0 <= int(problem) < len(book["problems"])):
        raise SheetError(f"Choose option and problem by number, 0-{len(book['options']) - 1}.")
    blanks = blank_names(kind, option, problem)
    fill = [" ".join(str(d).split()) for d in details or () if str(d).strip()]
    if len(fill) < len(blanks):
        raise SheetError(f"This {kind} needs {len(blanks)} details, in order: {'; '.join(blanks)}.")
    record = {"name": name, "kind": kind, "prosperity": book["ranks"][0], "population": book["ranks"][1],
              "defenses": book["ranks"][2], "tags": []}
    for change in [*book["tags"], *book["options"][int(option)][1], *book["problems"][int(problem)][1]]:
        if isinstance(change, tuple):
            tag, detail = change
            _add(record, tag, fill.pop(0) if detail is None else detail)
        elif change.startswith("remove:"):
            record["tags"] = [t for t in record["tags"] if t["tag"] != change.split(":")[1]]
        elif change.startswith("="):
            category, value = change[1:].split(":")
            record[category] = value
        else:
            _step(record, change)
    record["option"] = book["options"][int(option)][0]
    record["problem"] = book["problems"][int(problem)][0]
    return record


#: What each named tag names, for the model filling the blanks.
_NAMES = {"oath": "another steading's name", "trade": "another steading's name",
          "enmity": "another steading's name", "resource": "a good", "need": "a good", "exotic": "a good or service",
          "craft": "a craft", "blight": "a monster or threat", "personage": "a person", "religion": "a deity",
          "guild": "a kind of guild", "history": "an event", "dwarven or elven": "dwarven or elven"}


def blank_names(kind: str, option: int, problem: int) -> List[str]:
    """What each "your choice" in this build is, in the order ``details`` fills them."""
    book = KINDS[kind]
    return [f"{c[0]} ({_NAMES.get(c[0], 'your choice')})"
            for c in [*book["tags"], *book["options"][int(option)][1], *book["problems"][int(problem)][1]]
            if isinstance(c, tuple) and c[1] is None]


def adjust(record: Dict[str, Any], steps: Sequence[str] = (), add: Sequence[Tuple[str, str]] = (),
           remove: Sequence[str] = (), kind: str = "") -> Dict[str, Any]:
    """The steading changed: "+prosperity", tags gained or lost, a village grown into a town."""
    out = {**record, "tags": [dict(t) for t in record["tags"]]}
    for step in steps:
        step = str(step).strip().lower()
        if len(step) < 2 or step[0] not in "+-" or step[1:] not in RANKED:
            raise SheetError(f"A step is +/- prosperity, population or defenses, not {step!r}.")
        _step(out, step)
    for tag in remove:
        tag = str(tag).strip().lower()
        if not any(t["tag"] == tag for t in out["tags"]):
            raise SheetError(f"{out['name']} has no {tag} tag.")
        out["tags"] = [t for t in out["tags"] if t["tag"] != tag]
    for tag, detail in add:
        _add(out, str(tag).strip().lower(), detail)
    if kind:
        if kind not in KINDS:
            raise SheetError(f"A steading is a {', '.join(KINDS)}.")
        out["kind"] = kind
    return out


def has(record: Optional[Dict[str, Any]], tag: str, detail: str = "") -> bool:
    return any(t["tag"] == tag and (not detail or detail.lower() in t["detail"].lower())
               for t in (record or {}).get("tags") or ())


def details(record: Optional[Dict[str, Any]], tag: str) -> List[str]:
    return [t["detail"] for t in (record or {}).get("tags") or () if t["tag"] == tag]


def rank(record: Dict[str, Any], category: str) -> int:
    """1 for the lowest tag in the category, and up: "Dirt is 1, Poor is 2"."""
    return RANKED[category].index(record[category]) + 1


def for_sale(record: Optional[Dict[str, Any]], item_name: str, kind: str, price: Optional[int]) -> str:
    """Why this steading does not sell it, or "" if it does. No steading (the wilds, a
    camp) sells nothing to refuse here: the model decides whether there is a seller."""
    if record is None:
        return ""
    name = item_name.lower()
    if any(d and d.lower() in name for d in details(record, "exotic") + details(record, "craft")):
        return ""
    level = rank(record, "prosperity") + (1 if has(record, "market") else 0)  # Market: "far beyond their prosperity"
    where = f"{record['name']} ({record['prosperity'].title()})"
    if level <= 1:
        return f"Nothing is for sale in {where}: nobody has more than they need."
    if kind == "poison" and level < 4 and not has(record, "lawless"):
        return f"Nobody in {where} openly sells poison."
    if level == 2:
        necessities = kind == "gear" and (price or 0) <= 20
        militant = rank(record, "defenses") >= 5
        if not necessities and not (kind == "weapon" and militant):
            return f"Only the bare necessities are for sale in {where}."
    if level == 3 and (price or 0) > 100:
        return f"{item_name} is beyond what {where} has for sale -- most mundane goods, not the finest."
    return ""


def price_factor(record: Optional[Dict[str, Any]], item_name: str) -> Tuple[float, str]:
    """Resource: "significantly cheaper". Need: "sells for considerably more"."""
    name = item_name.lower()
    for detail in details(record, "resource"):
        if detail and detail.lower() in name:
            return 0.5, f"plentiful here (Resource: {detail})"
    for detail in details(record, "need"):
        if detail and detail.lower() in name:
            return 2.0, f"scarce here (Need: {detail})"
    return 1.0, ""


def recruit_bonus(record: Optional[Dict[str, Any]], seeking: str) -> Tuple[int, str]:
    """+1 to Recruit: Arcane for an adept, Divine for a priest, a Guild for its kind."""
    skill = str(seeking or "").strip().title()
    tag = RECRUIT_BONUS.get(skill)
    if tag and has(record, tag):
        return 1, tag.title()
    if skill and any(skill.lower() in d.lower() for d in details(record, "guild")):
        return 1, "Guild"
    return 0, ""


def describe(record: Dict[str, Any]) -> str:
    tags = ", ".join(t["tag"].title() + (f" ({t['detail']})" if t["detail"] else "") for t in record["tags"])
    return (f"{record['name']} — {record['kind']}: {record['prosperity'].title()}, {record['population'].title()}, "
            f"{record['defenses'].title()}" + (f"; {tags}" if tags else ""))


#: Landed (Landed Gentry): a stronghold's benefits and the GM's complication.
STRONGHOLD_BENEFITS = ("taxation", "fortified", "garrisoned", "prestigious", "skilled")
STRONGHOLD_COMPLICATIONS = ("infested", "unrest", "disputed")


def make_stronghold(name: str, owner: str, benefits: Sequence[str], complication: str,
                    craft: str = "") -> Dict[str, Any]:
    """Landed: "Add it to the world map as a keep steading with these tags: Poor, Steady,
    Guard, Oath (protection for nearby lands). Choose 2 benefits ... The GM will also give
    you one complication"."""
    chosen = list(dict.fromkeys(str(b).strip().lower() for b in benefits))
    if len(chosen) != 2 or not set(chosen) <= set(STRONGHOLD_BENEFITS):
        raise SheetError(f"Choose 2 benefits: {', '.join(STRONGHOLD_BENEFITS)}.")
    complication = str(complication or "").strip().lower()
    if complication not in STRONGHOLD_COMPLICATIONS:
        raise SheetError(f"The GM gives one complication: {', '.join(STRONGHOLD_COMPLICATIONS)}.")
    record = {"name": " ".join(str(name or "").split())[:MAX_TEXT], "kind": "keep", "prosperity": "poor",
              "population": "steady", "defenses": "guard", "tags": [], "stronghold_of": owner,
              "benefits": chosen, "complication": complication}
    if not record["name"]:
        raise SheetError("Give the stronghold a name!")
    _add(record, "oath", "protection for nearby lands")
    if "garrisoned" in chosen:
        record["defenses"] = "garrison"
    if "prestigious" in chosen:
        _add(record, "personage", owner)
    if "skilled" in chosen:
        _add(record, "craft", craft or "")
    if complication == "infested":
        _add(record, "blight", "monsters about")
    return record


def here(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The steading the party is in: the one the scene names, if it is known."""
    key = slug((state.get("scene") or {}).get("steading") or "")
    return (state.get("steadings") or {}).get(key) if key else None


def options_text(kind: str) -> str:
    """The template, for the model to choose from by number."""
    book = KINDS[kind]
    return ("options: " + "; ".join(f"{i} {t}" for i, (t, _) in enumerate(book["options"]))
            + " | problems: " + "; ".join(f"{i} {t}" for i, (t, _) in enumerate(book["problems"])))


def downtime(record: Dict[str, Any], ready: Sequence[str] = ()) -> str:
    """What the party can do on settling into a steading -- the book's downtime moves, with what
    this place's tags change -- so a player who doesn't know them can still ask."""
    level = rank(record, "prosperity") + (1 if has(record, "market") else 0)
    shops = ("nothing is for sale here" if level <= 1 else "only the bare necessities are for sale"
             if level == 2 else "most ordinary goods are for sale" if level == 3 else "almost anything is for sale")
    supply = "Supply (+Cha) finds something rare or special" + (", +1 here (Market)" if has(record, "market") else "")
    recruit = "Recruit hirelings" + "".join(f", +1 for {w}" for tag, w in (("arcane", "an adept"),
                                                                           ("divine", "a priest"))
                                            if has(record, tag))
    lines = [f"🏘️ **{record['name']}** — some downtime. You can:",
             f"• **Buy and sell** — {shops}.",
             f"• **{supply}.**",
             "• **Recover** — a day of rest in comfort heals you fully; three days shed a debility.",
             "• **Carouse** — spend coin on a night out (+1 per 100 spent) for contacts, rumours, renown.",
             f"• **{recruit}.**",
             "• **Bolster** — weeks of study or practice, banked as +1s for later."]
    if ready:
        lines.append(f"• **Level up** — {', '.join(ready)}: the button is on `/sheet`.")
    lines.append("Tell the GM what you're doing, or press on.")
    return "\n".join(lines)


# -- Updating the Campaign Map: the book's between-session conditions -------------------------

SIZES = ("village", "town", "keep", "city")
_SHRINK = {"city": ("town", ["=population:steady", "+prosperity"]),
           "keep": ("town", ["+defenses", "=population:steady"]),
           "town": ("village", ["=population:steady", "+prosperity"]),
           "village": ("ghost town", [])}


def _set(record: Dict[str, Any], change: str) -> Dict[str, Any]:
    out = {**record, "tags": [dict(t) for t in record["tags"]]}
    if change.startswith("="):
        category, value = change[1:].split(":")
        out[category] = value
    else:
        _step(out, change)
    return out


def _enmity(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return any(d and d.lower() == b["name"].lower() for d in details(a, "enmity")) or any(
        d and d.lower() == a["name"].lower() for d in details(b, "enmity"))


def between_sessions(steadings: Dict[str, Dict[str, Any]], distance=lambda a, b: 0
                     ) -> Tuple[Dict[str, Dict[str, Any]], List[str], List[str]]:
    """The book's conditions, checked for every steading: (changed records by key, what Gary changed,
    what the GM must decide). Gary applies what the tags alone decide -- Collapse, Profit, Surplus --
    and works out Opportunity's arithmetic; Growth (a "may"), Want, Trade, Capture, Aid, Embattled
    and Clash turn on the fiction, so they go to the GM as a checklist naming the steadings."""
    records = {k: {**v, "tags": [dict(t) for t in v["tags"]]} for k, v in steadings.items()}
    changed: Dict[str, Dict[str, Any]] = {}
    done: List[str] = []
    ask: List[str] = []

    def keep(key: str, record: Dict[str, Any], why: str) -> None:
        records[key] = changed[key] = record
        done.append(why)

    for key, s in list(records.items()):  # Collapse
        if s["kind"] in _SHRINK and s.get("population") == "exodus" and rank(s, "prosperity") <= 2:  # poor or less
            smaller, steps = _SHRINK[s["kind"]]
            out = {**s, "kind": smaller}
            for step in steps:
                out = _set(out, step)
            keep(key, out, f"Collapse: {s['name']}, emptying and poor, shrinks to a {smaller}.")
    for key, s in list(records.items()):  # Profit
        trades = len(details(s, "trade"))
        if s["kind"] != "ghost town" and trades > rank(s, "prosperity") and rank(s, "prosperity") < len(PROSPERITY):
            keep(key, _set(s, "+prosperity"), f"Profit: {s['name']} has {trades} trade partners, more than its "
                                              f"prosperity; +prosperity.")
    for key, s in list(records.items()):  # Surplus
        for other_key, other in list(records.items()):
            if other_key == key or _enmity(s, other):
                continue
            for good in details(s, "resource"):
                needed = next((n for n in details(other, "need") if good and n and
                               (good.lower() in n.lower() or n.lower() in good.lower())), None)
                if needed is None:
                    continue
                supplier = _set(records[key], "+prosperity")
                buyer = {**records[other_key], "tags": [t for t in records[other_key]["tags"]
                                                        if not (t["tag"] == "need" and t["detail"] == needed)]}
                if not any(d.lower() == s["name"].lower() for d in details(buyer, "trade")):
                    _add(buyer, "trade", s["name"])
                keep(key, supplier, f"Surplus: {s['name']} has {good}, which {other['name']} needs: they trade. "
                                    f"{s['name']} +prosperity, and its choice of an oath, +population or +defenses "
                                    "(yours to give with adjust_steading); "
                                    f"{other['name']} no longer needs {needed} and trades with {s['name']}.")
                records[other_key] = changed[other_key] = buyer
    for key, s in records.items():  # Opportunity
        for target_name in details(s, "enmity"):
            target = next((t for t in records.values() if t["name"].lower() == target_name.lower()), None)
            if target is None or s["kind"] not in SIZES or target["kind"] not in SIZES:
                continue
            reach = rank(s, "defenses") - distance(s["name"], target["name"])
            guard = rank(target, "defenses") + max(0, SIZES.index(target["kind"]) - SIZES.index(s["kind"]))
            if reach > guard:
                ask.append(f"Opportunity: {s['name']} definitely attacks {target['name']} (its enmity): {target['name']} "
                           f"is embattled, and {s['name']} has -defenses while the attack lasts.")
            elif rank(s, "defenses") >= rank(target, "defenses"):
                ask.append(f"Opportunity: {s['name']} could attack {target['name']}: your call -- has anything "
                           "stoked their anger lately?")
    for s in records.values():
        if s["kind"] in ("village", "town") and s.get("population") == "booming" and rank(s, "prosperity") > 3:
            ask.append(f"Growth: {s['name']} is booming and {s['prosperity']}: it may give up prosperity and defenses "
                       f"to become a {SIZES[SIZES.index(s['kind']) + 1]} (a new town gains Market, a new city a Guild).")
        for need in details(s, "need"):
            ask.append(f"Want: {s['name']} needs {need}. Is it met? If not: -prosperity, -population, or it loses a "
                       "tag that depends on it.")
        for partner in details(s, "trade"):
            gone = next((t for t in records.values() if t["name"].lower() == partner.lower()
                         and t["kind"] == "ghost town"), None)
            ask.append(f"Trade: {s['name']} trades with {partner}" + (" -- a ghost town now" if gone else "")
                       + ". If the route is blocked or the source gone: a need for that good, or -prosperity.")
    ask.append("Capture, Aid, Embattled, Clash: if a resource changed hands, a steading is under attack or two "
               "fight, apply the book's rule. Then add any tags that reflect what the players and the fronts did.")
    return changed, done, ask


# -- coming back: what changed since the party was last here -------------------------------

def snapshot(record: Dict[str, Any]) -> Dict[str, Any]:
    """The steading as the party leaves it, to compare when they return."""
    return {"kind": record.get("kind"), **{c: record.get(c) for c in RANKED},
            "tags": [dict(t) for t in record.get("tags") or []]}


def _tag(t: Dict[str, Any]) -> str:
    return t["tag"].title() + (f" ({t['detail']})" if t.get("detail") else "")


def changes_since(before: Dict[str, Any], now: Dict[str, Any]) -> List[str]:
    """What is different from how they left it: its size, its ranks, tags gained and lost."""
    out: List[str] = []
    if before.get("kind") != now.get("kind"):
        out.append(f"it was a {before.get('kind')}; now it is a {now.get('kind')}")
    for category in RANKED:
        if before.get(category) != now.get(category):
            out.append(f"{category} {before.get(category)} → {now.get(category)}")
    was = {_tag(t) for t in before.get("tags") or ()}
    is_ = {_tag(t) for t in now.get("tags") or ()}
    out += [f"new: {t}" for t in sorted(is_ - was)]
    out += [f"gone: {t}" for t in sorted(was - is_)]
    return out
