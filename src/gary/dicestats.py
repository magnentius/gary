"""How the table's dice have actually fallen, against how 2d6 should (`/dice`).

Read from the journal: every resolved move is there with its faces and modifier, so the
history needs no bookkeeping of its own. Only 2d6 move rolls count -- damage dice and
automatic failures are not in it.

A run of bad luck is the most-asked question about any dice roller, and the honest answer
is a comparison, not reassurance: the faces against 1 in 6, the totals against the 36
ways two dice fall, and how often the table's rolls landed in each band against how often
they should have at the modifiers actually rolled.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

#: Ways two six-sided dice make each total, out of 36.
_WAYS = {t: 6 - abs(t - 7) for t in range(2, 13)}

#: Chi-square values fair dice stay under 95% of the time, by degrees of freedom.
_CHI95 = {5: 11.07, 10: 18.31}


def _rolls(journal: Iterable[Dict[str, Any]], actor: Optional[str]) -> List[Dict[str, Any]]:
    out = []
    for entry in journal:
        if entry.get("kind") != "move_outcome":
            continue
        payload = entry.get("payload") or {}
        faces = payload.get("faces") or []
        if len(faces) != 2 or (actor and entry.get("target", "").lower() != actor.lower()):
            continue
        out.append({"actor": entry.get("target", ""), "faces": [int(f) for f in faces],
                    "modifier": int(payload.get("modifier", 0)), "band": payload.get("band", "")})
    return out


def _expected_bands(modifiers: Iterable[int]) -> Dict[str, float]:
    """Expected strong/weak/miss counts for rolls made at these modifiers."""
    out = {"strong": 0.0, "weak": 0.0, "miss": 0.0}
    for mod in modifiers:
        for total, ways in _WAYS.items():
            score = total + mod
            band = "strong" if score >= 10 else "weak" if score >= 7 else "miss"
            out[band] += ways / 36
    return out


def _chi(observed: Dict[Any, int], expected: Dict[Any, float]) -> float:
    return sum((observed.get(k, 0) - e) ** 2 / e for k, e in expected.items() if e > 0)


def summary(journal: Iterable[Dict[str, Any]], actor: Optional[str] = None) -> str:
    rolls = _rolls(journal, actor)
    who = f" for {actor}" if actor else ""
    if not rolls:
        return f"No move rolls{who} yet."
    n = len(rolls)
    faces = Counter(f for r in rolls for f in r["faces"])
    totals = Counter(sum(r["faces"]) for r in rolls)
    bands = Counter(r["band"] for r in rolls)
    expected_bands = _expected_bands(r["modifier"] for r in rolls)

    lines = [f"🎲 **{n} move roll{'s' if n != 1 else ''}{who}** ({2 * n} dice)."]
    lines.append("Faces: " + "  ".join(f"{f}: {faces.get(f, 0)}" for f in range(1, 7))
                 + f"  (fair: about {2 * n / 6:.1f} each)")
    lines.append("Bands: " + ", ".join(
        f"{label} {bands.get(b, 0)} (expected {expected_bands[b]:.1f})"
        for b, label in (("strong", "10+"), ("weak", "7–9"), ("miss", "6−"))))
    if n >= 30:
        chi_faces = _chi(dict(faces), {f: 2 * n / 6 for f in range(1, 7)})
        chi_totals = _chi(dict(totals), {t: n * w / 36 for t, w in _WAYS.items()})
        verdict = ("within what fair dice do" if chi_faces < _CHI95[5] and chi_totals < _CHI95[10]
                   else "unusual — but one table in twenty sees this from fair dice")
        lines.append(f"Fairness check: faces χ² {chi_faces:.1f} (fair dice stay under {_CHI95[5]} "
                     f"95% of the time), totals χ² {chi_totals:.1f} (under {_CHI95[10]}): {verdict}.")
    else:
        lines.append(f"Too few rolls for a fairness check ({n}; it needs 30). Small runs are streaky.")
    if not actor:
        per = Counter(r["actor"] for r in rolls)
        lines.append("By character: " + ", ".join(
            f"{a} {sum(1 for r in rolls if r['actor'] == a and r['band'] == 'miss')}/{c} misses"
            for a, c in per.most_common()))
    return "\n".join(lines)
