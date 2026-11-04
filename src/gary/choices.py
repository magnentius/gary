"""The choices a class makes at creation (Dungeon World, the classes' starting moves).

    Cleric   Deity: a name, a domain, a precept (which adds a Petition to Divine Guidance)
    Druid    Born of the Soil: a land, and a tell
    Bard     Bardic Lore: an area of expertise
    Thief    Poisoner: a poison, safe in their hands, with three uses to start
    Fighter  Signature Weapon: a look -- and an Elf, a weapon type that is always precise
    Barbarian  one of two starting moves, and two Herculean Appetites
    Everyone   a look: one from each of the class's lists (eyes, hair, clothes, body), and a
               line in their own words if they like

On the sheet as ``choices`` (key -> what was chosen), and for a Thief ``poison_uses``. The
lists are read from the move's own text, so the words are the authors'. Most choices are
the model's to honour; two are numbers Gary runs: the Elf's precise weapon (Hack and Slash
with Dex) and the Thief's poison uses (``use_poison``, ``brew_poison``).

Made step by step in ``Choices``, checked like a sheets.Draft: by ``/new``, and by
the Choices and look button on ``/sheet`` for a character made before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .class_moves import TEXTS
from .sheets import MAX_NAME_CHARS, SheetError

#: "You also start with three uses of the poison you choose", and brew three at a time.
POISON_BATCH = 3

#: Weapon types an Elf Fighter may make precise: the equipment list's weapons by kind,
#: and the signature weapon's bases.
WEAPON_TYPES = ("Sword", "Axe", "Hammer", "Mace", "Spear", "Flail", "Dagger", "Knife", "Shiv",
                "Club", "Staff", "Halberd", "Rapier", "Bow", "Crossbow", "Fists")



@dataclass(frozen=True)
class Step:
    key: str
    prompt: str
    options: Tuple[str, ...] = ()  # empty: written by the player
    descriptions: Tuple[str, ...] = ()
    # A written step's text box: Discord shows 45 characters of a label, 100 of a placeholder.
    label: str = ""
    placeholder: str = ""
    optional: bool = False  # a written step that may be left blank (the look's own words)

    @property
    def written(self) -> bool:
        return not self.options


def _bullet_groups(cls: str, move: str) -> List[List[str]]:
    """The runs of bullet points in a starting move's text, in order."""
    text = next(m["text"] for m in TEXTS[cls]["starting"] if m["name"] == move)
    groups: List[List[str]] = [[]]
    for line in text.split("\n"):
        if line.startswith("• "):
            groups[-1].append(line[2:].strip())
        elif groups[-1]:
            groups.append([])
    return [g for g in groups if g]


