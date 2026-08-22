"""Morale through the Director (§5.4) — the wiring, not the table.

`test_morale.py` pins when a creature breaks. This pins that Gary can tell which creature
the tracker's "BA1" is, that it offers rather than acts, and that it says it once.
"""

from __future__ import annotations

import pytest

from gary.fsm import Director, PromptModel, SendMessage
from gary.parser.avrae import CombatantView, ParsedAvraeMessage, ParseStatus
from gary.registry import PendingCheckRegistry

CHANNEL = 7


def _director(**kw):
    return Director(
        registry=PendingCheckRegistry(clock=lambda: 0.0),
        clock=lambda: 0.0,
        game_channels={CHANNEL},
        players={11: "Caspian Varr", 12: "Liriel", 13: "Vak-Mester"},
        **kw,
    )


def _tracker(*combatants, round_number=2):
    return ParsedAvraeMessage(
        status=ParseStatus.INITIATIVE,
        combatants=tuple(combatants),
        round_number=round_number,
    )


def _mob(name="BA1", status="Bloodied", current=True):
    return CombatantView(name=name, status=status, is_current_turn=current)


def _pc(name="Caspian Varr", hp=15):
    return CombatantView(name=name, hp_current=hp, hp_max=15, is_current_turn=False)


def _party():
    return (_pc(), _pc("Liriel"), _pc("Vak-Mester"))


# -- recovering what BA1 is ---------------------------------------------------------------


def test_a_tracker_abbreviation_is_matched_back_to_the_srd_creature():
    """Avrae renames `!init madd Bandit` to BA1, so without this nothing can be assessed."""
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit", "Bandit"])
    assert d._srd_name(CHANNEL, "BA1") == "Bandit"
    assert d._srd_name(CHANNEL, "BA2") == "Bandit"


def test_a_multi_word_creature_is_matched_by_initials():
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit Captain"])
    assert d._srd_name(CHANNEL, "BC1") == "Bandit Captain"


def test_creatures_sharing_a_prefix_are_refused_rather_than_guessed():
    """Bandit and Basilisk are both BA to Avrae, so BA1 genuinely could be either.
    Suggesting a rout for the wrong creature removes something that was fine."""
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit", "Basilisk"])
    assert d._srd_name(CHANNEL, "BA1") == ""


def test_a_shared_first_letter_is_not_ambiguity():
    """Avrae gives Bandit BA and Bandit Captain BC. An earlier matcher called these
    ambiguous and silently switched morale off for both."""
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit", "Bandit Captain"])
    assert d._srd_name(CHANNEL, "BA1") == "Bandit"
    assert d._srd_name(CHANNEL, "BC1") == "Bandit Captain"


def test_a_combatant_renamed_by_hand_gets_no_morale():
    """`!init madd Bandit -name Gorehowl` produces a tracker name matching no prefix.
    Gary would be guessing, so it says nothing."""
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit"])
    assert d._srd_name(CHANNEL, "Gorehowl") == ''


def test_no_roster_means_no_morale_at_all():
    """A fight the model did not start through start_combat is one Gary cannot identify."""
    d = _director()
    assert d._srd_name(CHANNEL, "BA1") == ""
    assert d.on_avrae_message(channel_id=CHANNEL, parsed=_tracker(_mob(), *_party())) != []
    assert not [e for e in d.on_avrae_message(
        channel_id=CHANNEL, parsed=_tracker(_mob(), *_party())
    ) if isinstance(e, PromptModel) and e.kind == "morale_break"]


# -- offering the rout --------------------------------------------------------------------


def _morale_effects(d, parsed):
    return d.on_avrae_message(channel_id=CHANNEL, parsed=parsed)


