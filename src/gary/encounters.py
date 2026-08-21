"""The Lazy Encounter Benchmark (Sly Flourish).

    https://slyflourish.com/the_lazy_encounter_benchmark.html

Arithmetic, deliberately, for the same reason ``resolver`` is: a model asked to judge
whether six goblins are dangerous for a level-3 party will produce a confident answer and
fumble the sum of six CR 1/4s on the way. The numbers are computed here; the model decides
what to do about them.

Unlike a DC, this decides nothing. The author calls it "a loose gauge, not a hard rule",
and lists where it misleads: when one side badly outnumbers the other, when the objective
is not defeating everything, when the party is fresh or exhausted. So nothing here refuses
an encounter — it reports, and the phrasing stays "may be".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Fraction of total party levels that total monster CR may reach before an encounter "may
# be deadly", by average party level. From the article, including its 11th+ adjustment.
_TOTAL_CR_FRACTION: Tuple[Tuple[int, float], ...] = (
    (4, 0.25),   # levels 1-4
    (10, 0.50),  # levels 5-10
    (16, 0.75),  # levels 11-16
    (99, 1.00),  # levels 17+
)

# Multiple of AVERAGE party level a SINGLE monster's CR may reach.
_SOLO_CR_MULTIPLE: Tuple[Tuple[int, float], ...] = (
    (4, 1.0),    # levels 1-4
    (99, 1.5),   # levels 5+
)


def _band(average_level: float, table: Sequence[Tuple[int, float]]) -> float:
    for ceiling, value in table:
        if average_level <= ceiling:
            return value
    return table[-1][1]


@dataclass(frozen=True)
class Benchmark:
    """Thresholds for one party. ``known`` is how many levels went into it."""

    characters: int
    known: int
    total_level: int
    average_level: float
    deadly_total_cr: float
    deadly_single_cr: float

    @property
    def partial(self) -> bool:
        """Someone has not run ``!sheet``, so the party total is understated."""
        return self.known < self.characters


def benchmark(party: Dict[str, Any], roster: Iterable[str] = ()) -> Optional[Benchmark]:
    """Thresholds for the party, or None if no level is known at all.

    ``roster`` is everyone at the table; ``party`` holds what ``!sheet`` last reported.
    A character with no sheet contributes nothing to the total, which understates it and
    lowers the threshold — the cautious direction, but the caller is told via ``partial``
    rather than left to trust a number quietly built from half a party.
    """
    names = {*roster, *party.keys()}
    levels = [
        int(data["level"])
        for name in names
        if isinstance((data := party.get(name) or {}).get("level"), int)
    ]
    if not levels:
        return None

    total = sum(levels)
    average = total / len(levels)
    return Benchmark(
        characters=len(names),
        known=len(levels),
        total_level=total,
        average_level=average,
        deadly_total_cr=round(total * _band(average, _TOTAL_CR_FRACTION), 2),
        deadly_single_cr=round(average * _band(average, _SOLO_CR_MULTIPLE), 2),
    )


def weigh(bench: Benchmark, monsters: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Weigh a specific line-up against the benchmark. Reports; never refuses."""
    # Normalised once, so nothing downstream re-parses a value that may not be a number.
    weighed: List[Tuple[str, float]] = []
    for monster in monsters:
        try:
            cr = float(monster.get("cr", 0) or 0)
        except (TypeError, ValueError):
            cr = 0.0
        weighed.append((str(monster.get("name") or "one monster"), cr))

    total_cr = sum(cr for _, cr in weighed)
    highest = max(weighed, key=lambda pair: pair[1], default=None)

    reasons: List[str] = []
    if total_cr > bench.deadly_total_cr:
        reasons.append(f"total CR {total_cr:g} exceeds {bench.deadly_total_cr:g}")
    if highest is not None and highest[1] > bench.deadly_single_cr:
        reasons.append(
            f"{highest[0]} at CR {highest[1]:g} exceeds "
            f"{bench.deadly_single_cr:g} on its own"
        )

    return {
        "ok": True,
        "total_cr": round(total_cr, 2),
        "deadly_total_cr": bench.deadly_total_cr,
        "deadly_single_cr": bench.deadly_single_cr,
        "may_be_deadly": bool(reasons),
        "why": "; ".join(reasons) or "within the benchmark",
        "partial_party": bench.partial,
        "note": (
            "A loose gauge, not a rule. It misleads when one side badly outnumbers the "
            "other, when the objective is not defeating everything, and when the party is "
            "fresh or exhausted. Deadly is allowed — just deliberate."
        ),
    }
