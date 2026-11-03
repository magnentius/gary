"""The Director: a move is resolved only by the roll of the player it was asked of.

Gary is the only roller. These tests pin who may roll and choose, that each happens
once, and that the result is the move's band with the modifier fixed before the dice.
"""

from __future__ import annotations

import pytest

from gary.fsm import (
    ChangeHp, ChannelState, ChoicePrompt, DamagePrompt, Director, PromptModel, RecordOutcome,
    RollPrompt, SendMessage,
)

CHANNEL = 555
ALICE = 111
BOB = 222

# Kethra: STR 8 (-1), DEX 16 (+2), CON 13 (+1), INT 12 (+0), WIS 15 (+1), CHA 9 (+0)
KETHRA = {"name": "Kethra", "stats": {"str": 8, "dex": 16, "con": 13, "int": 12, "wis": 15, "cha": 9},
          "hp": 19, "max_hp": 19, "armor": 1, "damage": "d8"}
DORN = {"name": "Dorn", "stats": {"str": 16, "dex": 9, "con": 15, "int": 8, "wis": 12, "cha": 13},
        "hp": 25, "max_hp": 25, "armor": 0, "damage": "d10"}


def goblin(hp=3):
    return {"kind": "Goblin", "hp": hp, "max_hp": 3, "armor": 1, "damage": "d6",
            "piercing": 0, "ignores_armor": False, "srd": True}


@pytest.fixture
def scene():
    """The creatures present. Mutable, so a test can apply a ChangeHp the way the runner
    would and see the next read reflect it."""
    return {"Goblin 1": goblin(), "Goblin 2": goblin()}


@pytest.fixture
def director(registry, clock, dice, scene):
    """Overrides conftest's: this one knows the party's sheets and the scene."""
    sheets = {"Kethra": dict(KETHRA), "Dorn": dict(DORN)}
    return Director(
        registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
        sheet_for=sheets.get, monsters=lambda: scene,
    )


def make_move(director, move="hack_and_slash", user_id=ALICE, actor="Kethra", **kw):
    if move in ("hack_and_slash", "volley"):
        kw.setdefault("target", "Goblin 1")
    return director.open_move(channel_id=CHANNEL, user_id=user_id, actor=actor, move=move, **kw)


def roll_damage(director, check_id, user_id=ALICE):
    return director.roll_damage(channel_id=CHANNEL, user_id=user_id, check_id=check_id)


def roll(director, user_id=ALICE, check_id=None):
    return director.roll(channel_id=CHANNEL, user_id=user_id, check_id=check_id)


def choose(director, check_id, picks, user_id=ALICE):
    return director.choose(channel_id=CHANNEL, user_id=user_id, check_id=check_id, picks=picks)


def prompts_in(effects):
    return [e for e in effects if isinstance(e, PromptModel)]


def sends_in(effects):
    return [e for e in effects if isinstance(e, SendMessage)]


def only_check(registry):
    (check,) = list(registry.open_checks(CHANNEL))
    return check


# -- calling a move ------------------------------------------------------------------------


def test_a_move_asks_the_player_to_roll_with_its_stat_and_modifier(director, registry):
    (send,) = sends_in(make_move(director, "volley"))
    assert send.text == "**Kethra** — Volley at Goblin 1, roll +Dex +2."
    assert send.prompt == RollPrompt(only_check(registry).check_id)
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_the_modifier_comes_from_the_sheet_and_is_fixed_before_the_dice(director, registry):
    make_move(director, "hack_and_slash")  # Kethra's STR is 8
    check = only_check(registry)
    assert (check.stat, check.modifier) == ("str", -1)


def test_the_move_decides_the_stat_whatever_the_model_passes(director, registry):
    make_move(director, "spout_lore", stat="dex")
    assert only_check(registry).stat == "int"


def test_defy_danger_takes_the_stat_the_approach_uses(director, registry):
    make_move(director, "defy_danger", stat="wis")
    check = only_check(registry)
    assert (check.stat, check.modifier) == ("wis", 1)


def test_defy_danger_without_a_stat_goes_back_to_the_model(director):
    effects = make_move(director, "defy_danger")
    (note,) = prompts_in(effects)
    assert note.kind == "system_note" and "powering through" in note.payload["error"]
    assert not sends_in(effects)
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_aid_rolls_plus_bond_with_the_character_helped(director, registry):
    director.set_player_character(ALICE, "Kethra")
    director.set_player_character(BOB, "Dorn")
    _sheets(director)["Kethra"]["bonds"] = [
        {"line": "I stole something from ___.", "with": "Dorn"},
        {"line": "___ has my back when things go wrong.", "with": "Dorn"},
    ]
    (send,) = sends_in(make_move(director, "aid_or_interfere", target="dorn"))
    assert send.text == "**Kethra** — Aid or Interfere with Dorn, roll +Bond +2."
    assert only_check(registry).modifier == 2 and only_check(registry).target == "Dorn"


def test_aid_with_no_bonds_rolls_plus_zero(director, registry):
    director.set_player_character(ALICE, "Kethra")
    director.set_player_character(BOB, "Dorn")
    (send,) = sends_in(make_move(director, "aid_or_interfere", target="Dorn"))
    assert "+Bond +0" in send.text


def test_aid_needs_another_character(director):
    director.set_player_character(ALICE, "Kethra")
    director.set_player_character(BOB, "Dorn")
    (note,) = prompts_in(make_move(director, "aid_or_interfere"))
    assert "needs the character being helped" in note.payload["error"]
    assert "At the table: Dorn." in note.payload["error"]
    (note,) = prompts_in(make_move(director, "aid_or_interfere", target="Kethra"))
    assert "needs the character being helped" in note.payload["error"]


def test_an_unknown_move_goes_back_to_the_model(director):
    (note,) = prompts_in(make_move(director, "fireball"))
    assert note.kind == "system_note" and "hack_and_slash" in note.payload["error"]


def test_a_character_without_a_sheet_cannot_roll(director):
    """There is no stat to add. The model is told to send them to /new."""
    (note,) = prompts_in(make_move(director, "volley", actor="Liriel", user_id=BOB))
    assert "no character sheet" in note.payload["error"]
    assert "`/new`" in note.payload["instruction"]
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_without_sheets_configured_every_move_rolls_plus_zero(registry, clock, dice):
    bare = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice)
    make_move(bare, "spout_lore", actor="Anyone")
    assert only_check(registry).modifier == 0


def test_supersede_is_announced(director):
    make_move(director, "volley")
    texts = [e.text for e in sends_in(make_move(director, "defend", target="the gate"))]
    assert texts[0] == "(Superseding Kethra's pending Volley.)"


# -- rolling a move without a choice ---------------------------------------------------------