def _named(options: Sequence[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """"Oil of Tagit (applied): The target falls…" -> ("Oil of Tagit", "applied: The target…")."""
    names, descriptions = [], []
    for o in options:
        m = re.match(r"([^(:]+?)\s*\(([^)]*)\):\s*(.*)", o)
        names.append(m[1] if m else o)
        descriptions.append(f"{m[2]}: {m[3]}" if m else "")
    return tuple(names), tuple(descriptions)


def look_steps(class_name: str) -> List[Step]:
    """The class's look: "Choose one for each" -- and a line of the player's own."""
    from .sheets import CLASSES

    lists = (CLASSES.get(class_name.lower()) or {}).get("looks") or []
    out = [Step(f"look_{i}", f"Your look ({i + 1} of {len(lists)}): choose one.", tuple(options))
           for i, options in enumerate(lists)]
    if lists:
        out.append(Step("look_own", "Anything else about how they look? A scar, a tattoo, a limp -- or skip it.",
                        label="Anything else about your look", placeholder="a scar through one eyebrow…",
                        optional=True))
    return out


def steps(class_name: str, race: str, favored: bool = False) -> List[Step]:
    return _class_steps(class_name, race, favored) + ([] if favored else look_steps(class_name))


def _class_steps(class_name: str, race: str, favored: bool = False) -> List[Step]:
    cls = class_name.lower()
    if favored and cls in ("paladin", "ranger"):
        # Divine Favor, God Amidst the Wastes: "Dedicate yourself to a deity".
        return [Step("deity", "Name the deity you are dedicated to (a new one, or one already "
                              "established).", label="Your god's name", placeholder="Helferth, Sucellus…")]
    if cls == "cleric":
        domains, precepts = _bullet_groups("cleric", "Deity")
        # "Your religion is cultish and insular, add Petition: Gaining Secrets": the petition
        # is what the choice gives, and a dropdown cuts long labels off well short of it.
        split = [re.match(r"(.+?),\s*add (Petition: .+)", p) for p in precepts]
        return [Step("deity", "Give your god a name (maybe Helferth, Sucellus, Zorica or Krugon the Bleak).",
                     label="Your god's name", placeholder="Helferth, Sucellus, Zorica, Krugon the Bleak…"),
                Step("domain", "Choose your deity's domain.", tuple(domains)),
                Step("precept", "Choose one precept of your religion — it adds a Petition to "
                                "Divine Guidance.",
                     tuple(m[2] for m in split), tuple(m[1] for m in split))]
    if cls == "druid":
        (lands,) = _bullet_groups("druid", "Born of the Soil")
        return [Step("land", "Choose the land to which you are attuned — you may take the shape of "
                             "any animal who might live there.", tuple(lands)),
                Step("tell", "Your tell: a physical mark of your land that stays in every shape "
                             "(antlers, leopard's spots, hair like leaves).",
                     label="Your tell", placeholder="antlers, leopard's spots, hair like leaves…")]
    if cls == "bard":
        (areas,) = _bullet_groups("bard", "Bardic Lore")
        return [Step("lore", "Choose your area of bardic lore.", tuple(areas))]
    if cls == "thief":
        (poisons,) = _bullet_groups("thief", "Poisoner")
        names, descriptions = _named(poisons)
        return [Step("poison", "Choose the poison you have mastered — you start with three uses.",
                     names, descriptions)]
    if cls == "barbarian":
        from .playbooks import appetites, choose_one

        pair = choose_one("barbarian")
        texts = {m["name"]: m["text"] for m in TEXTS["barbarian"]["starting"]}
        return [Step("start", "Choose one of these to start with.", tuple(pair),
                     tuple(texts[n][:100] for n in pair)),
                Step("appetite", "Herculean Appetites: choose your first appetite. While pursuing one, you "
                                 "roll 1d6+1d8 instead of 2d6.", tuple(appetites())),
                Step("appetite_2", "Choose your second appetite.", tuple(appetites()))]
    if cls == "fighter":
        looks = _bullet_groups("fighter", "Signature Weapon")[-1]
        out = [Step("look", "Choose a look for your signature weapon.", tuple(looks))]
        if race == "Elf":
            out.append(Step("precise_weapon", "Elf: choose one weapon type — you always treat it "
                                              "as precise.", WEAPON_TYPES))
        return out
    return []


class Choices:
    """A character's creation choices, made one step at a time."""

    def __init__(self, class_name: str, race: str, made: Optional[Dict[str, str]] = None,
                 favored: bool = False) -> None:
        self.steps = steps(class_name, race, favored)
        self.made: Dict[str, str] = dict(made or {})

    def step(self) -> Optional[Step]:
        return next((s for s in self.steps if s.key not in self.made), None)

    def choose(self, value: str) -> None:
        step = self.step()
        if step is None:
            raise SheetError("Nothing left to choose.")
        value = " ".join(str(value or "").split())
        if step.written:
            if not value and not step.optional:
                raise SheetError("Write something.")
            if len(value) > MAX_NAME_CHARS * 2:
                raise SheetError(f"Keep it to {MAX_NAME_CHARS * 2} characters.")
        elif value not in step.options:
            raise SheetError("That is not one of the choices.")
        if step.key.startswith("appetite") and value in appetites_of({"choices": self.made}):
            raise SheetError(f"{value} is already one of your appetites: choose another.")
        self.made[step.key] = value

    @property
    def complete(self) -> bool:
        return self.step() is None


def missing(sheet: Dict[str, Any]) -> bool:
    from .spells import favored

    return not Choices(sheet.get("class", ""), sheet.get("race", ""), sheet.get("choices"),
                       favored(sheet)).complete


def on_sheet(sheet: Dict[str, Any], made: Dict[str, str]) -> Dict[str, Any]:
    """The sheet with these choices -- and a new Thief's three uses of their poison."""
    out = {**sheet, "choices": {**(sheet.get("choices") or {}), **made}}
    if "poison" in made and "poison_uses" not in sheet:
        out["poison_uses"] = POISON_BATCH
    return out


def poison_text(name: str) -> str:
    (poisons,) = _bullet_groups("thief", "Poisoner")
    names, descriptions = _named(poisons)
    return descriptions[names.index(name)] if name in names else ""


def appetites_of(sheet: Dict[str, Any]) -> List[str]:
    """A Barbarian's appetites: the two from creation, any from Still Hungry, less any resolved."""
    c = sheet.get("choices") or {}
    got = [c[k] for k in ("appetite", "appetite_2") if c.get(k)] + list(sheet.get("more_appetites") or ())
    gone = set(sheet.get("resolved_appetites") or ())
    return [a for a in got if a not in gone]


def look_of(sheet: Dict[str, Any]) -> str:
    """How they look, in a line: "Hard Eyes, Wild Hair, Scarred Skin, Built Body; a scar…"."""
    c = sheet.get("choices") or {}
    picks = [c[k] for k in sorted(k for k in c if k.startswith("look_") and k != "look_own") if c[k]]
    own = str(c.get("look_own") or "").strip()
    return ", ".join(picks) + (("; " if picks else "") + own if own else "")


def describe(sheet: Dict[str, Any]) -> str:
    """One line for the player and the model. "" without choices."""
    c = sheet.get("choices") or {}
    parts = []
    if "deity" in c:
        parts.append(f"serves {c['deity']}" + (f", god of {c['domain']}" if c.get("domain") else "")
                     + (f" ({c['precept']})" if c.get("precept") else ""))
    if "land" in c:
        parts.append(f"born of {c['land']}" + (f", marked by {c['tell']}" if c.get("tell") else ""))
    if sheet.get("studied_species"):
        parts.append(f"essences studied: {', '.join(sheet['studied_species'])}")
    if "lore" in c:
        parts.append(f"bardic lore: {c['lore']}")
    if "poison" in c:
        parts.append(f"poison: {c['poison']} ({poison_text(c['poison'])}), "
                     f"{int(sheet.get('poison_uses', 0))} uses")
    if "look" in c:
        parts.append(f"signature weapon looks {c['look'].lower()}")
    if "precise_weapon" in c:
        parts.append(f"{c['precise_weapon'].lower()}s are precise in their hands")
    if "start" in c:
        parts.append(f"started with {c['start']}")
    if appetites_of(sheet):
        parts.append("appetites: " + ", ".join(a.lower() for a in appetites_of(sheet)))
    if look_of(sheet):
        parts.append(f"looks: {look_of(sheet)}")
    if sheet.get("portrait"):
        parts.append(f"as pictured: {sheet['portrait']}")
    return ("Choices — " + "; ".join(parts)) if parts else ""


def precise_for(sheet: Dict[str, Any], weapon_name: str) -> bool:
    """Elf Fighter: "you can always treat weapons of that type as if they had the precise
    tag". A weapon is of the type when a word of its name is the type ("Long Sword",
    "Signature sword") or ends in it ("Warhammer")."""
    wanted = str((sheet.get("choices") or {}).get("precise_weapon", "")).lower()
    if not wanted:
        return False
    words = [w.rstrip("s") for w in re.findall(r"[a-z]+", weapon_name.lower())]
    return any(w == wanted or w.endswith(wanted) for w in words)