def test_a_broken_creature_gets_a_command_to_run_and_a_narration():
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit", "Bandit", "Bandit"])
    dead = CombatantView(name="BA2", status="Dead")
    holding = CombatantView(name="BA3", status="Healthy")
    effects = _morale_effects(d, _tracker(_mob(), dead, holding, *_party()))

    posts = [e for e in effects if isinstance(e, SendMessage)]
    prompts = [e for e in effects if isinstance(e, PromptModel)]
    assert any('!init remove "BA1"' in p.text for p in posts)
    assert any("Run it if you agree" in p.text for p in posts)
    assert [p.kind for p in prompts] == ["morale_break"]
    # It never claims to have done it.
    assert not any("!init end" in p.text for p in posts)


def test_the_last_one_standing_is_offered_init_end_instead_of_a_removal():
    """One creature left with nothing behind it is the fight being over, not a combatant
    being removed -- and `!init end` is what the DM would type next anyway."""
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit", "Bandit"])
    dead = CombatantView(name="BA2", status="Dead")
    effects = _morale_effects(d, _tracker(_mob(), dead, *_party()))
    posts = [e for e in effects if isinstance(e, SendMessage)]
    assert any("!init end" in p.text for p in posts)
    assert not any("init remove" in p.text for p in posts)


def test_a_creature_is_offered_once_not_every_round():
    """Avrae edits the tracker several times a turn. Without this the DM is nagged to
    remove the same goblin on every HP change for the rest of the fight."""
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit", "Bandit"])
    dead = CombatantView(name="BA2", status="Dead")

    first = _morale_effects(d, _tracker(_mob(), dead, *_party(), round_number=2))
    assert any(isinstance(e, PromptModel) and e.kind == "morale_break" for e in first)

    again = _morale_effects(d, _tracker(_mob(), dead, *_party(), round_number=3))
    assert not any(isinstance(e, PromptModel) and e.kind == "morale_break" for e in again)


def test_a_healthy_creature_takes_its_turn_as_before():
    """Morale must not swallow the ordinary monster turn."""
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit"])
    effects = _morale_effects(d, _tracker(_mob(status="Healthy"), *_party()))
    kinds = [e.kind for e in effects if isinstance(e, PromptModel)]
    assert kinds == ["monster_turn"]


def test_an_undead_is_never_offered_a_rout():
    d = _director()
    d.note_encounter(CHANNEL, ["Skeleton"])
    effects = _morale_effects(d, _tracker(_mob(name="SK1"), *_party()))
    assert not any(isinstance(e, PromptModel) and e.kind == "morale_break" for e in effects)


def test_disposition_reaches_the_call():
    """A creature that loathes the party holds where an indifferent one runs (§5.4)."""
    hateful = _director(npc_disposition=lambda name: -5)
    hateful.note_encounter(CHANNEL, ["Bandit", "Bandit"])
    indifferent = _director(npc_disposition=lambda name: 0)
    indifferent.note_encounter(CHANNEL, ["Bandit", "Bandit"])

    # Bloodied and outnumbered three to two: pressure 4, which straddles an indifferent
    # bandit's nerve of 3 and the 4 that conviction buys.
    holding = CombatantView(name="BA2", status="Healthy")

    def broke(d):
        effects = d.on_avrae_message(
            channel_id=CHANNEL,
            parsed=_tracker(_mob(name="BA1", status="Bloodied"), holding, *_party()),
        )
        return any(isinstance(e, PromptModel) and e.kind == "morale_break" for e in effects)

    assert broke(indifferent) is True
    assert broke(hateful) is False


def test_a_broken_disposition_lookup_never_kills_the_turn():
    d = _director(npc_disposition=lambda name: (_ for _ in ()).throw(KeyError(name)))
    d.note_encounter(CHANNEL, ["Bandit"])
    d.on_avrae_message(channel_id=CHANNEL, parsed=_tracker(_mob(), _pc()))


def test_starting_a_new_encounter_forgets_who_had_broken():
    d = _director()
    d.note_encounter(CHANNEL, ["Bandit", "Bandit"])
    dead = CombatantView(name="BA2", status="Dead")
    _morale_effects(d, _tracker(_mob(), dead, *_party()))
    assert d._broken.get(CHANNEL)

    d.note_encounter(CHANNEL, ["Bandit", "Bandit"])
    assert not d._broken.get(CHANNEL)