def test_a_roll_resolves_to_its_band_with_the_modifier(director, dice):
    make_move(director, "defy_danger", stat="dex")  # +2
    dice.queue(4, 4)
    effects = roll(director)

    assert sends_in(effects)[0].text == "🎲 **Kethra** rolls 4 + 4 +2 = 10 — strong hit."
    (prompt,) = prompts_in(effects)
    assert prompt.kind == "check_outcome"
    assert prompt.payload["result"] == "10+ (strong hit)"
    assert prompt.payload["roll_total"] == 10
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_a_miss_asks_for_a_gm_move(director, dice):
    make_move(director, "spout_lore")
    dice.queue(1, 2)
    (prompt,) = prompts_in(roll(director))
    assert prompt.payload["result"] == "6- (miss)"
    assert "GM move" in prompt.payload["instruction"]


def test_a_hit_that_deals_damage_asks_for_the_damage_roll_and_waits(director, registry, dice):
    make_move(director, "hack_and_slash", actor="Dorn", user_id=BOB)  # STR 16, +2
    dice.queue(3, 2)                                                   # 7: weak hit
    effects = roll(director, user_id=BOB)

    assert not prompts_in(effects)
    ask = sends_in(effects)[-1]
    assert ask.text == "**Dorn**, roll your damage (d10) against Goblin 1."
    assert ask.prompt == DamagePrompt(only_check(registry).check_id)


def test_a_roll_emits_a_record_alongside_the_narration(director, dice):
    make_move(director, "spout_lore")
    dice.queue(5, 6)
    (record,) = [e for e in roll(director) if isinstance(e, RecordOutcome)]
    assert (record.outcome.move, record.outcome.faces, record.outcome.band) == (
        "spout_lore", (5, 6), "strong"
    )


def test_the_dice_are_posted_before_the_narration_is_asked_for(director, dice):
    make_move(director, "spout_lore")
    dice.queue(5, 6)
    kinds = [type(e) for e in roll(director)]
    assert kinds.index(SendMessage) < kinds.index(PromptModel)


def test_a_move_rolls_2d6(director, dice):
    make_move(director, "spout_lore")
    dice.queue(3, 3)
    roll(director)
    assert [r.spec for r in dice.rolls] == ["2d6"]


def test_a_move_rolls_once(director, dice):
    make_move(director, "spout_lore")
    dice.queue(3, 3)
    roll(director)
    assert roll(director) == []
    assert len(dice.rolls) == 1


def test_another_player_cannot_roll_your_move(director, dice):
    make_move(director, "spout_lore")
    assert roll(director, user_id=BOB) == []
    assert dice.rolls == []
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_two_players_roll_their_own_moves(director, dice):
    make_move(director, "defy_danger", stat="dex", user_id=ALICE, actor="Kethra")  # +2
    make_move(director, "spout_lore", user_id=BOB, actor="Dorn")                   # INT 8, -1
    dice.queue(3, 3, 3, 3)

    (bob,) = prompts_in(roll(director, user_id=BOB))
    assert (bob.payload["actor"], bob.payload["roll_total"]) == ("Dorn", 5)
    alice_effects = roll(director, user_id=ALICE)
    assert "rolls 3 + 3 +2 = 8" in sends_in(alice_effects)[0].text


def test_a_stale_roll_button_does_not_roll_the_move_that_replaced_it(director, registry, dice):
    make_move(director, "volley")
    volley = only_check(registry)
    make_move(director, "defend", target="the gate")
    assert roll(director, check_id=volley.check_id) == []
    assert dice.rolls == []


def test_rolling_with_nothing_pending_does_nothing(director, dice):
    assert roll(director) == []
    assert dice.rolls == []


# -- choices -----------------------------------------------------------------------------------


def _volley_weak_hit(director, dice):
    make_move(director, "volley")  # +2
    dice.queue(3, 2)               # 7: weak hit, choose one
    return roll(director)


def test_a_result_with_a_choice_asks_for_it_and_waits(director, registry, dice):
    effects = _volley_weak_hit(director, dice)

    assert not prompts_in(effects)  # the narration waits on the choice
    assert not [e for e in effects if isinstance(e, RecordOutcome)]
    rolled, ask = sends_in(effects)
    assert rolled.text == "🎲 **Kethra** rolls 3 + 2 +2 = 7 — weak hit."
    assert ask.text == (
        "Choose one (whichever you choose you deal your damage).\nPick below, **Kethra**."
    )
    prompt = ask.prompt
    assert isinstance(prompt, ChoicePrompt)
    assert (prompt.move, prompt.band, prompt.count) == ("volley", "weak", 1)
    assert prompt.options[1] == "Take what you can get: -1d6 damage."

    held = only_check(registry)
    assert held.rolled and held.faces == (3, 2)
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL


def test_choosing_then_rolling_damage_resolves_with_both(director, registry, dice, scene):
    _volley_weak_hit(director, dice)
    check_id = only_check(registry).check_id
    effects = choose(director, check_id, [1])
    assert sends_in(effects)[0].text == "**Kethra** chooses: Take what you can get: -1d6 damage."
    assert sends_in(effects)[1].text == "**Kethra**, roll your damage (d8 -1d6) against Goblin 1."
    assert not prompts_in(effects)

    dice.queue(7, 2)  # d8 7, then the -1d6: 2 -> 5, through armor 1 -> 4
    effects = roll_damage(director, check_id)
    assert sends_in(effects)[0].text == (
        "🎲 **Kethra** damage — d8 -1d6: 7 - 2 = 5 → **Goblin 1** takes **4 damage** (armor 1), "
        "HP 3 → 0 — **defeated**."
    )
    assert ChangeHp(CHANNEL, "monster", "Goblin 1", -1) in effects
    (prompt,) = prompts_in(effects)
    assert prompt.payload["player_chose"] == ["Take what you can get: -1d6 damage."]
    assert prompt.payload["damage"]["defeated"] is True
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_a_held_move_cannot_be_rolled_again(director, dice):
    _volley_weak_hit(director, dice)
    assert roll(director) == []
    assert len(dice.rolls) == 1


def test_another_player_cannot_choose_for_you(director, registry, dice):
    _volley_weak_hit(director, dice)
    check_id = only_check(registry).check_id
    assert choose(director, check_id, [1], user_id=BOB) == []
    assert only_check(registry).rolled  # still waiting on Kethra


@pytest.mark.parametrize("picks", [[], [0, 1], [3], [-1]])
def test_the_wrong_number_of_picks_or_a_missing_option_is_refused(director, registry, dice, picks):
    _volley_weak_hit(director, dice)
    assert choose(director, only_check(registry).check_id, picks) == []
    assert only_check(registry).rolled


def test_a_choice_needs_a_roll_first(director, registry):
    make_move(director, "volley")
    assert choose(director, only_check(registry).check_id, [0]) == []


def test_choosing_happens_once(director, registry, dice):
    _volley_weak_hit(director, dice)
    check_id = only_check(registry).check_id
    choose(director, check_id, [0])
    assert choose(director, check_id, [1]) == []  # held for damage now, choice made


