"""The advanced moves with a roll of their own, read from the book's data."""

from __future__ import annotations


from gary import class_moves
from gary.fsm import Director, SendMessage, UpdateSheet
from gary.moves import ADVANCED_ROLLS, MOVES, STRONG

CHANNEL, ALICE = 555, 111
STATS = {"str": 10, "dex": 16, "con": 15, "int": 16, "wis": 15, "cha": 13}


def test_every_one_is_read_from_the_book_the_compendium_and_the_playbooks():
    assert sorted(ADVANCED_ROLLS) == sorted([
        "it_goes_to_eleven", "metal_hurlant", "reputation", "communion_of_whispers", "elemental_mastery",
        "heirloom", "evil_eye", "through_deaths_eyes", "connections", "escape_route", "counterspell",
        "bind", "diplomat",  # from compendium classes (Flux, Landed Gentry)
        "what_are_you_waiting_for", "war_cry", "for_the_blood_god"])  # the Barbarian
    assert MOVES["bind"].stat == "con" and MOVES["bind"].strong.choice.count == 3
    assert MOVES["diplomat"].weak.text.startswith("they name some terms")
    eye = MOVES["evil_eye"]
    assert (eye.stat, eye.trigger, eye.strong.text) == ("cha", "enter combat", "hold 2.")
    assert eye.miss.text == "your enemies immediately identify you as their biggest threat."
    mastery = MOVES["elemental_mastery"]
    assert mastery.band(STRONG).choice.count == 2 and mastery.weak.choice.count == 1
    assert [o.text for o in mastery.weak.choice.options][0] == "The effect you desire comes to pass"


def sheet(cls, moves, **more):
    return {"name": "Eva", "class": cls, "race": "Human", "level": 4, "hp": 15, "max_hp": 15, "stats": STATS,
            "armor": 0, "damage": "d6", "advanced_moves": moves, **more}


def director_for(registry, clock, dice, eva, scene=None):
    return Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice, players={ALICE: "Eva"},
                    sheet_for={"Eva": eva}.get, monsters=lambda: scene or {})


def test_only_someone_with_the_move_can_make_it(registry, clock, dice):
    d = director_for(registry, clock, dice, sheet("Thief", []))
    (note,) = d.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="escape_route")
    assert "Escape Route is an advanced move Eva has not taken" in note.payload["error"]


def test_escape_route_rolls_dex(registry, clock, dice):
    d = director_for(registry, clock, dice, sheet("Thief", ["Escape Route"]))
    (ask,) = [e.text for e in d.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="escape_route")
              if isinstance(e, SendMessage)]
    assert "Escape Route" in ask and "+Dex +2" in ask
    assert "Escape Route (advanced):" in class_moves.render({"Eva": sheet("Thief", ["Escape Route"])})
    assert "move=escape_route" in class_moves.render({"Eva": sheet("Thief", ["Escape Route"])})


def test_metal_hurlant_deals_its_own_d10_without_a_weapon(registry, clock, dice):
    scene = {"Ogre 1": {"kind": "Ogre", "hp": 20, "max_hp": 20, "armor": 1, "damage": "d10"}}
    d = director_for(registry, clock, dice, sheet("Bard", ["Metal Hurlant"], gear=[]), scene)
    d.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="metal_hurlant", target="Ogre 1")
    dice.queue(5, 5)
    d.roll(channel_id=CHANNEL, user_id=ALICE)
    check = next(iter(registry.open_checks(CHANNEL)))
    dice.queue(8)
    effects = d.roll_damage(channel_id=CHANNEL, user_id=ALICE, check_id=check.check_id)
    assert any("d10: 8" in e.text and "7 damage" in e.text for e in effects if isinstance(e, SendMessage))


def test_counterspell_stakes_a_prepared_spell_and_a_7_9_forgets_it(registry, clock, dice):
    eva = sheet("Wizard", ["Counterspell"], spellbook=["Magic Missile", "Sleep"], spells=["Magic Missile"],
                lost=[], cast_penalty=0)
    d = director_for(registry, clock, dice, eva)
    (note,) = d.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="counterspell", spell="Sleep")
    assert "Prepared: Magic Missile" in note.payload["error"]
    d.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="counterspell", spell="magic missile")
    dice.queue(3, 3)  # 6 + 2 = 8
    effects = d.roll(channel_id=CHANNEL, user_id=ALICE)
    assert UpdateSheet(CHANNEL, "Eva", {"lost": ["Magic Missile"]}) in effects


def test_a_troubling_vision_is_minus_one_forward(registry, clock, dice):
    d = director_for(registry, clock, dice, sheet("Druid", ["Communion of Whispers"]))
    d.open_move(channel_id=CHANNEL, user_id=ALICE, actor="Eva", move="communion_of_whispers")
    dice.queue(1, 1)
    effects = d.roll(channel_id=CHANNEL, user_id=ALICE)
    assert any(isinstance(e, UpdateSheet) and e.changes.get("forwards", [{}])[0].get("amount") == -1 for e in effects)
