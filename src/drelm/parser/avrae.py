"""Avrae embed parsing (ARCHITECTURE.md §6).

**Avrae's embed layout is not an API.** It changes between releases and differs across
``!check``, ``!save``, ``!attack`` and ``!init list``. So this module is built on two
rules:

1. **Fail loud.** A parse miss returns ``UNKNOWN``. It never degrades to ``total=0`` or
   ``is_dying=False`` -- a silent false negative on a death save is far worse than
   asking the player to repeat themselves.
2. **Fixtures are the spec.** Patterns here are hypotheses until ``tests/fixtures/avrae/``
   contains real captures. ``tests/test_parser.py`` runs every fixture through
   ``parse_embed`` and asserts the expectation recorded alongside it, so replacing a
   guess with reality is a fixture drop, not a rewrite.

The input type is a plain dict shaped like ``discord.Message.to_dict()``, so this module
imports nothing from discord.py and is fully testable offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from ..models import AvraeRoll, CheckKind


class ParseStatus(str, Enum):
    ROLL = "roll"
    INITIATIVE = "initiative"
    IRRELEVANT = "irrelevant"  # confidently not something we care about (!help, sheets)
    UNKNOWN = "unknown"  # looked like a roll but could not be read -- escalate


@dataclass(frozen=True)
class CombatantView:
    """Read-only mirror of Avrae's combat state (ARCHITECTURE.md §3: Avrae owns this)."""

    name: str
    hp_current: Optional[int] = None
    hp_max: Optional[int] = None
    conditions: tuple = ()
    death_saves: Optional[str] = None

    @property
    def is_dying(self) -> bool:
        return self.hp_current == 0 or self.death_saves is not None


@dataclass(frozen=True)
class ParsedAvraeMessage:
    status: ParseStatus
    roll: Optional[AvraeRoll] = None
    combatants: tuple = ()
    interaction_user_id: Optional[int] = None
    referenced_user_id: Optional[int] = None
    reason: str = ""


# -- patterns ---------------------------------------------------------------------------
# Every pattern below is a HYPOTHESIS pending fixture verification (ARCHITECTURE.md §14.1).

_TOTAL_LABELLED = re.compile(r"\*\*(?:Total|Result)\*\*:?\s*(-?\d+)", re.IGNORECASE)
# Avrae's usual shape: "**1d20 (13) + 7** = `20`". The backticked value after the last
# '=' is the headline number.
_TOTAL_EQUALS = re.compile(r"=\s*`(-?\d+)`")
_TOTAL_BARE = re.compile(r"\btotal\b[^0-9-]{0,12}(-?\d+)", re.IGNORECASE)
# The parenthesised dice group: "1d20 (13)", "2d20kh1 (17, ~~3~~)", "1d20 (**20**)".
# Leading count is variable (advantage/disadvantage roll 2d20), and Avrae strikes through
# dropped dice -- which is why the face cannot just be the first number in the group.
_D20_GROUP = re.compile(r"\b\d*d20[a-z0-9]*\s*\(([^)]*)\)", re.IGNORECASE)
_TITLE_CHECK = re.compile(r"^(.+?)\s+makes?\s+an?\s+(.+?)\s+check", re.IGNORECASE)
_TITLE_SAVE = re.compile(r"^(.+?)\s+makes?\s+an?\s+(.+?)\s+(?:saving throw|save)", re.IGNORECASE)
_TITLE_ATTACK = re.compile(r"^(.+?)\s+attacks?\s+with\s+(?:an?\s+)?(.+?)!?$", re.IGNORECASE)
_HP = re.compile(r"<?\s*(\d+)\s*/\s*(\d+)\s*HP", re.IGNORECASE)
_CONDITIONS = re.compile(r"\[([A-Za-z][A-Za-z ]{2,20})\]")

# Titles that are confidently not rolls. Anything not matched here and not parsed as a
# roll becomes UNKNOWN rather than being silently dropped.
_IRRELEVANT_HINTS = (
    "help",
    "commands",
    "error",
    "invalid",
    "usage",
    "character sheet",
    "settings",
    "no active character",
)


def _embed_text(embed: Dict[str, Any]) -> str:
    parts = [embed.get("title") or "", embed.get("description") or ""]
    for field in embed.get("fields") or []:
        parts.append(f"{field.get('name', '')}: {field.get('value', '')}")
    author = (embed.get("author") or {}).get("name")
    if author:
        parts.append(author)
    footer = (embed.get("footer") or {}).get("text")
    if footer:
        parts.append(footer)
    return "\n".join(p for p in parts if p)


