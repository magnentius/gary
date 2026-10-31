"""+1 forward: granted by moves and by the GM, held with what it is for, spent when the GM
says a move acts on it, gone when the scene ends."""

from __future__ import annotations

import pytest

from gary.fsm import ChangeHp, Director, SendMessage, UpdateSheet
from gary.sheets import describe

CHANNEL, ALICE, BOB = 555, 111, 222


def sheet(name, cls="Fighter", **kw):
    return {"name": name, "class": cls, "race": "Human", "level": 1, "xp": 0, "hp": 20, "max_hp": 20,
            "stats": {"str": 12, "dex": 12, "con": 12, "int": 12, "wis": 12, "cha": 12},
            "armor": 0, "damage": "d8", **kw}


@pytest.fixture
def sheets():
    return {"Dorn": sheet("Dorn"), "Nim": sheet("Nim", cls="Thief")}


@pytest.fixture
def scene():
    return {"Ogre 1": {"kind": "Ogre", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d10"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn", BOB: "Nim"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp) and e.kind == "character":
            sheets[e.name]["hp"] = e.hp


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def only_check(registry):
    (check,) = list(registry.open_checks(CHANNEL))
    return check


def test_discern_realities_grants_one_and_the_sheet_shows_it(director, dice, sheets, registry):
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="discern_realities")
    dice.queue(4, 4)  # 8, weak: one question
    director.roll(channel_id=CHANNEL, user_id=ALICE)
    effects = director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id, picks=[0])
    apply(effects, sheets)
    assert sheets["Dorn"]["forwards"] == [{"amount": 1, "reason": "when acting on what Discern Realities showed"}]
    assert "+1 forward (when acting on what Discern Realities showed)" in describe(sheets["Dorn"])


def test_it_applies_only_when_the_move_acts_on_it(director, sheets, registry):
    sheets["Dorn"]["forwards"] = [{"amount": 1, "reason": "against the ogre"}]
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="spout_lore"))
    assert "forward" not in ask  # not what it was for: held
    registry.consume(only_check(registry).check_id)
    effects = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="hack_and_slash",
                                 target="Ogre 1", forward=True)
    assert texts(effects)[-1] == "**Dorn** — Hack and Slash at Ogre 1, roll +Str +1 (+1 forward)."
    assert UpdateSheet(CHANNEL, "Dorn", {"forwards": []}) in effects


def test_the_gm_can_give_or_take_one(director, sheets):
    effects, result = director.give_forward(channel_id=CHANNEL, character="nim", amount=-1,
                                            reason="off balance on the ledge")
    assert result == {"ok": True, "forward": -1}
    assert "➕ **Nim** takes -1 forward — off balance on the ledge." in texts(effects)
    assert "+1 or -1" in director.give_forward(channel_id=CHANNEL, character="Nim", amount=5, reason="x")[1]["error"]
    assert "reason" in director.give_forward(channel_id=CHANNEL, character="Nim", amount=1)[1]["error"]


def test_backstabs_advantage_and_i_am_the_law(director, dice, sheets, registry):
    director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Nim", move="backstab", target="Ogre 1")
    dice.queue(4, 4)
    director.roll(channel_id=CHANNEL, user_id=BOB)
    effects = director.choose(channel_id=CHANNEL, user_id=BOB, check_id=only_check(registry).check_id, picks=[2])
    assert "➕ **Nim** takes +1 forward — acting on the advantage Backstab made." in texts(effects)

    sheets["Dorn"]["class"] = "Paladin"
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="i_am_the_law")
    dice.queue(1, 2)
    assert "➕ **Dorn** takes -1 forward — against the one who defied their order." in \
        texts(director.roll(channel_id=CHANNEL, user_id=ALICE))


def test_a_damage_forward_lands_on_the_next_blow(director, dice, sheets, registry):
    sheets["Dorn"]["forwards"] = [{"damage": "1d4", "reason": "Arcane Art"}]
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="hack_and_slash", target="Ogre 1")
    dice.queue(5, 5)
    director.roll(channel_id=CHANNEL, user_id=ALICE)
    director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id, picks=[0])
    dice.queue(6, 3)
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id)
    assert "d8: 6, +1d4 forward: 3 = 9" in texts(effects)[0]
    assert UpdateSheet(CHANNEL, "Dorn", {"forwards": []}) in effects


def test_the_scene_ending_clears_them(director, sheets, scene):
    scene.clear()
    sheets["Dorn"]["forwards"] = [{"amount": 1, "reason": "against the ogre"}]
    effects, _ = director.make_camp(channel_id=CHANNEL, reason="a cave")
    assert any(isinstance(e, UpdateSheet) and e.name == "Dorn" and e.changes.get("forwards") == []
               for e in effects)
