"""Cleric and Wizard spells: what a caster has, choosing them, and casting them."""

from __future__ import annotations

import pytest

from gary import spells
from gary.fsm import ChangeHp, Director, PromptModel, SendMessage, SpellsPrompt, UpdateSheet
from gary.spells import Preparation, SpellError

CHANNEL = 555
ALICE, BOB = 111, 222

# Thorin WIS 16 (+2); Eva INT 16 (+2)
THORIN = {"name": "Thorin", "class": "Cleric", "race": "Dwarf", "level": 1,
          "stats": {"str": 12, "dex": 8, "con": 15, "int": 9, "wis": 16, "cha": 13},
          "hp": 23, "max_hp": 23, "armor": 2, "damage": "d6",
          "spells": ["Bless", "Cure Light Wounds"], "lost": [], "cast_penalty": 0}
EVA = {"name": "Eva", "class": "Wizard", "race": "Human", "level": 1,
       "stats": {"str": 8, "dex": 12, "con": 13, "int": 16, "wis": 15, "cha": 9},
       "hp": 9, "max_hp": 17, "armor": 0, "damage": "d4",
       "spellbook": ["Magic Missile", "Invisibility", "Sleep"], "borrowed_spell": "Cure Light Wounds",
       "spells": ["Magic Missile", "Invisibility"], "lost": [], "cast_penalty": 0}


def goblin(hp=7, armor=1):
    return {"kind": "Goblin", "hp": hp, "max_hp": 7, "armor": armor, "damage": "d6"}


@pytest.fixture
def sheets():
    return {"Thorin": dict(THORIN), "Eva": dict(EVA)}


@pytest.fixture
def scene():
    return {"Goblin 1": goblin(), "Goblin 2": goblin()}


@pytest.fixture
def director(registry, clock, dice, sheets, scene):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                    players={ALICE: "Thorin", BOB: "Eva"}, sheet_for=sheets.get,
                    monsters=lambda: scene)


def cast(director, spell, actor="Thorin", user_id=ALICE, **kw):
    return director.open_move(channel_id=CHANNEL, user_id=user_id, actor=actor,
                              move="cast_a_spell", spell=spell, **kw)


def roll(director, user_id=ALICE):
    return director.roll(channel_id=CHANNEL, user_id=user_id)


def only_check(registry):
    (check,) = list(registry.open_checks(CHANNEL))
    return check


def notes(effects):
    return [e.payload for e in effects if isinstance(e, PromptModel)]


def texts(effects):
    return [e.text for e in effects if isinstance(e, SendMessage)]


def apply(effects, sheets):
    """What the runner does with UpdateSheet and character ChangeHp."""
    for e in effects:
        if isinstance(e, UpdateSheet):
            sheets[e.name].update(e.changes)
        elif isinstance(e, ChangeHp) and e.kind == "character":
            sheets[e.name]["hp"] = e.hp


# -- the book's lists ---------------------------------------------------------------------


def test_both_spell_lists_are_extracted_with_levels_and_tags():
    assert len(spells.SPELLS["cleric"]) == 33 and len(spells.SPELLS["wizard"]) == 30
    bless = spells._named("cleric", "Bless")
    assert (bless.level, bless.ongoing, bless.penalty) == (1, True, True)
    assert spells._named("wizard", "Fireball").school == "Evocation"
    assert [s.name for s in spells.SPELLS["cleric"] if s.free] == ["Light", "Sanctify", "Guidance"]


def test_what_ends_or_forbids_casting_is_read_from_the_text():
    assert spells._named("wizard", "Invisibility").blocks_casting
    assert spells._named("wizard", "Mimic").blocks_casting
    assert spells._named("cleric", "Hold Person").ends_when_casting
    assert spells._named("cleric", "Hold Person").lasts  # not tagged Ongoing, but lasts
    assert not spells._named("wizard", "Telepathy").penalty  # ongoing, but free to keep


def test_race_moves_add_free_spells():
    assert "Words of the Unspeaking (stone)" in [s.name for s in spells.free_spells(THORIN)]
    elf = {**EVA, "race": "Elf"}
    assert "Detect Magic" in [s.name for s in spells.free_spells(elf)]
    assert "Detect Magic" not in [s.name for s in spells.preparable({**elf, "spellbook": ["Detect Magic"]})]
    assert "Detect Magic" not in [s.name for s in Preparation({"class": "Wizard", "race": "Elf"}).options()]


def test_a_cleric_prepares_from_their_whole_list_up_to_their_level():
    names = [s.name for s in spells.preparable(THORIN)]
    assert "Bless" in names and "Cure Moderate Wounds" not in names  # level 3
    assert "Light" not in names  # a rote is free
    assert spells.budget(THORIN) == 2


