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


def test_unseen_servant_carries_up_to_three_and_drops_it_when_dispelled(director, sheets):
    from gary import gear
    from gary.sheets import carried

    books, rope = gear.item_for("Bag of books")[0].to_dict(), gear.item_for("Adventuring gear")[0].to_dict()
    tome = {"name": "Tome of Ash", "tags": [], "weight": 2}
    sheets["Mae"].update(gear=[books, rope, tome], coins=0, ammo=0, rations=0,
                         ongoing=[{"spell": "Unseen Servant", "target": ""}])
    before = carried(sheets["Mae"])
    effects, given = director.unseen_servant(channel_id=CHANNEL, character="Mae", action="give", item="bag of books")
    apply(effects, sheets, {})
    assert given["load"] == "2 of 3" and carried(sheets["Mae"]) == before - 2
    _, too_heavy = director.unseen_servant(channel_id=CHANNEL, character="Mae", action="give", item="tome of ash")
    assert "load is 3" in too_heavy["error"]
    effects, _ = director.unseen_servant(channel_id=CHANNEL, character="Mae", action="give", item="adventuring gear")
    apply(effects, sheets, {})
    from gary.spells import describe
    assert "Unseen Servant (carrying Bag of Books, Adventuring Gear: 3 of 3)" in describe(sheets["Mae"])
    effects, ended = director.end_spell(channel_id=CHANNEL, character="Mae", spell="Unseen Servant")
    apply(effects, sheets, {})
    assert ended["dropped"] == ["Bag of Books", "Adventuring Gear"]
    assert [t["name"] for t in sheets["Mae"]["thrown"]] == ["Bag of Books", "Adventuring Gear"]
    from gary import trade
    back, got = trade.add_items(sheets["Mae"], ["bag of books"])  # picked up again: the very one
    assert got == ["Bag of Books (recovered)"]


def test_preparing_spells_needs_the_spellbook(director, sheets, scene):
    scene.clear()
    sheets["Mae"]["gear"] = [{"name": "Dagger", "tags": ["hand"], "weight": 1}]
    _, refused = director.prepare_spells(channel_id=CHANNEL, character="Mae", reason="a quiet hour")
    assert "no spellbook" in refused["error"]
    sheets["Mae"]["ongoing"] = [{"spell": "Unseen Servant", "target": "",
                                 "carrying": [{"name": "Spellbook", "tags": [], "weight": 1}]}]
    _, ok = director.prepare_spells(channel_id=CHANNEL, character="Mae", reason="a quiet hour")
    assert ok["ok"]  # the servant holds it: still to hand


def test_sleep_must_name_who_it_could_take(director, sheets, scene):
    (note,) = [e for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Mae", move="cast_a_spell",
                                             spell="Sleep") if hasattr(e, "payload")]
    assert "Sleep needs the creatures it's cast on" in note.payload["error"]


async def test_ending_a_spell_and_casting_in_one_response(clock):
    """Live: "I drop the Invisibility and cast Sleep" -- one response; the cast was refused because the
    ending wasn't written until after the turn."""
    from gary.adapters import gemini_tools as T
    from gary.models import Mutation, MutationKind
    from gary.registry import PendingCheckRegistry
    from gary.state.backend import InMemoryStateBackend
    from gary.state.history import InMemoryHistoryBackend
    from gary.state.store import CampaignStore
    from tests.test_gemini import CHANNEL as ROOM, ALICE as USER, narrator, player, text_response
    from google.genai import types

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.SHEET, "Mae", {"sheet": {
        "name": "Mae", "class": "Wizard", "race": "Human", "level": 5, "xp": 0, "hp": 15, "max_hp": 15, "armor": 0,
        "damage": "d4", "stats": STATS, "spellbook": ["Sleep", "Invisibility"], "spells": ["Sleep", "Invisibility"],
        "ongoing": [{"spell": "Invisibility", "target": "Dorn"}], "lost": [], "cast_penalty": 0}}),
        Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {"Bandit 1": {
            "kind": "Bandit", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d6"}}})], reason="setup")
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={ROOM},
                        players={USER: "Mae"}, sheet_for=lambda a: (store.state().get("sheets") or {}).get(a),
                        monsters=lambda: store.state().get("monsters") or {})
    both = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name=T.END_SPELL, args={"character": "Mae", "spell": "Invisibility"})),
        types.Part(function_call=types.FunctionCall(name=T.MAKE_MOVE, args={
            "actor": "Mae", "move": "cast_a_spell", "spell": "Sleep", "target": "Bandit 1"})),
    ]))])
    n = narrator((director, store, InMemoryHistoryBackend()), both, text_response("."))
    result = await player(n, "I drop the invisibility and cast Sleep")
    assert store.state()["sheets"]["Mae"]["ongoing"] == []
    assert not any("cannot cast a spell" in str(getattr(e, "payload", "")) for e in result.effects)
    assert director.registry.open_checks(ROOM)[0].spell == "Sleep"


