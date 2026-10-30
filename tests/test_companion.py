"""The Ranger's animal companion: making it, and Command."""

from __future__ import annotations

import asyncio

import pytest

from gary import companion
from gary.class_moves import render
from gary.companion import CompanionDraft
from gary.fsm import ChangeHp, Director, PromptModel, SendMessage
from gary.sheets import SheetError

CHANNEL, ALICE = 555, 111

FANG = {"name": "Fang", "species": "Wolf", "ferocity": 2, "cunning": 1, "armor": 1, "instinct": 1,
        "strengths": ["Fast", "Ferocious"], "trainings": ["Fight humanoids", "Hunt"],
        "weaknesses": ["Savage"]}
# DEX 16 (+2), WIS 15 (+1)
RHEA = {"name": "Rhea", "class": "Ranger", "race": "Elf", "level": 1, "xp": 0,
        "stats": {"str": 8, "dex": 16, "con": 13, "int": 12, "wis": 15, "cha": 9},
        "hp": 20, "max_hp": 20, "armor": 1, "damage": "d8", "companion": dict(FANG)}


@pytest.fixture
def sheets():
    return {"Rhea": dict(RHEA)}


@pytest.fixture
def scene():
    return {"Goblin 1": {"kind": "Goblin", "hp": 7, "max_hp": 7, "armor": 1, "damage": "d6"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Rhea"}, sheet_for=sheets.get, monsters=lambda: scene)


def move(director, name, **kw):
    return director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Rhea", move=name, **kw)


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def only_check(registry):
    (check,) = list(registry.open_checks(CHANNEL))
    return check


def test_making_a_companion_takes_as_many_as_its_numbers_say():
    d = CompanionDraft()
    d.choose_species("Wolf")
    d.choose_base(3)  # Ferocity +3, Cunning +1, 1 Armor, Instinct +2
    assert d.step() == "strengths" and d.counts() == 3
    with pytest.raises(SheetError, match="Choose 3"):
        d.choose(["Fast"])
    d.choose(["Fast", "Huge", "Calm"])
    assert d.counts() == 1
    d.choose(["Guard"])
    d.choose(["Savage", "Slow"])
    d.choose_name("  Ash  ")
    pet = d.build()
    assert pet["trainings"] == ["Fight humanoids", "Guard"]  # trained to fight humanoids anyway
    assert (pet["name"], pet["ferocity"], pet["instinct"]) == ("Ash", 3, 2)


def test_command_needs_the_right_training():
    rule, why = companion.command(RHEA, "hunt_and_track")
    assert rule.roll == "cunning" and not why
    rule, why = companion.command(RHEA, "parley")
    assert rule is None and "needs perform" in why
    rule, why = companion.command(RHEA, "defy_danger")
    assert rule is None and "adds nothing to that move" in why
    assert companion.command({"name": "Rhea"}, "volley")[0] is None


def test_working_together_on_tracking_adds_cunning(director, registry):
    (ask,) = texts(move(director, "hunt_and_track", companion=True))
    assert ask == "**Rhea** — Hunt and Track, with Fang, roll +Wis +2 (+1 Fang)."
    assert only_check(registry).companion == "Fang"


def test_an_untrained_companion_is_refused(director):
    (note,) = [e for e in move(director, "parley", companion=True) if isinstance(e, PromptModel)]
    assert "Fang is not trained for that" in note.payload["error"]


def test_attacking_together_adds_ferocity_to_the_damage(director, registry, dice):
    move(director, "volley", target="Goblin 1", companion=True)
    dice.queue(5, 5)  # +2: 12, strong
    assert "(d8 +2 Fang)" in texts(director.roll(channel_id=CHANNEL, user_id=ALICE))[-1]
    dice.queue(3)
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id)
    assert "d8: 3, +2 Fang = 5" in texts(effects)[0]
    assert ChangeHp(CHANNEL, "monster", "Goblin 1", 3) in effects  # 5 - armor 1


def test_the_blow_back_meets_the_companions_armor_too(director, registry, dice, sheets):
    sheets["Rhea"]["stats"] = {**RHEA["stats"], "str": 13}  # +1
    move(director, "hack_and_slash", target="Goblin 1", companion=True)
    dice.queue(3, 4)  # 7 +1 = 8, weak: deal damage, take the attack
    director.roll(channel_id=CHANNEL, user_id=ALICE)
    dice.queue(2, 4)  # Rhea's d8: 2; the goblin's d6: 4
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id)
    assert "hits **Rhea** for **2 damage** (d6: 4, armor 2)" in " ".join(texts(effects))


def test_the_model_sees_the_companion_and_how_to_command_it():
    block = render({"Rhea": RHEA})
    assert "- Companion — Fang the wolf: Ferocity +2, Cunning +1, 1 Armor, Instinct +1" in block
    assert "set companion=true on make_move" in block
    assert "not chosen yet" in render({"Rhea": {**RHEA, "companion": None}})


def test_the_form_walks_to_a_companion_and_saves_it():
    from gary.adapters.companion_form import CompanionForm

    saved, edits = [], []

    async def finish(pet):
        saved.append(pet)

    class _Resp:
        async def edit_message(self, content=None, view=None):
            edits.append((content, view))

    def click():
        return type("I", (), {"response": _Resp()})()

    async def run():
        form = CompanionForm("Rhea", finish)
        for step in (lambda: form.draft.choose_species("Owl"), lambda: form.draft.choose_base(0),
                     lambda: form.draft.choose(["Calm", "Stealthy"]), lambda: form.draft.choose(["Scout"]),
                     lambda: form.draft.choose(["Flighty"]), lambda: form.draft.choose_name("Hoot")):
            await form.apply(click(), step)
        assert "Take this companion?" in edits[-1][0]
        await form._on_done(click())

    asyncio.run(run())
    assert saved[0]["name"] == "Hoot" and saved[0]["trainings"] == ["Fight humanoids", "Scout"]
    assert edits[-1][1] is None