def test_a_wizard_prepares_from_their_spellbook_and_a_human_from_their_borrowed_spell():
    assert [s.name for s in spells.preparable(EVA)] == [
        "Magic Missile", "Invisibility", "Cure Light Wounds"]  # Sleep is level 3


def test_castable_is_free_and_prepared_less_lost():
    sheet = {**THORIN, "lost": ["Bless", "Light"]}
    names = [s.name for s in spells.castable(sheet)]
    assert "Cure Light Wounds" in names and "Sanctify" in names
    assert "Bless" not in names and "Light" not in names


def test_casting_modifier_counts_penalties_and_ongoing_spells():
    sheet = {**THORIN, "cast_penalty": 1, "ongoing": [{"spell": "Bless", "target": "Eva"}]}
    assert spells.casting_modifier(sheet) == (-2, ["-1 ongoing", "-1 Bless"])


# -- choosing -----------------------------------------------------------------------------


def test_a_new_human_wizard_fills_a_spellbook_borrows_a_spell_then_prepares():
    prep = Preparation({"name": "Ana", "class": "Wizard", "race": "Human", "level": 1})
    assert prep.step() == "spellbook" and prep.counts() == (3, 3)
    with pytest.raises(SpellError):
        prep.choose(["Magic Missile", "Sleep", "Alarm"])  # Sleep is level 3
    prep.choose(["Magic Missile", "Alarm", "Charm Person"])
    assert prep.step() == "borrowed"
    assert all(s.cls == "cleric" and 0 < s.level <= 5 for s in prep.options())
    prep.choose(["Bless"])
    assert prep.step() == "prepare"
    assert [s.name for s in prep.options()] == ["Magic Missile", "Alarm", "Charm Person", "Bless"]
    prep.choose(["Magic Missile", "Bless"])
    assert prep.changes() == {
        "spells": ["Magic Missile", "Bless"], "lost": [], "cast_penalty": 0, "preparing": False,
        "spellbook": ["Magic Missile", "Alarm", "Charm Person"], "borrowed_spell": "Bless",
    }


def test_preparing_refuses_over_budget():
    prep = Preparation({**THORIN, "level": 3})  # budget 4
    prep.choose(["Cure Moderate Wounds", "Bless"])  # 3 + 1
    assert prep.changes()["spells"] == ["Cure Moderate Wounds", "Bless"]
    prep = Preparation({**THORIN, "level": 3})
    with pytest.raises(SpellError, match="6 levels"):
        prep.choose(["Cure Moderate Wounds", "Hold Person"])


def test_a_known_caster_only_prepares():
    assert Preparation(EVA).step() == "prepare"
    with pytest.raises(SpellError):
        Preparation({"name": "Dorn", "class": "Fighter"})


# -- casting ------------------------------------------------------------------------------


def test_casting_rolls_wis_for_a_cleric_and_int_for_a_wizard(director, registry):
    assert texts(cast(director, "Sanctify")) == ["**Thorin** — Cast a Spell: Sanctify, roll +Wis +2."]
    assert only_check(registry).spell == "Sanctify"
    registry.consume(only_check(registry).check_id)
    cast(director, "Invisibility", actor="Eva", user_id=BOB, target="")
    assert only_check(registry).stat == "int" and only_check(registry).modifier == 2


def test_only_a_castable_spell_can_be_cast(director, sheets):
    (note,) = notes(cast(director, "Cure Moderate Wounds"))
    assert "does not have Cure Moderate Wounds prepared" in note["error"]
    assert "Bless" in note["error"]  # what they can cast
    sheets["Thorin"]["lost"] = ["Bless"]
    (note,) = notes(cast(director, "bless", target="Thorin"))
    assert "has lost Bless until they next commune" in note["error"]
    (note,) = notes(cast(director, "Wish"))
    assert "no spell called 'Wish'" in note["error"]


def test_a_class_without_spells_cannot_cast(director, sheets):
    sheets["Thorin"]["class"] = "Fighter"
    (note,) = notes(cast(director, "Bless"))
    assert "Cast a Spell is the Cleric or Wizard's move" in note["error"]


def test_a_caster_who_never_chose_is_pointed_at_dm_spells(director, sheets):
    for key in ("spells", "lost", "cast_penalty"):
        sheets["Thorin"].pop(key)
    (note,) = notes(cast(director, "Bless"))
    assert "`?dm sheet` has the Choose spells button" in note["error"]
    assert texts(cast(director, "Light"))  # rotes need no choosing