def test_discern_realities_on_a_hit_asks_three_distinct_questions(director, registry, dice):
    make_move(director, "discern_realities")  # WIS 15, +1
    dice.queue(5, 5)
    (_, ask) = sends_in(roll(director))
    assert ask.prompt.count == 3 and len(ask.prompt.options) == 6
    assert ask.text.endswith("Pick 3 below, **Kethra**.")

    check_id = only_check(registry).check_id
    assert choose(director, check_id, [0, 0, 1]) == []  # not distinct
    (prompt,) = prompts_in(choose(director, check_id, [5, 0, 2]))
    assert prompt.payload["player_chose"] == [  # rules order, whatever order clicked
        "What happened here recently?",
        "What should I be on the lookout for?",
        "What here is not what it appears to be?",
    ]


def test_a_strong_hack_and_slash_trades_an_attack_for_the_extra_d6(director, registry, dice, scene):
    scene["Goblin 1"] = goblin(hp=20)  # tough enough to survive, for the arithmetic
    make_move(director, "hack_and_slash", actor="Dorn", user_id=BOB)
    dice.queue(5, 5)
    roll(director, user_id=BOB)
    check_id = only_check(registry).check_id
    choose(director, check_id, [1], user_id=BOB)

    dice.queue(6, 4, 5)  # d10 6, +1d6 4 = 10, armor 1 -> 9; goblin's d6 hits back for 5
    effects = roll_damage(director, check_id, user_id=BOB)
    texts = [e.text for e in sends_in(effects)]
    assert texts[0] == ("🎲 **Dorn** damage — d10 +1d6: 6 + 4 = 10 → **Goblin 1** takes "
                        "**9 damage** (armor 1), HP 20 → 11.")
    assert texts[1] == "🗡️ **Goblin 1** hits **Dorn** for **5 damage** (d6: 5, armor 0) — HP 25 → 20."
    assert ChangeHp(CHANNEL, "character", "Dorn", 20) in effects
    (prompt,) = prompts_in(effects)
    assert "enemy makes an attack" in prompt.payload["enemy_attacks"]
    assert prompt.payload["enemy_attack"]["dealt"] == 5


def test_a_miss_offers_no_choice_and_no_damage(director, dice):
    make_move(director, "volley")
    dice.queue(1, 1)
    effects = roll(director)
    assert prompts_in(effects) and not any(s.prompt for s in sends_in(effects))


# -- expiry --------------------------------------------------------------------------------


def test_an_unrolled_move_lapses_as_a_pivot(director, clock):
    make_move(director, "spout_lore", ttl_seconds=60)
    clock.advance(61)

    (prompt,) = prompts_in(director.sweep())
    assert prompt.kind == "check_expired"
    assert prompt.payload["move"] == "Spout Lore"
    assert "Do not resolve" in prompt.payload["instruction"]
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_an_expired_move_can_no_longer_be_rolled(director, clock, dice):
    make_move(director, "spout_lore", ttl_seconds=60)
    clock.advance(61)
    assert roll(director) == []
    assert dice.rolls == []


def test_a_rolled_move_whose_choice_lapses_still_resolves_and_says_so(director, registry, clock, dice):
    """The dice already landed; the fiction has to honour them. The first option is taken,
    the damage is rolled for them, and the table is told rather than left wondering."""
    _volley_weak_hit(director, dice)
    clock.advance(registry._ttl + 1)

    dice.queue(5)
    effects = director.sweep()
    texts = [e.text for e in sends_in(effects)]
    assert texts[0] == (
        "**Kethra** didn't choose in time, so: Move to get the shot, placing yourself in "
        "danger of the GM’s choice."
    )
    assert texts[1].startswith("🎲 **Kethra** (rolled for them) damage — d8: 5")
    (prompt,) = prompts_in(effects)
    assert prompt.kind == "check_outcome"
    assert "did not choose in time" in prompt.payload["note"]
    (record,) = [e for e in effects if isinstance(e, RecordOutcome)]
    assert record.outcome.auto_chosen
    assert director.state(CHANNEL) is ChannelState.IDLE


# -- player routing ------------------------------------------------------------------------


def test_unregistered_channel_is_ignored(director):
    effects = director.on_player_message(
        channel_id=9999, user_id=ALICE, display_name="Alice",
        content="I open the door", mentions_bot=True,
    )
    assert effects == []


def test_unaddressed_chatter_does_not_reach_the_model(director):
    """Table talk is not a prompt (ARCHITECTURE.md §4.2)."""
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="brb getting coffee", mentions_bot=False,
    )
    assert effects == []


def test_mention_reaches_the_model(director):
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="@Gary I pick the lock", mentions_bot=True,
    )
    assert len(effects) == 1 and isinstance(effects[0], PromptModel)


def test_player_with_an_open_move_is_heard_without_a_mention(director):
    make_move(director, "spout_lore")
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="wait, I use my thieves' tools instead", mentions_bot=False,
    )
    assert len(effects) == 1


def test_freeform_mode_hears_everything(director):
    director.freeform_channels.add(CHANNEL)
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=BOB, display_name="Bob",
        content="I look around", mentions_bot=False,
    )
    assert len(effects) == 1


# -- state is derived, not stored ------------------------------------------------------------


def test_player_chatter_does_not_drop_the_roll_that_follows(director, dice):
    """Regression: storing channel state let ordinary table talk cancel a pending check."""
    make_move(director, "spout_lore")
    director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="Alice",
        content="rolling now!", mentions_bot=False,
    )
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL

    dice.queue(5, 5)
    assert prompts_in(roll(director)), "the roll was dropped"


def test_state_is_derived_from_the_registry(director, registry):
    """No stored copy to diverge: mutate the registry, the state follows."""
    assert director.state(CHANNEL) is ChannelState.IDLE
    check, _ = registry.open(
        channel_id=CHANNEL, user_id=ALICE, actor="Kethra", move="spout_lore",
        stat="int", modifier=0, stakes="", dice="2d6",
    )
    assert director.state(CHANNEL) is ChannelState.AWAITING_ROLL
    registry.consume(check.check_id)
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_a_fresh_director_on_the_same_registry_agrees(director, registry, clock, dice):
    """The property that makes the process replaceable: no state to hand over."""
    make_move(director, "spout_lore")
    replacement = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice)
    assert replacement.state(CHANNEL) is ChannelState.AWAITING_ROLL

    dice.queue(6, 6)
    assert prompts_in(replacement.roll(channel_id=CHANNEL, user_id=ALICE))


def test_a_held_choice_survives_a_fresh_director(director, registry, clock, dice, scene):
    _volley_weak_hit(director, dice)
    replacement = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice,
                           monsters=lambda: scene)
    effects = replacement.choose(
        channel_id=CHANNEL, user_id=ALICE, check_id=only_check(registry).check_id, picks=[2]
    )
    assert isinstance(sends_in(effects)[-1].prompt, DamagePrompt)


def test_a_director_without_dice_rolls_real_ones(registry, clock):
    """Production passes nothing and gets real dice; only tests script them."""
    from gary.ports import RandomDice

    assert isinstance(Director(registry=registry, clock=clock).dice, RandomDice)


# -- the roster: who is playing which character ------------------------------------------