def test_a_sleeper_woken_by_the_din_can_fight_again(director, dice, sheets, scene):
    """Live: the fight got loud, the bandits should have woken, and nothing could wake them."""
    cast(director, dice, sheets, scene, "Sleep", "Goblin 1", then=(1,))
    _, refused = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Dorn")
    assert "free_creature" in refused["error"]
    effects, freed = director.free_creature(channel_id=CHANNEL, creature="goblin 1", reason="the clash of steel")
    apply(effects, sheets, scene)
    assert freed["ok"] and scene["Goblin 1"]["conditions"] == []
    dice.queue(3)
    _, hit = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Dorn")
    assert hit["dealt"] == 3


def test_freeing_a_charmed_creature_ends_the_charm(director, dice, sheets, scene):
    cast(director, dice, sheets, scene, "Charm Person", "Goblin 2")
    effects, _ = director.free_creature(channel_id=CHANNEL, creature="Goblin 2", reason="Mae's lie is exposed")
    apply(effects, sheets, scene)
    assert sheets["Mae"]["ongoing"] == [] and scene["Goblin 2"]["conditions"] == []


def test_shadow_walk_names_its_destination_in_level_words_and_arrives(director, dice, sheets, scene):
    from gary.fsm import Arrive

    sheets["Mae"]["spells"].append("Shadow Walk")
    sheets["Mae"]["spellbook"].append("Shadow Walk")
    (note,) = [e for e in director.open_move(
        channel_id=CHANNEL, user_id=ALICE, actor="Mae", move="cast_a_spell", spell="Shadow Walk",
        target="the treasure vault deep inside the Lich-King's sealed citadel beyond the Ashen Wastes")
        if hasattr(e, "payload")]
    assert "9 words at most" in note.payload["error"] and "is 13" in note.payload["error"]
    effects = cast(director, dice, sheets, scene, "Shadow Walk", "the Lich-King's vault")
    assert Arrive(CHANNEL, "the Lich-King's vault") in effects


async def test_arriving_moves_the_scene_and_puts_it_on_the_map(clock):
    from gary.ports import EchoNarrator
    from gary.adapters.runner import EffectRunner
    from gary.fsm import Arrive
    from gary.models import Mutation, MutationKind
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.SCENE, "", {"location": "Fenwick", "steading": "Fenwick"})], reason="setup")

    class Quiet:
        async def send(self, *a, **k):
            pass

    await EffectRunner(Quiet(), EchoNarrator(), store=store).run([Arrive(CHANNEL, "the Lich-King's vault")])
    state = store.state()
    assert state["scene"]["location"] == "the Lich-King's vault" and state["scene"]["steading"] == ""
    assert state["world"]["places"]["the_lich_king_s_vault"]["note"] == "reached by Shadow Walk"



async def test_shadow_walking_to_a_known_steading_lands_in_it(clock):
    from gary import steadings
    from gary.adapters.runner import EffectRunner
    from gary.fsm import Arrive
    from gary.models import Mutation, MutationKind
    from gary.ports import EchoNarrator
    from gary.state.backend import InMemoryStateBackend
    from gary.state.store import CampaignStore

    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    town = steadings.make("Fenwick", "town", 1, 0, ["Pigton", "Curland", "grain", "Mayhill"])
    store.apply([Mutation(MutationKind.STEADING, "fenwick", {"steading": town}),
                 Mutation(MutationKind.SCENE, "", {"location": "the Lich-King's vault"})], reason="setup")

    class Quiet:
        async def send(self, *a, **k):
            pass

    await EffectRunner(Quiet(), EchoNarrator(), store=store).run([Arrive(CHANNEL, "fenwick")])
    assert store.state()["scene"]["steading"] == "Fenwick"


def test_only_one_telepathic_bond_at_a_time(director, dice, sheets, scene):
    sheets["Mae"]["spells"].append("Telepathy")
    sheets["Mae"]["spellbook"].append("Telepathy")
    cast(director, dice, sheets, scene, "Telepathy", "Dorn")
    effects = cast(director, dice, sheets, scene, "Telepathy", "the reeve")
    assert sheets["Mae"]["ongoing"] == [{"spell": "Telepathy", "target": "the reeve"}]
    assert any("telepathy with Dorn breaks" in getattr(e, "text", "") for e in effects)


