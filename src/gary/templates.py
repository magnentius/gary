"""Avrae command rendering (ARCHITECTURE.md §5.3).

The model never writes an Avrae command. It calls ``open_check`` with a *canonical key*,
and Gary renders the command from this table. A hallucinated ``!init end``, a mention
injected into a skill name, or a malformed alias cannot reach the channel, because the
only strings ever posted are built here from an allowlist.

Multi-word skill tokens were confirmed against a live Avrae on 2026-08-21, and the
inference was wrong. Avrae's internal camelCase keys (``sleightOfHand``) are not what its
command parser wants: it fuzzy-matches them and can answer with a "Multiple Matches Found"
selector instead of rolling. The display name works, quoted -- ``!check "sleight of hand"``
-- because an exact match beats every fuzzy candidate. A bare prefix (``!check sleight``)
also rolls, but stays hostage to whatever skills Avrae adds later.
"""

from __future__ import annotations

import re
from typing import Mapping

from .models import CheckKind

# Canonical snake_case key -> the token Avrae's fuzzy skill matcher expects.
AVRAE_SKILL_TOKEN: Mapping[str, str] = {
    "acrobatics": "acrobatics",
    "animal_handling": "animal handling",
    "arcana": "arcana",
    "athletics": "athletics",
    "deception": "deception",
    "history": "history",
    "insight": "insight",
    "intimidation": "intimidation",
    "investigation": "investigation",
    "medicine": "medicine",
    "nature": "nature",
    "perception": "perception",
    "performance": "performance",
    "persuasion": "persuasion",
    "religion": "religion",
    "sleight_of_hand": "sleight of hand",
    "stealth": "stealth",
    "survival": "survival",
    "initiative": "initiative",
}

AVRAE_SAVE_TOKEN: Mapping[str, str] = {
    "strength": "strength",
    "dexterity": "dexterity",
    "constitution": "constitution",
    "intelligence": "intelligence",
    "wisdom": "wisdom",
    "charisma": "charisma",
}

TEMPLATES: Mapping[CheckKind, str] = {
    CheckKind.CHECK: "!check {token}",
    CheckKind.SAVE: "!save {token}",
    CheckKind.ATTACK: '!attack "{token}"',
}

# Attack names are open-ended ("Longsword", "Fire Bolt", "Shortbow +1"), so they get a
# charset guard rather than an allowlist. Deliberately excludes the characters that make
# Discord or Avrae interpret the string as something other than a name.
_ATTACK_NAME = re.compile(r"^[A-Za-z0-9 '\-+]{1,40}$")


class TemplateError(ValueError):
    """Raised when a check cannot be rendered into a safe command."""


def normalize_key(kind: CheckKind, key: str) -> str:
    """Map a loosely-written key onto its canonical form.

    Accepts what a model plausibly emits ("Sleight of Hand", "sleight-of-hand", "dex")
    and returns the canonical key, or raises. Attack names pass through unchanged after
    a charset check.
    """
    raw = key.strip()
    if not raw:
        raise TemplateError("empty key")

    if kind is CheckKind.ATTACK:
        if not _ATTACK_NAME.match(raw):
            raise TemplateError(f"unsafe attack name: {key!r}")
        return raw

    slug = re.sub(r"[\s\-]+", "_", raw.lower())

    if kind is CheckKind.CHECK:
        if slug in AVRAE_SKILL_TOKEN:
            return slug
        raise TemplateError(f"unknown skill: {key!r}")

    if kind is CheckKind.SAVE:
        if slug in AVRAE_SAVE_TOKEN:
            return slug
        # "dex" / "dex_save" / "dexterity_save" are all common model output.
        slug = slug.removesuffix("_save")
        for full in AVRAE_SAVE_TOKEN:
            if full == slug or (full.startswith(slug) and len(slug) >= 3):
                return full
        raise TemplateError(f"unknown save: {key!r}")

    raise TemplateError(f"unsupported kind: {kind!r}")


def render_command(kind: CheckKind, key: str) -> str:
    """Render the exact command string to post. Raises rather than emitting anything unsafe."""
    canonical = normalize_key(kind, key)

    if kind is CheckKind.CHECK:
        token = AVRAE_SKILL_TOKEN[canonical]
    elif kind is CheckKind.SAVE:
        token = AVRAE_SAVE_TOKEN[canonical]
    else:
        token = canonical

    # A token with a space must be quoted or Avrae reads only the first word. Confirmed
    # live: `!check "sleight of hand"` rolls; `!check sleightOfHand` opens a selector.
    # ATTACK is excluded because its template already quotes -- attack names are
    # open-ended and were always multi-word ("Fire Bolt").
    if " " in token and kind is not CheckKind.ATTACK:
        token = f'"{token}"'

    command = TEMPLATES[kind].format(token=token)

    # Belt and braces: nothing rendered here may span lines or ping anyone, whatever a
    # future template edit does.
    if "\n" in command or "@" in command or command.count('"') % 2 or '""' in command:
        raise TemplateError(f"rendered unsafe command: {command!r}")
    return command
