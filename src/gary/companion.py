"""The Ranger's Animal Companion and Command (Dungeon World, the Ranger).

On the sheet as ``companion``: name, species, the four numbers (ferocity, cunning, armor,
instinct), and its strengths, trainings and weaknesses. Made step by step in a
``CompanionDraft``, checked at every step like a sheets.Draft.

Command -- "when you work with your animal companion on something it's trained in" -- is
the model's to notice (it flags the move) and Gary's to check and apply:

- an attack, with fight humanoids or fight monsters: +ferocity to the damage, and its armor
  on top of the Ranger's against the blow back;
- tracking (Hunt and Track), with hunt or search: +cunning to the roll;
- Discern Realities, with search, scout or guard: +cunning;
- Parley, with perform: +cunning.

"Someone interferes with you, add its instinct to their roll" is left to the table: who is
helped or hindered is chosen after the roll, too late to change it.

The lists are the book's, hand-copied: there are few, and the XML sets them as prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .sheets import MAX_NAME_CHARS, SheetError

SPECIES = ("Wolf", "Cougar", "Bear", "Eagle", "Dog", "Hawk", "Cat", "Owl", "Pigeon", "Rat", "Mule")

#: (ferocity, cunning, armor, instinct)
BASES: Tuple[Tuple[int, int, int, int], ...] = ((2, 1, 1, 1), (2, 2, 0, 1), (1, 2, 1, 1), (3, 1, 1, 2))

STRENGTHS = ("Fast", "Burly", "Huge", "Calm", "Adaptable", "Quick reflexes", "Tireless",
             "Camouflage", "Ferocious", "Intimidating", "Keen senses", "Stealthy")
#: "Your animal companion is trained to fight humanoids." The rest are chosen.
INNATE = "Fight humanoids"
TRAININGS = ("Hunt", "Search", "Scout", "Guard", "Fight monsters", "Perform", "Labor", "Travel")
WEAKNESSES = ("Flighty", "Savage", "Slow", "Broken", "Frightening", "Forgetful", "Stubborn", "Lame")


@dataclass(frozen=True)
class Command:
    """What working together on a move needs, and gives."""

    trainings: Tuple[str, ...]  # any one of these will do
    roll: str = ""  # a number added to the roll: "cunning"
    damage: str = ""  # a number added to the damage: "ferocity"
    armor: bool = False  # its armor on top of the Ranger's against the blow back


_ATTACK = Command((INNATE, "Fight monsters"), damage="ferocity", armor=True)

COMMANDS: Dict[str, Command] = {
    "hack_and_slash": _ATTACK,
    "volley": _ATTACK,
    "called_shot": _ATTACK,
    "hunt_and_track": Command(("Hunt", "Search"), roll="cunning"),
    "discern_realities": Command(("Search", "Scout", "Guard"), roll="cunning"),
    "parley": Command(("Perform",), roll="cunning"),
}


def base_label(base: Sequence[int]) -> str:
    f, c, a, i = base
    return f"Ferocity +{f}, Cunning +{c}, {a} Armor, Instinct +{i}"


def command(sheet: Dict[str, Any], move: str) -> Tuple[Optional[Command], str]:
    """(what working with the companion on this move gives, or None; why not)."""
    pet = sheet.get("companion")
    if not pet:
        return None, f"{sheet.get('name', 'They')} has no animal companion."
    rule = COMMANDS.get(move)
    if rule is None:
        return None, (f"{pet['name']} adds nothing to that move. Command covers attacks, "
                      "tracking, Discern Realities and Parley.")
    if not set(rule.trainings) & set(pet.get("trainings") or ()):
        return None, (f"{pet['name']} is not trained for that: it needs "
                      f"{' or '.join(t.lower() for t in rule.trainings)}. It is trained to "
                      f"{', '.join(t.lower() for t in pet.get('trainings') or ())}.")
    return rule, ""


def describe(sheet: Dict[str, Any]) -> str:
    """One line for the player and the model. "" without a companion."""
    pet = sheet.get("companion")
    if not pet:
        return ""
    return (f"Companion — {pet['name']} the {pet['species'].lower()}: "
            f"{base_label((pet['ferocity'], pet['cunning'], pet['armor'], pet['instinct']))} · "
            f"{', '.join(pet['strengths'])} · trained to {', '.join(t.lower() for t in pet['trainings'])}"
            f" · {', '.join(pet['weaknesses']).lower()}")


class CompanionDraft:
    def __init__(self) -> None:
        self.species: Optional[str] = None
        self.base: Optional[Tuple[int, int, int, int]] = None
        self.strengths: Optional[List[str]] = None
        self.trainings: Optional[List[str]] = None
        self.weaknesses: Optional[List[str]] = None
        self.name: Optional[str] = None

    def step(self) -> Optional[str]:
        for field in ("species", "base", "strengths", "trainings", "weaknesses", "name"):
            if getattr(self, field) is None:
                return field
        return None

    def counts(self) -> int:
        """How many to choose at this step: "as many strengths as its ferocity"..."""
        step = self.step()
        if step == "strengths":
            return self.base[0]
        if step == "trainings":
            return self.base[1]
        if step == "weaknesses":
            return self.base[3]
        return 1

    def options(self) -> Sequence[str]:
        return {"species": SPECIES, "strengths": STRENGTHS, "trainings": TRAININGS,
                "weaknesses": WEAKNESSES}.get(self.step(), ())

    def choose_species(self, species: str) -> None:
        if species not in SPECIES:
            raise SheetError(f"Choose one of {', '.join(SPECIES)}.")
        self.species = species

    def choose_base(self, index: int) -> None:
        if not 0 <= index < len(BASES):
            raise SheetError("That is not one of the bases.")
        self.base = BASES[index]

    def choose(self, picks: Sequence[str]) -> None:
        step = self.step()
        picks = list(picks)
        if len(picks) != self.counts() or len(set(picks)) != len(picks):
            raise SheetError(f"Choose {self.counts()}.")
        if not all(p in self.options() for p in picks):
            raise SheetError("That is not one of the choices.")
        setattr(self, step, picks)

    def choose_name(self, name: str) -> None:
        name = " ".join(str(name or "").split())
        if not name:
            raise SheetError("Your companion needs a name.")
        if len(name) > MAX_NAME_CHARS:
            raise SheetError(f"Keep the name to {MAX_NAME_CHARS} characters.")
        self.name = name

    def build(self) -> Dict[str, Any]:
        if self.step() is not None:
            raise SheetError("Still choosing.")
        f, c, a, i = self.base
        return {"name": self.name, "species": self.species, "ferocity": f, "cunning": c,
                "armor": a, "instinct": i, "strengths": self.strengths,
                "trainings": [INNATE, *self.trainings], "weaknesses": self.weaknesses}
