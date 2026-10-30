"""The Druid's Shapeshifter: a form, its hold, and the moves it pays for."""

from __future__ import annotations

import pytest

from gary.fsm import Director, PromptModel, SendMessage, UpdateSheet
from gary.sheets import describe

CHANNEL, ALICE = 555, 111
# WIS 16 (+2)
WREN = {"name": "Wren", "class": "Druid", "race": "Elf", "level": 1, "xp": 0,
        "stats": {"str": 8, "dex": 13, "con": 12, "int": 9, "wis": 16, "cha": 15},
        "hp": 18, "max_hp": 18, "armor": 0, "damage": "d6"}


@pytest.fixture
def sheets():
    return {"Wren": dict(WREN)}


@pytest.fixture
def director(registry, clock, dice, sheets):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Wren"}, sheet_for=sheets.get)


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)


def shift(director, dice, faces, form="bear"):
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Wren", move="shapeshifter", target=form)
    dice.queue(*faces)
    return director.roll(channel_id=CHANNEL, user_id=ALICE)


@pytest.mark.parametrize("faces,hold", [((5, 5), 3), ((3, 2), 2), ((1, 1), 1)])
def test_shifting_holds_3_2_or_1_even_on_a_miss(director, dice, sheets, faces, hold):
    effects = shift(director, dice, faces)
    assert UpdateSheet(CHANNEL, "Wren", {"shape": {"form": "bear", "hold": hold, "moves": []}}) in effects
    payload = [e.payload for e in effects if isinstance(e, PromptModel)][0]
    assert payload["shape"]["hold"] == hold and "form_moves" in payload["shape"]["instruction"]


def test_the_roll_names_the_form(director):
    (ask,) = [e.text for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Wren",
                                                  move="shapeshifter", target="hawk")
              if isinstance(e, SendMessage)]
    assert ask == "**Wren** — Shapeshifter into a hawk, roll +Wis +2."


def test_no_form_goes_back_to_the_model(director):
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Wren", move="shapeshifter")
    assert "needs the form to take" in note.payload["error"]


def test_spending_hold_on_the_forms_moves_until_they_turn_back(director, dice, sheets):
    apply(shift(director, dice, (3, 2)), sheets)  # hold 2
    effects, result = director.form_moves(channel_id=CHANNEL, character="wren", moves=["Maul", "Roar"])
    apply(effects, sheets)
    assert result["moves"] == ["Maul", "Roar"]
    assert "in the shape of a bear (hold 2: Maul, Roar)" in describe(sheets["Wren"])
    _, result = director.spend_hold(channel_id=CHANNEL, character="Wren", move="fly")
    assert "not one of the bear's moves: Maul; Roar" in result["error"]
    effects, result = director.spend_hold(channel_id=CHANNEL, character="Wren", move="maul")
    apply(effects, sheets)
    assert result == {"ok": True, "move": "Maul", "hold": 1}
    effects, result = director.spend_hold(channel_id=CHANNEL, character="Wren", move="Roar")
    apply(effects, sheets)
    assert result["reverted"] and sheets["Wren"]["shape"] is None


def test_turning_back_early(director, dice, sheets):
    apply(shift(director, dice, (5, 5)), sheets)
    effects, result = director.spend_hold(channel_id=CHANNEL, character="Wren", revert=True)
    assert UpdateSheet(CHANNEL, "Wren", {"shape": None}) in effects
    _, result = director.form_moves(channel_id=CHANNEL, character="Wren", moves=["x"])
    assert result["ok"] is True  # still shaped until the effect is applied
    apply(effects, sheets)
    _, result = director.spend_hold(channel_id=CHANNEL, character="Wren", move="x")
    assert "in their natural form" in result["error"]


def test_only_a_druid_shapeshifts(director, sheets):
    sheets["Wren"]["class"] = "Bard"
    (note,) = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Wren", move="shapeshifter",
                                 target="bear")
    assert "Shapeshifter is the Druid's move" in note.payload["error"]
