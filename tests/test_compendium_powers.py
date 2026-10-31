"""Compendium powers: weapon and deity bonds, dragon blood, Faraday's Law, hurling, the stronghold."""

from __future__ import annotations

import pytest

from gary import bonds, steadings
from gary.fsm import ChangeHp, Director, SendMessage, UpdateSheet

CHANNEL, ALICE = 555, 111
STATS = {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}


def dorn(**more):
    return {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 5, "xp": 0, "hp": 10, "max_hp": 20,
            "stats": STATS, "armor": 0, "damage": "d10", **more}


@pytest.fixture
def sheets():
    return {"Dorn": dorn()}


@pytest.fixture
def scene():
    return {"Orc 1": {"kind": "Orc", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d8"},
            "Orc 2": {"kind": "Orc", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d8"}}


@pytest.fixture
def here():
    return {"steading": None}


@pytest.fixture
def director(registry, clock, dice, sheets, scene, here):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice, players={ALICE: "Dorn"},
                    sheet_for=sheets.get, monsters=lambda: scene, steading=lambda: here["steading"])


def apply(effects, sheets, scene=None):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp):
            (sheets if e.kind == "character" else scene)[e.name]["hp"] = e.hp


def ask(director, **kw):
    out = [e for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", **kw)]
    for c in list(director.registry.open_checks(CHANNEL)):
        director.registry.cancel(c.check_id)
    return out


def test_a_bearer_bonds_with_their_weapon_and_it_feeds_them(director, dice, sheets, scene):
    sheets["Dorn"].update(advanced_moves=["Whosoever Holdeth", "Hunger", "Its Name Is Dread"],
                          bonds=[{"line": "___ will be mine", "with": bonds.WEAPON},
                                 {"line": "___ thirsts", "with": bonds.WEAPON}])
    assert bonds.partners(sheets["Dorn"]) == ["my weapon"] and bonds.weapon_bonds(sheets["Dorn"]) == 2
    dice.queue(5)
    _, result = director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Orc 1", reason="x")
    assert result["rolled"] == 7  # d10 5 + 2 bonds (Hunger)
    (ask_line,) = [e.text for e in ask(director, move="parley", weapon_leverage=True) if isinstance(e, SendMessage)]
    assert "+2 bonds with their weapon, not Cha" in ask_line and "+Cha +2" in ask_line
    dice.queue(4, 4, 4)
    effects, result = director.compendium_power(channel_id=CHANNEL, character="Dorn", power="weapon_boons",
                                                boons=["heal", "insight"])
    apply(effects, sheets)
    assert sheets["Dorn"]["hp"] == 20 and sheets["Dorn"]["xp"] == 1
    _, refused = director.compendium_power(channel_id=CHANNEL, character="Dorn", power="weapon_boons",
                                           boons=["heal", "insight", "peace"])
    assert "2 bond(s) with their weapon" in refused["error"]


def test_divine_bond_marks_off_a_deity_bond(director, sheets):
    sheets["Dorn"].update(advanced_moves=["Divine Bond"], choices={"deity": "Ilmara"},
                          bonds=[{"line": "___ watches me", "with": "Ilmara"}])
    assert bonds.partners(sheets["Dorn"]) == ["Ilmara"]
    effects, result = director.compendium_power(channel_id=CHANNEL, character="Dorn", power="deity_favor")
    apply(effects, sheets)
    assert sheets["Dorn"]["bonds"] == [] and "new bond" in result["note"]


def test_dragon_blood_its_forward_its_risk_and_fury(director, dice, sheets, scene):
    sheets["Dorn"].update(advanced_moves=["Dovahkiin", "Fury", "Wake the Dragon"], blood=2)
    dice.queue(2)  # d4 2 <= Blood 2: gives in
    effects, result = director.compendium_power(channel_id=CHANNEL, character="Dorn", power="dragon_blood")
    apply(effects, sheets)
    assert result["gave_in"] and sheets["Dorn"]["forwards"][0]["amount"] == 2 and sheets["Dorn"]["fury"]
    scene["Orc 1"]["armor"] = 1
    dice.queue(4)
    effects, result = director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Orc 1", reason="x")
    apply(effects, sheets, scene)
    assert result["dealt"] == 4 and sheets["Dorn"]["fury"] is None  # Fury's +1 piercing beat the armor, once
    (line,) = [e.text for e in ask(director, move="parley", dragon_nature=True) if isinstance(e, SendMessage)]
    assert "+2 Blood" in line
    effects, result = director.compendium_power(channel_id=CHANNEL, character="Dorn", power="drink_heart_blood")
    assert result["blood"] == 3


def test_dragons_breath_burns_its_path_and_its_breather(director, dice, sheets, scene):
    sheets["Dorn"].update(advanced_moves=["Dovahkiin", "Dragon’s Breath"], blood=1)
    dice.queue(6, 3)  # d10 6, then the d4 3 > Blood 1
    effects, result = director.compendium_power(channel_id=CHANNEL, character="Dorn", power="dragons_breath",
                                                targets=["orc 1", "Orc 2"])
    apply(effects, sheets, scene)
    assert scene["Orc 1"]["hp"] == 14 and scene["Orc 2"]["hp"] == 14 and sheets["Dorn"]["hp"] == 4
    assert not result["gave_in"]


def test_faraday_charges_the_weapon_and_weighs_on_bind(director, dice, sheets, scene):
    sheets["Dorn"].update(advanced_moves=["Bind", "Faraday’s Law"])
    apply(director.compendium_power(channel_id=CHANNEL, character="Dorn", power="faraday", on=True)[0], sheets)
    dice.queue(5, 2)
    _, result = director.deal_damage(channel_id=CHANNEL, character="Dorn", target="Orc 1", reason="x")
    assert result["rolled"] == 7
    (line,) = [e.text for e in ask(director, move="bind") if isinstance(e, SendMessage)]
    assert "-1 Faraday's Law" in line


def test_velocitas_eradico_hurls_with_con(director, sheets):
    sheets["Dorn"].update(advanced_moves=["Bind", "Velocitas Eradico"], gear=[])
    (line,) = [e.text for e in ask(director, move="volley", target="Orc 1", hurl=True) if isinstance(e, SendMessage)]
    assert "+Con +1" in line
    (note,) = ask(director, move="volley", target="Orc 1")
    assert note.payload["error"].startswith("Dorn has nothing to Volley with")  # without hurl, a bow is needed


def test_the_stronghold_and_calling_the_banners(director, dice, sheets, here):
    keep = steadings.make_stronghold("Harrison's Hold", "Dorn", ["garrisoned", "prestigious"], "disputed")
    assert (keep["kind"], keep["defenses"]) == ("keep", "garrison")
    assert steadings.has(keep, "oath", "protection") and steadings.has(keep, "personage", "Dorn")
    sheets["Dorn"].update(advanced_moves=["Landed", "Call the Banners"], stronghold="harrison_s_hold",
                          stronghold_name="Harrison's Hold", coins=50)
    here["steading"] = keep
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Dorn", move="recruit", offers=[])
    dice.queue(1, 2)
    texts = [e.text for e in director.roll(channel_id=CHANNEL, user_id=ALICE) if isinstance(e, SendMessage)]
    assert "Call the Banners: no miss at their stronghold, a 7–9" in texts[0] and "weak hit" in texts[0]
