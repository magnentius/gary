"""Class starting moves: the book's text, and which Gary rolls."""

from __future__ import annotations

from gary.class_moves import NARRATIVE, NOT_YET, ROLLED, TEXTS, _KINDS, moves_of, render
from gary.moves import MOVES


def test_every_class_and_race_move_is_extracted():
    assert sum(len(c["starting"]) for c in TEXTS.values()) == 35
    assert sum(len(c["race"]) for c in TEXTS.values()) == 18
    assert "Backstab" in [m["name"] for m in TEXTS["thief"]["starting"]]
    assert "Elf" not in [m["name"] for m in TEXTS["bard"]["starting"]]  # race moves apart


def test_every_mapped_move_exists_and_belongs_to_its_class():
    for cls, mapping in _KINDS.items():
        names = [m["name"] for m in TEXTS[cls]["starting"]]
        for name, key in mapping.items():
            assert name in names, (cls, name)
            if key != NOT_YET:
                assert MOVES[key].classes == (cls.title(),), key


def test_a_dwarf_cleric_has_their_race_move_then_their_class_moves():
    moves = moves_of("Cleric", "Dwarf")
    assert moves[0]["name"] == "Dwarf (Cleric)" and "one with stone" in moves[0]["text"]
    kinds = {m["name"]: m["kind"] for m in moves}
    assert kinds["Turn Undead"] == ROLLED
    assert kinds["Divine Guidance"] == NARRATIVE
    assert kinds["Cast a Spell"] == NOT_YET


def test_the_model_sees_how_to_roll_what_is_rolled_and_the_text_of_the_rest():
    block = render({"Thorin": {"class": "Cleric", "race": "Dwarf"}})
    assert "- Thorin (Dwarf Cleric):" in block
    assert "- Turn Undead: call make_move with move=turn_undead." in block
    assert "- Divine Guidance: When you petition your deity" in block
    assert "- Cast a Spell: not yet playable" in block


def test_the_dead_and_the_classless_are_left_out():
    assert render({"Old": {"name": "Old"}, "Gone": {"class": "Thief", "race": "Human", "dead": True}}) == ""
