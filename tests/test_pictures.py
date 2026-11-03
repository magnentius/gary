"""/picture: only what the table knows, in one style, kept, and capped."""

from __future__ import annotations

import asyncio

import pytest

from gary import pictures
from gary.adapters.pictures import Painter, PictureError

STATE = {
    "setting": "A one-shot: steal the bell -- and it ends at the Glass Spire, the final destination.",
    "players": {"1": "Gorm"},
    "sheets": {"Gorm": {"name": "Gorm", "race": "Human", "class": "Barbarian",
                        "choices": {"look_0": "Wild eyes", "look_1": "Mighty thews"}},
               "Kael": {"name": "Kael", "race": "Dwarf", "class": "Fighter", "retired": True}},
    "npcs": {"mara": {"name": "Mara", "known": True, "public": "a smuggler with a silver tooth"},
             "vex": {"name": "Lady Vex", "known": False}},
    "monsters": {"Dragon 1": {"kind": "Dragon", "hp": 16}, "Goblin 1": {"kind": "Goblin", "hp": 0}},
    "scene": {"location": "The salt flats", "summary": "White crust to the horizon."},
}


def test_only_what_the_table_knows_can_be_pictured():
    found = pictures.subjects(STATE)
    assert found == [("character", "Gorm"), ("npc", "Mara"), ("creature", "Dragon"), ("place", "The salt flats")]
    assert pictures.find(STATE, "lady vex") is None and pictures.find(STATE, "goblin") is None
    assert pictures.find(STATE, "mar") == ("npc", "Mara")


def test_prompts_come_from_the_record_and_carry_the_style():
    gorm = pictures.prompt(STATE, "character", "Gorm")
    assert "Human Barbarian: Wild eyes, Mighty thews" in gorm and pictures.STYLE in gorm
    assert "art style ONLY" in gorm and "not the person" in gorm and "No text" in gorm
    assert "Glass Spire" not in gorm  # the premise drew the adventure's MacGuffin into everything
    assert "a smuggler with a silver tooth" in pictures.prompt(STATE, "npc", "Mara")
    dragon = pictures.prompt(STATE, "creature", "Dragon")
    assert "Huge" in dragon and "Wings" in dragon and "bite" in dragon
    assert "White crust to the horizon" in pictures.prompt(STATE, "place", "The salt flats")


class _Models:
    def __init__(self, image=b"JPEG"):
        self.calls, self.image = [], image

    async def generate_content(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        part = type("P", (), {"inline_data": type("D", (), {"data": self.image})() if self.image else None})()
        return type("R", (), {"candidates": [type("C", (), {"content": type("X", (), {"parts": [part]})()})()]})()


def _painter(tmp_path, per_day=2, image=b"JPEG"):
    models = _Models(image)
    client = type("Cl", (), {"aio": type("A", (), {"models": models})()})()
    return Painter("", "gemini-3.1-flash-image", tmp_path / "pictures", per_day, client=client), models


def test_the_first_picture_is_kept_and_new_ones_are_capped(tmp_path):
    painter, models = _painter(tmp_path)
    path, new = asyncio.run(painter.picture(STATE, "character", "Gorm"))
    assert new and path.read_bytes() == b"JPEG"
    model, contents, config = models.calls[0]
    assert model == "gemini-3.1-flash-image" and contents[0].inline_data.mime_type == "image/jpeg"  # the style
    assert config.image_config.aspect_ratio == "3:4" and config.image_config.image_size == "1K"
    again, new = asyncio.run(painter.picture(STATE, "character", "Gorm"))
    assert again == path and not new and len(models.calls) == 1  # shown again, not paid again
    asyncio.run(painter.picture(STATE, "character", "Gorm", redraw=True))
    with pytest.raises(PictureError, match="today's 2 new pictures"):
        asyncio.run(painter.picture(STATE, "place", "The salt flats"))


def test_a_refusal_is_said_plainly(tmp_path):
    painter, _ = _painter(tmp_path, image=None)
    with pytest.raises(PictureError, match="declined to draw that"):
        asyncio.run(painter.picture(STATE, "creature", "Dragon"))
    assert not painter.path_for("creature", "Dragon").exists()


def test_off_without_a_key_or_a_budget(tmp_path):
    assert not Painter("", "gemini-3.1-flash-image", tmp_path, 30).enabled
    assert not Painter("key", "gemini-3.1-flash-image", tmp_path, 0).enabled
