"""The model's tool surface (ARCHITECTURE.md §5.1, §12.6).

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
DEAL_DAMAGE = "deal_damage"
MAKE_CAMP = "make_camp"
GIVE_COINS = "give_coins"
BUY = "buy"
SELL = "sell"
USE_ITEM = "use_item"
PREPARE_SPELLS = "prepare_spells"
END_SPELL = "end_spell"
FORM_MOVES = "form_moves"
SPEND_HOLD = "spend_hold"
DEBILITY = "debility"
FORWARD = "forward"
HIRE = "hire"
FIRST_AID = "hireling_first_aid"
BOLSTER = "bolster"
CREATE_FRONT = "create_front"
ADVANCE_PORTENT = "advance_portent"
END_DANGER = "end_danger"
UPDATE_HIRELING = "update_hireling"
RECOVER = "recover"
PERILOUS_JOURNEY = "perilous_journey"
USE_POISON = "use_poison"
BREW_POISON = "brew_poison"


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
            + "\n".join(f"- {m.key}: when you {m.trigger}"
                        for m in MOVES.values() if m.callable and not m.classes)
            + "\nClass moves -- only a character of that class has them:\n"
            + "\n".join(f"- {m.key} ({'/'.join(m.classes)}): when you {m.trigger}"
                        for m in MOVES.values() if m.callable and m.classes)
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
                        "For an attack (hack_and_slash, volley, backstab, called_shot): the "
                        "creature, exactly as the scene lists it ('Goblin 2') -- add it with "
                        "add_monsters first. For lay_on_hands or arcane_art: the character "
                        "helped. For aid_or_interfere: the character helped or hindered. "
                        "For defend: who or what they defend -- a character, or 'the gate'. "
                        "For cast_a_spell: the creature a damaging spell hits (for "
                        "Fireball every creature caught in it, comma-separated), or the "
                        "character a healing spell or Bless is cast on. For shapeshifter: the "
                        "species to become ('bear', 'hawk'). For order_hirelings: the hireling given "
                        "the order. Omit otherwise."
                    ),
                ),
                "offers": _s(
                    type=_ARRAY, items=_s(type=_STRING, enum=["generous pay", "purpose", "share", "reputation"]),
                    description=(
                        "recruit only: what they made known -- generous pay, what they're setting out "
                        "to do (purpose), a share of what they find -- and reputation if they have a "
                        "useful one here. Each is +1."
                    ),
                ),
                "aide": _s(
                    type=_STRING,
                    description=(
                        "A hireling helping with this move, by name: a Warrior aiding an attack "
                        "(+their skill to damage; they take any counter-attack), or a Protector helping "
                        "Defy Danger (+1, but a 10+ counts as 7-9)."
                    ),
                ),
                "preparation": _s(
                    type=types.Type.BOOLEAN,
                    description=(
                        "True when the character's preparation (from bolster) pays off on this move: "
                        "+1, spending one. Only when their study plausibly helps here."
                    ),
                ),
                "forward": _s(
                    type=types.Type.BOOLEAN,
                    description=(
                        "True when this move acts on a forward the character holds (the state lists "
                        "them with what they are for: 'when acting on what Discern Realities showed', "
                        "'against the ogre'). Gary adds it and it is spent. Omit when this move is not "
                        "what the forward was for."
                    ),
                ),
                "coins": _s(
                    type=_INTEGER,
                    description="carouse only: the coin spent on the party, 100 or more; each "
                                "100 past the first is +1.",
                ),
                "companion": _s(
                    type=types.Type.BOOLEAN,
                    description=(
                        "True when a Ranger works with their animal companion on this move -- "
                        "the player says so, or the companion is plainly in it with them. The "
                        "rules engine checks it is trained for it and adds its bonus."
                    ),
                ),
                "spell": _s(
                    type=_STRING,
                    description=(
                        "cast_a_spell only: the spell, exactly as the caster's spell list in "
                        "the state names it. Only a spell they have now can be cast."
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
                            "instinct": _s(type=_STRING, description="Your own creature only: what it wants, e.g. 'To hoard'."),
                            "moves": _s(type=_ARRAY, items=_s(type=_STRING),
                                        description="Your own creature only: 2-4 GM moves for it, e.g. 'Call for reinforcements'."),
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
                "also": _s(type=_ARRAY, items=_s(type=_STRING), description=(
                    "Other creatures striking the same character at the same moment -- a pack, a "
                    "surrounding ring. One combined blow: the biggest damage die among them, rolled "
                    "once per attacker, highest counts. Not for blows at different moments.")),
                "protector": _s(type=_STRING, description=(
                    "A hired Protector standing between the character and this attack: +their skill "
                    "to armor, then their skill drops 1 until they rest.")),
            },
            required=["monster", "character"],
        ),
    ),
    types.FunctionDeclaration(
        name=DEAL_DAMAGE,
        description=(
            "A character simply deals their damage, no roll: the fiction gives them the blow -- "
            "the enemy is helpless, surprised, unaware, pinned, or a hard choice the player took "
            "promised it. Not for a fair fight: that is hack_and_slash or volley. Their weapon must "
            "reach; Gary rolls and applies the damage."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "target": _s(type=_STRING, description="Instance name, e.g. 'Goblin 2'."),
                "ranged": _s(type=types.Type.BOOLEAN, description="True for a shot or a throw."),
                "reason": _s(type=_STRING, description="What leaves the enemy open. Shown to the table."),
            },
            required=["character", "target", "reason"],
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
        name=PREPARE_SPELLS,
        description=(
            "A Cleric communes with their deity, or a Wizard studies their spellbook to "
            "prepare spells: an hour or so of uninterrupted quiet. The spells they held are "
            "gone, the 7-9 penalties and lost spells reset, and their player chooses new ones "
            "privately; the clock moves an hour. Only when the hour truly is quiet: not "
            "mid-fight, not with something hunting them. Call it when the player says their "
            "character prays or studies -- they cannot cast spells they do not have."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING, description="The Cleric or Wizard, as the party roster names them."),
                "reason": _s(type=_STRING, description="Where they spend the hour and why it is undisturbed. Shown to the table."),
            },
            required=["character", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=END_SPELL,
        description=(
            "An ongoing spell ends: the caster dismisses it, or the fiction breaks it "
            "(Charm Person's target is hurt, the invisible ally attacks, the battle Bless was "
            "cast for is over). Ongoing spells are listed in the state; while they run they "
            "cost the caster -1 to cast, so end them when they end."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING, description="Who cast it."),
                "spell": _s(type=_STRING, description="The spell, as the state lists it."),
            },
            required=["character", "spell"],
        ),
    ),
    types.FunctionDeclaration(
        name=FORM_MOVES,
        description=(
            "After a Druid shapeshifts: record the one to three moves their new form can make "
            "('maul with claws', 'roar to scatter the weak'). Tell the player them in the "
            "fiction too. Each use costs 1 hold, through spend_hold."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "moves": _s(type=_ARRAY, items=_s(type=_STRING), description="Short names, as the player would ask for them."),
            },
            required=["character", "moves"],
        ),
    ),
    types.FunctionDeclaration(
        name=SPEND_HOLD,
        description=(
            "A shapeshifted Druid makes one of their form's moves: spend 1 hold, then narrate "
            "it -- no roll, the hold is the cost. At 0 hold they return to their natural form. "
            "revert=true when they choose to turn back early. Refused for a move the form "
            "does not have."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "move": _s(type=_STRING, description="One of the moves form_moves recorded."),
                "revert": _s(type=types.Type.BOOLEAN, description="True to spend all hold and turn back."),
            },
            required=["character"],
        ),
    ),
    types.FunctionDeclaration(
        name=PERILOUS_JOURNEY,
        description=(
            "The party travels through hostile territory: Undertake a Perilous Journey. Give "
            "each job to a different character from the fiction -- trailblazer, scout, "
            "quartermaster -- or ask the players who takes which. A job nobody holds counts as a "
            "6. Rations for the expected days are spent at once; each job-holder rolls +Wis and "
            "the results come back as rules events. You move the clock once you know how long it "
            "really took. Not for a stroll between safe places: just advance_time."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "destination": _s(type=_STRING),
                "days": _s(type=_INTEGER, description="The expected length in days, 1-30."),
                "trailblazer": _s(type=_STRING),
                "scout": _s(type=_STRING),
                "quartermaster": _s(type=_STRING),
            },
            required=["destination", "days"],
        ),
    ),
    types.FunctionDeclaration(
        name=RECOVER,
        description=(
            "Characters do nothing but rest in comfort and safety for days -- an inn, a temple, "
            "a friend's home. A day heals all HP; every three days clears a debility (every "
            "two under a healer's care); the player picks which. The clock moves the days for "
            "everyone. Not in the wilds or the dungeon: that is make_camp."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "days": _s(type=_INTEGER, description="Whole days of rest, 1-30."),
                "reason": _s(type=_STRING, description="Where they rest and why it is comfortable and safe. Shown to the table."),
                "healer": _s(type=types.Type.BOOLEAN, description="True if a healer, magical or otherwise, tends them."),
                "characters": _s(type=_ARRAY, items=_s(type=_STRING), description="Who rests. Omit for the whole party."),
            },
            required=["days", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=CREATE_FRONT,
        description=(
            "Write down a threat that will move whether the party looks or not -- a front -- as it "
            "takes shape: at a campaign's start, from an imported setting, when a villain appears. "
            "Each danger has an impulse (what it wants), 2-5 grim portents (escalating steps it takes "
            "if unchecked, each something the party could notice and stop), and an impending doom. "
            "Players never see fronts; you are shown each danger's next portent every turn."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "name": _s(type=_STRING, description="e.g. 'The Dragon of the Spear'."),
                "dangers": _s(type=_ARRAY, items=_s(type=_OBJECT, properties={
                    "name": _s(type=_STRING), "impulse": _s(type=_STRING, description="'to defend her young'"),
                    "portents": _s(type=_ARRAY, items=_s(type=_STRING)),
                    "doom": _s(type=_STRING, description="'destruction', 'usurpation', or what it means here"),
                }, required=["name", "impulse", "portents", "doom"])),
                "stakes": _s(type=_ARRAY, items=_s(type=_STRING),
                             description="Questions play will answer, e.g. 'Will the lizardmen side with the dragon?'"),
            },
            required=["name", "dangers"],
        ),
    ),
    types.FunctionDeclaration(
        name=ADVANCE_PORTENT,
        description=(
            "A danger's next grim portent comes true -- onscreen, or offscreen while the party is "
            "elsewhere or ignores the signs. Then show its signs in the fiction. When the last one "
            "falls, its impending doom comes to pass."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"front": _s(type=_STRING), "danger": _s(type=_STRING),
                        "reason": _s(type=_STRING, description="What brought it about.")},
            required=["front", "danger", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=END_DANGER,
        description="A danger is stopped for good -- defeated, thwarted, made peace with.",
        parameters=_s(
            type=_OBJECT,
            properties={"front": _s(type=_STRING), "danger": _s(type=_STRING),
                        "reason": _s(type=_STRING, description="How it ended.")},
            required=["front", "danger", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=BOLSTER,
        description=(
            "A character spends leisure weeks in study, meditation or hard practice: a week or two "
            "is 1 preparation, a month or more 3, each +1 to a later roll it pays off on. The clock "
            "moves the weeks. Downtime only."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "weeks": _s(type=_INTEGER, description="1-26."),
                "what": _s(type=_STRING, description="What they study or practise. Shown to the table."),
            },
            required=["character", "weeks", "what"],
        ),
    ),
    types.FunctionDeclaration(
        name=FIRST_AID,
        description=(
            "A hired Priest staunches a character's wounds: heals 2 × their skill, and the patient "
            "takes -1 forward from the pain. The rules engine applies it."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"hireling": _s(type=_STRING, description="The priest."),
                        "patient": _s(type=_STRING)},
            required=["hireling", "patient"],
        ),
    ),
    types.FunctionDeclaration(
        name=HIRE,
        description=(
            "Someone joins a character as a hireling, after a recruit roll or a deal in the fiction. "
            "Give their skills with ranks (Adept, Burglar, Minstrel, Priest, Protector, Tracker, "
            "Warrior), loyalty, cost (The Thrill of Victory, Money, Uncovered Knowledge, Fame and Glory, "
            "Debauchery, Good Accomplished) and where they were found: their loyalty and skill ranks "
            "share village 5, town 6, keep 8 or city 10 points. Starting loyalty is usually 0-2."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING, description="The employer."),
                "name": _s(type=_STRING),
                "skills": _s(type=_OBJECT, description="Skill -> rank, e.g. {\"Protector\": 3}.",
                             properties={s: _s(type=_INTEGER) for s in
                                         ("Adept", "Burglar", "Minstrel", "Priest", "Protector", "Tracker", "Warrior")}),
                "loyalty": _s(type=_INTEGER),
                "cost": _s(type=_STRING),
                "found_in": _s(type=_STRING, enum=["village", "town", "keep", "city"]),
            },
            required=["character", "name", "skills", "loyalty", "cost", "found_in"],
        ),
    ),
    types.FunctionDeclaration(
        name=UPDATE_HIRELING,
        description=(
            "A hireling's standing changes: paid=true when their cost is met (usually once a session), "
            "paid=false when it lapses (-1 loyalty until paid); loyalty +1 or -1 for a great deed shared "
            "or a bad beating; leaves=true when they quit, are dismissed or die."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING, description="The employer."),
                "name": _s(type=_STRING),
                "paid": _s(type=types.Type.BOOLEAN),
                "loyalty": _s(type=_INTEGER, description="+1 or -1, permanently."),
                "leaves": _s(type=types.Type.BOOLEAN),
                "reason": _s(type=_STRING),
            },
            required=["character", "name", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=FORWARD,
        description=(
            "Give a character +1 forward, or -1 forward, as the fiction earns it: a 7-9's price "
            "('you're off balance'), a GM move, an advantage you grant. It waits on their sheet, "
            "with what it is for, until a move you flag forward=true. Moves that grant forwards "
            "themselves (Discern Realities, Take Watch, I Am the Law...) do it for you."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "amount": _s(type=_INTEGER, description="+1 or -1 (at most 2 either way)."),
                "reason": _s(type=_STRING, description="When it applies, e.g. 'against the troll'."),
            },
            required=["character", "amount", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=DEBILITY,
        description=(
            "A character suffers a debility -- weak (Str), shaky (Dex), sick (Con), stunned "
            "(Int), confused (Wis), scarred (Cha) -- because the fiction did it to them: a GM "
            "move, a poison, a monster's touch, a bad fall. Or cured=true when the fiction "
            "cures one (magic, real treatment). Each is -1 to that stat's rolls until cured, "
            "applied for you; they do not stack. Say the cause; the table sees it."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "debility": _s(type=_STRING, enum=["weak", "shaky", "sick", "stunned", "confused", "scarred"]),
                "cause": _s(type=_STRING, description="What did it, or what cures it, in a few words."),
                "cured": _s(type=types.Type.BOOLEAN, description="True to remove it."),
            },
            required=["character", "debility", "cause"],
        ),
    ),
    types.FunctionDeclaration(
        name=USE_POISON,
        description=(
            "A Thief uses the poison they mastered (the state shows which, and how many uses "
            "are left): spends one and returns what it does. No roll -- getting it onto the "
            "target may be a move of its own (Backstab, Defy Danger)."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "target": _s(type=_STRING, description="Who or what it is used on.")},
            required=["character"],
        ),
    ),
    types.FunctionDeclaration(
        name=BREW_POISON,
        description=(
            "A Thief with time to gather materials and a safe place brews three more uses of "
            "their poison. Not mid-fight; say where and why it is safe."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "reason": _s(type=_STRING)},
            required=["character", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=ADVANCE_TIME,
        description=(
            "Advance the world clock. A night's rest is 480 (make_camp moves it for you), "
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
            "A character buys something, paid from their coin. Something readily available here "
            "is the book's price: omit price. Something special, rare or magical needs the "
            "supply move first; then give price -- the fair price on a 10+, more on a 7-9 (at "
            "most twice the book's), and for something the book does not price, yours (up to "
            "500). Refused if they cannot afford it."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING),
                "item": _s(type=_STRING, description="e.g. 'long sword', '2 healing potions'."),
                "price": _s(type=_INTEGER, description="Each, after a supply roll only."),
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
