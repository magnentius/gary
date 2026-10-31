"""Damage from no creature: falls, traps, fire -- the GM says how bad, Gary rolls it."""

from __future__ import annotations

import pytest

from gary.fsm import ChangeHp, Director, ReactPrompt, SendMessage, UpdateSheet

CHANNEL, ALICE, BOB = 555, 111, 222
STATS = {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}


def sheet(name, **more):
    return {"name": name, "class": "Fighter", "race": "Dwarf", "level": 2, "xp": 0, "hp": 10, "max_hp": 20,
            "stats": STATS, "armor": 2, "damage": "d10", **more}


@pytest.fixture
def sheets():
    return {"Dorn": sheet("Dorn"), "Thorin": sheet("Thorin", armor=0)}


@pytest.fixture
def director(registry, clock, dice, sheets):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn", BOB: "Thorin"}, sheet_for=sheets.get, monsters=lambda: {})


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def test_a_fall_is_rolled_for_everyone_through_their_armor(director, dice):
    dice.queue(5, 5)  # d8 each
    effects, result = director.hazard_damage(channel_id=CHANNEL, characters=["dorn", "Thorin"],
                                             severity="bones", reason="the rope bridge snaps")
    assert result["die"] == "d8"
    assert ("💥 **Dorn** is hurt — the rope bridge snaps — **3 damage** (d8: 5, armor 2) — HP 10 → 7."
            in texts(effects))
    assert ChangeHp(CHANNEL, "character", "Thorin", 5) in effects  # no armor: all 5


def test_magic_or_poison_ignores_armor_and_cover_helps(director, dice):
    dice.queue(4)
    effects, _ = director.hazard_damage(channel_id=CHANNEL, characters=["Dorn"], severity="blood",
                                        reason="a gout of green fire", ignores_armor=True)
    assert ChangeHp(CHANNEL, "character", "Dorn", 6) in effects
    dice.queue(4)
    effects, _ = director.hazard_damage(channel_id=CHANNEL, characters=["Thorin"], severity="blood",
                                        reason="falling masonry", cover=2)
    assert ChangeHp(CHANNEL, "character", "Thorin", 8) in effects  # 4 - 2 cover


def test_a_lethal_hazard_can_bring_last_breath(director, dice, registry, sheets):
    sheets["Thorin"]["hp"] = 3
    dice.queue(9)
    director.hazard_damage(channel_id=CHANNEL, characters=["Thorin"], severity="lethal",
                           reason="the portcullis drops")
    assert [c.move for c in registry.open_checks(CHANNEL)] == ["last_breath"]


def test_a_hazard_can_be_answered_like_any_blow(director, dice, sheets):
    sheets["Dorn"].update(advanced_moves=["Bloody Aegis"])
    dice.queue(6)
    effects, _ = director.hazard_damage(channel_id=CHANNEL, characters=["Dorn"], severity="bones",
                                        reason="a spiked pit")
    (prompt,) = [e.prompt for e in effects if isinstance(e, SendMessage) and isinstance(e.prompt, ReactPrompt)]
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp):
            sheets[e.name]["hp"] = e.hp
    found, _ = director.reactions(user_id=ALICE, hit_id=prompt.hit_id)
    assert "aegis:weak" in [r.key for r in found]


def test_it_is_refused_without_a_severity_or_a_reason(director):
    _, result = director.hazard_damage(channel_id=CHANNEL, characters=["Dorn"], severity="ouch", reason="x")
    assert "severity is one of: bruises, blood, bones, lethal" in result["error"]
    _, result = director.hazard_damage(channel_id=CHANNEL, characters=["Dorn"], severity="blood", reason="")
    assert "Say what hurt them" in result["error"]