def test_player_text_carries_the_character_name(director):
    director.set_player_character(ALICE, "Kethra")
    make_move(director, "spout_lore")
    effects = director.on_player_message(
        channel_id=CHANNEL, user_id=ALICE, display_name="John K",
        content="I pick the lock", mentions_bot=False,
    )
    assert prompts_in(effects)[0].payload["actor"] == "Kethra"


def test_unknown_actor_is_refused_not_opened(director):
    """Nobody could ever roll it -- a silent dead end that TTL later narrates as player
    hesitation."""
    director.set_player_character(ALICE, "Kethra")
    effects = make_move(director, "volley", actor="John K")
    (note,) = prompts_in(effects)
    assert note.kind == "system_note" and "Kethra" in note.payload["error"]
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_set_player_character_updates_validation(director):
    """?dm playing must take effect immediately, without a restart."""
    assert sends_in(make_move(director, "spout_lore"))
    director.set_player_character(ALICE, "Dorn")
    assert prompts_in(make_move(director, "spout_lore", actor="Kethra"))[0].kind == "system_note"


def test_forget_everything_drops_the_in_memory_roster(director):
    director.set_player_character(ALICE, "Kethra")
    director.forget_everything()
    assert director.players == {}
    assert sends_in(make_move(director, "spout_lore", actor="Dorn"))


# -- targets and damage ------------------------------------------------------------------


def test_an_attack_needs_a_target_in_the_scene(director):
    (note,) = prompts_in(make_move(director, "volley", target="the sentry"))
    assert note.kind == "system_note"
    assert "No creature named 'the sentry'" in note.payload["error"]
    assert "Goblin 1, Goblin 2" in note.payload["error"]
    assert "add_monsters" in note.payload["instruction"]
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_the_target_matches_regardless_of_case_and_is_stored_as_written(director, registry):
    make_move(director, "volley", target="goblin 2")
    assert only_check(registry).target == "Goblin 2"


def test_a_defeated_creature_is_not_a_target(director, scene):
    scene["Goblin 1"] = goblin(hp=0)
    (note,) = prompts_in(make_move(director, "volley", target="Goblin 1"))
    assert "Standing: Goblin 2" in note.payload["error"]


def test_moves_that_deal_no_damage_ignore_a_target(director, registry):
    make_move(director, "spout_lore", target="Goblin 1")
    assert only_check(registry).target == ""


def test_a_strong_volley_goes_straight_to_damage(director, registry, dice, scene):
    make_move(director, "volley")
    dice.queue(6, 6)
    (rolled, ask) = sends_in(roll(director))
    assert isinstance(ask.prompt, DamagePrompt)

    dice.queue(2)  # d8 2, armor 1 -> 1
    effects = roll_damage(director, only_check(registry).check_id)
    assert ChangeHp(CHANNEL, "monster", "Goblin 1", 2) in effects
    (prompt,) = prompts_in(effects)
    assert prompt.payload["damage"] == {
        "target": "Goblin 1", "rolled": 2, "armor": 1, "dealt": 1,
        "hp_before": 3, "hp_after": 2, "defeated": False, "severity": "a solid hit",
    }
    assert "enemy_attack" not in prompt.payload


def test_damage_is_rolled_once_and_only_by_the_roller(director, registry, dice):
    make_move(director, "volley")
    dice.queue(6, 6)
    roll(director)
    check_id = only_check(registry).check_id
    assert roll_damage(director, check_id, user_id=BOB) == []
    dice.queue(4)
    assert roll_damage(director, check_id)
    assert roll_damage(director, check_id) == []


def test_damage_cannot_be_rolled_before_the_choice(director, registry, dice):
    _volley_weak_hit(director, dice)
    assert roll_damage(director, only_check(registry).check_id) == []


def test_a_target_that_left_the_scene_takes_nothing(director, registry, dice, scene):
    make_move(director, "volley")
    dice.queue(6, 6)
    roll(director)
    del scene["Goblin 1"]
    dice.queue(4)
    effects = roll_damage(director, only_check(registry).check_id)
    assert not [e for e in effects if isinstance(e, ChangeHp)]
    assert "no longer in the scene" in prompts_in(effects)[0].payload["damage"]["note"]


def test_damage_that_was_never_rolled_is_rolled_for_them_on_expiry(director, registry, clock, dice):
    make_move(director, "volley")
    dice.queue(6, 6)
    roll(director)
    clock.advance(registry._ttl + 1)
    dice.queue(8)
    effects = director.sweep()
    assert sends_in(effects)[0].text.startswith("🎲 **Kethra** (rolled for them) damage — d8: 8")
    assert ChangeHp(CHANNEL, "monster", "Goblin 1", -4) in effects


# -- the GM's side --------------------------------------------------------------------------


def test_a_creature_attack_is_rolled_and_applied_by_the_rules(director, dice):
    dice.queue(4)  # goblin d6: 4, Kethra's armor 1 -> 3
    effects, facts = director.monster_attacks(channel_id=CHANNEL, monster="goblin 1", character="Kethra")
    assert sends_in(effects)[0].text == (
        "🗡️ **Goblin 1** hits **Kethra** for **3 damage** (d6: 4, armor 1) — HP 19 → 16."
    )
    assert ChangeHp(CHANNEL, "character", "Kethra", 16) in effects
    assert facts["ok"] and facts["dealt"] == 3 and facts["hp_after"] == 16
    assert facts["severity"] == "a solid hit"  # 3 of Kethra's 19
    assert "as bad as its severity" in facts["note"]


def test_piercing_and_ignoring_armor_are_honoured(director, dice, scene):
    scene["Rat 1"] = {**goblin(), "kind": "Cave Rat", "piercing": 1}
    scene["Wisp 1"] = {**goblin(), "kind": "Wisp", "ignores_armor": True}
    dice.queue(4, 4)
    _, rat = director.monster_attacks(channel_id=CHANNEL, monster="Rat 1", character="Kethra")
    _, wisp = director.monster_attacks(channel_id=CHANNEL, monster="Wisp 1", character="Kethra")
    assert (rat["dealt"], wisp["dealt"]) == (4, 4)  # armor 1 pierced, and ignored


def test_a_character_brought_to_zero_is_down_and_dying(director, dice, scene):
    scene["Ogre 1"] = {**goblin(), "kind": "Ogre", "damage": "d10+30"}
    dice.queue(5)
    effects, facts = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 1", character="Kethra")
    assert facts["down"] and "dying" in facts["dying"]
    assert ChangeHp(CHANNEL, "character", "Kethra", 0) in effects  # never below 0


@pytest.mark.parametrize("monster,character,error", [
    ("Dragon 1", "Kethra", "No standing creature named 'Dragon 1'"),
    ("Goblin 1", "Liriel", "no character sheet"),
])
def test_a_creature_attack_that_cannot_happen_goes_back_to_the_model(director, monster, character, error):
    effects, facts = director.monster_attacks(channel_id=CHANNEL, monster=monster, character=character)
    assert effects == [] and not facts["ok"] and error in facts["error"]


# -- gear --------------------------------------------------------------------------------


