"""Command rendering: the model cannot put arbitrary text in the channel."""

from __future__ import annotations

import pytest

from drelm.models import CheckKind
from drelm.templates import TemplateError, normalize_key, render_command


@pytest.mark.parametrize(
    "written,expected",
    [
        ("sleight of hand", "!check sleightOfHand"),
        ("Sleight of Hand", "!check sleightOfHand"),
        ("sleight-of-hand", "!check sleightOfHand"),
        ("SLEIGHT_OF_HAND", "!check sleightOfHand"),
        ("stealth", "!check stealth"),
        ("  perception  ", "!check perception"),
        ("animal handling", "!check animalHandling"),
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
    from drelm.templates import AVRAE_SAVE_TOKEN, AVRAE_SKILL_TOKEN

    for skill in AVRAE_SKILL_TOKEN:
        cmd = render_command(CheckKind.CHECK, skill)
        assert "\n" not in cmd and "@" not in cmd and cmd.startswith("!")
    for save in AVRAE_SAVE_TOKEN:
        cmd = render_command(CheckKind.SAVE, save)
        assert "\n" not in cmd and "@" not in cmd and cmd.startswith("!")
