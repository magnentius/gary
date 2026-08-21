"""The soundness property: outcomes are arithmetic against a pre-committed DC.

These tests exist to catch a regression where an LLM is reintroduced into the decision
path. If any of them ever need a model to pass, the design has broken.
"""

from __future__ import annotations

import pytest

from gary.models import AvraeRoll, CheckKind, PendingCheck
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
        command='!check "sleight of hand"',
        created_at=0.0,
        expires_at=180.0,
    )


@pytest.mark.parametrize(
    "total,dc,succeeded,margin",
    [
        (17, 15, True, 2),
        (15, 15, True, 0),  # meets it, beats it
        (14, 15, False, -1),
        (30, 15, True, 15),
        (1, 15, False, -14),
    ],
)
def test_resolution_is_arithmetic(total, dc, succeeded, margin):
    outcome = resolve(make_check(dc), AvraeRoll(actor="Kethra", total=total))
    assert outcome.succeeded is succeeded
    assert outcome.margin == margin


def test_consequence_is_chosen_not_generated():
    check = make_check(15)
    assert resolve(check, AvraeRoll("Kethra", 20)).consequence == check.on_success
    assert resolve(check, AvraeRoll("Kethra", 3)).consequence == check.on_failure


def test_same_input_always_same_output():
    check = make_check(12)
    roll = AvraeRoll("Kethra", 12, natural=8)
    first = resolve(check, roll)
    for _ in range(50):
        assert resolve(check, roll) == first


def test_naturals_are_reported_not_inferred():
    check = make_check(15)
    assert resolve(check, AvraeRoll("Kethra", 25, natural=20)).critical_success
    assert resolve(check, AvraeRoll("Kethra", 6, natural=1)).critical_failure

    # A total of 25 does not imply a nat 20. Without a parsed face, claim nothing.
    blind = resolve(check, AvraeRoll("Kethra", 25))
    assert blind.natural is None
    assert not blind.critical_success


def test_payload_tells_the_model_the_result_is_settled():
    payload = outcome_for_model(resolve(make_check(15), AvraeRoll("Kethra", 17)))
    assert payload["succeeded"] is True
    assert payload["dc"] == 15
    assert "final" in payload["instruction"].lower()
    # The model gets the consequence to narrate, never a question to answer.
    assert payload["established_consequence"] == "The tumblers give."
