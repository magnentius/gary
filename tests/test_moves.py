"""The basic moves, pinned: these are the rules, so a change here is a rules change."""

from __future__ import annotations

import pytest

from gary.moves import ANY, BOND, CASTER, MOVES, band_of
from gary.sheets import STATS


def test_the_eight_basic_moves_and_their_stats():
    assert {k: m.stat for k, m in MOVES.items() if m.callable and not m.classes} == {
        "hack_and_slash": "str", "volley": "dex", "defy_danger": ANY, "defend": "con",
        "spout_lore": "int", "discern_realities": "wis", "parley": "cha",
        "aid_or_interfere": BOND,
    }
    assert all(m.stat in (*STATS, ANY, BOND, CASTER, "nothing") for m in MOVES.values())
    assert {k for k, m in MOVES.items() if not m.callable} == {
        "last_breath", "take_watch", "defend_charge", "defend_self",
        "journey_trailblazer", "journey_scout", "journey_quartermaster"}


@pytest.mark.parametrize("total,band", [(12, "strong"), (10, "strong"), (9, "weak"),
                                        (7, "weak"), (6, "miss"), (2, "miss"), (-1, "miss")])
def test_bands(total, band):
    assert band_of(total) == band


def test_the_choices_players_make():
    """Who picks how many of what -- the part a parser could not have got right."""
    def choice(key, band):
        c = MOVES[key].band(band).choice
        return (c.count, len(c.options)) if c else None

    assert choice("hack_and_slash", "strong") == (1, 2)
    assert choice("hack_and_slash", "weak") is None
    assert choice("volley", "weak") == (1, 3)
    assert choice("volley", "strong") is None
    assert choice("discern_realities", "strong") == (3, 6)
    assert choice("discern_realities", "weak") == (1, 6)
    assert choice("aid_or_interfere", "strong") == (1, 2)
    assert choice("aid_or_interfere", "weak") == (1, 2)
    # A miss offers no choice -- except spending hold, which is no roll at all.
    assert all(m.miss.choice is None for k, m in MOVES.items() if not k.startswith("defend_"))


def test_only_hack_and_slash_makes_the_enemy_attack_and_only_where_the_rules_say():
    flagged = {
        (k, b) for k, m in MOVES.items() for b in ("strong", "weak", "miss")
        if m.band(b).enemy_attacks
    }
    assert flagged == {("hack_and_slash", "weak")}
    extra_damage = MOVES["hack_and_slash"].strong.choice.options
    assert [o.enemy_attacks for o in extra_damage] == [False, True]


def test_every_option_fits_on_a_discord_button():
    """Labels over 80 characters are cut; none of the rules text should need to be."""
    for move in MOVES.values():
        for band in (move.strong, move.weak):
            for option in band.choice.options if band.choice else ():
                assert len(option.text) <= 80, option.text