def _geared(director, name, items, ammo=None):
    """Give a sheet gear, the way ?dm new or ?dm gear would."""
    from gary.sheets import with_gear

    sheet = director._sheet_for(name)
    geared = with_gear(sheet, items)
    if ammo is not None:
        geared["ammo"] = ammo
    sheet.clear()
    sheet.update(geared)


def test_volley_needs_something_that_shoots(director):
    from gary.gear import LEATHER, SHORT_SWORD

    _geared(director, "Kethra", [LEATHER, SHORT_SWORD])
    (note,) = prompts_in(make_move(director, "volley"))
    assert "nothing to Volley with" in note.payload["error"]
    assert "near, far or thrown" in note.payload["error"]
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_hack_and_slash_needs_something_that_reaches(director):
    from gary.gear import BOOKS, WORN_BOW, ARROWS

    _geared(director, "Kethra", [BOOKS, WORN_BOW, ARROWS])
    (note,) = prompts_in(make_move(director, "hack_and_slash"))
    assert "hand, close or reach" in note.payload["error"]


def test_a_bow_without_ammo_cannot_volley(director):
    from gary.gear import HUNTERS_BOW

    _geared(director, "Kethra", [HUNTERS_BOW], ammo=0)
    (note,) = prompts_in(make_move(director, "volley"))
    assert "ammo" in note.payload["error"]


def test_a_sheet_made_before_gear_is_not_gated(director):
    """Thorin attacked with nothing on his sheet before gear existed; he still can."""
    assert sends_in(make_move(director, "volley"))


def test_a_weapons_damage_and_piercing_go_into_the_roll(director, registry, dice, scene):
    from gary.gear import DUELING_RAPIER, LONG_SWORD

    scene["Goblin 1"] = {**goblin(hp=20), "armor": 2}
    _geared(director, "Dorn", [DUELING_RAPIER, LONG_SWORD])  # the +1 damage sword wins
    make_move(director, "hack_and_slash", actor="Dorn", user_id=BOB)
    dice.queue(6, 6)
    roll(director, user_id=BOB)
    ask = sends_in(choose(director, only_check(registry).check_id, [0], user_id=BOB))[-1]
    assert ask.text == "**Dorn**, roll your damage (d10+1, Long sword) against Goblin 1."

    dice.queue(5)  # d10 5 +1 = 6, through armor 2 -> 4
    effects = roll_damage(director, only_check(registry).check_id, user_id=BOB)
    assert "d10+1: 5 +1 = 6 → **Goblin 1** takes **4 damage** (armor 2)" in sends_in(effects)[0].text


def test_piercing_cuts_through_armor(director, registry, dice, scene):
    from gary.gear import DUELING_RAPIER

    scene["Goblin 1"] = {**goblin(hp=20), "armor": 2}
    _geared(director, "Dorn", [DUELING_RAPIER])  # close, 1 piercing
    make_move(director, "hack_and_slash", actor="Dorn", user_id=BOB)
    dice.queue(6, 6)
    roll(director, user_id=BOB)
    choose(director, only_check(registry).check_id, [0], user_id=BOB)
    dice.queue(5)  # d10 5, armor 2 less 1 piercing -> 4
    effects = roll_damage(director, only_check(registry).check_id, user_id=BOB)
    assert "**Goblin 1** takes **4 damage** (armor 2)" in sends_in(effects)[0].text


def test_taking_several_shots_spends_one_ammo(director, registry, dice):
    from gary.fsm import SpendAmmo
    from gary.gear import ARROWS, HUNTERS_BOW

    _geared(director, "Kethra", [HUNTERS_BOW, ARROWS])  # 3 ammo
    make_move(director, "volley")
    dice.queue(3, 2)  # 7 with +2: weak hit
    roll(director)
    check_id = only_check(registry).check_id
    choose(director, check_id, [2])  # take several shots
    dice.queue(4)
    effects = roll_damage(director, check_id)
    assert SpendAmmo(CHANNEL, "Kethra", 2) in effects
    assert "**Kethra** has 2 ammo left." in [e.text for e in sends_in(effects)]


def test_other_volley_choices_spend_no_ammo(director, registry, dice):
    from gary.fsm import SpendAmmo
    from gary.gear import ARROWS, HUNTERS_BOW

    _geared(director, "Kethra", [HUNTERS_BOW, ARROWS])
    make_move(director, "volley")
    dice.queue(3, 2)
    roll(director)
    check_id = only_check(registry).check_id
    choose(director, check_id, [1])  # -1d6 damage instead
    dice.queue(4, 1)
    assert not [e for e in roll_damage(director, check_id) if isinstance(e, SpendAmmo)]


# -- Last Breath ----------------------------------------------------------------------------


def _drop(director, dice, name="Kethra", user_id=ALICE, scene=None):
    """A creature brings a character to 0; returns the effects."""
    director.set_player_character(user_id, name)
    scene["Ogre 1"] = {**goblin(), "kind": "Ogre", "damage": "d10+30"}
    dice.queue(5)
    effects, _ = director.monster_attacks(channel_id=CHANNEL, monster="Ogre 1", character=name)
    director._sheet_for(name)["hp"] = 0  # what the runner's ChangeHp does
    return effects


def test_falling_to_zero_asks_that_player_to_roll_last_breath(director, registry, dice, scene):
    effects = _drop(director, dice, scene=scene)
    ask = sends_in(effects)[-1]
    assert ask.text == "**Kethra** — Last Breath, roll +nothing."
    check = only_check(registry)
    assert (check.move, check.user_id, check.modifier) == ("last_breath", ALICE, 0)
    assert ask.prompt == RollPrompt(check.check_id)


def test_last_breath_strong_cheats_death_at_1_hp(director, registry, dice, scene):
    from gary.fsm import UpdateSheet

    _drop(director, dice, scene=scene)
    dice.queue(5, 5)
    effects = roll(director)
    assert UpdateSheet(CHANNEL, "Kethra", {"hp": 1}) in effects
    (prompt,) = prompts_in(effects)
    assert prompt.kind == "rules_event"  # never the answer to someone's pending make_move
    assert prompt.payload["result"] == "10+ (strong hit)"


def test_last_breath_weak_has_death_name_the_price_before_the_choice(director, registry, dice, scene):
    _drop(director, dice, scene=scene)
    dice.queue(4, 4)
    effects = roll(director)
    kinds = [type(e).__name__ for e in effects]
    assert kinds == ["SendMessage", "PromptModel", "SendMessage"]  # dice, the bargain, the buttons
    bargain = prompts_in(effects)[0]
    assert bargain.kind == "rules_event" and "specific bargain" in bargain.payload["instruction"]
    assert "Do NOT decide whether they take it" in bargain.payload["instruction"]
    buttons = sends_in(effects)[-1].prompt
    assert buttons.options == ("Take Death's bargain, and stabilize.",
                               "Refuse, and pass beyond the Black Gates.")
    assert only_check(registry).rolled  # waiting on the answer


