"""The model's tool surface (ARCHITECTURE.md §5.1, §7.2).

**Design decision: narrow, flat tools rather than one generic ``update_campaign_state``.**

§7.2 specifies ``update_campaign_state(mutations, reason)`` as the *store* API, and that
is what ``CampaignStore.apply`` provides. It is a poor *tool* API. Models are markedly
more reliable with flat, typed parameters than with an array of free-form nested objects,
and every narrow tool can carry its own bounds, its own description, and its own failure
message. Each one maps to store mutations internally, so the store API is unchanged.

``open_check`` is the important one, and its description does a job the system instruction
alone does not reliably do: it tells the model, at the point of use, that calling this
means stopping.
"""

from __future__ import annotations

from typing import Any, Dict, List

from google.genai import types

OPEN_CHECK = "open_check"
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
        name=OPEN_CHECK,
        description=(
            "Require a dice roll. Call this the moment an action could fail and the "
            "failure would matter. You MUST supply the DC and BOTH outcomes now, before "
            "any dice are rolled -- they are recorded and cannot be changed afterwards. "
            "After calling this, STOP. Do not describe the result. The rules engine "
            "resolves the roll and will hand you the outcome as fact to narrate."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING, description="Character name exactly as the party roster gives it."),
                "kind": _s(type=_STRING, enum=["check", "save"]),
                "key": _s(
                    type=_STRING,
                    description=(
                        "Skill (e.g. 'sleight of hand', 'stealth'), or ability for a save "
                        "(e.g. 'dexterity')."
                    ),
                ),
                "dc": _s(type=_INTEGER, description="Difficulty class. 10 easy, 15 moderate, 20 hard."),
                "on_success": _s(type=_STRING, description="What happens if they succeed. One or two sentences."),
                "on_failure": _s(
                    type=_STRING,
                    description=(
                        "What happens if they fail. Must be a real consequence, not "
                        "'nothing happens' -- if failure costs nothing, do not call this."
                    ),
                ),
                "stakes": _s(type=_STRING, description="Optional. What is at risk, for your own later reference."),
            },
            required=["actor", "kind", "key", "dc", "on_success", "on_failure"],
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
