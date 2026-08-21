"""Command rendering: the model cannot put arbitrary text in the channel."""

from __future__ import annotations

import pytest

from gary.models import CheckKind
from gary.templates import TemplateError, normalize_key, render_command


@pytest.mark.parametrize(
    "written,expected",
    [
        ("sleight of hand", '!check "sleight of hand"'),
        ("Sleight of Hand", '!check "sleight of hand"'),
        ("sleight-of-hand", '!check "sleight of hand"'),
        ("SLEIGHT_OF_HAND", '!check "sleight of hand"'),
        ("stealth", "!check stealth"),
        ("  perception  ", "!check perception"),
        ("animal handling", '!check "animal handling"'),
    ],
)
def test_skill_variants_normalize(written, expected):
    """The model writes prose-ish keys; we canonicalise rather than reject."""
    assert render_command(CheckKind.CHECK, written) == expected


@pytest.mark.parametrize(
    "written,expected",
    [
        ("dexterity", "!save dexterity"),
        ("dex", "!save dexterity"),
        ("DEX save", "!save dexterity"),
        ("constitution", "!save constitution"),
        ("con", "!save constitution"),
        ("wis", "!save wisdom"),
    ],
)
def test_save_variants_normalize(written, expected):
    assert render_command(CheckKind.SAVE, written) == expected


def test_attack_names_pass_through_quoted():
    assert render_command(CheckKind.ATTACK, "Longsword") == '!attack "Longsword"'
    assert render_command(CheckKind.ATTACK, "Fire Bolt") == '!attack "Fire Bolt"'


@pytest.mark.parametrize(
    "hostile",
    [
        "!init end",
        "stealth\n!init end",
        "stealth`; !init end",
        "@everyone",
        "stealth @everyone",
        "",
        "   ",
        "notaskill",
        "x" * 500,
    ],
)
def test_hostile_or_unknown_keys_are_refused(hostile):
    """Nothing reaches the channel unless it came out of the allowlist."""
    with pytest.raises(TemplateError):
        render_command(CheckKind.CHECK, hostile)


@pytest.mark.parametrize(
    "hostile",
    ["Longsword\n!init end", "Longsword @everyone", 'Longsword" ; !init end', "x" * 100],
)
def test_hostile_attack_names_are_refused(hostile):
    with pytest.raises(TemplateError):
        render_command(CheckKind.ATTACK, hostile)


def test_ambiguous_save_abbreviation_is_refused():
    """'c' could be constitution or charisma -- refuse rather than pick."""
    with pytest.raises(TemplateError):
        normalize_key(CheckKind.SAVE, "c")


def test_rendered_commands_are_single_line_and_pingless():
    from gary.templates import AVRAE_SAVE_TOKEN, AVRAE_SKILL_TOKEN

    for skill in AVRAE_SKILL_TOKEN:
        cmd = render_command(CheckKind.CHECK, skill)
        assert "\n" not in cmd and "@" not in cmd and cmd.startswith("!")
    for save in AVRAE_SAVE_TOKEN:
        cmd = render_command(CheckKind.SAVE, save)
        assert "\n" not in cmd and "@" not in cmd and cmd.startswith("!")


def test_multi_word_skills_are_quoted():
    """Confirmed live 2026-08-21: Avrae fuzzy-matches its own camelCase keys and can
    answer `!check sleightOfHand` with a "Multiple Matches Found" selector instead of
    rolling. The quoted display name is an exact match and beats every candidate."""
    assert render_command(CheckKind.CHECK, "sleight of hand") == '!check "sleight of hand"'
    assert render_command(CheckKind.CHECK, "animal handling") == '!check "animal handling"'


def test_attack_names_are_not_double_quoted():
    """The attack template already quotes; quoting again yields !attack ""Fire Bolt""."""
    assert render_command(CheckKind.ATTACK, "Two Handed Sword") == '!attack "Two Handed Sword"'
    assert render_command(CheckKind.ATTACK, "Fire Bolt") == '!attack "Fire Bolt"'
    assert '""' not in render_command(CheckKind.ATTACK, "Fire Bolt")


