"""Fixture-driven parser tests (ARCHITECTURE.md §6.1).

Every fixture in ``tests/fixtures/avrae/`` is run through ``parse_embed`` and checked
against the expectation stored beside it. Fixtures currently marked HYPOTHETICAL are
guesses at Avrae's output; replacing one with a real capture is a file drop, and any
pattern that was wrong fails here rather than in a session.

``test_no_fixture_is_still_hypothetical`` is deliberately left as a marker of that work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gary.parser.avrae import ParseStatus, parse_embed

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "avrae"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_fixture(path: Path):
    data = load(path)
    expect = data["expect"]
    parsed = parse_embed(data["message"])

    assert parsed.status is ParseStatus(expect["status"]), (
        f"{path.name}: expected {expect['status']}, got {parsed.status.value} "
        f"({parsed.reason})"
    )

    if "total" in expect:
        assert parsed.roll is not None
        assert parsed.roll.total == expect["total"]
    if "natural" in expect:
        assert parsed.roll.natural == expect["natural"]
    if "actor" in expect:
        # A sheet names its character in embed.author.name and carries no roll.
        source = parsed.sheet if parsed.status is ParseStatus.SHEET else parsed.roll
        assert source is not None
        assert source.actor == expect["actor"]
    if "level" in expect:
        assert parsed.sheet is not None
        assert parsed.sheet.level == expect["level"]
    if "descriptor" in expect:
        assert parsed.sheet is not None
        assert parsed.sheet.descriptor == expect["descriptor"]
    if "kind" in expect:
        assert parsed.roll.kind is not None
        assert parsed.roll.kind.value == expect["kind"]
    if "interaction_user_id" in expect:
        assert parsed.interaction_user_id == expect["interaction_user_id"]
    if "referenced_user_id" in expect:
        assert parsed.referenced_user_id == expect["referenced_user_id"]
    if "combatants" in expect:
        assert len(parsed.combatants) == expect["combatants"]
    if "dying" in expect:
        dying = [c.name for c in parsed.combatants if c.is_dying]
        assert dying == expect["dying"]


def test_fixtures_exist():
    assert FIXTURES, "no Avrae fixtures; the parser is completely unverified"


def test_attack_uses_the_d20_not_the_damage_roll():
    """Explicit: reading the damage number would resolve checks against the wrong value."""
    parsed = parse_embed(load(FIXTURE_DIR / "attack_with_damage.json")["message"])
    assert parsed.roll.total == 21  # **To Hit**
    assert parsed.roll.total != 5  # **Damage**, in the same field


def test_high_total_alone_is_not_a_crit():
    """A +15 modifier can total 25 off a natural 10."""
    parsed = parse_embed(
        {"embeds": [{"title": "Kethra makes a Stealth check!",
                     "description": "**1d20 (10) + 15** = `25`"}]}
    )
    assert parsed.roll.total == 25
    assert parsed.roll.natural == 10


def test_empty_message_is_irrelevant_not_unknown():
    assert parse_embed({"embeds": [], "content": ""}).status is ParseStatus.IRRELEVANT


def test_parser_never_raises_on_malformed_input():
    """Whatever Discord hands us, the bot must not crash mid-session."""
    for bad in [
        {},
        {"embeds": None},
        {"embeds": [{}]},
        {"embeds": [{"title": None, "description": None}]},
        {"embeds": [{"fields": [{"name": "x"}]}]},
        {"interaction": {"user": {"id": "not-a-number"}}, "embeds": [{"title": "x"}]},
        {"referenced_message": {"author": {}}, "embeds": [{"title": "x"}]},
    ]:
        parse_embed(bad)  # must not raise


def test_no_fixture_is_an_unverified_guess():
    """Every fixture is either traced to Avrae's source or deliberately synthetic.

    The earlier bar was "authored guess", and the whole suite failed it. Patterns are now
    derived from ``cogs5e/utils/checkutils.py``, ``cogs5e/initiative/combat.py`` at
    ``avrae@cb75be2``, and from running ``d20`` locally.
    """
    guesses = [
        p.name
        for p in FIXTURES
        if "HYPOTHETICAL" in load(p).get("_provenance", "")
    ]
    assert not guesses, f"still guessing at: {guesses}"


def test_every_fixture_records_its_provenance():
    for path in FIXTURES:
        provenance = load(path).get("_provenance", "")
        assert provenance, f"{path.name} has no _provenance"
        assert any(
            marker in provenance for marker in ("DERIVED", "CAPTURED", "SYNTHETIC")
        ), f"{path.name}: unrecognised provenance {provenance[:40]!r}"


def test_monster_status_word_counts_as_dying():
    """Avrae hides monster HP, writing a status word where the numbers would be.

    Captured live 2026-08-20: "# 11: GO1 <Dead>". There is no 0 and no condition, so
    is_dying saw nothing and a dead monster mirrored as alive -- Gary would narrate it
    still swinging.
    """
    parsed = parse_embed(load(FIXTURE_DIR / "initiative_dying.json")["message"])
    dead = [c for c in parsed.combatants if c.name == "GO1"]
    assert dead and dead[0].is_dying
    assert dead[0].status == "Dead"
    assert dead[0].hp_current is None  # no numeric HP to read

    healthy = parse_embed(load(FIXTURE_DIR / "initiative_healthy.json")["message"])
    assert not any(c.is_dying for c in healthy.combatants)
