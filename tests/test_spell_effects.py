"""What Wizard spells do once cast: conditions on creatures, spells that end themselves, Mirror
Image, Dominate's hold, summoned allies, and Contingency."""

from __future__ import annotations

import pytest

from gary.fsm import (
    ChangeHp, ConditionMonster, Director, Dismiss, UpdateSheet, creature_harmed, summon_fell,
)

from tests.test_gemini import rig as _rig

rig = _rig

CHANNEL, ALICE = 555, 111
STATS = {"str": 10, "dex": 16, "con": 13, "int": 16, "wis": 12, "cha": 9}
BOOK = ["Sleep", "Charm Person", "Invisibility", "Mirror Image", "Alarm", "Dominate", "Summon Monster",
        "Contingency", "Magic Missile", "Cage"]


@pytest.fixture
def sheets():
    return {"Mae": {"name": "Mae", "class": "Wizard", "race": "Human", "level": 9, "xp": 0, "hp": 15, "max_hp": 15,
                    "stats": STATS, "armor": 0, "damage": "d4", "spellbook": list(BOOK), "spells": list(BOOK),
                    "ongoing": [], "lost": [], "cast_penalty": 0},
            "Dorn": {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 9, "xp": 0, "hp": 20,
                     "max_hp": 20, "stats": STATS, "armor": 0, "damage": "d10"}}


@pytest.fixture
def scene():
    return {f"Goblin {i}": {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"} for i in (1, 2, 3)}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Mae", 222: "Dorn"}, sheet_for=sheets.get, monsters=lambda: scene)


def apply(effects, sheets, scene):
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp):
            (sheets if e.kind == "character" else scene)[e.name]["hp"] = e.hp
        elif isinstance(e, ConditionMonster):
            scene[e.name]["conditions"] = [dict(c) for c in e.conditions]
        elif isinstance(e, Dismiss):
            sheets.pop(e.name, None)


def cast(director, dice, sheets, scene, spell, target="", faces=(6, 6), then=()):
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Mae", move="cast_a_spell", spell=spell, target=target)
    dice.queue(*faces, *then)
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    apply(effects, sheets, scene)
    return effects


def state(sheets, scene):
    return {"sheets": sheets, "monsters": scene}


