"""Move resolution (ARCHITECTURE.md §5.2).

The whole point of this module is that it is boring. A move's result is its band -- 10+,
7-9, 6- -- and what each band means was written down before the dice were touched, in the
move itself. No model is consulted, so no model can rationalise a total into the outcome
it would rather narrate.

If this file ever grows a call to an LLM, the design has regressed.
"""

from __future__ import annotations

from typing import Sequence

from .models import MoveOutcome, PendingCheck, Roll
from .moves import MOVES, band_of


def band(roll: Roll) -> str:
    return band_of(roll.total)


def resolve(
    check: PendingCheck, roll: Roll, picks: Sequence[int] = (), auto_chosen: bool = False
) -> MoveOutcome:
    """The outcome of a rolled move, with the player's picks if its band offers a choice.

    ``picks`` are indices into the band's options, already validated by the caller.
    """
    move = MOVES[check.move]
    result = band(roll)
    rules = move.band(result)
    options = rules.choice.options if rules.choice else ()
    chosen = [options[i] for i in picks]
    return MoveOutcome(
        check_id=check.check_id,
        actor=check.actor,
        move=check.move,
        stat=check.stat,
        modifier=check.modifier,
        faces=roll.faces,
        total=roll.total,
        band=result,
        text=rules.text,
        chosen=tuple(o.text for o in chosen),
        enemy_attacks=rules.enemy_attacks or any(o.enemy_attacks for o in chosen),
        auto_chosen=auto_chosen,
    )


_BAND_WORDS = {"strong": "10+ (strong hit)", "weak": "7-9 (weak hit)", "miss": "6- (miss)"}


def outcome_for_model(outcome: MoveOutcome) -> dict:
    """The structured payload handed back to the model.

    Delivered as a tool result, never as prose in a user turn (ARCHITECTURE.md §9.1) -- the
    transport is what carries the trust. Phrased as settled fact, and deliberately does
    not include anything the model could read as an invitation to re-adjudicate.
    """
    move = MOVES[outcome.move]
    payload = {
        "check_id": outcome.check_id,
        "actor": outcome.actor,
        "move": move.name,
        "stat": outcome.stat,
        "roll_total": outcome.total,
        "result": _BAND_WORDS[outcome.band],
        "what_it_means": outcome.text,
    }
    if outcome.chosen:
        payload["player_chose"] = list(outcome.chosen)
    if outcome.auto_chosen:
        payload["note"] = "The player did not choose in time, so the first options were taken."
    if outcome.damage:
        payload["damage"] = outcome.damage
    if outcome.enemy_attacks:
        payload["enemy_attacks"] = (
            "The rules say the enemy makes an attack against them. It has been rolled and "
            "applied (enemy_attack); narrate it landing."
            if outcome.enemy_attack else
            "The rules say the enemy makes an attack against them. Narrate it landing."
        )
    if outcome.enemy_attack:
        payload["enemy_attack"] = outcome.enemy_attack
    if outcome.healing:
        payload["healing"] = {k: v for k, v in outcome.healing.items() if k != "rolled_render"}
    if outcome.band == "miss":
        instruction = (
            "This outcome is final and was determined by the rules engine. It is a miss: "
            "make a GM move that follows from the fiction, as hard as you like. Do not "
            "restate the arithmetic, and do not soften the result."
        )
    else:
        instruction = (
            "This outcome is final and was determined by the rules engine. Narrate what it "
            "means, including anything the player chose. Do not restate the arithmetic, "
            "and do not reverse or soften the result."
        )
        if outcome.damage or outcome.enemy_attack:
            instruction += (
                " The damage and HP are exact: a creature that is not defeated is still "
                "fighting, however the blow reads, and one that is defeated is done. "
                "Describe each wound as bad as its severity says, no worse or better."
            )
    payload["instruction"] = instruction
    return payload
