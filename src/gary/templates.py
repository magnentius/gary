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

import json
import re
from pathlib import Path
from typing import Mapping, Optional

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
    # Not an ability save, but Avrae takes it the same way -- `!save death`, confirmed by
    # the save_death fixture captured live. Without it a character at 0 HP cannot roll the
    # save that decides whether they live, which is the one roll that must not be improvised.
    "death": "death",
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
# Avrae's tracker names: "GU1", "NO1", "Caspian Varr", "Ana-Maria". Every separator must
# be followed by an alphanumeric, so "NO1 -f" is refused -- otherwise a target could
# smuggle a flag into the command, which is the whole reason §5.3 renders from an
# allowlist rather than letting the model write commands.
_COMBATANT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9']{0,29}(?:[ -][A-Za-z0-9'][A-Za-z0-9']{0,29})*$")
# Avrae's built-in coinpurse, per `!help game coinpurse` (2026-08-21):
#
#     !game coins <amount>
#
# "The amount can either be a number like +102.13 (gold pieces), or explicit numbers of
# currencies like +10pp +2gp +1sp +3cp." Avrae autoconverts, breaking larger coins for
# change. `coinpurse` and `coin` are accepted in place of `coins`.
#
# Built in, which matters more than it sounds: an earlier version rendered a workshop
# alias, and an alias that is not installed does nothing at all -- silently -- while the
# fiction says the pint was paid for.
#
# The SIGN is required on every term. An unsigned "1gp 5sp" was refused on 2026-08-21,
# which was first misread here as a ban on mixed denominations.
#
# A term is signed, and is either an explicit denomination or a bare number of gold --
# which may carry decimals. Written as one alternation so "+1.5gp" is refused: decimals
# are only meaningful on the bare-gold form.
_COIN_TERM = re.compile(r"[+-]\d{1,6}(?:\.\d{1,2}|pp|gp|ep|sp|cp)?")
_COIN_AMOUNT = re.compile(r"^(?:[+-]\d{1,6}(?:\.\d{1,2}|pp|gp|ep|sp|cp)?){1,5}$")


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


# Per `!help game longrest` / `shortrest` (2026-08-21). Avrae resets each character's
# counters; the clock is Gary's (§3). A rest is per-character but time is per-world, so
# these durations are applied ONCE for the whole party however many people rest.
# Per `!help game deathsave` (2026-08-21). A critical hit on a creature at 0 HP
# costs two failed death saves; Avrae applies the damage and tracks neither.
DEATHSAVE_FAIL = "!game deathsave fail"

REST_MINUTES = {"short": 60, "long": 480}
REST_COMMAND = {"short": "!game shortrest", "long": "!game longrest"}


def render_rest_command(kind: str) -> str:
    """The Avrae command each player runs for a rest, or raise."""
    try:
        return REST_COMMAND[kind.strip().lower()]
    except KeyError:
        raise TemplateError(f"rest kind must be 'short' or 'long', not {kind!r}")


def _prone_modifier(target_is_down: bool, ranged: bool) -> str:
    """The modifier for attacking a creature at 0 HP. Advantage, at any range.

    A creature at 0 HP has the Unconscious condition, not merely Prone, and the SRD
    separates them: Prone gives "Advantage if the attacker is within 5 feet of you.
    Otherwise, that attack roll has Disadvantage", while Unconscious says flatly "Attack
    rolls against you have Advantage".

    An earlier version rendered disadvantage for a ranged attack on a downed target,
    reasoning from Prone alone. That was wrong, and reading the extracted glossary is
    what caught it. ``ranged`` is kept because it is genuinely needed for a target who is
    Prone but conscious — which Gary cannot currently tell apart.
    """
    del ranged  # Unconscious does not qualify on range
    return " adv" if target_is_down else ""


def render_attack_command(
    weapon: str, target: str, advantage: bool = False, ranged: bool = False
) -> str:
    """`!attack "Longsword" -t NO1`, or raise.

    Separate from render_command because an in-combat attack is not a check. It resolves
    against the target's AC, which Avrae owns (§3) -- Avrae rolls it, compares it and
    applies the damage to hit points it is already tracking. Gary commits no DC and has
    nothing to resolve; it renders the command and narrates what Avrae reports.
    """
    weapon = weapon.strip()
    target = target.strip()
    if not _ATTACK_NAME.match(weapon):
        raise TemplateError(f"unsafe weapon name: {weapon!r}")
    if not _COMBATANT_NAME.match(target):
        raise TemplateError(
            f"unsafe target {target!r} — use the name from the initiative tracker, e.g. 'NO1'"
        )
    return f'!attack "{weapon}" -t {target}' + _prone_modifier(advantage, ranged)


def render_enemy_attack_command(
    attack: str, target: str, advantage: bool = False, ranged: bool = False
) -> str:
    """`!init attack "Spear" -t "Caspian Varr"`, or raise.

    Attacks as whoever's turn it currently is, which is how a monster acts. Monsters do
    not act on their own in Avrae -- whoever added them runs their turns -- and Gary
    cannot run commands itself (§1: bots cannot invoke bots). So the Director pattern
    applies to the opposition too: Gary decides what the creature does and posts the
    command for a human to paste.
    """
    attack = attack.strip()
    target = target.strip()
    if not _ATTACK_NAME.match(attack):
        raise TemplateError(f"unsafe attack name: {attack!r}")
    if not _COMBATANT_NAME.match(target):
        raise TemplateError(
            f"unsafe target {target!r} — use the name from the initiative tracker"
        )
    return f'!init attack "{attack}" -t "{target}"' + _prone_modifier(advantage, ranged)


# The 5e conditions, plus room for a narrative label. Per `!help init effect` the command
# accepts a large mechanical argument set -- `-ac +10`, `-maxhp +100`, `-attack`,
# `-immune`, `adv` -- so NONE of it is rendered here. A model that can pass those under a
# condition name can grant itself armour class, hit points and attacks, which is precisely
# what §5.3 renders from an allowlist to prevent. Name and duration only.
_CONDITIONS_PATH = Path(__file__).with_name("data") / "conditions.json"


def _load_conditions() -> Mapping[str, Mapping[str, str]]:
    try:
        return json.loads(_CONDITIONS_PATH.read_text())["conditions"]
    except (OSError, ValueError, KeyError):  # pragma: no cover - data is shipped
        return {}


#: SRD 5.2.1 conditions, folded name -> {name, rules}. Extracted from the glossary by
#: scripts/extract_srd_conditions.py. The rules text matters as much as the names: Avrae
#: tracks a condition once told and never infers its consequences, so the model needs to
#: know that Prone means advantage within 5 feet and disadvantage beyond.
CONDITIONS: Mapping[str, Mapping[str, str]] = _load_conditions()


def condition(name: str) -> Optional[Mapping[str, str]]:
    """Look up an SRD condition by name, forgiving case."""
    return CONDITIONS.get(name.strip().lower())
_EFFECT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 '-]{0,29}$")


def render_effect_command(
    target: str, effect: str, rounds: Optional[int] = None
) -> str:
    """`!init effect GU1 Poisoned -dur 3`, or raise.

    Conditions are mechanical facts Avrae will track faithfully once told, and will never
    infer -- it held Caspian at 0 HP for three rounds without Prone or Incapacitated on
    him. Telling it is adjudication, which is Gary's (§3).
    """
    target = target.strip()
    effect = effect.strip()
    if not _COMBATANT_NAME.match(target):
        raise TemplateError(f"unsafe target {target!r} — use the tracker name")
    if not _EFFECT_NAME.match(effect):
        raise TemplateError(
            f"unsafe effect name {effect!r} — a condition or a short label, no arguments"
        )
    # Both quoted: a multi-word target read bare would split, making "Caspian Varr"
    # a target of "Caspian" with an effect called "Varr".
    # Canonical capitalisation when it is a real condition, so the tracker reads "Prone"
    # rather than whatever case the model used.
    known = condition(effect)
    if known is not None:
        effect = known["name"]
    command = f'!init effect "{target}" "{effect}"'
    if rounds is not None:
        if not isinstance(rounds, int) or not 1 <= rounds <= 100:
            raise TemplateError(f"duration {rounds!r} must be 1-100 rounds")
        command += f" -dur {rounds}"
    return command


def render_remove_effect_command(target: str, effect: Optional[str] = None) -> str:
    """`!init re "GU1" "Poisoned"`, or raise.

    Omitting the effect removes every effect on that combatant, per `!help init re`. That
    is a blunt instrument -- it clears conditions the DM applied deliberately as readily
    as the one that just ended -- so the caller has to mean it.
    """
    target = target.strip()
    if not _COMBATANT_NAME.match(target):
        raise TemplateError(f"unsafe target {target!r} — use the tracker name")
    if effect is None or not effect.strip():
        return f'!init re "{target}"'
    effect = effect.strip()
    if not _EFFECT_NAME.match(effect):
        raise TemplateError(
            f"unsafe effect name {effect!r} — a condition or a short label, no arguments"
        )
    known = condition(effect)
    if known is not None:
        effect = known["name"]
    return f'!init re "{target}" "{effect}"'


def render_coin_command(amount: str, command: str = "game coins") -> str:
    """Render an Avrae coinpurse invocation, or raise.

    Avrae owns coin (§3), so Gary never records a payment — it posts a command the player
    runs, the same Director pattern as a check. The alias refuses an overspend itself
    ("can't afford that much"), which is why Gary does not need to know the balance.
    """
    raw = amount.strip().replace(" ", "")
    if not _COIN_AMOUNT.match(raw):
        raise TemplateError(
            f"coin amount {amount!r} must be signed terms like '-2cp', '+5gp', '-2' or "
            "'+102.13' (bare numbers are gold), or several such as '-1gp -5sp'. Every term "
            "needs its own sign, and decimals apply only to bare gold."
        )
    if not command.strip() or not all(w.isalnum() for w in command.split()):
        raise TemplateError(f"unsafe coin command: {command!r}")
    # Re-spaced for readability at the table. Spacing is irrelevant to the alias, but
    # "-1gp -5sp" is what a player reads back as a price; "-1gp-5sp" is not.
    return f"!{command} " + " ".join(_COIN_TERM.findall(raw))


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
