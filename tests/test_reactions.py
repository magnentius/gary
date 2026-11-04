"""Answering a blow once it lands: Spell Defense, Armor Mastery, Bloody Aegis, Shed, Man's
Best Friend, Divine Intervention, Penitent."""

from __future__ import annotations

import pytest

from gary.fsm import ChangeHp, Director, PromptModel, ReactPrompt, SendMessage, UpdateSheet

CHANNEL, ALICE, BOB = 555, 111, 222

STATS = {"str": 16, "dex": 9, "con": 15, "int": 16, "wis": 12, "cha": 13}


def sheet(name, cls, **more):
    return {"name": name, "class": cls, "race": "Human", "level": 3, "xp": 0, "hp": 20, "max_hp": 20,
            "stats": STATS, "armor": 0, "damage": "d8", **more}


@pytest.fixture
def sheets():
    return {"Dorn": dict(sheet("Dorn", "Fighter"), race="Dwarf"), "Thorin": sheet("Thorin", "Cleric")}


@pytest.fixture
def scene():
    return {"Ogre 1": {"kind": "Ogre", "hp": 20, "max_hp": 20, "armor": 0, "damage": "d10"}}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Dorn", BOB: "Thorin"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets):
    """What the runner would do to the sheets."""
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp) and e.kind == "character":
            sheets[e.name]["hp"] = e.hp


def hit(director, dice, sheets, who="Dorn", roll=6):
    dice.queue(roll)
    effects, _ = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 1", character=who)
    apply(effects, sheets)
    return effects


def hit_id(effects):
    (prompt,) = [e.prompt for e in effects if isinstance(e, SendMessage) and isinstance(e.prompt, ReactPrompt)]
    return prompt.hit_id


def test_a_hit_nobody_can_answer_has_no_button(director, dice, sheets):
    effects = hit(director, dice, sheets)
    assert not [e for e in effects if isinstance(e, SendMessage) and e.prompt]
    assert sheets["Dorn"]["last_hit"]["dealt"] == 6


def test_armor_mastery_negates_the_blow_and_wears_the_armor(director, dice, sheets):
    sheets["Dorn"].update(advanced_moves=["Armor Mastery"], armor=2, gear=[
        {"name": "Chainmail", "armor": 1, "shield": 0, "tags": ["1 armor"], "weight": 1},
        {"name": "Shield", "armor": 0, "shield": 1, "tags": ["+1 armor"], "weight": 2}])
    hid = hit_id(hit(director, dice, sheets, roll=8))  # 8 - 2 = 6
    found, _ = director.reactions(user_id=ALICE, hit_id=hid)
    assert [r.label for r in found] == ["Let Chainmail take it (destroyed)", "Let Shield take it (destroyed)"]
    effects = director.react(channel_id=CHANNEL, user_id=ALICE, hit_id=hid, key="armor:1")
    apply(effects, sheets)
    assert sheets["Dorn"]["hp"] == 20 and sheets["Dorn"]["armor"] == 1
    assert [g["name"] for g in sheets["Dorn"]["gear"]] == ["Chainmail"]
    assert "— it is **destroyed**" in effects[0].text
    assert director.react(channel_id=CHANNEL, user_id=ALICE, hit_id=hid, key="armor:0") == []  # once


def test_bloody_aegis_trades_the_damage_for_a_debility(director, dice, sheets):
    sheets["Dorn"].update(advanced_moves=["Bloody Aegis", "Indomitable"], debilities=["weak"])
    hid = hit_id(hit(director, dice, sheets))
    found, _ = director.reactions(user_id=ALICE, hit_id=hid)
    assert "aegis:weak" not in [r.key for r in found]
    effects = director.react(channel_id=CHANNEL, user_id=ALICE, hit_id=hid, key="aegis:shaky")
    apply(effects, sheets)
    assert sheets["Dorn"]["hp"] == 20 and sheets["Dorn"]["debilities"] == ["weak", "shaky"]
    assert sheets["Dorn"]["forwards"][0]["reason"] == "against Ogre 1 (Indomitable)"


def test_spell_defense_ends_a_spell_and_takes_its_level_off(director, dice, sheets):
    sheets["Eva"] = sheet("Eva", "Wizard", spellbook=["Invisibility"], spells=["Invisibility"],
                          ongoing=[{"spell": "Invisibility", "target": "Eva"}])
    director.players[333] = "Eva"
    hid = hit_id(hit(director, dice, sheets, who="Eva", roll=5))
    (only,) = director.reactions(user_id=333, hit_id=hid)[0]
    assert only.label == "End Invisibility: take 1 less"
    apply(director.react(channel_id=CHANNEL, user_id=333, hit_id=hid, key="spell:0"), sheets)
    assert sheets["Eva"]["hp"] == 16 and sheets["Eva"]["ongoing"] == []


