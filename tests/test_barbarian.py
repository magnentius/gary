"""The Barbarian (a Dungeon World playbook, CC BY 3.0): made, kitted, and played by its rules."""

from __future__ import annotations

import pytest

from gary import advanced, levelling, reactions
from gary.choices import Choices, appetites_of, describe
from gary.dicestats import summary
from gary.fsm import APPETITE_DICE, ChangeHp, Director, PromptModel, SendMessage, UpdateSheet
from gary.sheets import Draft, clumsy

CHANNEL, ALICE = 555, 111
STATS = {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13}


def barbarian(moves=(), start="Unencumbered, Unharmed", **more):
    return {"name": "Gorm", "class": "Barbarian", "race": "Human", "level": 6, "xp": 0, "hp": 23, "max_hp": 23,
            "stats": STATS, "armor": 0, "damage": "d10", "advanced_moves": list(moves), "gear": [],
            "choices": {"start": start, "appetite": "Conquest", "appetite_2": "Fame and glory"}, **more}


@pytest.fixture
def sheets():
    return {"Gorm": barbarian()}


@pytest.fixture
def scene():
    return {"Orc 1": {"kind": "Orc", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d6"},
            "Orc 2": {"kind": "Orc", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d6"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Gorm"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp) and e.kind == "character":
            sheets[e.name]["hp"] = e.hp


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def notes(effects):
    return [n for e in effects if isinstance(e, PromptModel) for n in e.payload.get("also", ())]


def move(director, dice, sheets, name, faces, **kw):
    opened = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Gorm", move=name, **kw)
    dice.queue(*faces)
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    apply(effects, sheets)
    return opened, effects


# -- made ------------------------------------------------------------------------------------


def test_a_barbarian_is_made_with_the_playbooks_hp_load_and_gear():
    d = Draft()
    d.choose_class("barbarian")
    d.choose_race("Elf")
    for stat in ("str", "con", "dex", "wis", "cha"):
        d.assign(stat, d.next_score())
    d.choose_gear(0, [1])  # two-handed sword
    d.choose_gear(1, [1])  # chainmail
    d.choose_alignment("Chaotic")
    for value in ("Full Plate and Packing Steel", "Riches and property", "Mortal pleasures",
                  "Wild eyes", "Mighty thews", "Strange tattoos", "Scraps", "a broken tusk on a cord"):
        d.choose_class_choice(value)
    d.choose_name("Gorm")
    sheet = d.build()
    assert sheet["max_hp"] == 8 + 15 and sheet["damage"] == "d10"
    names = [g["name"] for g in sheet["gear"]]
    assert {"Dungeon rations", "Dagger", "Two-handed sword", "Chainmail"} <= set(names)
    assert appetites_of(sheet) == ["Riches and property", "Mortal pleasures"]
    assert "started with Full Plate and Packing Steel" in describe(sheet)


def test_two_different_appetites():
    c = Choices("Barbarian", "Human")
    c.choose("Unencumbered, Unharmed")
    c.choose("Conquest")
    with pytest.raises(Exception, match="already one of your appetites"):
        c.choose("Conquest")


def test_only_the_chosen_starting_move_of_the_pair_is_theirs():
    from gary.class_moves import moves_of

    names = [m["name"] for m in moves_of("Barbarian", "Dwarf", "Full Plate and Packing Steel")]
    assert names[0] == "Outsider" and "Full Plate and Packing Steel" in names
    assert "Unencumbered, Unharmed" not in names and "What Are You Waiting For?" in names


# -- the starting moves ----------------------------------------------------------------------


def test_chasing_an_appetite_rolls_1d6_plus_1d8_and_a_higher_d6_brings_trouble(director, dice, sheets):
    opened, effects = move(director, dice, sheets, "defy_danger", (5, 3), stat="str", appetite=True)
    assert any("1d6+1d8" in t for t in texts(opened))
    assert any("Herculean Appetites" in n for n in notes(effects))
    _, effects = move(director, dice, sheets, "defy_danger", (2, 8), stat="str", appetite=True)
    assert not any("Herculean Appetites" in n for n in notes(effects))


def test_appetite_rolls_stay_out_of_the_fair_dice_check():
    journal = [{"kind": "move_outcome", "target": "Gorm", "payload": {"faces": [5, 8], "modifier": 0,
                                                                        "band": "strong", "dice": APPETITE_DICE}}]
    assert summary(journal) == "No move rolls yet."


def test_no_appetite_no_appetite_dice(director, sheets):
    sheets["Gorm"]["choices"] = {}
    effects = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Gorm", move="defy_danger",
                                 stat="str", appetite=True)
    assert not director.registry.open_checks(CHANNEL)
    assert any("Herculean Appetites" in str(getattr(e, "payload", "")) for e in effects)


def test_a_challenge_at_10_plus_is_plus_2_damage_against_those_standing(director, dice, sheets):
    sheets["Gorm"]["gear"] = [{"name": "Axe", "tags": ["close"], "weight": 1}]
    move(director, dice, sheets, "what_are_you_waiting_for", (5, 5))
    assert sheets["Gorm"]["challenged"] == ["Orc 1", "Orc 2"]
    opened, effects = move(director, dice, sheets, "hack_and_slash", (2, 3), target="Orc 1")  # 7 with Str: trade blows
    check = next(iter(director.registry.open_checks(CHANNEL)), None)
    hit = effects
    if check is not None:
        dice.queue(4, 1)
        hit = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id)
    assert any("+2 challenged" in t for t in texts(hit)), texts(effects) + texts(hit)


