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
WEIGH_ENCOUNTER = "weigh_encounter"
START_COMBAT = "start_combat"
ATTACK = "attack"
ENEMY_ATTACK = "enemy_attack"
APPLY_EFFECT = "apply_effect"


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
                "kind": _s(type=_STRING, enum=["check", "save"]),
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
        name=APPLY_EFFECT,
        description=(
            "Put a condition on a combatant — poisoned, restrained, frightened, prone. "
            "Avrae tracks a condition faithfully once told and never infers one, so if "
            "you do not apply it, it does not exist mechanically and nobody at the table "
            "can see it. Use the standard condition names where one fits; a short label "
            "is fine for something narrative. Duration is in rounds; omit it for "
            "something that lasts until removed. You cannot pass mechanical arguments "
            "here, deliberately — nothing that changes AC, hit points, damage or attacks."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "target": _s(type=_STRING, description="Tracker name, e.g. 'GU1'."),
                "effect": _s(type=_STRING, description="Condition or short label, e.g. 'Poisoned'."),
                "rounds": _s(type=_INTEGER, description="Duration in rounds. Omit for indefinite."),
            },
            required=["target", "effect"],
        ),
    ),
    types.FunctionDeclaration(
        name=ENEMY_ATTACK,
        description=(
            "A monster takes its turn. Call this when initiative reaches a creature you "
            "control — they do not act on their own, and nothing else in the fight will "
            "make them. Narrate what the creature does, then call this; the command is "
            "posted for the DM to run, and it attacks as whoever's turn it currently is, "
            "so only call it on that creature's turn. Use `!init attack list` in the "
            "channel if you do not know a creature's attacks. You are told nothing until "
            "Avrae reports, and you do not decide whether it hit."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "attacker": _s(type=_STRING, description="Tracker name whose turn it is, e.g. 'GU2'."),
                "attack": _s(type=_STRING, description="The creature's attack, e.g. 'Spear'."),
                "target": _s(type=_STRING, description="Tracker name of the target, e.g. 'Caspian Varr'."),
                "ranged": _s(
                    type=types.Type.BOOLEAN,
                    description="True for a bow, thrown weapon or ranged spell. "
                                "Nothing tracks positions, so only you know.",
                ),
            },
            required=["attacker", "attack", "target"],
        ),
    ),
    types.FunctionDeclaration(
        name=ATTACK,
        description=(
            "Someone attacks in combat. This is NOT a check: Avrae owns the target's AC "
            "and hit points, so it rolls, compares and applies damage itself — you commit "
            "no DC and decide no outcome. Posts the command for the player to run on "
            "their turn; you are told nothing until Avrae reports. Target by the name in "
            "the initiative tracker ('NO1', 'GU1'), not the character's fictional name."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING, description="Who is attacking, as Avrae names them."),
                "weapon": _s(type=_STRING, description="Weapon or attack name, e.g. 'Longsword'."),
                "target": _s(type=_STRING, description="Tracker name of the target, e.g. 'NO1'."),
                "ranged": _s(
                    type=types.Type.BOOLEAN,
                    description="True for a bow, thrown weapon or ranged spell. "
                                "Nothing tracks positions, so only you know.",
                ),
            },
            required=["actor", "weapon", "target"],
        ),
    ),
    types.FunctionDeclaration(
        name=START_COMBAT,
        description=(
            "Begin initiative. Call this the moment a fight starts — before anyone swings "
            "— and never render an attack outside initiative: Avrae tracks turn order, "
            "hit points and durations only once combat has begun, so a fight started "
            "without it has no rounds and the enemies never act. Posts the commands the "
            "table runs. Weigh the line-up with weigh_encounter first."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "monsters": _s(
                    type=types.Type.ARRAY,
                    description="Creatures to add, duplicates listed separately. SRD names are looked up.",
                    items=_s(type=_STRING),
                ),
            },
            required=["monsters"],
        ),
    ),
    types.FunctionDeclaration(
        name=WEIGH_ENCOUNTER,
        description=(
            "Weigh a planned line-up against the Lazy Encounter Benchmark before you "
            "commit to it. Name every creature, listing duplicates separately. CRs are "
            "looked up in the SRD, so give the name exactly and do not worry about the "
            "number — pass a cr only for something homebrew that is not in the SRD, and "
            "the reply will tell you which names it did not recognise. Use it whenever "
            "more than one creature faces the party, or anything you suspect is large. "
            "Deadly is allowed; it is a gauge, not a veto."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "monsters": _s(
                    type=types.Type.ARRAY,
                    description="Every creature in the encounter, with duplicates listed separately.",
                    items=_s(
                        type=_OBJECT,
                        properties={
                            "name": _s(type=_STRING),
                            "cr": _s(
                                type=types.Type.NUMBER,
                                description="Only for homebrew. SRD creatures are looked up.",
                            ),
                        },
                        required=["name"],
                    ),
                ),
            },
            required=["monsters"],
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
