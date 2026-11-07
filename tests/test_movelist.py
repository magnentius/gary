"""/moves: a character's own moves in the book's words, and everyone's."""

from __future__ import annotations

from gary import movelist

THIEF = {"name": "Nim", "class": "Thief", "race": "Halfling", "level": 6,
         "advanced_moves": ["Cheap Shot"], "multiclass_moves": [{"class": "fighter", "name": "Merciless"}]}


def test_a_characters_own_moves_come_with_their_text():
    names = [n for n, _ in movelist.own(THIEF)]
    assert names[0] == "Halfling (Thief)" and "Backstab" in names and "Cheap Shot" in names
    assert "Merciless (from the Fighter)" in names
    backstab = dict(movelist.own(THIEF))["Backstab"]
    assert backstab.startswith("When you attack a surprised or defenseless enemy")


def test_a_barbarian_sees_only_the_starting_move_they_chose():
    gorm = {"name": "Gorm", "class": "Barbarian", "race": "Human", "level": 1,
            "choices": {"start": "Full Plate and Packing Steel"}}
    names = [n for n, _ in movelist.own(gorm)]
    assert "Full Plate and Packing Steel" in names and "Unencumbered, Unharmed" not in names


def test_everyones_moves_are_listed_with_when_they_come_up():
    lines = dict(movelist.basic())
    assert lines["Hack and Slash"] == "when you attack an enemy in melee (+Str)"
    assert lines["Defy Danger"].endswith("(+the stat your approach uses)")
    assert "Make Camp" in lines and "Recover" in lines and lines["End of Session"].endswith("/end")


def test_one_move_in_full_mine_or_everyones():
    assert movelist.find(THIEF, "backstab")[0] == "Backstab"
    name, text = movelist.find(THIEF, "hack and slash")
    assert name == "Hack and Slash" and text.startswith("When you attack an enemy in melee, roll +Str. 10+:")
    assert movelist.find(THIEF, "fly") is None


def test_the_list_fits_discords_messages():
    pages = movelist.pages(THIEF)
    assert pages[0].startswith("**Nim's moves**") and all(len(p) <= 2000 for p in pages)
    assert any("**Everyone's moves**" in p for p in pages)


def test_every_starting_and_race_move_has_a_written_line_saying_what_it_does():
    from gary.class_moves import TEXTS

    for cls, book in TEXTS.items():
        for move in book["starting"]:
            assert f"{cls}|{move['name']}" in movelist.WRITTEN, (cls, move["name"])
        for race in book["race"]:
            assert f"{cls}|race:{race}" in movelist.WRITTEN, (cls, race)


def test_moves_is_one_message_each_move_in_a_line_with_what_it_does():
    sheet = {**THIEF, "choices": {**(THIEF.get("choices") or {}), "poison": "Oil of Tagit"}, "poison_uses": 2}
    (page,) = movelist.pages(sheet)
    assert "**Backstab** — attack a surprised or defenceless enemy in melee (+Dex) → choose 2" in page
    assert "Oil of Tagit (applied: the target falls into a light sleep) is safe in your hands → 2 uses left" in page
    assert "**Everyone's moves** — Hack and Slash, Volley" in page