def test_the_upper_hand_adds_one_to_last_breath_and_makes_the_offer_theirs(director, dice, sheets):
    sheets["Gorm"]["hp"] = 0
    director._trigger(CHANNEL, "Gorm", "last_breath")
    check = next(c for c in director.registry.open_checks(CHANNEL) if c.move == "last_breath")
    assert check.modifier == 1
    dice.queue(3, 3)  # 6 +1 = 7: a 7-9
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    assert any("makes an offer to Death" in str(e.payload) for e in effects if isinstance(e, PromptModel))


def test_unencumbered_unharmed_is_plus_one_armor_only_bare_and_under_load():
    sheet = barbarian()
    assert advanced.armor_bonus(sheet, False)[0] == 1
    assert advanced.armor_bonus({**sheet, "gear": [{"name": "Shield", "shield": 1, "weight": 2}]}, False)[0] == 0
    assert advanced.armor_bonus({**sheet, "gear": [{"name": "Anvil", "weight": 30}]}, False)[0] == 0
    assert advanced.armor_bonus(barbarian(start="Full Plate and Packing Steel"), False)[0] == 0


def test_full_plate_ignores_clumsy():
    plate = [{"name": "Plate", "armor": 3, "weight": 4, "tags": ["clumsy"]}]
    assert clumsy(barbarian(start="Full Plate and Packing Steel", gear=plate)) == ""
    assert clumsy(barbarian(gear=plate)) == "Plate"


# -- the advanced moves ----------------------------------------------------------------------


def test_on_the_move_and_healthy_distrust(director, dice, sheets):
    sheets["Gorm"]["advanced_moves"] = ["On the Move", "Healthy Distrust"]
    opened, _ = move(director, dice, sheets, "defy_danger", (3, 3), stat="dex", movement=True)
    assert any("On the Move" in t for t in texts(opened))
    _, effects = move(director, dice, sheets, "defy_danger", (1, 2), stat="dex", mortal_magic=True)
    assert any("Healthy Distrust" in t and "7–9" in t for t in texts(effects))


def test_a_good_day_to_die_below_con(director, dice, sheets):
    sheets["Gorm"].update(advanced_moves=["A Good Day to Die"], hp=14)
    opened, _ = move(director, dice, sheets, "defy_danger", (3, 3), stat="str")
    assert any("A Good Day to Die" in t for t in texts(opened))
    sheets["Gorm"]["hp"] = 15
    opened, _ = move(director, dice, sheets, "defy_danger", (3, 3), stat="str")
    assert not any("A Good Day to Die" in t for t in texts(opened))


def test_indestructible_hunger_is_offered_once():
    sheet = barbarian(["Indestructible Hunger"], last_hit={"dealt": 5, "by": "Orc 1"})
    offered = reactions.options("Gorm", sheet, "Gorm", sheet)
    assert any(r.key == "hunger" for r in offered)
    hungry = {**sheet, "ongoing_mods": [{"amount": -1, "reason": "Indestructible Hunger: until…", "until": "it ends"}]}
    assert not any(r.key == "hunger" for r in reactions.options("Gorm", hungry, "Gorm", hungry))


def test_still_hungry_and_appetite_for_destruction_at_level_up():
    sheet = barbarian(level=5, xp=12)
    a = levelling.Advance(sheet)
    a.choose_stat(a.stat_options()[0])
    a.choose_move("Still Hungry")
    assert a.step() == "move_choice" and "Conquest" not in a.move_choice_options()
    a.choose_move_choice("Mortal pleasures")
    assert appetites_of(a.result()) == ["Conquest", "Fame and glory", "Mortal pleasures"]

    b = levelling.Advance(barbarian(level=5, xp=12))
    b.choose_stat(b.stat_options()[0])
    b.choose_move("Appetite for Destruction")
    assert b.multi_class_options() == ["fighter", "bard", "thief"]
    b.choose_multi_class("fighter")
    offered = [m["name"] for m in b.multi_move_options()]
    assert "Merciless" in offered and "Signature Weapon" not in offered
    assert not any(n.startswith("Multiclass") for n in offered)


def test_more_always_more_resolves_an_appetite_for_xp(director, sheets):
    sheets["Gorm"]["advanced_moves"] = ["More! Always More!"]
    effects, result = director.resolve_appetite(channel_id=CHANNEL, character="Gorm", appetite="conquest",
                                                new="Vengeance on the Iron Duke")
    apply(effects, sheets)
    assert result["ok"] and sheets["Gorm"]["xp"] == 1
    assert appetites_of(sheets["Gorm"]) == ["Fame and glory", "Vengeance on the Iron Duke"]


def test_the_model_is_told_what_gary_applies():
    assert "1d6+1d8" in advanced.applied(barbarian(), "Herculean Appetites")
    assert advanced.applied(barbarian(), "Kill ’em All")


def test_the_outsider_marks_xp_for_answering_once_a_session(director, sheets):
    effects, result = director.mark_xp(channel_id=CHANNEL, character="Gorm", reason="Outsider", session=2)
    apply(effects, sheets)
    assert result["ok"] and sheets["Gorm"]["xp"] == 1
    _, again = director.mark_xp(channel_id=CHANNEL, character="Gorm", reason="Outsider", session=2)
    assert "already" in again["error"]
    effects, result = director.mark_xp(channel_id=CHANNEL, character="Gorm", reason="Outsider", session=3)
    assert result["ok"]
    sheets["Gorm"]["class"] = "Fighter"
    _, other = director.mark_xp(channel_id=CHANNEL, character="Gorm", reason="Outsider", session=4)
    assert "not an Outsider" in other["error"]