def test_coin_amounts_must_be_signed():
    """Documented syntax is "+/- # [cp/sp/ep/gp/pp]". The sign is required and a bare
    number means gold; spacing is irrelevant. An unsigned amount is what "1gp 5sp"
    actually tripped over — first misread here as a ban on mixed denominations."""
    from gary.templates import render_coin_command

    assert render_coin_command("-2cp") == "!game coins -2cp"
    assert render_coin_command("+5gp") == "!game coins +5gp"
    assert render_coin_command("- 5 gp") == "!game coins -5gp"
    assert render_coin_command("-2") == "!game coins -2"      # bare number is gold
    assert render_coin_command("+102.13") == "!game coins +102.13"  # decimals, bare gold
    assert render_coin_command("+3gp", command="coins") == "!coins +3gp"  # alias override
    # Mixed terms, per `!help game coinpurse`, re-spaced so a player can read the price.
    assert render_coin_command("-1gp -5sp") == "!game coins -1gp -5sp"
    assert render_coin_command("+10pp +2gp +1sp +3cp") == "!game coins +10pp +2gp +1sp +3cp"

    # "+1.5gp" is refused: decimals are only meaningful on the bare-gold form.
    for bad in ("2cp", "1gp 5sp", "2 chickens", "-2cp; !init end", "", "cp",
                "-1gp -", "+1.5gp"):
        with pytest.raises(TemplateError):
            render_coin_command(bad)


def test_death_saves_can_be_rendered():
    """save_death.json was captured live, so Avrae takes `!save death` — but the token
    map held only the six abilities, so a character at 0 HP could not roll the save that
    decides whether they live."""
    assert render_command(CheckKind.SAVE, "death") == "!save death"
    assert render_command(CheckKind.SAVE, "Death") == "!save death"


def test_a_downed_target_is_attacked_at_advantage_at_any_range():
    """A creature at 0 HP is Unconscious, not merely Prone, and the SRD separates them:
    Prone qualifies on range ("Advantage if the attacker is within 5 feet ... Otherwise
    ... Disadvantage"), Unconscious does not ("Attack rolls against you have Advantage").

    An earlier version rendered disadvantage for a ranged attack on a downed target,
    reasoning from Prone alone. Reading the extracted glossary caught it."""
    from gary.templates import render_attack_command, render_enemy_attack_command

    assert render_enemy_attack_command("Spear", "Kethra", advantage=True).endswith(" adv")
    assert render_enemy_attack_command(
        "Longbow", "Kethra", advantage=True, ranged=True
    ).endswith(" adv")
    assert render_attack_command("Fire Bolt", "GU1", advantage=True, ranged=True).endswith(" adv")

    # Nothing is added against someone standing.
    assert render_enemy_attack_command("Longbow", "Kethra", ranged=True) == (
        '!init attack "Longbow" -t "Kethra"'
    )


def test_condition_names_are_canonicalised_from_the_srd():
    from gary.templates import CONDITIONS, condition, render_effect_command

    assert len(CONDITIONS) == 15
    assert condition("PRONE")["name"] == "Prone"
    assert condition("not a condition") is None
    assert render_effect_command("GU1", "prone") == '!init effect "GU1" "Prone"'
    # a narrative label is still allowed through unchanged
    assert 'Blessed by the tide' in render_effect_command("GU1", "Blessed by the tide")


def test_unconscious_names_the_conditions_it_implies():
    """Gary's own advantage rule is derived from this text, so it needs to be intact."""
    from gary.templates import condition

    rules = condition("unconscious")["rules"]
    assert "Incapacitated and Prone" in rules
    assert "Attack rolls against you have Advantage" in rules


def test_effect_commands_carry_no_mechanical_arguments():
    """`!init effect` accepts -ac, -maxhp, -attack, -immune, adv and more. A model that
    can pass those under a condition name grants itself armour class, hit points and
    attacks, which is what §5.3 renders from an allowlist to prevent."""
    from gary.templates import render_effect_command

    assert render_effect_command("GU1", "Poisoned", 3) == '!init effect "GU1" "Poisoned" -dur 3'
    assert render_effect_command("Caspian Varr", "Prone") == '!init effect "Caspian Varr" "Prone"'
    assert render_effect_command("GU1", "Blessed by the tide", 10).endswith("-dur 10")

    for target, effect, rounds in [
        ("GU1", "Prone -ac +10", None),
        ("GU1", 'x" -maxhp +100 "', None),
        ("GU1", '-attack "|2d6[fire]|"', None),
        ("GU1 -t GU2", "Prone", None),
        ("GU1", "Prone", 999),
        ("GU1", "", None),
    ]:
        with pytest.raises(TemplateError):
            render_effect_command(target, effect, rounds)
