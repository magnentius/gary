"""Avrae embed parsing (ARCHITECTURE.md §6).

**Avrae's embed layout is not an API.** It changes between releases and differs across
``!check``, ``!save``, ``!attack`` and ``!init list``. So this module is built on two
rules:

1. **Fail loud.** A parse miss returns ``UNKNOWN``. It never degrades to ``total=0`` or
   ``is_dying=False`` -- a silent false negative on a death save is far worse than
   asking the player to repeat themselves.
2. **Fixtures are the spec.** Every pattern here is traced to Avrae's own source
   (``cogs5e/utils/checkutils.py``, ``cogs5e/initiative/combat.py``) at ``avrae@cb75be2``,
   plus the ``d20`` library's ``MarkdownStringifier``, which was run locally to confirm
   how dice render. ``tests/test_parser.py`` drives every fixture through ``parse_embed``,
   so a format change shows up as a failing fixture rather than a silent misread.

**Two containers, not one.** Rolls arrive as embeds. The **initiative tracker is not an
embed at all** -- it is a pinned plain message wrapping a ```md code block in
``message.content``. A parser that only inspects ``message.embeds`` never sees combat
state.

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
    AVRAE_PROMPT = "avrae_prompt"  # Avrae is asking the player something -- a roll may still come
    PROMPT_ABANDONED = "prompt_abandoned"  # that question lapsed; no roll is coming
    UNKNOWN = "unknown"  # looked like a roll but could not be read -- escalate


@dataclass(frozen=True)
class CombatantView:
    """Read-only mirror of Avrae's combat state (ARCHITECTURE.md §3: Avrae owns this)."""

    name: str
    hp_current: Optional[int] = None
    hp_max: Optional[int] = None
    conditions: tuple = ()
    is_current_turn: bool = False

    @property
    def is_dying(self) -> bool:
        """At 0 HP, or carrying an effect that says so.

        Avrae's tracker shows no death-save counter, so 0 HP is the signal available.
        Gary mirrors this for narrative colour only; Avrae remains authoritative (§3).
        """
        if self.hp_current == 0:
            return True
        return any("dying" in c.lower() or "unconscious" in c.lower() for c in self.conditions)


@dataclass(frozen=True)
class ParsedAvraeMessage:
    status: ParseStatus
    roll: Optional[AvraeRoll] = None
    combatants: tuple = ()
    interaction_user_id: Optional[int] = None
    referenced_user_id: Optional[int] = None
    reason: str = ""


# -- patterns ---------------------------------------------------------------------------
# Traced to avrae@cb75be2 (2026-08-06) and to d20's MarkdownStringifier, which was run
# locally to confirm dice rendering. Not guesses -- but not immune to Avrae changing
# either, which is what the fixture suite is for.

_TOTAL_LABELLED = re.compile(r"\*\*(?:Total|Result)\*\*:?\s*(-?\d+)", re.IGNORECASE)
# checkutils does `from d20 import roll`, so the default MarkdownStringifier applies and
# the shape is "1d20 (13) + 7 = `20`" -- no bold wrapper. VerboseMDStringifier, used by
# other commands, produces "**Total**: N" instead, which _TOTAL_LABELLED covers.
_TOTAL_EQUALS = re.compile(r"=\s*`(-?\d+)`")
_TOTAL_BARE = re.compile(r"\btotal\b[^0-9-]{0,12}(-?\d+)", re.IGNORECASE)
# The parenthesised dice group: "1d20 (13)", "2d20kh1 (17, ~~3~~)", "1d20 (**20**)".
# Leading count is variable (advantage/disadvantage roll 2d20), and Avrae strikes through
# dropped dice -- which is why the face cannot just be the first number in the group.
_D20_GROUP = re.compile(r"\b\d*d20[a-z0-9]*\s*\(([^)]*)\)", re.IGNORECASE)
_TITLE_CHECK = re.compile(r"^(.+?)\s+makes?\s+an?\s+(.+?)\s+check", re.IGNORECASE)
_TITLE_SAVE = re.compile(r"^(.+?)\s+makes?\s+an?\s+(.+?)\s+(?:saving throw|save)", re.IGNORECASE)
_TITLE_ATTACK = re.compile(r"^(.+?)\s+attacks?\s+with\s+(?:an?\s+)?(.+?)!?$", re.IGNORECASE)
# Combatant line: "# 18: Dorn <28/34 HP> (AC 16, Prone)". Leading "# " marks whose turn
# it is; everyone else is indented two spaces.
_COMBATANT = re.compile(
    r"^(?P<turn>#)?\s*(?P<init>-?\d+):\s+(?P<name>.+?)\s+<(?P<hp>[^>]*)>\s*(?:\((?P<effects>[^)]*)\))?\s*$"
)
_HP = re.compile(r"(\d+)\s*/\s*(\d+)\s*HP", re.IGNORECASE)
_HP_BARE = re.compile(r"^(\d+)\s*HP", re.IGNORECASE)
_INIT_HEADER = re.compile(r"^(?P<name>.+?):\s*-?\d+\s*\(round\s*\d+\)\s*$", re.IGNORECASE)

# Avrae's disambiguation selector. Confirmed live 2026-08-20: an embed titled "Multiple
# Matches Found" whose description asks "Which one were you looking for? (Type the number
# or `c` to cancel)". No roll happened and Avrae is now waiting on the player, so treating
# it as an unreadable roll makes Gary interrupt with "what did you roll?" while Avrae is
# mid-question -- two bots interrogating one player.
_AVRAE_PROMPT = re.compile(
    r"multiple matches found|which one were you looking for|type the number", re.IGNORECASE
)
# The tail of a selector nobody answered. Distinct from AVRAE_PROMPT because it is
# terminal: no roll is coming from that command, so the Director can re-prompt instead of
# leaving the check to lapse on TTL. Confirmed live 2026-08-20.
_PROMPT_ABANDONED = re.compile(r"selection timed out|selection .{0,20}cancelled", re.IGNORECASE)
# Combatant status announcements: "GO1: <Dead>", "GO1: <Bloodied>". Confirmed live
# 2026-08-20 from `!init hp GO1 set 0`. Avrae owns combat state (§3); these never resolve
# a check.
_COMBATANT_STATUS = re.compile(r"^\S+:\s*<[^>]+>\s*$")

