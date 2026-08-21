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
# ADJUST_GOLD removed 2026-08-21: Avrae owns coin now (§3). Kept out of the tool
# surface entirely rather than left as a no-op, so the model cannot believe it paid.
GIVE_ITEMS = "give_items"
TAKE_ITEMS = "take_items"
RECORD_NOTE = "record_note"
SET_SCENE = "set_scene"
UPSERT_NPC = "upsert_npc"
REQUEST_PAYMENT = "request_payment"
REQUEST_REST = "request_rest"


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
                "actor": _s(type=_STRING, description="Character name exactly as it appears in Avrae."),
                "kind": _s(type=_STRING, enum=["check", "save", "attack"]),
                "key": _s(
                    type=_STRING,
                    description=(
                        "Skill (e.g. 'sleight of hand', 'stealth'), ability for a save "
                        "(e.g. 'dexterity'), or attack name (e.g. 'Longsword')."
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
            "Advance the world clock and expire finished effects. Short rest is 60, long "
            "rest is 480 — both fixed by the rules, so use exactly those. Searching a room "
            "is about 10, a conversation about 5, crossing a town about 30. Refused during "
            "combat: Avrae owns duration while initiative runs."
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
        description=(
            "Record items gained. Gary cannot write to D&D Beyond, so also tell the "
            "player in your narration to add them to their sheet."
        ),
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
            "Remember something for later: an NPC's attitude, a clue, a promise, a lie "
            "you told. These are retrieved into your context when the subject reappears, "
            "so record anything you would be embarrassed to contradict in three sessions."
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
        name=REQUEST_REST,
        description=(
            "The party rests. Call this ONCE for the whole party, however many characters "
            "are resting — it moves the world clock (short 60 minutes, long 480) and posts "
            "the Avrae command each player runs on their own sheet. Calling it per "
            "character would advance the clock several times for one night. Refused during "
            "combat. If a rest is interrupted, do not call this: narrate the interruption "
            "and use advance_time for however long actually passed."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"kind": _s(type=_STRING, enum=["short", "long"])},
            required=["kind"],
        ),
    ),
    types.FunctionDeclaration(
        name=REQUEST_PAYMENT,
        description=(
            "Ask a player to pay or receive coin. You do not hold coin and cannot move it "
            "— this posts a command the player runs on their own sheet, and their purse "
            "refuses an overspend by itself. ALWAYS sign every term: '-2cp' spends two "
            "copper, '+5gp' grants five gold, '-1gp -5sp' spends a gold and five silver. "
            "A bare number is gold and may have decimals ('-2', '+102.13'). Never state "
            "what someone can afford; you cannot see their purse."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING, description="Character name as it appears in Avrae."),
                "amount": _s(type=_STRING, description="Signed single denomination, e.g. '-2cp'."),
                "reason": _s(type=_STRING, description="What it is for, shown at the table."),
            },
            required=["actor", "amount"],
        ),
    ),
    types.FunctionDeclaration(
        name=UPSERT_NPC,
        description=(
            "Create or update an NPC the party has actually met or heard named. Use this "
            "the moment you give someone a name — a named character who exists only in "
            "your prose is forgotten, and you will contradict yourself about them later. "
            "Omit a field to leave it unchanged; disposition runs -5 hostile to +5 devoted."
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