def test_taking_the_bargain_stabilizes_at_1_hp(director, registry, dice, scene):
    from gary.fsm import UpdateSheet

    _drop(director, dice, scene=scene)
    dice.queue(4, 4)
    roll(director)
    effects = choose(director, only_check(registry).check_id, [0])
    assert UpdateSheet(CHANNEL, "Kethra", {"hp": 1}) in effects
    assert prompts_in(effects)[0].payload["player_chose"] == ["Take Death's bargain, and stabilize."]


def test_refusing_the_bargain_is_death(director, registry, dice, scene):
    from gary.fsm import UpdateSheet

    _drop(director, dice, scene=scene)
    dice.queue(4, 4)
    roll(director)
    effects = choose(director, only_check(registry).check_id, [1])
    assert UpdateSheet(CHANNEL, "Kethra", {"dead": True}) in effects
    assert "passes beyond the Black Gates" in sends_in(effects)[1].text


def test_a_last_breath_miss_marks_them_as_deaths_own(director, registry, dice, scene):
    from gary.fsm import UpdateSheet

    _drop(director, dice, scene=scene)
    dice.queue(1, 2)
    effects = roll(director)
    assert UpdateSheet(CHANNEL, "Kethra", {"deaths_mark": True}) in effects


def test_last_breath_is_rolled_for_them_if_nobody_clicks(director, registry, clock, dice, scene):
    """Death does not wait on a click; a rules-triggered move never just lapses."""
    from gary.fsm import UpdateSheet

    _drop(director, dice, scene=scene)
    clock.advance(registry._ttl + 1)
    dice.queue(6, 6)
    effects = director.sweep()
    assert sends_in(effects)[0].text.startswith("🎲 **Kethra** (rolled for them) rolls 6 + 6")
    assert UpdateSheet(CHANNEL, "Kethra", {"hp": 1}) in effects


def test_a_weak_hack_and_slash_that_drops_you_asks_for_last_breath_after_the_narration(
    director, registry, dice, scene
):
    scene["Ogre 1"] = {**goblin(hp=30), "kind": "Ogre", "damage": "d10+30"}
    director.set_player_character(BOB, "Dorn")
    make_move(director, "hack_and_slash", actor="Dorn", user_id=BOB, target="Ogre 1")
    dice.queue(3, 2)
    roll(director, user_id=BOB)
    dice.queue(4, 5)  # Dorn's d10, then the ogre's blow back
    effects = roll_damage(director, only_check(registry).check_id, user_id=BOB)
    kinds = [type(e).__name__ for e in effects]
    assert kinds.index("PromptModel") < len(kinds) - 1  # narration first...
    assert sends_in(effects)[-1].text == "**Dorn** — Last Breath, roll +nothing."  # ...then Death
    assert only_check(registry).move == "last_breath"


def test_the_down_and_the_dead_cannot_act(director, scene):
    director._sheet_for("Kethra")["hp"] = 0
    (note,) = prompts_in(make_move(director, "spout_lore"))
    assert "Kethra is down at 0 HP." in note.payload["error"]
    director._sheet_for("Dorn")["dead"] = True
    (note,) = prompts_in(make_move(director, "spout_lore", actor="Dorn", user_id=BOB))
    assert "Dorn is dead." in note.payload["error"]


def test_a_creature_cannot_attack_someone_already_down(director, dice):
    director._sheet_for("Kethra")["hp"] = 0
    effects, facts = director.monster_attacks(channel_id=CHANNEL, monster="Goblin 1", character="Kethra")
    assert effects == [] and "down at 0 HP" in facts["error"]


def test_the_rules_triggered_moves_cannot_be_called_by_the_model(director):
    for move in ("last_breath", "take_watch"):
        (note,) = prompts_in(make_move(director, move))
        assert "There is no move" in note.payload["error"]


# -- Make Camp ------------------------------------------------------------------------------


CAMP = "the cleared guardroom, door barred"


def _camp_party(director):
    from gary.gear import ARROWS, HUNTERS_BOW, LEATHER, RATIONS

    director.set_player_character(ALICE, "Kethra")
    director.set_player_character(BOB, "Dorn")
    _geared(director, "Kethra", [LEATHER, HUNTERS_BOW, ARROWS, RATIONS])  # 5 rations
    director._sheet_for("Kethra")["hp"] = 5   # of 19
    director._sheet_for("Dorn")["hp"] = 20    # of 25; no gear -- rations not counted


def test_a_quiet_night_feeds_and_heals_half_max_hp(director, scene):
    scene.clear()
    from gary.fsm import UpdateSheet

    _camp_party(director)
    effects, facts = director.make_camp(channel_id=CHANNEL, reason=CAMP)
    assert UpdateSheet(CHANNEL, "Kethra", {"rations": 4, "hp": 14}) in effects  # +9 of 19
    assert UpdateSheet(CHANNEL, "Dorn", {"hp": 25}) in effects  # capped at max
    assert facts["party"]["Kethra"] == {"ate": True, "rations_left": 4, "healed": 9, "hp": 14, "max_hp": 19}
    assert "eight hours" in facts["note"]
    summary = sends_in(effects)[0].text
    assert summary.startswith("🏕️ The party makes camp — the cleared guardroom, door barred")
    assert "**Kethra** eats (4 rations left) and heals 9 — HP 14/19." in summary


def test_no_ration_goes_hungry_but_heals_by_the_book(director, scene):
    """The book says to eat a ration, and sets no penalty for having none: the hungry heal, and
    the GM makes something of the hunger."""
    scene.clear()
    _camp_party(director)
    director._sheet_for("Kethra")["rations"] = 0
    effects, facts = director.make_camp(channel_id=CHANNEL, reason=CAMP)
    assert facts["party"]["Kethra"]["ate"] is False and facts["party"]["Kethra"]["healed"] > 0
    assert facts["hungry"] == ["Kethra"] and "make a GM move of it" in facts["note"]
    assert "**Kethra** goes hungry, with no rations, and heals" in sends_in(effects)[0].text


def test_an_inn_or_a_host_feeds_them_without_rations(director, scene):
    """"Only mark off a ration if you're eating from the food you carry, not paying for a meal or
    receiving hospitality." """
    scene.clear()
    from gary.fsm import UpdateSheet

    _camp_party(director)
    effects, facts = director.make_camp(channel_id=CHANNEL, reason="the Drowned Rat's back room", fed_by_host=True)
    assert not any(isinstance(e, UpdateSheet) and "rations" in e.changes for e in effects)
    assert "eats at their host's table" in sends_in(effects)[0].text and "hungry" not in facts


def test_something_in_the_night_means_take_watch_and_no_healing(director, registry, scene):
    scene.clear()
    from gary.fsm import UpdateSheet

    _camp_party(director)
    effects, facts = director.make_camp(channel_id=CHANNEL, approaches=True, watcher="dorn", reason=CAMP)
    assert UpdateSheet(CHANNEL, "Kethra", {"rations": 4}) in effects  # ate, did not heal
    assert not any(isinstance(e, UpdateSheet) and "hp" in e.changes for e in effects)
    assert sends_in(effects)[-1].text == "**Dorn** — Take Watch, roll +Wis +0."
    check = only_check(registry)
    assert (check.move, check.user_id) == ("take_watch", BOB)
    assert "nobody heals" in facts["note"]


