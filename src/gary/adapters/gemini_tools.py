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
ADD_MONSTERS = "add_monsters"
REMOVE_MONSTERS = "remove_monsters"
MONSTER_ATTACKS = "monster_attacks"
MAKE_CAMP = "make_camp"
GIVE_COINS = "give_coins"
BUY = "buy"
SELL = "sell"
USE_ITEM = "use_item"


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
            + "\n".join(f"- {m.key}: when you {m.trigger}" for m in MOVES.values() if m.callable)
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING, description="Character name exactly as the party roster gives it."),
                "move": _s(type=_STRING, enum=sorted(k for k, m in MOVES.items() if m.callable)),
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
                "target": _s(
                    type=_STRING,
                    description=(
                        "hack_and_slash and volley only: the creature attacked, exactly as "
                        "the scene lists it ('Goblin 2'). Add it with add_monsters first."
                    ),
                ),
                "stakes": _s(type=_STRING, description="Optional. The danger or what is at risk, for your own later reference."),
            },
            required=["actor", "move"],
        ),
    ),
    types.FunctionDeclaration(
        name=ADD_MONSTERS,
        description=(
            "Put creatures into the scene the moment they appear, before anyone fights "
            "them. Each becomes a numbered instance with its own HP ('Goblin 1', "
            "'Goblin 2') that attacks can target and you can see in the state. Dungeon "
            "World creatures are looked up -- name them and give nothing else. Give hp, "
            "armor and damage only for a creature of your own; it is checked against the "
            "rules for making monsters."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "monsters": _s(
                    type=_ARRAY,
                    items=_s(
                        type=_OBJECT,
                        properties={
                            "name": _s(type=_STRING, description="e.g. 'Goblin', 'Dwarven Warrior', or your own."),
                            "count": _s(type=_INTEGER, description="How many. Default 1."),
                            "hp": _s(type=_INTEGER, description="Your own creature only. Horde 3, group 6, solitary 12."),
                            "armor": _s(type=_INTEGER, description="Your own creature only. 0-4."),
                            "damage": _s(type=_STRING, description="Your own creature only, e.g. 'd6', 'd8+2', 'b[2d10]'."),
                        },
                        required=["name"],
                    ),
                ),
            },
            required=["monsters"],
        ),
    ),
    types.FunctionDeclaration(
        name=REMOVE_MONSTERS,
        description=(
            "Take creatures out of the scene: they fled, the party left, the fight is over. "
            "A defeated creature stays listed as defeated until you remove it. Omit names "
            "to clear every creature."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"names": _s(type=_ARRAY, items=_s(type=_STRING), description="Instance names, e.g. 'Goblin 1'.")},
        ),
    ),
    types.FunctionDeclaration(
        name=MONSTER_ATTACKS,
        description=(
            "A creature deals its damage to a character -- the GM move 'deal damage', when "
            "a player misses or everyone looks to you. Whether it happens is your call; "
            "how much is not: the rules engine rolls the creature's damage, takes off the "
            "character's armor, and tells you what landed. Do not use it for the enemy's "
            "attack on a weak Hack and Slash -- that one is rolled for you already."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "monster": _s(type=_STRING, description="Instance name, e.g. 'Goblin 2'."),
                "character": _s(type=_STRING, description="Character name exactly as the party roster gives it."),
            },
            required=["monster", "character"],
        ),
    ),
    types.FunctionDeclaration(
        name=MAKE_CAMP,
        description=(
            "The party settles in to rest for the night. Call it ONCE for everyone. Each "
            "character eats a ration; if the night is quiet, everyone who ate heals half "
            "their max HP and the clock moves eight hours. Whether something approaches in "
            "the night is your call, as dangerous as the place is: if it does, name who is "
            "on watch -- they roll Take Watch, nobody heals, and you narrate what came "
            "once the roll is in. Only somewhere they can actually settle in: not mid-fight, "
            "not mid-chase, not clinging to a cliff. If they try to rest in danger, tell them "
            "they can't, or let them try and set something_approaches."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "reason": _s(type=_STRING, description="Where they rest and why it is safe enough to sleep, e.g. 'the cleared guardroom, door barred'. Shown to the table."),
                "something_approaches": _s(type=types.Type.BOOLEAN, description="True if something comes in the night."),
                "watch": _s(type=_STRING, description="Who is on watch, if something approaches."),
            },
            required=["reason"],
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
        description=(
            "A character gains items -- found, looted, given. Name them as the book does "
            "where you can ('long sword', 'plate', 'bundle of arrows', '2 healing "
            "potions'): book items come with their real stats, so a long sword hits harder "
            "and plate is armor at once. Anything else ('a silver locket') is carried with "
            "no effect on the rules. '12 coins' is coin. Not for purchases: use buy."
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
        description=(
            "A character loses items -- dropped, used up, stolen, broken. Refused for "
            "anything they do not carry. Dropping a weapon or armor changes what they can "
            "attack with and their armor at once. Not for sales: use sell."
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
        name=USE_ITEM,
        description=(
            "Someone uses a healing item. A healing potion heals whoever drinks it 10 -- "
            "their own, or poured down a friend's throat (target). Bandages heal someone "
            "ELSE 4; poultices and herbs heal anyone 7; both spend a use, and both are slow, "
            "so not while creatures are still standing. The rules engine applies it and "
            "tells you what was healed; narrate that."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING, description="Who uses it -- whose item it is."),
                "item": _s(type=_STRING, description="'healing potion', 'bandages', 'poultices and herbs'."),
                "target": _s(type=_STRING, description="Who is healed, if not the actor."),
            },
            required=["actor", "item"],
        ),
    ),
    types.FunctionDeclaration(
        name=GIVE_COINS,
        description=(
            "Coin gained or lost outside a sale: treasure, a reward, a bribe, a fine. "
            "Positive gains, negative spends. Refused if they cannot pay."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING),
                "amount": _s(type=_INTEGER, description="Coins; negative to spend."),
                "reason": _s(type=_STRING, description="What it is for, shown at the table."),
            },
            required=["actor", "amount", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=BUY,
        description=(
            "A character buys something at the book's price, paid from their coin. Refused "
            "if they cannot afford it, or if the book gives it no price -- then name a price "
            "in the fiction, take it with give_coins, and hand the item over with give_items."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING),
                "item": _s(type=_STRING, description="e.g. 'long sword', '2 healing potions'."),
            },
            required=["actor", "item"],
        ),
    ),
    types.FunctionDeclaration(
        name=SELL,
        description=(
            "A character sells something they carry. A book item fetches half its price; "
            "give offer only if this buyer pays less (a fence, a stingy merchant) -- never "
            "more. An item the book does not price needs an offer, capped at 100 coins."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING),
                "item": _s(type=_STRING),
                "offer": _s(type=_INTEGER, description="Optional: what this buyer pays, if less."),
            },
            required=["actor", "item"],
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