def test_ongoing_spells_and_penalties_lower_the_roll(director, sheets, registry):
    sheets["Thorin"].update(cast_penalty=1, ongoing=[{"spell": "Bless", "target": "Eva"}])
    (ask,) = texts(cast(director, "Light"))
    assert ask == "**Thorin** — Cast a Spell: Light, roll +Wis +0 (-1 ongoing, -1 Bless)."


def test_invisibility_forbids_casting_while_it_lasts(director, sheets):
    sheets["Eva"]["ongoing"] = [{"spell": "Invisibility", "target": "Thorin"}]
    (note,) = notes(cast(director, "Magic Missile", actor="Eva", user_id=BOB, target="Goblin 1"))
    assert "While Invisibility lasts, Eva cannot cast a spell." == note["error"]


def test_a_strong_hit_casts_and_keeps_the_spell(director, dice, sheets):
    cast(director, "Bless", target="eva")
    dice.queue(5, 5)
    effects = roll(director)
    apply(effects, sheets)
    assert sheets["Thorin"]["ongoing"] == [{"spell": "Bless", "target": "Eva"}]
    (payload,) = notes(effects)
    assert payload["spell"]["cast"] is True and payload["spell"]["name"] == "Bless"
    assert "Bless lasts on Eva — -1 to cast while it does." in texts(effects)[-1]


@pytest.mark.parametrize("pick,changed", [
    (1, {"cast_penalty": 1}),
    (2, {"lost": ["Sanctify"]}),
])
def test_a_weak_hit_costs_what_the_player_picks(director, dice, registry, sheets, pick, changed):
    cast(director, "Sanctify")
    dice.queue(3, 2)  # +2: 7
    effects = roll(director)
    assert "choose one" in texts(effects)[-1]
    effects = director.choose(channel_id=CHANNEL, user_id=ALICE,
                              check_id=only_check(registry).check_id, picks=[pick])
    assert UpdateSheet(CHANNEL, "Thorin", changed) in effects


def test_a_miss_casts_nothing(director, dice, sheets):
    cast(director, "Bless", target="Thorin")
    dice.queue(1, 2)
    effects = roll(director)
    apply(effects, sheets)
    assert "ongoing" not in sheets["Thorin"]
    assert notes(effects)[0]["spell"]["cast"] is False


def test_cure_light_wounds_heals_the_ally_touched(director, dice, sheets):
    cast(director, "Cure Light Wounds", target="Eva")
    dice.queue(5, 5, 6)  # strong, then 1d8
    effects = roll(director)
    assert UpdateSheet(CHANNEL, "Eva", {"hp": 15}) in effects
    assert notes(effects)[0]["healing"]["healed"] == 6


def test_magic_missile_asks_for_damage_and_armor_applies(director, dice, registry, scene):
    cast(director, "Magic Missile", actor="Eva", user_id=BOB, target="goblin 1")
    dice.queue(5, 5)
    (ask,) = texts(roll(director, user_id=BOB))[-1:]
    assert ask == "**Eva**, roll your damage (2d4, Magic Missile) against Goblin 1."
    dice.queue(3, 2)
    effects = director.roll_damage(channel_id=CHANNEL, user_id=BOB, check_id=only_check(registry).check_id)
    assert ChangeHp(CHANNEL, "monster", "Goblin 1", 3) in effects  # 5 - armor 1
    assert "2d4: 3 + 2 = 5 → **Goblin 1** takes **4 damage** (armor 1)" in texts(effects)[0]


def test_fireball_hits_every_creature_named_and_ignores_armor(director, dice, registry, sheets, scene):
    sheets["Eva"].update(level=3, spells=["Fireball"], spellbook=["Fireball"])
    scene["Goblin 2"]["armor"] = 3
    cast(director, "Fireball", actor="Eva", user_id=BOB, target="Goblin 1, Goblin 2")
    dice.queue(5, 5, 4, 3)
    roll(director, user_id=BOB)
    effects = director.roll_damage(channel_id=CHANNEL, user_id=BOB, check_id=only_check(registry).check_id)
    assert ChangeHp(CHANNEL, "monster", "Goblin 1", 0) in effects
    assert ChangeHp(CHANNEL, "monster", "Goblin 2", 0) in effects
    damage = notes(effects)[0]["damage"]
    assert [t["dealt"] for t in damage["targets"]] == [7, 7]


def test_harm_burns_the_caster_too(director, dice, registry, sheets):
    sheets["Thorin"].update(level=7, spells=["Harm"], hp=3)
    cast(director, "Harm", target="Goblin 1")
    dice.queue(5, 5, 4, 4, 5)  # strong; 2d8; then 1d6 to Thorin
    roll(director)
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id)
    assert ChangeHp(CHANNEL, "character", "Thorin", 0) in effects
    assert texts(effects)[-1] == "**Thorin** — Last Breath, roll +nothing."