def test_take_watch_resolves_as_a_rules_event(director, registry, dice, scene):
    scene.clear()
    _camp_party(director)
    director.make_camp(channel_id=CHANNEL, approaches=True, watcher="Dorn", reason=CAMP)
    dice.queue(5, 5)
    (prompt,) = prompts_in(roll(director, user_id=BOB))
    assert prompt.kind == "rules_event"
    assert "camp takes +1 forward" in prompt.payload["what_it_means"]


def test_an_interrupted_night_needs_a_watcher_from_the_party(director, scene):
    scene.clear()
    _camp_party(director)
    effects, facts = director.make_camp(channel_id=CHANNEL, approaches=True, watcher="Gandalf", reason=CAMP)
    assert effects == [] and "Name who is on watch" in facts["error"]


def test_the_dead_do_not_camp(director, scene):
    scene.clear()
    _camp_party(director)
    director._sheet_for("Dorn")["dead"] = True
    _, facts = director.make_camp(channel_id=CHANNEL, reason=CAMP)
    assert set(facts["party"]) == {"Kethra"}



# -- when you cannot rest ---------------------------------------------------------------------


def test_no_rest_with_creatures_still_standing(director, scene):
    _camp_party(director)
    effects, facts = director.make_camp(channel_id=CHANNEL, reason=CAMP)
    assert effects == [] and not facts["ok"]
    assert "Goblin 1 and Goblin 2 still standing" in facts["error"]


def test_the_defeated_do_not_stop_a_rest(director, scene):
    _camp_party(director)
    scene["Goblin 1"]["hp"] = 0
    scene["Goblin 2"]["hp"] = -2
    _, facts = director.make_camp(channel_id=CHANNEL, reason=CAMP)
    assert facts["ok"]


def test_no_rest_while_a_last_breath_is_unresolved(director, dice, scene):
    _drop(director, dice, scene=scene)
    scene.clear()
    _camp_party(director)
    effects, facts = director.make_camp(channel_id=CHANNEL, reason=CAMP)
    assert effects == [] and "Kethra's Last Breath is still to be resolved" in facts["error"]


def test_no_rest_mid_move(director, scene):
    scene.clear()
    _camp_party(director)
    make_move(director, "spout_lore")
    _, facts = director.make_camp(channel_id=CHANNEL, reason=CAMP)
    assert "Kethra's Spout Lore" in facts["error"]


def test_a_rest_must_say_where_and_why_it_is_safe(director, scene):
    scene.clear()
    _camp_party(director)
    effects, facts = director.make_camp(channel_id=CHANNEL, reason="   ")
    assert effects == [] and "why it is safe" in facts["error"]


# -- class moves ----------------------------------------------------------------------------


def _sheets(director):
    """The fixture's sheets, to add or change characters in place."""
    return director._sheet_for.__self__


def _thief(director, user_id=ALICE, **extra):
    _sheets(director)["Kethra"].update({"class": "Thief", "race": "Human", **extra})
    director.set_player_character(user_id, "Kethra")


def test_a_class_move_is_that_classes_alone(director):
    _sheets(director)["Dorn"]["class"] = "Fighter"
    (note,) = prompts_in(make_move(director, "backstab", actor="Dorn", user_id=BOB, target="Goblin 1"))
    assert "Backstab is the Thief's move; Dorn is a Fighter." in note.payload["error"]


def test_a_class_move_rolls_like_any_other(director, registry, dice):
    _sheets(director)["Kethra"]["class"] = "Cleric"
    (ask,) = sends_in(make_move(director, "turn_undead"))
    assert ask.text == "**Kethra** — Turn Undead, roll +Wis +1."
    dice.queue(4, 4)
    (prompt,) = prompts_in(roll(director))
    assert "no undead may come within reach" in prompt.payload["what_it_means"]


def test_backstab_deals_damage_only_if_the_thief_picks_it(director, registry, dice):
    _thief(director)
    make_move(director, "backstab", target="Goblin 1")
    dice.queue(5, 5)
    (_, ask) = sends_in(roll(director))
    assert ask.prompt.count == 2
    check_id = only_check(registry).check_id
    effects = choose(director, check_id, [1, 3])  # +1d6 damage, reduce armor
    assert isinstance(sends_in(effects)[-1].prompt, DamagePrompt)
    assert "(d8 +1d6)" in sends_in(effects)[-1].text


def test_backstab_without_the_damage_pick_resolves_at_once(director, registry, dice):
    _thief(director)
    make_move(director, "backstab", target="Goblin 1")
    dice.queue(5, 5)
    roll(director)
    effects = choose(director, only_check(registry).check_id, [0, 2])
    assert prompts_in(effects) and not any(s.prompt for s in sends_in(effects))


def test_called_shot_needs_something_that_shoots_and_a_target(director):
    from gary.gear import SHORT_SWORD

    _sheets(director)["Kethra"]["class"] = "Ranger"
    _geared(director, "Kethra", [SHORT_SWORD])
    (note,) = prompts_in(make_move(director, "called_shot", target="Goblin 1"))
    assert "near, far or thrown" in note.payload["error"]


def test_lay_on_hands_heals_the_named_ally(director, registry, dice):
    from gary.fsm import UpdateSheet

    _sheets(director)["Dorn"].update({"class": "Paladin", "hp": 25})
    _sheets(director)["Kethra"]["hp"] = 10
    director.set_player_character(BOB, "Dorn")
    director.set_player_character(ALICE, "Kethra")  # as ?dm new would have
    (ask,) = sends_in(make_move(director, "lay_on_hands", actor="Dorn", user_id=BOB, target="kethra"))
    assert ask.text.startswith("**Dorn** — Lay on Hands on Kethra, roll +Cha")
    dice.queue(5, 5, 6)  # strong, then the 1d8
    effects = roll(director, user_id=BOB)
    assert UpdateSheet(CHANNEL, "Kethra", {"hp": 16}) in effects
    assert prompts_in(effects)[0].payload["healing"]["healed"] == 6


def test_lay_on_hands_on_a_7_to_9_moves_the_wound_onto_the_paladin(director, registry, dice):
    from gary.fsm import UpdateSheet

    _sheets(director)["Dorn"].update({"class": "Paladin", "hp": 4})
    _sheets(director)["Kethra"]["hp"] = 10
    director.set_player_character(BOB, "Dorn")
    make_move(director, "lay_on_hands", actor="Dorn", user_id=BOB, target="Kethra")
    dice.queue(3, 3, 6)  # CHA 13 +1: 7, weak; heals 6
    effects = roll(director, user_id=BOB)
    assert UpdateSheet(CHANNEL, "Kethra", {"hp": 16}) in effects
    assert UpdateSheet(CHANNEL, "Dorn", {"hp": 0}) in effects  # 4 - 6, floored
    assert sends_in(effects)[-1].text == "**Dorn** — Last Breath, roll +nothing."


