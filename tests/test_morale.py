"""Morale (ARCHITECTURE.md §5.4).

The point of computing morale rather than asking the model is that the same fight always
breaks in the same place, so these are exact rather than illustrative.
"""

from __future__ import annotations

import pytest

from gary.morale import (
    DEFAULT_NERVE,
    MoraleCall,
    assess,
    base_type,
    nerve,
    pressure_of_status,
    routed,
)


# -- the table ----------------------------------------------------------------------------


def test_mindless_things_never_break():
    """An undead has no fear and a construct no instinct. Avrae's fight-to-the-death is
    already correct for these, so morale must not second-guess it."""
    for kind in ("Undead", "Construct", "Ooze", "Plant", "Swarm of Tiny Beasts"):
        assert nerve(kind) is None, kind
        assert assess("X1", kind, status="bloodied", allies_down=5) is None


def test_a_devoted_ally_and_a_hateful_enemy_both_hold_longer_than_a_mercenary():
    """Conviction is |disposition|, not friendliness. The creature with no feeling either
    way is the one that leaves first -- which is the whole fiction of a hired sword."""
    mercenary = nerve("Humanoid", 0)
    devoted = nerve("Humanoid", 5)
    hateful = nerve("Humanoid", -5)
    assert devoted == hateful > mercenary


def test_disposition_adjusts_the_type_rather_than_overriding_it():
    """A devoted wolf is still a wolf."""
    assert nerve("Beast", 5) < nerve("Humanoid", 0)


def test_a_subtype_is_read_before_its_base_type():
    assert nerve("Fiend (Devil)") > nerve("Fiend (Demon)")
    assert base_type("Fiend (Devil)") == "Fiend"


def test_an_unlisted_type_falls_back_rather_than_never_breaking():
    """Homebrew and any type the SRD adds later must not silently become unbreakable --
    that failure would look exactly like the feature not existing."""
    assert nerve("Fungal Horror") == DEFAULT_NERVE
    assert nerve("Aberration (Unspeakable)") == nerve("Aberration")


def test_an_unknown_creature_is_never_assessed():
    assert nerve("") is None
    assert nerve(None) is None
    assert assess("GO1", "", status="bloodied") is None


# -- reading the tracker ------------------------------------------------------------------


def test_status_words_and_numeric_hp_agree():
    """Avrae shows a word for monsters and numbers for PCs (§6.4). Both are read, and a
    half-dead creature is under the same pressure either way."""
    assert pressure_of_status("Healthy") == pressure_of_status(hp_current=10, hp_max=10)
    assert pressure_of_status("Bloodied") == pressure_of_status(hp_current=4, hp_max=10)
    assert pressure_of_status("Injured") == pressure_of_status(hp_current=8, hp_max=10)


def test_an_unrecognised_status_word_suppresses_morale_rather_than_guessing():
    """An unknown word most likely means Avrae changed format. A rout suggested off a
    misread tracker is worse than no rout: it removes a creature that was fine."""
    assert pressure_of_status("Grievously Vexed") is None
    assert pressure_of_status("") is None
    assert assess("GO1", "Humanoid", status="Grievously Vexed", allies_down=3) is None


def test_none_and_holding_are_different_answers():
    """`None` is Gary declining to guess; broke=False is Gary saying it holds. Conflating
    them would journal a decision that was never made."""
    assert assess("GO1", "Undead", status="bloodied") is None
    holding = assess("GO1", "Humanoid", status="healthy")
    assert holding is not None and holding.broke is False


# -- breaking -----------------------------------------------------------------------------


def test_a_healthy_creature_alone_and_unhurt_holds():
    call = assess("GO1", "Humanoid", status="healthy")
    assert call.broke is False


def test_a_bloodied_goblin_watching_its_side_fall_breaks():
    call = assess("GO1", "Humanoid", status="bloodied", allies_down=2, allies_standing=0)
    assert call.broke is True
    assert "bloodied" in call.explain() and "last one standing" in call.explain()


def test_type_sets_when_a_creature_breaks_not_whether():
    """Only the mindless types never break. Everything else has a price, and the type is
    how high it is: a bloodied dragon with its side still up holds where a goblin runs."""
    kw = dict(status="bloodied", allies_down=1, allies_standing=1)
    assert assess("RD1", "Dragon (Chromatic)", **kw).broke is False
    assert assess("GO1", "Humanoid", **kw).broke is True


def test_even_a_dragon_breaks_when_it_is_bloodied_and_alone():
    """Proud is not suicidal, and a dragon that keeps fighting a lost fight is a dead
    dragon nobody believes in."""
    call = assess("RD1", "Dragon (Chromatic)", status="bloodied", allies_down=2,
                  allies_standing=0)
    assert call.broke is True


def test_a_wolf_breaks_where_a_person_would_not():
    kw = dict(status="injured", allies_down=1, allies_standing=1)
    assert assess("WO1", "Beast", **kw).broke is True
    assert assess("BA1", "Humanoid", **kw).broke is False


def test_fallen_allies_stop_counting_past_the_cap():
    """Otherwise a big enough fight breaks everything on the field regardless of type."""
    many = assess("DR1", "Dragon", status="healthy", allies_down=20, allies_standing=1)
    assert many.broke is False


def test_a_call_explains_itself_for_the_journal():
    call = assess("GO1", "Humanoid", status="bloodied", allies_down=1, allies_standing=1)
    text = call.explain()
    assert "GO1" in text and "Humanoid" in text
    assert str(call.pressure) in text and str(call.nerve) in text


# -- the whole side -----------------------------------------------------------------------


def test_routed_needs_every_call_broken():
    broke = MoraleCall("A", True, 1, 9, "Humanoid")
    held = MoraleCall("B", False, 9, 1, "Humanoid")
    assert routed([broke, broke]) is True
    assert routed([broke, held]) is False
    assert routed([]) is False


def test_being_outnumbered_presses_a_creature_that_never_had_allies_to_lose():
    """A lone survivor facing the party feels nothing under the fallen-ally rule, because
    it has no allies. Outnumbered is counted separately so it is not immune."""
    alone = dict(status="bloodied", allies_down=0, allies_standing=0)
    assert assess("BA1", "Humanoid", foes_standing=4, **alone).broke is True
    assert assess("BA1", "Humanoid", foes_standing=1, **alone).broke is False


def test_a_creature_with_its_side_up_is_not_outnumbered():
    call = assess("BA1", "Humanoid", status="healthy", allies_standing=3, foes_standing=3)
    assert "outnumbered" not in call.explain()