def test_casting_ends_hold_person(director, dice, sheets):
    sheets["Thorin"].update(level=3, spells=["Hold Person", "Bless"],
                            ongoing=[{"spell": "Hold Person", "target": ""}])
    cast(director, "Sanctify")
    dice.queue(5, 5)
    effects = roll(director)
    assert UpdateSheet(CHANNEL, "Thorin", {"ongoing": []}) in effects
    assert notes(effects)[0]["spell"]["ended"] == ["Hold Person"]


def test_bless_gives_its_target_plus_one_while_creatures_stand(director, sheets, registry, scene):
    sheets["Thorin"]["ongoing"] = [{"spell": "Bless", "target": "Eva"}]
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Eva",
                                      move="defy_danger", stat="dex"))
    assert ask == "**Eva** — Defy Danger, roll +Dex +1 (+1 Bless)."
    registry.consume(only_check(registry).check_id)
    scene.clear()
    (ask,) = texts(director.open_move(channel_id=CHANNEL, user_id=BOB, actor="Eva",
                                      move="defy_danger", stat="dex"))
    assert "Bless" not in ask


def test_magic_weapon_adds_1d4_to_the_casters_weapon(director, dice, registry, sheets):
    sheets["Thorin"]["ongoing"] = [{"spell": "Magic Weapon", "target": ""}]
    director.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Thorin",
                       move="hack_and_slash", target="Goblin 1")
    dice.queue(5, 5)
    roll(director)
    chosen = director.choose(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id, picks=[0])
    assert "(d6 +1d4 Magic Weapon)" in texts(chosen)[-1]
    dice.queue(2, 3)
    effects = director.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id)
    assert "d6: 2, +1d4 Magic Weapon: 3 = 5" in texts(effects)[0]
    assert ChangeHp(CHANNEL, "monster", "Goblin 1", 3) in effects


# -- Commune, Prepare Spells, end_spell ---------------------------------------------------


def test_communing_forgets_the_old_spells_and_offers_the_choice(director, scene):
    scene.clear()
    effects, result = director.prepare_spells(channel_id=CHANNEL, character="thorin",
                                              reason="the chapel, door barred")
    assert result["ok"]
    assert UpdateSheet(CHANNEL, "Thorin", spells.forgetting()) in effects
    (offer,) = [e for e in effects if isinstance(e, SendMessage)]
    assert offer.prompt == SpellsPrompt(ALICE, "Thorin")
    assert "communes with their deity — the chapel, door barred" in offer.text


def test_no_communing_mid_fight_or_without_a_reason(director, scene):
    _, result = director.prepare_spells(channel_id=CHANNEL, character="Thorin", reason="here")
    assert "can't commune with Goblin 1 and Goblin 2 still standing" in result["error"]
    scene.clear()
    _, result = director.prepare_spells(channel_id=CHANNEL, character="Thorin")
    assert "reason" in result["error"]


def test_only_casters_prepare(director, sheets, scene):
    scene.clear()
    sheets["Thorin"]["class"] = "Fighter"
    _, result = director.prepare_spells(channel_id=CHANNEL, character="Thorin", reason="x")
    assert "casts no spells" in result["error"]


def test_end_spell_takes_it_off_the_ongoing_list(director, sheets):
    sheets["Thorin"]["ongoing"] = [{"spell": "Bless", "target": "Eva"}]
    effects, result = director.end_spell(channel_id=CHANNEL, character="Thorin", spell="bless")
    assert result == {"ok": True, "ended": "Bless"}
    assert UpdateSheet(CHANNEL, "Thorin", {"ongoing": []}) in effects
    _, result = director.end_spell(channel_id=CHANNEL, character="Thorin", spell="Sleep")
    assert "no 'Sleep' running" in result["error"]


def test_describe_shows_the_spells_for_the_table():
    sheet = {**THORIN, "lost": ["Bless"], "ongoing": [{"spell": "Bless", "target": "Eva"}]}
    assert spells.describe(sheet) == (
        "Spells — Granted: Bless (lost), Cure Light Wounds · rotes: Light, Sanctify, Guidance, "
        "Words of the Unspeaking (stone) · ongoing: Bless on Eva · -1 to cast."
    )
    assert spells.describe({"class": "Fighter"}) == ""


def test_the_spell_survives_a_restart_with_the_check(director, registry):
    from gary.models import PendingCheck

    cast(director, "Bless", target="Eva")
    check = only_check(registry)
    assert PendingCheck.from_dict(check.to_dict()) == check