def test_arcane_art_heals_only_if_that_effect_is_picked(director, registry, dice):
    from gary.fsm import UpdateSheet

    _sheets(director)["Kethra"].update({"class": "Bard", "hp": 19})
    _sheets(director)["Dorn"]["hp"] = 10
    make_move(director, "arcane_art", target="Dorn")
    dice.queue(5, 5)
    roll(director)
    dice.queue(5)
    effects = choose(director, only_check(registry).check_id, [0])
    assert UpdateSheet(CHANNEL, "Dorn", {"hp": 15}) in effects


def test_a_precise_weapon_hacks_with_dex(director, registry):
    from gary.gear import RAPIER

    _geared(director, "Kethra", [RAPIER])  # STR 8 (-1), DEX 16 (+2)
    (ask,) = sends_in(make_move(director, "hack_and_slash"))
    assert ask.text == "**Kethra** — Hack and Slash at Goblin 1, roll +Dex +2."
    assert only_check(registry).stat == "dex"


def test_a_halfling_thief_deals_2_more_at_range(director, registry, dice):
    from gary.gear import ARROWS, HUNTERS_BOW

    _thief(director, race="Halfling")
    _geared(director, "Kethra", [HUNTERS_BOW, ARROWS])
    make_move(director, "volley")
    dice.queue(6, 6)
    assert "(d8+2, Hunter's bow)" in sends_in(roll(director))[-1].text


def test_a_druid_needs_no_ration_and_a_halfling_druid_sings_the_camp_healthy(director, scene, dice):
    scene.clear()
    _camp_party(director)
    _sheets(director)["Dorn"].update({"class": "Druid", "race": "Halfling", "gear": [], "rations": 0})
    dice.queue(3)
    effects, facts = director.make_camp(channel_id=CHANNEL, reason=CAMP)
    assert facts["party"]["Dorn"]["ate"] is True  # By Nature Sustained
    assert facts["party"]["Kethra"]["healed"] == 9 + 3  # half of 19, plus the song
    assert "healing song" in sends_in(effects)[0].text


# -- XP --------------------------------------------------------------------------------------


def test_a_miss_marks_xp(director, dice):
    from gary.fsm import UpdateSheet

    _sheets(director)["Kethra"].update(level=1, xp=4)
    make_move(director, "defy_danger", stat="dex")
    dice.queue(1, 2)
    effects = roll(director)
    assert UpdateSheet(CHANNEL, "Kethra", {"xp": 5}) in effects
    assert "**Kethra** marks XP — 5/8 XP." in [e.text for e in sends_in(effects)]


def test_a_hit_marks_no_xp(director, dice):
    from gary.fsm import UpdateSheet

    make_move(director, "defy_danger", stat="dex")
    dice.queue(5, 5)
    assert not [e for e in roll(director) if isinstance(e, UpdateSheet)]


def test_enough_xp_says_ready_to_level_up(director, dice):
    _sheets(director)["Kethra"].update(level=1, xp=7)
    make_move(director, "defy_danger", stat="dex")
    dice.queue(1, 1)
    assert ("**Kethra** marks XP — 8/8 XP, ready to level up. When there's downtime, `/sheet` has "
            "the Level up button.") in [e.text for e in sends_in(roll(director))]


# -- the weapon they say they use ----------------------------------------------------------

def test_a_named_weapon_is_the_one_used(director, registry, dice, scene):
    from gary.gear import DAGGER, LONG_SWORD

    scene["Goblin 1"] = goblin(hp=20)
    _geared(director, "Dorn", [LONG_SWORD, DAGGER])  # the sword is better, but they're grappling
    make_move(director, "hack_and_slash", actor="Dorn", user_id=BOB, weapon="dagger")
    dice.queue(6, 6)
    roll(director, user_id=BOB)
    ask = sends_in(choose(director, only_check(registry).check_id, [0], user_id=BOB))[-1]
    assert ask.text == "**Dorn**, roll your damage (d10, Dagger) against Goblin 1."


def test_a_weapon_they_dont_carry_or_that_doesnt_fit_is_refused(director):
    from gary.gear import HUNTERS_BOW, LONG_SWORD

    _geared(director, "Dorn", [LONG_SWORD, HUNTERS_BOW])
    (note,) = prompts_in(make_move(director, "hack_and_slash", actor="Dorn", user_id=BOB, weapon="axe"))
    assert "no 'axe'" in note.payload["error"] and "Long sword" in note.payload["error"]
    (note,) = prompts_in(make_move(director, "hack_and_slash", actor="Dorn", user_id=BOB, weapon="bow"))
    assert "no 'bow'" in note.payload["error"]


def test_a_thrown_spear_is_gone_once_thrown_and_still_deals_its_damage(director, registry, dice, scene):
    """Thrown: "once you throw it, it's gone until you can recover it" -- whatever the roll."""
    from gary.fsm import SpendAmmo, UpdateSheet
    from gary.gear import ARROWS, DRUID_SPEAR, HUNTERS_BOW

    scene["Goblin 1"] = goblin(hp=20)
    _geared(director, "Kethra", [HUNTERS_BOW, ARROWS, DRUID_SPEAR])
    sheet = director._sheet_for("Kethra")
    make_move(director, "volley", weapon="spear")
    dice.queue(6, 6)
    effects = roll(director)
    (update,) = [e for e in effects if isinstance(e, UpdateSheet)]
    assert [g["name"] for g in update.changes["gear"]] == ["Hunter's bow", "Bundle of arrows"]
    assert not [e for e in effects if isinstance(e, SpendAmmo)]  # the arrows are untouched
    assert any("spear is thrown" in e.text for e in sends_in(effects))
    sheet["gear"] = update.changes["gear"]  # as the runner would
    assert any("roll your damage" in e.text and "Spear" in e.text for e in sends_in(effects))
    dice.queue(4)
    hit = sends_in(roll_damage(director, only_check(registry).check_id))[0].text
    assert "Goblin 1** takes" in hit  # the spear's blow lands, though it's left the sheet


def test_a_thrown_weapon_cannot_take_several_shots(director, registry, dice):
    from gary.gear import DRUID_SPEAR

    _geared(director, "Kethra", [DRUID_SPEAR])
    make_move(director, "volley")
    dice.queue(3, 2)  # weak hit
    roll(director)
    check_id = only_check(registry).check_id
    (said,) = sends_in(choose(director, check_id, [2]))  # take several shots: refused
    assert "can't take several shots" in said.text and not only_check(registry).picks


def test_throwing_daggers_lose_one_a_throw(director, dice):
    from gary.fsm import SpendAmmo
    from gary.gear import THROWING_DAGGERS

    _geared(director, "Kethra", [THROWING_DAGGERS])  # 3
    make_move(director, "volley")
    dice.queue(1, 1)  # even a miss: thrown is thrown
    assert SpendAmmo(CHANNEL, "Kethra", 2) in roll(director)


def test_their_best_never_throws_a_weapon_away_unasked():
    from gary import gear

    kit = [gear.HUNTERS_BOW.to_dict(), gear.ARROWS.to_dict(), gear.DRUID_SPEAR.to_dict()]
    assert gear.weapon_for(kit, ranged=True, ammo=3).name == "Hunter's bow"
