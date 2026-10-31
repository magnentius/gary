"""?dm dice: the journal's move rolls against fair 2d6."""

from __future__ import annotations

import random

from gary.dicestats import summary


def roll_entry(actor, a, b, mod=0):
    total = a + b + mod
    band = "strong" if total >= 10 else "weak" if total >= 7 else "miss"
    return {"kind": "move_outcome", "target": actor,
            "payload": {"faces": [a, b], "modifier": mod, "band": band}}


def test_no_rolls_says_so():
    assert summary([]) == "No move rolls yet."
    assert summary([roll_entry("Dorn", 3, 4)], "Thorin") == "No move rolls for Thorin yet."


def test_a_short_run_is_counted_but_not_judged():
    text = summary([roll_entry("Dorn", 3, 3), roll_entry("Dorn", 6, 5, 1), {"kind": "sheet"}])
    assert text.startswith("🎲 **2 move rolls** (4 dice).")
    assert "Faces: 1: 0  2: 0  3: 2  4: 0  5: 1  6: 1" in text
    assert "Too few rolls for a fairness check" in text
    assert "By character: Dorn 1/2 misses" in text


def test_fair_dice_pass_the_check():
    rng = random.Random(4)
    journal = [roll_entry("Dorn", rng.randint(1, 6), rng.randint(1, 6), 1) for _ in range(400)]
    assert "within what fair dice do" in summary(journal)


def test_loaded_dice_are_flagged():
    journal = [roll_entry("Dorn", 3, 3) for _ in range(60)]
    assert "unusual" in summary(journal)