def test_an_alarm_circle_wakes_its_caster_when_something_comes(director, sheets, scene):
    scene.clear()
    _here(director, "the ruined chapel")
    sheets["Mae"]["ongoing"] = [{"spell": "Alarm", "target": "", "where": "the ruined chapel"}]
    effects, facts = director.make_camp(channel_id=CHANNEL, approaches=True, watcher="Dorn", reason="in the nave")
    assert "Mae's Alarm circle" in facts["alarm"]
    from gary.spells import describe
    assert "Alarm at the ruined chapel" in describe(sheets["Mae"])
    _here(director, "the old road")
    _, elsewhere = director.make_camp(channel_id=CHANNEL, approaches=True, watcher="Dorn", reason="a ditch")
    assert "alarm" not in elsewhere


# -- Turn Undead -----------------------------------------------------------------------------

@pytest.fixture
def crypt(registry, clock, dice):
    sheets = {"Thorin": {"name": "Thorin", "class": "Cleric", "race": "Dwarf", "level": 3, "xp": 0, "hp": 18,
                         "max_hp": 18, "armor": 1, "damage": "d6", "stats": STATS},
              "Dorn": {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 3, "xp": 0, "hp": 20,
                       "max_hp": 20, "armor": 0, "damage": "d10", "stats": STATS}}
    scene = {"Zombie 1": {"kind": "Zombie", "hp": 11, "max_hp": 11, "armor": 1, "damage": "d6"},
             "Wight-Wolf 1": {"kind": "Wight-Wolf", "hp": 6, "max_hp": 6, "armor": 1, "damage": "d6"},
             "Goblin 1": {"kind": "Goblin", "hp": 3, "max_hp": 3, "armor": 1, "damage": "d6"}}
    director = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                        players={ALICE: "Thorin", 222: "Dorn"}, sheet_for=sheets.get, monsters=lambda: scene)
    return director, sheets, scene


def _turn(director, dice, sheets, scene, faces):
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin", move="turn_undead")
    dice.queue(*faces)
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    apply(effects, sheets, scene)
    return effects


def kinds(scene):
    return {n: [c["kind"] for c in r.get("conditions") or ()] for n, r in scene.items()}


def test_turn_undead_on_a_7_holds_the_undead_off_the_cleric(crypt, dice):
    director, sheets, scene = crypt
    _turn(director, dice, sheets, scene, (3, 4))
    assert kinds(scene) == {"Zombie 1": ["turned"], "Wight-Wolf 1": ["turned"], "Goblin 1": []}
    _, refused = director.monster_attacks(channel_id=CHANNEL, monster="Zombie 1", character="Thorin")
    assert "held off by Thorin's Turn Undead" in refused["error"]
    dice.queue(2)
    _, hit = director.monster_attacks(channel_id=CHANNEL, monster="Zombie 1", character="Dorn")
    assert hit["dealt"] == 2  # it can still go for someone else


def test_on_a_10_mindless_undead_flee_and_intelligent_ones_are_dazed(crypt, dice):
    director, sheets, scene = crypt
    _turn(director, dice, sheets, scene, (5, 6))
    assert kinds(scene) == {"Zombie 1": ["fleeing"], "Wight-Wolf 1": ["dazed"], "Goblin 1": []}
    _, refused = director.monster_attacks(channel_id=CHANNEL, monster="Wight-Wolf 1", character="Dorn")
    assert "dazed and can't attack" in refused["error"]


def test_the_ward_ends_when_the_cleric_stops_praying_or_on_aggression(crypt, dice):
    director, sheets, scene = crypt
    _turn(director, dice, sheets, scene, (3, 4))
    apply(creature_harmed({"sheets": sheets, "monsters": scene}, "Wight-Wolf 1", CHANNEL), sheets, scene)
    assert kinds(scene)["Wight-Wolf 1"] == []  # aggression breaks it
    effects = director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin", move="hack_and_slash",
                                 target="Goblin 1")
    apply(effects, sheets, scene)
    assert kinds(scene)["Zombie 1"] == [] and any("stops praying" in getattr(e, "text", "") for e in effects)