# Titles that are confidently not rolls. Anything not matched here and not parsed as a
# roll becomes UNKNOWN rather than being silently dropped.
_IRRELEVANT_HINTS = (
    "error",
    "help",
    "commands",
    "invalid",
    "usage",
    "character sheet",
    "settings",
    "no active character",
    # Confirmed live 2026-08-20: initiative chatter that carries no roll. Without these
    # an empty tracker makes Gary ask the table what they rolled.
    "awaiting combatants",
    "no combatants",
    "roll for initiative",
    # Confirmed live 2026-08-20. "Selection timed out" is the tail of an AVRAE_PROMPT the
    # player never answered. "Cannot cast" is an in-game refusal rendered as an EMBED --
    # ARCHITECTURE.md §6 says errors are plain text, which is true of command errors but
    # not of these. Neither carries a roll, so neither may reach the player as a question.
    "cannot cast",
    # Initiative chatter, confirmed live 2026-08-20. DANGER: "added to combat" lines read
    # "... initiative 1d20 (4) + 0 = `4`", which _TOTAL_EQUALS matches. They are safe only
    # because the no-embed path never mines content for a total. If that ever changes, an
    # initiative roll could resolve a pending skill check -- someone else's dice deciding
    # another character's outcome, which §3 gives to Avrae and §5 gives to the resolver.
    "added to combat",
    "already in combat",
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

    content = (message.get("content") or "").strip()

    # The initiative tracker lives in message.content, not in an embed. Checked first so
    # combat state is never mistaken for a roll.
    combatants = parse_initiative(content)
    if combatants is not None:
        return ParsedAvraeMessage(
            status=ParseStatus.INITIATIVE,
            combatants=tuple(combatants),
            interaction_user_id=interaction_user_id,
            referenced_user_id=referenced_user_id,
        )

    embeds = message.get("embeds") or []
    if not embeds:
        # Avrae reports command errors as plain text ("Error: ..."), not embeds. Without
        # this, an error arriving while a check is pending reads as an unparseable roll
        # and Gary asks the player what they rolled -- when nothing was rolled at all.
        lowered_content = content.lower()
        if _COMBATANT_STATUS.match(content):
            return ParsedAvraeMessage(
                status=ParseStatus.IRRELEVANT,
                interaction_user_id=interaction_user_id,
                referenced_user_id=referenced_user_id,
                reason="combatant status line",
            )
        if _PROMPT_ABANDONED.search(content):
            return ParsedAvraeMessage(
                status=ParseStatus.PROMPT_ABANDONED,
                interaction_user_id=interaction_user_id,
                referenced_user_id=referenced_user_id,
                reason="selector lapsed without an answer",
            )
        if not content or any(hint in lowered_content for hint in _IRRELEVANT_HINTS):
            return ParsedAvraeMessage(
                status=ParseStatus.IRRELEVANT,
                interaction_user_id=interaction_user_id,
                referenced_user_id=referenced_user_id,
                reason="plain text, no roll" if content else "empty message",
            )
        return ParsedAvraeMessage(
            status=ParseStatus.UNKNOWN,
            interaction_user_id=interaction_user_id,
            referenced_user_id=referenced_user_id,
            reason="no embeds",
        )

    embed = embeds[0]
    text = _embed_text(embed)
    lowered = text.lower()

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

    if _AVRAE_PROMPT.search(text):
        return ParsedAvraeMessage(
            status=ParseStatus.AVRAE_PROMPT,
            interaction_user_id=interaction_user_id,
            referenced_user_id=referenced_user_id,
            reason="avrae is awaiting player input",
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


def parse_initiative(content: str) -> Optional[List[CombatantView]]:
    """Parse the initiative tracker out of a message's content.

    Returns None when this is not a tracker at all, and a (possibly empty) list when it
    is -- the distinction matters, because an empty combat is still combat and must not
    fall through to roll parsing.
    """
    if "```" not in content:
        return None

    body = content.split("```", 2)
    if len(body) < 2:
        return None
    block = body[1]
    if block.startswith("md"):
        block = block[2:]

    lines = [ln for ln in block.splitlines() if ln.strip()]
    if not lines:
        return None
    if not _INIT_HEADER.match(lines[0].strip()):
        return None

    out: List[CombatantView] = []
    for line in lines[1:]:
        if set(line.strip()) <= {"="}:  # the ==== underline
            continue
        match = _COMBATANT.match(line.rstrip())
        if not match:
            continue

        hp_text = match.group("hp") or ""
        current = maximum = None
        pair = _HP.search(hp_text)
        if pair:
            current, maximum = int(pair.group(1)), int(pair.group(2))
        else:
            bare = _HP_BARE.match(hp_text.strip())
            if bare:
                current = int(bare.group(1))

        raw_effects = match.group("effects") or ""
        conditions = tuple(
            e.strip()
            for e in raw_effects.split(",")
            # AC is a stat Avrae renders alongside effects, not a condition.
            if e.strip() and not e.strip().upper().startswith("AC ")
        )

        out.append(
            CombatantView(
                name=match.group("name").strip(),
                hp_current=current,
                hp_max=maximum,
                conditions=conditions,
                is_current_turn=bool(match.group("turn")),
            )
        )
    return out