def test_sleep_takes_1d4_of_those_named_and_pain_wakes_them(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Sleep", "Goblin 1, Goblin 2, Goblin 3", then=(2,))  # 1d4: 2
    asleep = [n for n, r in scene.items() if any(c["kind"] == "asleep" for c in r.get("conditions") or ())]
    assert asleep == ["Goblin 1", "Goblin 2"]
    _, refused = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Dorn")
    assert "asleep and can't attack" in refused["error"]
    woke = creature_harmed(state(sheets, scene), "Goblin 1", CHANNEL)
    apply(woke, sheets, scene)
    assert scene["Goblin 1"]["conditions"] == [] and any("no longer asleep" in getattr(e, "text", "") for e in woke)


def test_charm_ends_on_harm_and_on_the_casters_side_too(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Charm Person", "Goblin 3")
    assert sheets["Mae"]["ongoing"] == [{"spell": "Charm Person", "target": "Goblin 3"}]
    apply(creature_harmed(state(sheets, scene), "Goblin 3", CHANNEL), sheets, scene)
    assert sheets["Mae"]["ongoing"] == [] and scene["Goblin 3"]["conditions"] == []


def test_ending_a_spell_lifts_its_condition(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Cage", "Goblin 2")
    assert scene["Goblin 2"]["conditions"][0]["kind"] == "caged"
    effects, _ = director.end_spell(channel_id=CHANNEL, character="Mae", spell="Cage")
    apply(effects, sheets, scene)
    assert scene["Goblin 2"]["conditions"] == []


def test_invisibility_ends_when_its_target_attacks(director, dice, sheets, scene):
    sheets["Mae"]["ongoing"] = [{"spell": "Invisibility", "target": "Dorn"}]
    effects = director.open_move(channel_id=CHANNEL, user_id=222, actor="Dorn", move="hack_and_slash", target="Goblin 1")
    apply(effects, sheets, scene)
    assert sheets["Mae"]["ongoing"] == [] and any("visible again" in getattr(e, "text", "") for e in effects)


def test_mirror_image_takes_a_blow_on_four_to_six(director, dice, sheets, scene):
    sheets["Mae"]["ongoing"] = [{"spell": "Mirror Image", "target": ""}]
    dice.queue(5)
    effects, facts = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Mae")
    apply(effects, sheets, scene)
    assert facts["mirror_image"] and sheets["Mae"]["hp"] == 15 and sheets["Mae"]["ongoing"] == []
    sheets["Mae"]["ongoing"] = [{"spell": "Mirror Image", "target": ""}]
    dice.queue(2, 4)  # the real one; then d6 damage 4
    effects, facts = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Mae")
    assert facts["dealt"] == 4 and any("finds the real Mae" in getattr(e, "text", "") for e in effects)


def test_alarm_lasts_until_spells_are_prepared(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Alarm")
    assert sheets["Mae"]["ongoing"] == [{"spell": "Alarm", "target": ""}]
    scene.clear()  # no studying with goblins about
    effects, _ = director.prepare_spells(channel_id=CHANNEL, character="Mae", reason="a quiet hour")
    apply(effects, sheets, scene)
    assert sheets["Mae"]["ongoing"] == []


def test_dominate_holds_spends_and_loses_hold_to_harm(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Dominate", "Goblin 1", then=(3,))  # 1d4 hold: 3
    assert sheets["Mae"]["ongoing"][0]["hold"] == 3 and scene["Goblin 1"]["conditions"][0]["kind"] == "dominated"
    effects, result = director.dominate(channel_id=CHANNEL, character="Mae", command="answer", reason="where's the boss?")
    apply(effects, sheets, scene)
    assert result["hold"] == 2
    apply(creature_harmed(state(sheets, scene), "Goblin 1", CHANNEL), sheets, scene)
    assert sheets["Mae"]["ongoing"][0]["hold"] == 1
    effects, result = director.dominate(channel_id=CHANNEL, character="Mae", command="attack")
    apply(effects, sheets, scene)
    assert result["ended"] and sheets["Mae"]["ongoing"] == [] and scene["Goblin 1"]["conditions"] == []


def test_summon_monster_rolls_traits_and_the_ally_falls_at_zero(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Summon Monster", then=(2,))  # 1d6 traits: 2
    assert sheets["Mae"]["summoning"] == {"spell": "Summon Monster", "traits": 2}
    _, too_many, _ = director.summon_ally(channel_id=CHANNEL, character="Mae", name="Vex", kind="a shadow mastiff",
                                          traits=["d8", "durable", "careful"])
    assert not too_many["ok"]
    effects, made, ally = director.summon_ally(channel_id=CHANNEL, character="Mae", name="Vex",
                                               kind="a shadow mastiff", traits=["d8", "durable"])
    apply(effects, sheets, scene)
    sheets["Vex"] = ally
    assert made["ok"] and ally["hp"] == 19 and ally["damage"] == "d8" and ally["stats"]["str"] == 13
    assert sheets["Mae"]["ongoing"][-1] == {"spell": "Summon Monster", "target": "Vex"}
    assert director._trigger(CHANNEL, "Vex", "last_breath") == []  # no Last Breath for a summoned thing
    sheets["Vex"]["hp"] = 0
    gone = summon_fell(state(sheets, scene), "Vex", CHANNEL)
    apply(gone, sheets, scene)
    assert "Vex" not in sheets and sheets["Mae"]["ongoing"] == []


def test_dismissing_a_summon_ends_it(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Summon Monster", then=(1,))
    effects, _, ally = director.summon_ally(channel_id=CHANNEL, character="Mae", name="Vex", kind="an imp")
    apply(effects, sheets, scene)
    sheets["Vex"] = ally
    effects, _ = director.end_spell(channel_id=CHANNEL, character="Mae", spell="Summon Monster")
    apply(effects, sheets, scene)
    assert "Vex" not in sheets


def test_contingency_holds_a_spell_and_releases_it_without_a_roll(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Contingency")
    _, wordy = director.contingency(channel_id=CHANNEL, character="Mae", action="hold", spell="Magic Missile",
                                    trigger="when anyone at all draws a weapon against me or mine")  # 11 words > level 9
    assert "at most 9 words" in wordy["error"]
    effects, held = director.contingency(channel_id=CHANNEL, character="Mae", action="hold", spell="Magic Missile",
                                         trigger="when a goblin draws steel")
    apply(effects, sheets, scene)
    assert held["ok"] and sheets["Mae"]["contingency"]["spell"] == "Magic Missile"
    dice.queue(2, 2)  # 2d4: 4, through armor 1 -> 3
    effects, released = director.contingency(channel_id=CHANNEL, character="Mae", action="release", target="Goblin 2")
    apply(effects, sheets, scene)
    assert released["ok"] and scene["Goblin 2"]["hp"] == 0 and sheets["Mae"]["contingency"] is None


# -- area spells ---------------------------------------------------------------------------

def _here(director, location):
    director._scene = lambda: {"location": location}


def test_fireball_catches_a_friend_nearby_with_the_same_roll(director, dice, sheets, scene):
    sheets["Mae"]["spells"].append("Fireball")
    sheets["Mae"]["spellbook"].append("Fireball")
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Mae", move="cast_a_spell", spell="Fireball",
                       target="Goblin 1, Goblin 2, Dorn")
    dice.queue(6, 6)
    apply(director.roll(channel_id=CHANNEL, user_id=ALICE), sheets, scene)
    check = director.registry.open_checks(CHANNEL)[0]
    dice.queue(3, 4)  # 2d6: 7, ignoring armor
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id)
    apply(effects, sheets, scene)
    assert scene["Goblin 1"]["hp"] <= 0 and scene["Goblin 2"]["hp"] <= 0
    assert sheets["Dorn"]["hp"] == 13 and any("**Dorn** is hurt — Fireball" in getattr(e, "text", "") for e in effects)


def test_cloudkill_stays_where_it_was_cast(director, dice, sheets, scene):
    _here(director, "The Drowned Crypt")
    sheets["Mae"]["ongoing"] = [{"spell": "Cloudkill", "target": "", "where": "The Drowned Crypt"}]
    assert director._cloudkill()
    _here(director, "the crypt stairs, Drowned Crypt")
    assert director._cloudkill()  # still the crypt
    _here(director, "The old road east")
    assert not director._cloudkill()


def test_cloudkill_hurts_whatever_takes_damage_in_it(director, dice, sheets, scene):
    _here(director, "The Drowned Crypt")
    sheets["Mae"]["ongoing"] = [{"spell": "Cloudkill", "target": "", "where": "The Drowned Crypt"}]
    scene["Ogre 1"] = {"kind": "Ogre", "hp": 10, "max_hp": 10, "armor": 1, "damage": "d10"}
    dice.queue(4, 2)  # the ogre's d10: 4, through armor 1 = 3, +1d6 Cloudkill 2
    _, hit = director.creature_attacks(channel_id=CHANNEL, attacker="Ogre 1", target="Goblin 3")
    assert hit["dealt"] == 5
    dice.queue(3, 5)  # a goblin's d6: 3 on Dorn (armor 0), +1d6 Cloudkill 5
    _, blow = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Dorn")
    assert blow["dealt"] == 8


async def test_leaving_ends_a_cloudkill_out_of_sight(rig):
    from gary.adapters import gemini_tools as T
    from gary.models import Mutation, MutationKind
    from tests.test_gemini import call_response, narrator, player, text_response

    director, store, history = rig
    store.apply([Mutation(MutationKind.SHEET, "Mae", {"sheet": {
        "name": "Mae", "class": "Wizard", "race": "Human", "level": 9, "xp": 0, "hp": 15, "max_hp": 15,
        "armor": 0, "damage": "d4", "stats": STATS,
        "ongoing": [{"spell": "Cloudkill", "target": "", "where": "The Drowned Crypt"}]}}),
        Mutation(MutationKind.SCENE, "", {"location": "The Drowned Crypt"})], reason="setup")
    n = narrator(rig, call_response(T.SET_SCENE, {"location": "The old road east"}), text_response("."))
    result = await player(n, "we get out of there")
    assert store.state()["sheets"]["Mae"]["ongoing"] == []
    assert any("Mae's Cloudkill (out of sight, it fades)" in getattr(e, "text", "") for e in result.effects)
