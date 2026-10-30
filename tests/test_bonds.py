"""Bonds: the book's lines, filled in with companions, and +Bond."""

from __future__ import annotations

import asyncio

import pytest

from gary import bonds
from gary.class_moves import TEXTS
from gary.sheets import SheetError

THORIN = {"name": "Thorin", "class": "Cleric", "race": "Dwarf"}
INSULTED = "___ has insulted my deity; I do not trust them."
FAITHFUL = "___ is a good and faithful person; I trust them implicitly."


def test_every_class_has_bond_lines_and_alignments():
    for cls, data in TEXTS.items():
        assert data["bonds"] and all(line.count("___") == 1 for line in data["bonds"]), cls
        assert data["alignments"], cls
    assert TEXTS["ranger"]["bonds"]  # its div is id="bond", not "bonds"
    assert TEXTS["cleric"]["alignments"]["Good"] == "Endanger yourself to heal another."


def test_a_bond_is_written_with_its_companion_and_counted():
    sheet = bonds.add(THORIN, INSULTED, "Eva", ["Eva", "Dorn"])
    sheet = bonds.add(sheet, FAITHFUL, "Eva", ["Eva", "Dorn"])
    assert bonds.bond_with(sheet, "eva") == 2 and bonds.bond_with(sheet, "Dorn") == 0
    assert bonds.describe(sheet) == ("Bonds — Eva has insulted my deity; I do not trust them. · "
                                     "Eva is a good and faithful person; I trust them implicitly.")


def test_only_the_classs_lines_once_each_and_only_companions():
    with pytest.raises(SheetError, match="not one of your class's"):
        bonds.add(THORIN, "I stole something from ___.", "Eva", ["Eva"])
    with pytest.raises(SheetError, match="not a companion"):
        bonds.add(THORIN, INSULTED, "Thorin", ["Eva"])
    sheet = bonds.add(THORIN, INSULTED, "Eva", ["Eva", "Dorn"])
    with pytest.raises(SheetError, match="written that bond already"):
        bonds.add(sheet, INSULTED, "Dorn", ["Eva", "Dorn"])
    assert bonds.remove(sheet, 0)["bonds"] == []


def test_the_form_writes_removes_and_saves():
    from gary.adapters.bonds_form import BondsForm

    saved, edits = [], []

    async def finish(sheet):
        saved.append(sheet)
        return None

    class _Resp:
        async def edit_message(self, content=None, view=None):
            edits.append((content, view))

    def click():
        return type("I", (), {"response": _Resp()})()

    async def run():
        form = BondsForm(THORIN, ["Eva", "Dorn"], finish)
        await form._on_line(click(), "0")
        assert "Who fills the blank? *___ has insulted my deity" in edits[-1][0]
        await form._on_companion(click(), "Dorn")
        assert "• Dorn has insulted my deity; I do not trust them." in edits[-1][0]
        assert len(form._unused()) == 3
        await form._on_save(click())

    asyncio.run(run())
    assert saved[0]["bonds"] == [{"line": INSULTED, "with": "Dorn"}]
    assert edits[-1][1] is None


def test_alignment_is_a_creation_choice_from_the_class_list():
    from gary.sheets import Draft, alignment_line

    d = Draft()
    d.choose_class("cleric")
    assert list(d.alignments()) == ["Good", "Lawful", "Evil"]
    with pytest.raises(SheetError, match="Choose one of Good, Lawful, Evil"):
        d.choose_alignment("Chaotic")
    d.choose_alignment("Lawful")
    d.choose_class("thief")
    assert d.alignment is None  # a new class, new alignments
    assert alignment_line({"class": "Cleric", "alignment": "Good"}) == \
        "Alignment — Good: Endanger yourself to heal another."
    assert alignment_line({"class": "Cleric"}) == ""


def test_the_model_sees_the_alignment():
    from gary.class_moves import render

    block = render({"Thorin": {**THORIN, "alignment": "Good"}})
    assert "    - Alignment — Good: Endanger yourself to heal another." in block


def test_choosing_an_alignment_later_saves_only_that():
    from gary.adapters.discord_bot import GaryClient

    sheets = {"Thorin": {**THORIN, "hp": 4}}
    sent = []

    class _Store:
        def state(self):
            return {"sheets": sheets}

        def apply(self, mutations, reason=""):
            for m in mutations:
                sheets[m.target] = m.payload["sheet"]
            return type("Diff", (), {"ok": True, "rejected": ()})()

    class _Sender:
        async def send(self, channel_id, text):
            sent.append(text)

    class _Client:
        store, sender = _Store(), _Sender()

    finish = GaryClient._finish_alignment.__get__(_Client(), _Client)
    assert asyncio.run(finish("Thorin", "Evil", 1)) is None
    assert sheets["Thorin"]["alignment"] == "Evil" and sheets["Thorin"]["hp"] == 4
    assert sent == ["⚖️ **Thorin** — Alignment — Evil: Harm another to prove the superiority of "
                    "your church or god."]
