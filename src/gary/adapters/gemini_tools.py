"""The model's tool surface (ARCHITECTURE.md §5.1, §7.2).

**Design decision: narrow, flat tools rather than one generic ``update_campaign_state``.**

§7.2 specifies ``update_campaign_state(mutations, reason)`` as the *store* API, and that
is what ``CampaignStore.apply`` provides. It is a poor *tool* API. Models are markedly
more reliable with flat, typed parameters than with an array of free-form nested objects,
and every narrow tool can carry its own bounds, its own description, and its own failure
message. Each one maps to store mutations internally, so the store API is unchanged.

``make_move`` is the important one, and its description does a job the system instruction
alone does not reliably do: it tells the model, at the point of use, that calling this
means stopping -- and lists every move's trigger where the model is choosing one.
"""

from __future__ import annotations

from typing import Any, Dict, List

from google.genai import types

from ..moves import MOVES
from ..sheets import STATS

MAKE_MOVE = "make_move"
#: The tool moves replaced. Histories written before still hold unanswered calls to it.
LEGACY_OPEN_CHECK = "open_check"
ADVANCE_TIME = "advance_time"
GIVE_ITEMS = "give_items"
TAKE_ITEMS = "take_items"
RECORD_NOTE = "record_note"
SET_SCENE = "set_scene"
UPSERT_NPC = "upsert_npc"


def _s(**kw) -> types.Schema:
    return types.Schema(**kw)


_STRING = types.Type.STRING
_INTEGER = types.Type.INTEGER
_ARRAY = types.Type.ARRAY
_OBJECT = types.Type.OBJECT


DECLARATIONS: List[types.FunctionDeclaration] = [
    types.FunctionDeclaration(
        name=MAKE_MOVE,
        description=(
            "A player's action triggers a move: call this, the moment it does. The move "
            "fixes what every result means, so you decide nothing about the outcome -- "
            "only which move the fiction triggered and, for Defy Danger, which stat their "
            "approach uses. After calling this, STOP. Do not describe the result. The "
            "player rolls, and the result comes back to you as fact to narrate.\n"
            + "\n".join(f"- {m.key}: when you {m.trigger}" for m in MOVES.values())
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING, description="Character name exactly as the party roster gives it."),
                "move": _s(type=_STRING, enum=sorted(MOVES)),
                "stat": _s(
                    type=_STRING,
                    enum=list(STATS),
                    description=(
                        "Defy Danger only: str powering through, dex getting out of the "
                        "way or acting fast, con enduring, int quick thinking, wis mental "
                        "fortitude, cha charm and social grace. Every other move has its "
                        "own stat; omit it."
                    ),
                ),
                "stakes": _s(type=_STRING, description="Optional. The danger or what is at risk, for your own later reference."),
            },
            required=["actor", "move"],
        ),
    ),
    types.FunctionDeclaration(
        name=ADVANCE_TIME,
        description=(
            "Advance the world clock and expire finished effects. A night's rest is 480, "
            "and is advanced once for the whole party. Searching a room is about 10, a "
            "conversation about 5, crossing a town about 30."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "minutes": _s(type=_INTEGER, description="Elapsed in-world minutes."),
                "reason": _s(type=_STRING, description="Short reason, e.g. 'short rest'."),
            },
            required=["minutes", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=GIVE_ITEMS,
        description="Record items a character gains — found, bought, given, or looted.",
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING),
                "items": _s(type=_ARRAY, items=_s(type=_STRING)),
                "reason": _s(type=_STRING),
            },
            required=["actor", "items", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=TAKE_ITEMS,
        description="Record items consumed, sold, lost, or stolen.",
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING),
                "items": _s(type=_ARRAY, items=_s(type=_STRING)),
                "reason": _s(type=_STRING),
            },
            required=["actor", "items", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=RECORD_NOTE,
        description=(
            "Remember something for later: gossip an NPC volunteers, a rumour, a deadline, "
            "a threat, a lie you told, a figure noticed across a room. Retrieved into your "
            "context when the subject comes up again, so record anything you would be "
            "embarrassed to contradict in three sessions. If you invented a detail nobody "
            "asked for, you invented it for a reason — this is where the reason survives."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "kind": _s(type=_STRING, enum=["npc", "clue", "promise", "foreshadow", "general"]),
                "subject": _s(type=_STRING, description="Who or what this is about."),
                "body": _s(type=_STRING),
            },
            required=["kind", "subject", "body"],
        ),
    ),
    types.FunctionDeclaration(
        name=UPSERT_NPC,
        description=(
            "Create or update an NPC the party has met OR merely heard named — someone "
            "another character mentions counts, and needs this more than someone present, "
            "because nothing else anchors them until the party finally meets them. A role "
            "a player could ask for ('the harbour master', 'Maren's son') is a name. Omit "
            "a field to leave it unchanged; disposition runs -5 hostile to +5 devoted."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "npc_id": _s(
                    type=_STRING,
                    description="Stable short id, lowercase, e.g. 'garik_harlen'. Reuse it to update.",
                ),
                "name": _s(type=_STRING, description="Display name, e.g. 'Garik Harlen'."),
                "disposition": _s(type=_INTEGER, description="-5 hostile to +5 devoted."),
                "note": _s(type=_STRING, description="Appended to what is already known."),
            },
            required=["npc_id"],
        ),
    ),
    types.FunctionDeclaration(
        name=SET_SCENE,
        description="Update where the party is. Call when they move somewhere meaningfully new.",
        parameters=_s(
            type=_OBJECT,
            properties={
                "location": _s(type=_STRING),
                "summary": _s(type=_STRING, description="One line on the current situation."),
            },
            required=["location"],
        ),
    ),
]


def tool_config() -> List[types.Tool]:
    return [types.Tool(function_declarations=DECLARATIONS)]


def known_names() -> frozenset:
    return frozenset(d.name for d in DECLARATIONS)
