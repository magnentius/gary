"""The Lazy Encounter Benchmark — arithmetic, so it is tested as arithmetic.

Thresholds are taken from the article, including the 11th+ adjustment:
https://slyflourish.com/the_lazy_encounter_benchmark.html
"""

import pytest

from gary.encounters import benchmark, weigh


def party(*levels):
    return {f"pc{i}": {"level": lvl} for i, lvl in enumerate(levels)}


# -- thresholds ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "levels,total_cr,single_cr",
    [
        ((1, 1, 1, 1), 1.0, 1.0),        # tier 1: quarter of total, single = average
        ((3, 3, 3), 2.25, 3.0),
        ((5, 5, 5, 5), 10.0, 7.5),       # 5+: half of total, single = 1.5x average
        ((12, 12, 12, 12), 36.0, 18.0),  # 11-16: three quarters
        ((17, 17, 17, 17), 68.0, 25.5),  # 17+: equal to total
    ],
)
def test_thresholds_match_the_article(levels, total_cr, single_cr):
    bench = benchmark(party(*levels))
    assert bench.deadly_total_cr == total_cr
    assert bench.deadly_single_cr == single_cr


def test_no_levels_known_yields_nothing_rather_than_a_wrong_number():
    assert benchmark({"pc0": {}}, roster=["pc0"]) is None


def test_a_partial_party_is_flagged():
    """A character with no sheet contributes nothing, understating the total. Cautious,
    but the caller must know rather than trust a number built from half a party."""
    bench = benchmark({"a": {"level": 5}}, roster=["a", "b", "c"])
    assert bench.partial
    assert bench.known == 1 and bench.characters == 3


# -- weighing a line-up -------------------------------------------------------------------


def test_six_goblins_against_a_level_seven_party():
    """The arithmetic a model fumbles: six CR 1/4 is 1.5, under a 1.75 threshold."""
    bench = benchmark(party(1, 3, 3))
    result = weigh(bench, [{"name": "goblin", "cr": 0.25}] * 6)
    assert result["total_cr"] == 1.5
    assert result["may_be_deadly"] is False


def test_one_more_goblin_crosses_it():
    bench = benchmark(party(1, 3, 3))
    result = weigh(bench, [{"name": "goblin", "cr": 0.25}] * 8)
    assert result["total_cr"] == 2.0
    assert result["may_be_deadly"] is True
    assert "exceeds 1.75" in result["why"]


def test_a_single_monster_can_be_deadly_while_the_total_is_not():
    """An ogre alone is under the total budget but over the solo threshold."""
    bench = benchmark(party(3, 3, 3, 3))     # total 12, deadly total CR 3, single CR 3
    result = weigh(bench, [{"name": "ogre", "cr": 4}])
    assert result["may_be_deadly"] is True
    assert "on its own" in result["why"]


def test_it_reports_and_never_refuses():
    """The author calls it a loose gauge; nothing here may block an encounter."""
    bench = benchmark(party(1, 1))
    result = weigh(bench, [{"name": "adult red dragon", "cr": 17}])
    assert result["ok"] is True
    assert result["may_be_deadly"] is True
    assert "not a rule" in result["note"]


def test_unparseable_cr_counts_as_zero_rather_than_raising():
    bench = benchmark(party(5, 5))
    result = weigh(bench, [{"name": "mystery", "cr": "big"}])
    assert result["total_cr"] == 0
