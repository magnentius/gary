"""The soundness property: a move's result is its band, fixed before the dice.

These tests exist to catch a regression where an LLM is reintroduced into the decision
path. If any of them ever need a model to pass, the design has broken.
"""

from __future__ import annotations

import pytest

from gary.models import PendingCheck, Roll
from gary.moves import MOVES
from gary.resolver import band, outcome_for_model, resolve


def make_check(move="hack_and_slash", modifier=0) -> PendingCheck:
    return PendingCheck(
        check_id="01ABC",
        channel_id=1,
        user_id=2,
        actor="Kethra",
        move=move,
        stat=MOVES[move].stat,
        modifier=modifier,
        stakes="the guard next door",
        dice="2d6",
        created_at=0.0,
        expires_at=180.0,
    )


def two_d6(a: int, b: int, modifier: int = 0) -> Roll:
    return Roll(spec="2d6", faces=(a, b), modifier=modifier)


@pytest.mark.parametrize("faces,modifier,expected", [
    ((6, 6), 0, "strong"),
    ((5, 5), 0, "strong"),   # 10 is a strong hit
    ((4, 5), 0, "weak"),     # 9
    ((3, 4), 0, "weak"),     # 7 is a weak hit
    ((3, 3), 0, "miss"),     # 6 misses
    ((1, 1), 0, "miss"),
    ((4, 4), 2, "strong"),   # the modifier counts: 8 + 2
    ((4, 4), -2, "miss"),    # and against you: 8 - 2
])
def test_the_band_is_arithmetic(faces, modifier, expected):
    assert band(two_d6(*faces, modifier)) == expected


def test_the_rules_text_is_chosen_not_generated():
    check = make_check("defy_danger")
    assert resolve(check, two_d6(6, 6)).text == MOVES["defy_danger"].strong.text
    assert resolve(check, two_d6(4, 4)).text == MOVES["defy_danger"].weak.text
    assert resolve(check, two_d6(1, 2)).text == MOVES["defy_danger"].miss.text


def test_same_input_always_same_output():
    check = make_check()
    roll = two_d6(3, 5, 1)
    first = resolve(check, roll)
    for _ in range(50):
        assert resolve(check, roll) == first


def test_the_faces_and_modifier_travel_with_the_outcome():
    """They are journalled, so the record shows the dice and not only a total."""
    outcome = resolve(make_check(modifier=2), two_d6(3, 5, 2))
    assert (outcome.faces, outcome.modifier, outcome.total) == ((3, 5), 2, 10)


# -- the enemy attacks: the rules decide, not the model ------------------------------------


def test_a_weak_hack_and_slash_means_the_enemy_attacks():
    assert resolve(make_check(), two_d6(4, 4)).enemy_attacks


def test_a_strong_hack_and_slash_avoids_the_attack_unless_they_pick_the_extra_damage():
    check = make_check()
    assert not resolve(check, two_d6(6, 6), picks=[0]).enemy_attacks
    assert resolve(check, two_d6(6, 6), picks=[1]).enemy_attacks


def test_a_miss_carries_no_attack_flag_because_the_gm_chooses_the_move():
    assert not resolve(make_check(), two_d6(1, 1)).enemy_attacks


# -- what the player chose -----------------------------------------------------------------


def test_picks_become_the_options_text():
    outcome = resolve(make_check("discern_realities"), two_d6(6, 6), picks=[0, 2, 5])
    assert outcome.chosen == (
        "What happened here recently?",
        "What should I be on the lookout for?",
        "What here is not what it appears to be?",
    )


def test_payload_tells_the_model_the_result_is_settled():
    payload = outcome_for_model(resolve(make_check("volley"), two_d6(4, 4), picks=[1]))
    assert payload["move"] == "Volley"
    assert payload["result"] == "7-9 (weak hit)"
    assert payload["player_chose"] == ["Take what you can get: -1d6 damage."]
    assert "final" in payload["instruction"].lower()


def test_a_miss_tells_the_model_to_make_a_gm_move():
    payload = outcome_for_model(resolve(make_check(), two_d6(1, 2)))
    assert payload["result"] == "6- (miss)"
    assert "GM move" in payload["instruction"]


def test_an_enemy_attack_is_stated_as_fact():
    payload = outcome_for_model(resolve(make_check(), two_d6(4, 4)))
    assert "enemy makes an attack" in payload["enemy_attacks"]


def test_an_auto_choice_is_disclosed():
    payload = outcome_for_model(
        resolve(make_check("volley"), two_d6(4, 4), picks=[0], auto_chosen=True)
    )
    assert "did not choose in time" in payload["note"]
