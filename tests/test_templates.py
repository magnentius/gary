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

    assert render_coin_command("-2cp") == "!coins -2cp"
    assert render_coin_command("+5gp") == "!coins +5gp"
    assert render_coin_command("- 5 gp") == "!coins -5gp"
    assert render_coin_command("-2") == "!coins -2"          # bare number is gold
    assert render_coin_command("+3gp", alias="purse") == "!purse +3gp"

    for bad in ("2cp", "1gp 5sp", "2 chickens", "-2cp; !init end", "", "cp"):
        with pytest.raises(TemplateError):
            render_coin_command(bad)