def test_cause_fear_takes_one_living_mind(crypt, dice):
    director, sheets, scene = crypt
    sheets["Thorin"].update(spells=["Cause Fear"], ongoing=[], lost=[], cast_penalty=0)
    (note,) = [e for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin", move="cast_a_spell",
                                             spell="Cause Fear", target="Zombie 1") if hasattr(e, "payload")]
    assert "less than animal intelligence" in note.payload["error"]
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin", move="cast_a_spell", spell="Cause Fear",
                       target="Goblin 1, Zombie 1")
    dice.queue(6, 6)
    apply(director.roll(channel_id=CHANNEL, user_id=ALICE), sheets, scene)
    assert kinds(scene) == {"Zombie 1": [], "Wight-Wolf 1": [], "Goblin 1": ["afraid"]}  # one target


def test_sanctuary_lasts_where_it_was_walked_and_heals_more_there(crypt, dice):
    director, sheets, scene = crypt
    scene.clear()
    _here(director, "the ruined chapel")
    sheets["Thorin"].update(spells=["Sanctuary", "Cure Light Wounds"], ongoing=[], lost=[], cast_penalty=0)
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin", move="cast_a_spell", spell="Sanctuary")
    dice.queue(6, 6)
    apply(director.roll(channel_id=CHANNEL, user_id=ALICE), sheets, scene)
    assert sheets["Thorin"]["ongoing"] == [{"spell": "Sanctuary", "target": "", "where": "the ruined chapel"}]
    assert director.sanctuary() == "Thorin"
    sheets["Dorn"]["hp"] = 5
    dice.queue(3, 2)  # the Sanctuary's 1d4, for each of them
    effects, _ = director.make_camp(channel_id=CHANNEL, reason="behind the barred door")
    apply(effects, sheets, scene)
    assert sheets["Dorn"]["hp"] == 5 + 10 + 3  # half of 20, +1d4
    _here(director, "the old road")
    assert director.sanctuary() == ""


def test_a_called_shot_to_the_head_stuns_and_its_damage_doesnt_end_it(director, dice, sheets, scene):
    sheets["Ren"] = {"name": "Ren", "class": "Ranger", "race": "Elf", "level": 1, "xp": 0, "hp": 18, "max_hp": 18,
                     "armor": 0, "damage": "d8", "stats": STATS,
                     "gear": [{"name": "Hunter's bow", "tags": ["near", "far"], "needs_ammo": True, "weight": 1}],
                     "ammo": 3}
    director.set_player_character(333, "Ren")
    director.open_move(channel_id=CHANNEL, user_id=333, actor="Ren", move="called_shot", target="Goblin 1")
    dice.queue(5, 5)
    director.roll(channel_id=CHANNEL, user_id=333)
    check = director.registry.open_checks(CHANNEL)[0]
    effects = director.choose(channel_id=CHANNEL, user_id=333, check_id=check.check_id, picks=[0])  # head
    apply(effects, sheets, scene)
    assert kinds_of(scene, "Goblin 1") == ["stunned"]
    apply(creature_harmed({"sheets": sheets, "monsters": scene}, "Goblin 1", CHANNEL), sheets, scene)
    assert kinds_of(scene, "Goblin 1") == ["stunned"]  # the shot's own damage leaves it drooling
    _, refused = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Mae")
    assert "stunned and can't attack" in refused["error"]


def kinds_of(scene, name):
    return [c["kind"] for c in scene[name].get("conditions") or ()]


def test_trap_expert_hold_is_counted_and_stays_in_its_area(director, dice, sheets, scene):
    sheets["Nim"] = {"name": "Nim", "class": "Thief", "race": "Halfling", "level": 1, "xp": 0, "hp": 12, "max_hp": 12,
                     "armor": 1, "damage": "d8", "stats": STATS, "advanced_moves": ["Cautious"]}
    director.set_player_character(444, "Nim")
    _here(director, "the flooded gallery")
    director.open_move(channel_id=CHANNEL, user_id=444, actor="Nim", move="trap_expert")
    dice.queue(1, 1)  # a miss -- Cautious still gives 1
    apply(director.roll(channel_id=CHANNEL, user_id=444), sheets, scene)
    assert sheets["Nim"]["trap_hold"] == {"hold": 1, "where": "the flooded gallery"}
    effects, asked = director.trap_question(channel_id=CHANNEL, character="Nim", question="Is there a trap here?")
    apply(effects, sheets, scene)
    assert asked["hold"] == 0 and sheets["Nim"]["trap_hold"] is None
    _, none = director.trap_question(channel_id=CHANNEL, character="Nim", question="What else is hidden here?")
    assert "no Trap Expert hold" in none["error"]
    sheets["Nim"]["trap_hold"] = {"hold": 2, "where": "the flooded gallery"}
    _here(director, "the outer stair")
    _, gone = director.trap_question(channel_id=CHANNEL, character="Nim", question="Is there a trap here?")
    assert "gone now they've left" in gone["error"]