def test_a_clerics_deity_can_spare_an_ally_with_communes_hold(director, dice, sheets, scene):
    sheets["Thorin"].update(advanced_moves=["Divine Intervention"], choices={"deity": "Ilmara"})
    ogre = scene.pop("Ogre 1")  # an hour of prayer needs a quiet hour
    effects, _ = director.prepare_spells(channel_id=CHANNEL, character="Thorin", reason="a quiet chapel")
    apply(effects, sheets)
    scene["Ogre 1"] = ogre
    assert sheets["Thorin"]["intervention"] == 1
    hid = hit_id(hit(director, dice, sheets))
    found, why = director.reactions(user_id=ALICE, hit_id=hid)
    assert found == [] and why == "You have no move that answers this blow."
    (spare,) = director.reactions(user_id=BOB, hit_id=hid)[0]
    assert spare.label == "Call on Ilmara to spare Dorn (no damage)"
    effects = director.react(channel_id=CHANNEL, user_id=BOB, hit_id=hid, key="deity")
    apply(effects, sheets)
    assert sheets["Dorn"]["hp"] == 20 and sheets["Thorin"]["intervention"] == 0
    assert "✨ **Thorin** calls on Ilmara, who spares Dorn — **Dorn** HP 14 → 20." in effects[0].text
    assert any(isinstance(e, PromptModel) and e.payload["move"] == "Divine Intervention" for e in effects)


def test_answering_a_downing_blow_cancels_last_breath(director, dice, sheets, registry):
    sheets["Dorn"].update(hp=4, advanced_moves=["Bloody Aegis"])
    hid = hit_id(hit(director, dice, sheets))
    assert sheets["Dorn"]["hp"] == 0
    assert [c.move for c in registry.open_checks(CHANNEL)] == ["last_breath"]
    effects = director.react(channel_id=CHANNEL, user_id=ALICE, hit_id=hid, key="aegis:sick")
    apply(effects, sheets)
    assert sheets["Dorn"]["hp"] == 4 and not list(registry.open_checks(CHANNEL))


def test_only_the_latest_blow_can_be_answered(director, dice, sheets):
    sheets["Dorn"].update(advanced_moves=["Bloody Aegis"])
    first = hit_id(hit(director, dice, sheets, roll=2))
    hit(director, dice, sheets, roll=3)
    found, why = director.reactions(user_id=ALICE, hit_id=first)
    assert not found and why.startswith("That blow is past answering")


def test_mans_best_friend_spends_the_companion_until_camp(director, dice, sheets, scene):
    sheets["Dorn"].update(**{"class": "Ranger"}, advanced_moves=["Man's Best Friend"],
                          companion={"name": "Ash", "ferocity": 2, "cunning": 1, "armor": 1, "instinct": 1})
    hid = hit_id(hit(director, dice, sheets))
    apply(director.react(channel_id=CHANNEL, user_id=ALICE, hit_id=hid, key="pet"), sheets)
    assert sheets["Dorn"]["companion"]["spent"] is True
    second = hit(director, dice, sheets)
    assert not [e for e in second if isinstance(e, SendMessage) and e.prompt]  # Ash is spent
    scene.clear()
    effects, _ = director.make_camp(channel_id=CHANNEL, reason="a dry hollow")
    apply(effects, sheets)
    assert sheets["Dorn"]["companion"]["spent"] is False


def test_penitence_hurts_more_for_a_forward(director, dice, sheets):
    sheets["Thorin"].update(advanced_moves=["Penitent"])
    hid = hit_id(hit(director, dice, sheets, who="Thorin", roll=5))
    dice.queue(3)
    effects = director.react(channel_id=CHANNEL, user_id=BOB, hit_id=hid, key="pain")
    apply(effects, sheets)
    assert sheets["Thorin"]["hp"] == 12 and sheets["Thorin"]["forwards"][0]["reason"] == "to cast a spell (Penitent)"


def test_mans_best_friend_works_with_the_books_curly_apostrophe(director, dice, sheets):
    sheets["Dorn"].update(**{"class": "Ranger"}, advanced_moves=["Man’s Best Friend"],
                          companion={"name": "Ash", "ferocity": 2, "cunning": 1, "armor": 1, "instinct": 1})
    hid = hit_id(hit(director, dice, sheets))
    assert [r.key for r in director.reactions(user_id=ALICE, hit_id=hid)[0]] == ["pet"]


def test_spell_defense_on_charm_lets_the_creature_go(director, dice, sheets, scene):
    """Ending Charm Person to turn a blow frees the creature it held, as ending it any other way does."""
    from gary.fsm import ConditionMonster

    sheets["Eva"] = sheet("Eva", "Wizard", spellbook=["Charm Person"], spells=["Charm Person"],
                          ongoing=[{"spell": "Not In Any Book", "target": ""},  # skipped by running()
                                   {"spell": "Charm Person", "target": "Bandit 1"}])
    scene["Bandit 1"] = {"kind": "Bandit", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d6",
                         "conditions": [{"kind": "charmed", "spell": "Charm Person", "caster": "Eva"}]}
    director.players[333] = "Eva"
    hid = hit_id(hit(director, dice, sheets, who="Eva", roll=5))
    (only,) = director.reactions(user_id=333, hit_id=hid)[0]
    assert only.label == "End Charm Person: take 1 less"
    effects = director.react(channel_id=CHANNEL, user_id=333, hit_id=hid, key="spell:0")
    apply(effects, sheets)
    assert sheets["Eva"]["ongoing"] == [{"spell": "Not In Any Book", "target": ""}]  # the right one went
    assert ConditionMonster(CHANNEL, "Bandit 1", ()) in effects
