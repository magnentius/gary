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


def test_aid_rolls_plus_zero_because_bonds_are_not_tracked(director, registry):
    (send,) = sends_in(make_move(director, "aid_or_interfere"))
    assert "+Bond (not tracked yet, so +0)" in send.text
    assert only_check(registry).modifier == 0


def test_an_unknown_move_goes_back_to_the_model(director):
    (note,) = prompts_in(make_move(director, "backstab"))
    assert note.kind == "system_note" and "hack_and_slash" in note.payload["error"]


def test_a_character_without_a_sheet_cannot_roll(director):
    """There is no stat to add. The model is told to send them to ?dm new."""
    (note,) = prompts_in(make_move(director, "volley", actor="Liriel", user_id=BOB))
    assert "no character sheet" in note.payload["error"]
    assert "?dm new" in note.payload["instruction"]
    assert director.state(CHANNEL) is ChannelState.IDLE


def test_without_sheets_configured_every_move_rolls_plus_zero(registry, clock, dice):
    bare = Director(registry=registry, clock=clock, game_channels={CHANNEL}, dice=dice)
    make_move(bare, "spout_lore", actor="Anyone")
    assert only_check(registry).modifier == 0


def test_supersede_is_announced(director):
    make_move(director, "volley")
    texts = [e.text for e in sends_in(make_move(director, "defend"))]
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
    make_move(director, "defend")
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
        "🎲 **Kethra** damage — d8 -1d6: 7 - 2 = 5 → Goblin 1: 4 through armor 1, "
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
    assert texts[0] == ("🎲 **Dorn** damage — d10 +1d6: 6 + 4 = 10 → Goblin 1: 9 through "
                        "armor 1, HP 20 → 11.")
    assert texts[1] == "🗡️ **Goblin 1** hits **Dorn** — d6: 5 → 5 through armor 0, HP 25 → 20."
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
        "🗡️ **Goblin 1** hits **Kethra** — d6: 4 → 3 through armor 1, HP 19 → 16."
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