def _actor_and_kind(embed: Dict[str, Any]) -> tuple:
    """Recover (actor, kind, key) from the embed title or author."""
    title = (embed.get("title") or "").strip()
    for pattern, kind in (
        (_TITLE_CHECK, CheckKind.CHECK),
        (_TITLE_SAVE, CheckKind.SAVE),
        (_TITLE_ATTACK, CheckKind.ATTACK),
    ):
        m = pattern.match(title)
        if m:
            return m.group(1).strip(), kind, m.group(2).strip()

    author = (embed.get("author") or {}).get("name") or ""
    if author:
        return author.strip(), None, None
    return "", None, None


def _find_total(text: str) -> Optional[int]:
    for pattern in (_TOTAL_LABELLED, _TOTAL_EQUALS, _TOTAL_BARE):
        matches = pattern.findall(text)
        if matches:
            # When an embed carries several numbers (an attack roll followed by damage),
            # the first is the d20 result -- the one a check resolves against.
            return int(matches[0])
    return None


def _find_natural(text: str) -> Optional[int]:
    """The d20 face that actually counted, or None.

    With advantage or disadvantage Avrae rolls two dice and strikes through the dropped
    one, so the kept face is the first entry not wrapped in ``~~``. Returning None when
    unsure is deliberate: a fabricated face invents crits (ARCHITECTURE.md §6.1).
    """
    group = _D20_GROUP.search(text)
    if not group:
        return None
    for entry in group.group(1).split(","):
        if "~~" in entry:
            continue  # dropped by kh/kl
        digits = re.search(r"\d+", entry)
        if digits:
            return int(digits.group())
    return None


def parse_embed(message: Dict[str, Any]) -> ParsedAvraeMessage:
    """Parse one Avrae message dict. Never raises; returns UNKNOWN instead."""
    interaction = message.get("interaction") or {}
    interaction_user_id = None
    if interaction.get("user"):
        try:
            interaction_user_id = int(interaction["user"]["id"])
        except (KeyError, TypeError, ValueError):
            interaction_user_id = None

    referenced_user_id = None
    ref = (message.get("referenced_message") or {}).get("author") or {}
    if ref.get("id"):
        try:
            referenced_user_id = int(ref["id"])
        except (TypeError, ValueError):
            referenced_user_id = None

    embeds = message.get("embeds") or []
    if not embeds:
        content = (message.get("content") or "").strip()
        return ParsedAvraeMessage(
            status=ParseStatus.IRRELEVANT if not content else ParseStatus.UNKNOWN,
            interaction_user_id=interaction_user_id,
            referenced_user_id=referenced_user_id,
            reason="no embeds",
        )

    embed = embeds[0]
    text = _embed_text(embed)
    lowered = text.lower()

    combatants = _parse_initiative(embed, text)
    if combatants:
        return ParsedAvraeMessage(
            status=ParseStatus.INITIATIVE,
            combatants=tuple(combatants),
            interaction_user_id=interaction_user_id,
            referenced_user_id=referenced_user_id,
        )

    actor, kind, key = _actor_and_kind(embed)
    total = _find_total(text)

    if total is not None:
        return ParsedAvraeMessage(
            status=ParseStatus.ROLL,
            roll=AvraeRoll(
                actor=actor,
                total=total,
                natural=_find_natural(text),
                kind=kind,
                key=key,
                raw=text,
            ),
            interaction_user_id=interaction_user_id,
            referenced_user_id=referenced_user_id,
        )

    if any(hint in lowered for hint in _IRRELEVANT_HINTS):
        return ParsedAvraeMessage(
            status=ParseStatus.IRRELEVANT,
            interaction_user_id=interaction_user_id,
            referenced_user_id=referenced_user_id,
            reason="matched irrelevant hint",
        )

    return ParsedAvraeMessage(
        status=ParseStatus.UNKNOWN,
        interaction_user_id=interaction_user_id,
        referenced_user_id=referenced_user_id,
        reason="embed present but no total found",
    )


def _parse_initiative(embed: Dict[str, Any], text: str) -> List[CombatantView]:
    """Extract the combatant mirror from an initiative tracker embed."""
    title = (embed.get("title") or "").lower()
    if "initiative" not in title and "current initiative" not in text.lower():
        return []

    out: List[CombatantView] = []
    for line in text.splitlines():
        hp = _HP.search(line)
        if not hp:
            continue
        name = re.sub(r"^[\s\-*_#>0-9.:]+", "", line).split("<")[0].split("(")[0].strip()
        conditions = tuple(c.strip() for c in _CONDITIONS.findall(line))
        deaths = None
        if "death save" in line.lower():
            deaths = line.strip()
        out.append(
            CombatantView(
                name=name,
                hp_current=int(hp.group(1)),
                hp_max=int(hp.group(2)),
                conditions=conditions,
                death_saves=deaths,
            )
        )
    return out
