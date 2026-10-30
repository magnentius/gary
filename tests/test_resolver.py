"""The soundness property: outcomes are arithmetic against a pre-committed DC.

These tests exist to catch a regression where an LLM is reintroduced into the decision
path. If any of them ever need a model to pass, the design has broken.
"""

from __future__ import annotations

import pytest

from gary.models import CheckKind, PendingCheck, Roll
from gary.resolver import outcome_for_model, resolve


def make_check(dc: int = 15) -> PendingCheck:
    return PendingCheck(
        check_id="01ABC",
        channel_id=1,
        user_id=2,
        actor="Kethra",
        kind=CheckKind.CHECK,
        key="sleight_of_hand",
        dc=dc,
        on_success="The tumblers give.",
        on_failure="The pick snaps.",
        stakes="the guard next door",
        dice="1d20",
        created_at=0.0,
        expires_at=180.0,
    )


def d20(face: int, modifier: int = 0) -> Roll:
    return Roll(spec="1d20", faces=(face,), modifier=modifier)


@pytest.mark.parametrize(
    "face,modifier,dc,succeeded,margin",
    [
        (17, 0, 15, True, 2),
        (15, 0, 15, True, 0),  # meets it, beats it
        (14, 0, 15, False, -1),
        (20, 10, 15, True, 15),
        (1, 0, 15, False, -14),
    ],
)
def test_resolution_is_arithmetic(face, modifier, dc, succeeded, margin):
    outcome = resolve(make_check(dc), d20(face, modifier))
    assert outcome.succeeded is succeeded
    assert outcome.margin == margin


def test_consequence_is_chosen_not_generated():
    check = make_check(15)
    assert resolve(check, d20(20)).consequence == check.on_success
    assert resolve(check, d20(3)).consequence == check.on_failure


def test_same_input_always_same_output():
    check = make_check(12)
    roll = d20(8, 4)
    first = resolve(check, roll)
    for _ in range(50):
        assert resolve(check, roll) == first


def test_naturals_come_from_the_face_not_the_total():
    check = make_check(15)
    assert resolve(check, d20(20, 5)).critical_success
    assert resolve(check, d20(1, 5)).critical_failure

    # A total of 20 from a 15 and a +5 is not a natural 20.
    assert not resolve(check, d20(15, 5)).critical_success


def test_several_dice_have_no_natural():
    outcome = resolve(make_check(7), Roll(spec="2d6", faces=(6, 6)))
    assert outcome.natural is None
    assert not outcome.critical_success
    assert outcome.total == 12


def test_the_faces_travel_with_the_outcome():
    """They are journalled, so the record shows the dice and not only a total."""
    assert resolve(make_check(15), d20(13, 2)).faces == (13,)


def test_payload_tells_the_model_the_result_is_settled():
    payload = outcome_for_model(resolve(make_check(15), d20(17)))
    assert payload["succeeded"] is True
    assert payload["dc"] == 15
    assert "final" in payload["instruction"].lower()
    # The model gets the consequence to narrate, never a question to answer.
    assert payload["established_consequence"] == "The tumblers give."
