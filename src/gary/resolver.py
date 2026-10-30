"""Check resolution (ARCHITECTURE.md §5.2).

The whole point of this module is that it is boring. Success is arithmetic against a DC
that was written down before the dice were touched. No model is consulted, so no model
can rationalise a total into the outcome it would rather narrate, and no context
truncation can lose the DC.

If this file ever grows a call to an LLM, the design has regressed.
"""

from __future__ import annotations

from .models import CheckOutcome, PendingCheck, Roll


def resolve(check: PendingCheck, roll: Roll) -> CheckOutcome:
    margin = roll.total - check.dc
    succeeded = margin >= 0
    return CheckOutcome(
        check_id=check.check_id,
        actor=check.actor,
        kind=check.kind,
        key=check.key,
        total=roll.total,
        dc=check.dc,
        margin=margin,
        succeeded=succeeded,
        consequence=check.on_success if succeeded else check.on_failure,
        natural=roll.natural,
        faces=roll.faces,
    )


def outcome_for_model(outcome: CheckOutcome) -> dict:
    """The structured payload handed back to the model.

    Delivered as a tool result, never as prose in a user turn (ARCHITECTURE.md §9.1) -- the
    transport is what carries the trust. Phrased as settled fact, and deliberately does
    not include anything the model could read as an invitation to re-adjudicate.
    """
    return {
        "check_id": outcome.check_id,
        "actor": outcome.actor,
        "kind": outcome.kind.value,
        "key": outcome.key,
        "roll_total": outcome.total,
        "dc": outcome.dc,
        "margin": outcome.margin,
        "succeeded": outcome.succeeded,
        "critical_success": outcome.critical_success,
        "critical_failure": outcome.critical_failure,
        "established_consequence": outcome.consequence,
        "instruction": (
            "This outcome is final and was determined by the rules engine. "
            "Narrate the established consequence. Do not restate the DC or the "
            "arithmetic, and do not reverse or soften the result."
        ),
    }