def test_backstab_can_take_a_point_of_armor_off(director, dice, sheets, scene):
    from gary.fsm import ArmorMonster

    sheets["Nim"] = {"name": "Nim", "class": "Thief", "race": "Halfling", "level": 1, "xp": 0, "hp": 12, "max_hp": 12,
                     "armor": 1, "damage": "d8", "stats": STATS,
                     "gear": [{"name": "Dagger", "tags": ["hand"], "weight": 1}]}
    director.set_player_character(444, "Nim")
    director.open_move(channel_id=CHANNEL, user_id=444, actor="Nim", move="backstab", target="Goblin 2")
    dice.queue(5, 5)
    director.roll(channel_id=CHANNEL, user_id=444)
    check = director.registry.open_checks(CHANNEL)[0]
    effects = director.choose(channel_id=CHANNEL, user_id=444, check_id=check.check_id, picks=[0, 3])
    assert ArmorMonster(CHANNEL, "Goblin 2", 0) in effects


def test_a_healing_spell_on_an_npc_ally_is_rolled_and_narrated(crypt, dice):
    director, sheets, scene = crypt
    sheets["Thorin"].update(spells=["Cure Light Wounds"], ongoing=[], lost=[], cast_penalty=0)
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin", move="cast_a_spell",
                       spell="Cure Light Wounds", target="Sibel")
    dice.queue(6, 6, 5)  # a hit, then the 1d8: 5
    effects = director.roll(channel_id=CHANNEL, user_id=ALICE)
    assert any("heals **Sibel** — 1d8: 5 → 5 healed (an NPC" in getattr(e, "text", "") for e in effects)
    assert "Sibel" not in sheets  # no sheet made, no HP written


def test_deaths_mark_survives_healing_and_comes_due():
    from gary import opening
    from gary.sheets import describe

    dorn = {"name": "Dorn", "class": "Fighter", "race": "Human", "level": 2, "xp": 0, "hp": 6, "max_hp": 20,
            "armor": 1, "damage": "d10", "stats": STATS, "deaths_mark": True}
    assert "marked as Death's own" in describe(dorn)  # healed above 0, the mark still shows
    event = opening.with_doom(opening.scene_event(3), [dorn])
    assert event["deaths_own"] == ["Dorn"] and "the time has come" in event["instruction"]
    assert opening.with_doom(opening.scene_event(3), [{**dorn, "deaths_mark": False}]) == opening.scene_event(3)


async def test_death_comes_for_the_marked(rig):
    from gary.adapters import gemini_tools as T
    from gary.models import Mutation, MutationKind
    from tests.test_gemini import call_response, narrator, player, text_response

    director, store, history = rig
    store.apply([Mutation(MutationKind.SHEET, "Dorn", {"sheet": {
        "name": "Dorn", "class": "Fighter", "race": "Human", "level": 2, "xp": 0, "hp": 6, "max_hp": 20,
        "armor": 1, "damage": "d10", "stats": STATS, "deaths_mark": True}})], reason="setup")
    n = narrator(rig, call_response(T.DEATH_COMES, {"character": "Dorn", "reason": "holding the bridge alone"}),
                 text_response("."))
    await player(n, "Dorn turns to face them")
    assert store.state()["sheets"]["Dorn"]["dead"] is True


def test_resurrection_is_refused_the_dead_stay_dead(crypt):
    director, sheets, scene = crypt
    sheets["Thorin"].update(level=5, spells=["Resurrection"], ongoing=[], lost=[], cast_penalty=0)
    notes = [e.payload for e in director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin",
                                                   move="cast_a_spell", spell="Resurrection", target="Dorn")
             if hasattr(e, "payload")]
    assert notes and "the dead stay dead" in notes[0]["error"]


def test_the_gms_own_wight_is_intelligent_and_its_zombie_isnt():
    from gary.fsm import undead

    assert undead({"kind": "Barrow-wight", "tags": []}) == (True, True)  # dazed on a 10+, not fleeing
    assert undead({"kind": "Bog Zombie", "tags": []}) == (True, False)
    assert undead({"kind": "Skeleton"}) == (True, False)  # the bestiary's own say so
