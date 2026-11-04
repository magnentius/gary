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
from ..compendium import CLASSES as COMPENDIUM_CLASSES, describe_requirements
from ..fronts import DOOMS, type_names as front_types
from ..monsters import (ATTACK_TRAITS as M_ATTACK, DEFENSES as M_DEF, KNOWN_FOR as M_KNOWN,
                        ORGANIZATION as M_ORG, SIZES as M_SIZES, TRAITS as M_TRAITS)
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
START_QUEST = "start_quest"
HAZARD_DAMAGE = "hazard_damage"
ONGOING = "ongoing"
LEAD_THE_CHARGE = "lead_the_charge"
CHANNEL_BALANCE = "channel_balance"
CREATE_STEADING = "create_steading"
QUALIFY_COMPENDIUM = "qualify_compendium"
COMPENDIUM_POWER = "compendium_power"
INSTANT_NPC = "instant_npc"
ADJUST_STEADING = "adjust_steading"
SPELL_AUGMENTATION = "spell_augmentation"
STUN = "stun"
SWEAR_ENEMY = "swear_enemy"
RESOLVE_APPETITE = "resolve_appetite"
GIVE_MAGIC_ITEM = "give_magic_item"
RUMMAGE = "rummage"
SET_WEATHER = "set_weather"
PAY_FOR = "pay_for"
MOUNT = "mount"
RITUAL = "ritual"
WORLD_MAP = "world_map"
PROMOTE_MONSTER = "promote_monster"
SET_GRIP = "set_grip"
POISON_CHARACTER = "poison_character"
SHARE_PIPELEAF = "share_pipeleaf"
WEAR_ARMOR = "wear_armor"
DOMINATE = "dominate"
UNSEEN_SERVANT = "unseen_servant"
FREE_CREATURE = "free_creature"
STUDY_ESSENCE = "study_essence"
SUMMON_ALLY = "summon_ally"
CONTINGENCY = "contingency"
CREATURE_ATTACKS = "creature_attacks"
GRAFT = "graft"
MARK_XP = "mark_xp"
END_QUEST = "end_quest"
TREASURE = "roll_treasure"
FIRST_AID = "hireling_first_aid"
BOLSTER = "bolster"
CREATE_FRONT = "create_front"
REVISE_FRONT = "revise_front"
MOVE_DANGER = "move_danger"
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
                "keg": _s(type=types.Type.BOOLEAN, description=(
                    "carouse: they open a keg of dwarven stout they carry for everyone (+1, the keg is gone).")),
                "book": _s(type=types.Type.BOOLEAN, description=(
                    "spout_lore: their bag of books holds just the right book (+1, a use marked off).")),
                "weapon": _s(type=_STRING, description=(
                    "For hack_and_slash or volley: the weapon they say they use, from their sheet ('dagger', "
                    "'spear') -- one whose range suits the distance (hand: grappling; close: arm's reach; reach: "
                    "several feet; near: the whites of their eyes; far: shouting distance). Omit for their best.")),
                "target": _s(
                    type=_STRING,
                    description=(
                        "For Shadow Walk: the destination, in no more words than the caster's level. "
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
                "weak_point": _s(type=types.Type.BOOLEAN, description=(
                    "volley with Smaug's Belly, when they know the target's weakest point: 2 piercing.")),
                "two_weapons": _s(type=types.Type.BOOLEAN, description=(
                    "hack_and_slash with Viper's Strike or Fangs, striking with a weapon in each hand.")),
                "extra_targets": _s(type=_ARRAY, items=_s(type=_STRING), description=(
                    "volley with Blot Out the Sun: more creatures hit by the same roll, one ammo each.")),
                "form_boon": _s(type=_STRING, enum=["armor", "damage"], description=(
                    "shapeshifter with Formshaper: +1 armor or +1d4 damage while in the form (their pick).")),
                "form_plus": _s(type=_STRING, enum=list(STATS), description=(
                    "shapeshifter with Formcrafter: the stat they take +1 ongoing in while shifted.")),
                "form_minus": _s(type=_STRING, enum=list(STATS), description=(
                    "shapeshifter with Formcrafter: the stat YOU choose for -1 ongoing while shifted.")),
                "minion": _s(type=_STRING, description=(
                    "A Blob's slime minion making this move: it rolls its own Str, Dex or Con and deals d4.")),
                "weapon_leverage": _s(type=types.Type.BOOLEAN, description=(
                    "parley with Its Name Is Dread, using the weapon's reputation: +bonds with the weapon, not Cha.")),
                "dragon_nature": _s(type=types.Type.BOOLEAN, description=(
                    "parley with Wake the Dragon, using greed, wrath or disdain: +Blood.")),
                "hurl": _s(type=types.Type.BOOLEAN, description=(
                    "volley with Velocitas Eradico: any object up to 2 weight, far only, +Con, no ammo.")),
                "appetite": _s(type=types.Type.BOOLEAN, description=(
                    "a Barbarian rolling while pursuing one of THEIR appetites, as their sheet lists them "
                    "(Herculean Appetites): 1d6+1d8 instead of 2d6. Only when the act itself chases the "
                    "appetite -- grabbing the torc they covet, crushing a foe for glory -- not every roll in "
                    "a fight; a higher d6 brings trouble, so it is a cost as well as a boost.")),
                "movement": _s(type=types.Type.BOOLEAN, description=(
                    "defy_danger caused by movement, with On the Move: +1.")),
                "mortal_magic": _s(type=types.Type.BOOLEAN, description=(
                    "defy_danger caused by magic wielded by mortals, with Healthy Distrust: a 6- counts as 7-9.")),
                "ally": _s(
                    type=_STRING,
                    description=(
                        "hack_and_slash with Setup Strike or Tandem Strike: the ally whose next attack "
                        "on the same target gets +1d4 damage."
                    ),
                ),
                "race_move": _s(
                    type=types.Type.BOOLEAN,
                    description=(
                        "The character's race move applies, as the fiction shows it: a Dwarf Fighter "
                        "parleying over a shared drink (Con instead of Cha), a Halfling Fighter using "
                        "small size to Defy Danger (+1), a Human Thief spouting lore or discerning "
                        "about criminal activity (+1)."
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
                        "cast_a_spell: the spell, exactly as the caster's spell list in the state "
                        "names it. Only a spell they have now can be cast. counterspell: the "
                        "prepared spell they stake on the defense."
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
            "World creatures are looked up -- name them and give nothing else. For a creature "
            "of your own, answer the book's Making Monsters questions in recipe, with an "
            "instinct and at least one move, and Gary works out its numbers; hp, armor and "
            "damage by hand are the fallback, checked against the same rules."
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
                            "recipe": _s(type=_OBJECT, description=(
                                "Your own creature, made by the book's Making Monsters questions: give the answers "
                                "and Gary works out HP, armor, damage and tags (then leave hp, armor and damage "
                                "out). Prefer this to inventing numbers."), properties={
                                "organization": _s(type=_STRING, enum=list(M_ORG)),
                                "size": _s(type=_STRING, enum=list(M_SIZES)),
                                "defense": _s(type=_STRING, enum=list(M_DEF), description=(
                                    "flesh 0, leather 1, mail 2, plate or bone 3, permanent magic 4.")),
                                "known_for": _s(type=_ARRAY, items=_s(type=_STRING, enum=list(M_KNOWN)), description=(
                                    "strength, offense (skill), defense (skill), deft strikes, endurance, deceit, "
                                    "adaptation (wings, amphibious), divine favor, magic.")),
                                "divine_favor": _s(type=_STRING, enum=["damage", "hp", "both"]),
                                "attack_traits": _s(type=_ARRAY, items=_s(type=_STRING, enum=list(M_ATTACK)), description=(
                                    "vicious and obvious, keeps others at bay (reach), small and weak, slices metal, "
                                    "tears metal apart, armor doesn't help, attacks at range.")),
                                "traits": _s(type=_ARRAY, items=_s(type=_STRING, enum=list(M_TRAITS)), description=(
                                    "devious, organized, intelligent, cautious (shield), hoarder, planar, unnatural "
                                    "(kept alive by more than biology), construct, terrifying, amorphous, ancient, "
                                    "abhors violence.")),
                            }, required=["organization", "size", "defense"]),
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
                    "surrounding ring. One combined blow: the highest damage among them, rolled once, "
                    "+1 for each attacker beyond the first. Not for blows at different moments.")),
                "cover": _s(type=_INTEGER, description=(
                    "The character's cover against this attack: 1 partial, 2 major.")),
                "minion": _s(type=_STRING, description="The character's slime minion this blow falls on (Blob)."),
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
                "fed_by_host": _s(type=types.Type.BOOLEAN, description=(
                    "True at an inn where they pay for a meal, or as someone's guest: nobody eats from their "
                    "own rations.")),
                "watch": _s(type=_STRING, description="Who is on watch, if something approaches."),
                "dungeon_or_city": _s(type=types.Type.BOOLEAN, description="True if they camp inside a dungeon or a city (a Human Ranger eats no ration there)."),
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
                "wilderness": _s(type=types.Type.BOOLEAN, description="True if the way runs through wilderness (an Elf Ranger's job then succeeds as a 10+), not roads or settled land."),
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
            "Write a front, by the book's recipe. Only when Gary opens front work (after an end of "
            "session) -- fronts are built outside play; in play, note threats with record_note "
            "kind=threat. A front is campaign (the slow threat across sessions; 3-5 portents a danger; "
            "it may be incomplete) or adventure (here and now; 1-3 portents a danger). 1-3 dangers, each "
            "of a type from the book (its impulse and its family's GM moves come with it), with grim "
            "portents -- steps it takes if unchecked, each something the party could notice -- and an "
            "impending doom: a kind and its concrete form. 1-3 stakes questions: what you want play to "
            "find out. Players never see fronts."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "name": _s(type=_STRING, description="e.g. 'The Opening of the White Gate'."),
                "scope": _s(type=_STRING, enum=["campaign", "adventure"]),
                "dangers": _s(type=_ARRAY, items=_s(type=_OBJECT, properties={
                    "name": _s(type=_STRING, description="'The College of Arcanists'"),
                    "type": _s(type=_STRING, enum=front_types()),
                    "impulse": _s(type=_STRING, description="Optional: the type's impulse is the default."),
                    "portents": _s(type=_ARRAY, items=_s(type=_STRING)),
                    "doom_type": _s(type=_STRING, enum=list(DOOMS)),
                    "doom": _s(type=_STRING, description="Its concrete form: 'the College seizes the north'."),
                }, required=["name", "type", "portents", "doom_type", "doom"])),
                "stakes": _s(type=_ARRAY, items=_s(type=_STRING),
                             description="1-3: 'Who will be the champion?' -- concrete, never the same after."),
                "description": _s(type=_STRING, description="The front in a nutshell."),
                "cast": _s(type=_ARRAY, items=_s(type=_STRING), description="Its people, each with a detail."),
                "moves": _s(type=_ARRAY, items=_s(type=_STRING), description=(
                    "Custom moves a danger suggests, written out ('When you stand in the Light From Beyond, "
                    "roll+WIS...'). You honour them; Gary does not roll them.")),
            },
            required=["name", "scope", "dangers", "stakes"],
        ),
    ),
    types.FunctionDeclaration(
        name=REVISE_FRONT,
        description=(
            "Change a front in play, as the book allows ('these often change in play'): a danger's "
            "portents still to come or its doom, the stakes, the cast (added to), the description, "
            "a custom move. Portents already come to pass stay."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "front": _s(type=_STRING), "danger": _s(type=_STRING),
                "portents": _s(type=_ARRAY, items=_s(type=_STRING), description="Replaces those not yet reached."),
                "doom_type": _s(type=_STRING), "doom": _s(type=_STRING),
                "stakes": _s(type=_ARRAY, items=_s(type=_STRING), description="Replaces them: 1-3."),
                "cast": _s(type=_ARRAY, items=_s(type=_STRING)), "description": _s(type=_STRING),
                "moves": _s(type=_ARRAY, items=_s(type=_STRING)),
                "reason": _s(type=_STRING, description="What in play changed it."),
            },
            required=["front", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=MOVE_DANGER,
        description=(
            "Move a danger to another front with its progress: an unresolved one from an adventure front "
            "up to the campaign front, or one from the campaign front that has come to bear into an "
            "adventure front."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"front": _s(type=_STRING), "danger": _s(type=_STRING),
                        "to_front": _s(type=_STRING), "reason": _s(type=_STRING)},
            required=["front", "danger", "to_front", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=ADVANCE_PORTENT,
        description=(
            "A danger's next grim portent comes true: descriptively, when play has shown it happen; "
            "offscreen, while the party is elsewhere or ignores the signs; or prescriptively, as your "
            "hard move after a miss or a golden opportunity. Then show its effects and ask what they do. "
            "When the last one falls, its impending doom comes to pass."
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
        name=TREASURE,
        description=(
            "Someone searches a creature's belongings or lair and it has gathered some wealth: "
            "roll its treasure by the book. Gary rolls its damage die (twice for a hoarder, +1d4 "
            "for a lord over others or something ancient) and every amount, gives the coins and "
            "valuables to the finder, and tells you what else turned up for you to name and "
            "describe -- with a few of the book's magic items as ideas if something magical did. "
            "Once per creature. Not every creature carries anything: a wolf has no purse."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "finder": _s(type=_STRING, description="The character who searches and takes it."),
                "creature": _s(type=_STRING, description="A creature in the scene by name ('Orc 2', even "
                                                         "defeated), or a bestiary kind ('Dragon') for a lair."),
                "far_from_home": _s(type=types.Type.BOOLEAN, description="It is far from home: at least one ration."),
                "lord": _s(type=types.Type.BOOLEAN, description="It is a lord over others: +1d4."),
                "ancient": _s(type=types.Type.BOOLEAN, description="It is ancient and noteworthy: +1d4."),
            },
            required=["finder", "creature"],
        ),
    ),
    types.FunctionDeclaration(
        name=START_QUEST,
        description=(
            "A Paladin dedicates themselves to a mission through prayer and ritual cleansing "
            "(their Quest move). Record what they swore -- slay a blight, defend someone, or "
            "discover a truth -- the up to two boons THEY chose, and the vow or vows YOU name as "
            "GM to keep the blessing. Gary applies what has a number while it lasts."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "mission": _s(type=_STRING, enum=["slay", "defend", "discover"]),
                "subject": _s(type=_STRING, description="The blank: who or what -- 'the Wyrm of Hollowmere'."),
                "boons": _s(type=_ARRAY, items=_s(type=_OBJECT, properties={
                    "boon": _s(type=_STRING, enum=["direction", "invulnerability", "authority", "truth",
                                                   "tongues", "sustenance"]),
                    "detail": _s(type=_STRING, description="For direction (to where) and invulnerability "
                                                           "(to what: edged weapons, fire, enchantment)."),
                }, required=["boon"])),
                "vows": _s(type=_ARRAY, items=_s(type=_STRING, enum=["honor", "temperance", "piety", "valor",
                                                                     "truth", "hospitality"])),
            },
            required=["character", "mission", "subject", "vows"],
        ),
    ),
    types.FunctionDeclaration(
        name=STUN,
        description=(
            "Stun damage -- a club to the head, a thunderclap, a net of shock: no HP lost, but until "
            "they clear their head (end=true) anything the character does is Defy Danger."
        ),
        parameters=_s(type=_OBJECT, properties={
            "character": _s(type=_STRING), "reason": _s(type=_STRING, description="What stunned them, or what cleared it."),
            "end": _s(type=types.Type.BOOLEAN, description="True when they have cleared their head.")},
            required=["character"]),
    ),
    types.FunctionDeclaration(
        name=SPELL_AUGMENTATION,
        description=(
            "A Wizard with Spell Augmentation, having just dealt damage to a creature, ends one of "
            "their ongoing spells to add its level to that damage. Gary applies it to the creature "
            "they last hit."
        ),
        parameters=_s(type=_OBJECT, properties={"character": _s(type=_STRING),
                                                 "spell": _s(type=_STRING, description="The ongoing spell ended.")},
                      required=["character", "spell"]),
    ),
    types.FunctionDeclaration(
        name=CHANNEL_BALANCE,
        description=(
            "A Druid with Balance touches someone and channels the spirits of life: spend balance "
            "(earned 1 each time they deal damage), healing 1d4 per point. Gary rolls and applies it."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "patient": _s(type=_STRING),
                        "amount": _s(type=_INTEGER, description="How much balance to spend.")},
            required=["character", "patient", "amount"],
        ),
    ),
    types.FunctionDeclaration(
        name=LEAD_THE_CHARGE,
        description=(
            "A Paladin with Charge! or Ever Onward leads the charge into combat: those who follow "
            "take +1 forward (and +2 armor forward with Ever Onward), applied by Gary."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING, description="Who leads."),
                        "led": _s(type=_ARRAY, items=_s(type=_STRING), description="Who follows the charge.")},
            required=["character", "led"],
        ),
    ),
    types.FunctionDeclaration(
        name=ONGOING,
        description=(
            "Give a character +1 or -1 ongoing (at most 2): added to ALL their move rolls, or to "
            "one move, until it ends -- a curse, a blessing, a lasting wound, a spell's effect, "
            "fear. until='battle' ends by itself when this fight does; until='ended' lasts until "
            "you call this again with end=true and its reason. For a single roll, use forward."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "amount": _s(type=_INTEGER, description="+1, -1, or at most 2 either way."),
                "reason": _s(type=_STRING, description="What causes it. Shown to the table."),
                "until": _s(type=_STRING, enum=["battle", "ended"]),
                "move": _s(type=_STRING, description="Only this move (its make_move key), if the bonus is that narrow."),
                "end": _s(type=types.Type.BOOLEAN, description="True to end the ongoing bonus named by reason."),
            },
            required=["character", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=HAZARD_DAMAGE,
        description=(
            "Characters are hurt by something that is not a creature: a fall, a trap, fire, a "
            "collapsing ceiling, bad water -- when the fiction makes it land (a missed Defy Danger, "
            "a sprung trap). You say how bad; Gary rolls it for each one caught, through their armor "
            "(plus any cover), and applies it. A creature's blow is monster_attacks instead."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "characters": _s(type=_ARRAY, items=_s(type=_STRING), description="Everyone caught by it."),
                "severity": _s(type=_STRING, enum=["bruises", "blood", "bones", "lethal"], description=(
                    "bruises: scrapes at worst (d4). blood: spills some blood, nothing horrendous (d6). "
                    "bones: might break some bones (d8). lethal: could kill a common person (d10).")),
                "reason": _s(type=_STRING, description="What hurt them, in a few words. Shown to the table."),
                "ignores_armor": _s(type=types.Type.BOOLEAN, description=(
                    "True if it is particularly large, or magic, or poison: armor does not help.")),
                "cover": _s(type=_INTEGER, description="1 for partial cover, 2 for major cover."),
            },
            required=["characters", "severity", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=SWEAR_ENEMY,
        description=(
            "A Paladin with Exterminatus speaks aloud their promise to defeat an enemy: +2d4 damage "
            "against it and -4 against anyone else, applied by Gary, until it is defeated (which "
            "ends it by itself). If the enemy escapes or they give up, the oath holds until they "
            "redeem themselves: then call this with released=true and the reason."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "enemy": _s(type=_STRING, description="The creature by its listed name ('Orc Warchief 1'), or a "
                                                      "kind or NPC not yet here ('the Dragon')."),
                "released": _s(type=types.Type.BOOLEAN),
                "reason": _s(type=_STRING, description="With released: how they redeemed themselves."),
            },
            required=["character"],
        ),
    ),
    types.FunctionDeclaration(
        name=MARK_XP,
        description=(
            "A Barbarian (an Outsider) answered your question about their homeland, why they left, or "
            "what they left behind: they mark 1 XP. Once a session. reason must be 'Outsider'."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "reason": _s(type=_STRING, enum=["Outsider"])},
            required=["character", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=RESOLVE_APPETITE,
        description=(
            "A Barbarian with More! Always More! satisfies an appetite to the extreme (destroys something "
            "unique and significant, gains enormous fame, riches or power) and chooses to resolve it: "
            "Gary crosses it off, marks 1 XP and records the new appetite in its place."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "appetite": _s(type=_STRING, description="The one resolved, as their sheet names it."),
                "new": _s(type=_STRING, description="Its replacement: from the list, or the player's own words."),
            },
            required=["character", "appetite", "new"],
        ),
    ),
    types.FunctionDeclaration(
        name=END_QUEST,
        description="A Paladin's quest ends: fulfilled, a vow broken (the blessing leaves them), or abandoned.",
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "how": _s(type=_STRING, enum=["fulfilled", "broken", "abandoned"]),
                "reason": _s(type=_STRING, description="What happened, in a few words. Shown to the table."),
            },
            required=["character", "how"],
        ),
    ),
    types.FunctionDeclaration(
        name=HIRE,
        description=(
            "Someone joins a character as a hireling, after a recruit roll or a deal in the fiction. "
            "Give their skills with ranks (Adept, Burglar, Minstrel, Priest, Protector, Tracker, "
            "Warrior), loyalty, cost (The Thrill of Victory, Money, Uncovered Knowledge, Fame and Glory, "
            "Debauchery, Good Accomplished) and where they were found: their loyalty and skill ranks "
            "share village 5, town 6, keep 8 or city 10 points. Starting loyalty is usually 0-2. No skill "
            "may outrank the employer's level: hirelings work for adventurers at least as experienced."
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
            "Someone uses a poison: a Thief's mastered one (the state shows its uses), or a dose they "
            "carry (bought or found -- spent, and Dangerous). Returns what it does; on a creature in "
            "the scene, Bloodweed and Serpent's Tears change its numbers from then on. No roll -- "
            "getting it onto the target may be a move of its own (Backstab, Defy Danger)."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "target": _s(type=_STRING, description="Who or what it is used on."),
                        "poison": _s(type=_STRING, description="Which poison, if not their mastered one."),
                        "on_weapon": _s(type=types.Type.BOOLEAN, description="Put on their weapon (Envenom: touch).")},
            required=["character"],
        ),
    ),
    types.FunctionDeclaration(
        name=BREW_POISON,
        description=(
            "A Thief with time to gather materials and a safe place brews three more uses of "
            "their poison. Not mid-fight; say where and why it is safe. With Brewer, poison= any one "
            "they've used before (three doses); with Alchemist, also a poison of their own design: give "
            "its effect and the caveats you set (only works under specific circumstances; a weaker "
            "version; it'll take a while)."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "reason": _s(type=_STRING), "poison": _s(type=_STRING),
                        "effect": _s(type=_STRING),
                        "caveats": _s(type=_ARRAY, items=_s(type=_STRING))},
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
        name=STUDY_ESSENCE,
        description=("Studied Essence (Druid): after time contemplating an animal spirit, its species is one they "
                     "can shapeshift into, wherever its land. Recorded on the sheet and shown at Shapeshifter."),
        parameters=_s(type=_OBJECT, properties={"character": _s(type=_STRING), "species": _s(type=_STRING),
                                                "reason": _s(type=_STRING)},
                      required=["character", "species", "reason"]),
    ),
    types.FunctionDeclaration(
        name=FREE_CREATURE,
        description=("A spell's hold on a creature is broken by the fiction: a sleeper woken by loud noise or a "
                     "shake (Sleep: 'They awake as normal: loud noises, jolts, pain'), a charm seen through, a hold "
                     "shrugged off. Pain does this by itself; this is for everything else. The caster's spell on it "
                     "ends too."),
        parameters=_s(type=_OBJECT, properties={"creature": _s(type=_STRING), "reason": _s(type=_STRING)},
                      required=["creature", "reason"]),
    ),
    types.FunctionDeclaration(
        name=UNSEEN_SERVANT,
        description=("The caster hands their Unseen Servant an item (give) or takes one back (take). It carries up "
                     "to weight 3, which leaves the caster's load; it can't pick things up itself. If it takes damage "
                     "or leaves their presence, end_spell: it drops what it carried where it was."),
        parameters=_s(type=_OBJECT, properties={
            "character": _s(type=_STRING), "action": _s(type=_STRING, enum=["give", "take"]),
            "item": _s(type=_STRING)}, required=["character", "action", "item"]),
    ),
    types.FunctionDeclaration(
        name=DOMINATE,
        description=("Dominate: the caster spends one hold to make the target speak a few words of their choice, "
                     "give them something they hold, make a concerted attack on a target of their choice, or "
                     "truthfully answer one question. At no hold the spell ends; damage to the target costs a hold."),
        parameters=_s(type=_OBJECT, properties={
            "character": _s(type=_STRING), "command": _s(type=_STRING, enum=["speak", "give", "attack", "answer"]),
            "reason": _s(type=_STRING, description="What exactly: the words, the item, the target, the question.")},
            required=["character", "command"]),
    ),
    types.FunctionDeclaration(
        name=SUMMON_ALLY,
        description=("After Summon Monster or Animate Dead is cast: the creature arrives with the traits the player "
                     "chose (no more than Gary rolled). It acts as the caster's character with only the basic moves "
                     "(make_move actor=its name), +1 to all stats, 1 HP; at 0 HP it's destroyed."),
        parameters=_s(type=_OBJECT, properties={
            "character": _s(type=_STRING, description="The caster."), "name": _s(type=_STRING),
            "kind": _s(type=_STRING, description="What it is: 'a shadow mastiff', 'the bandit's corpse'."),
            "traits": _s(type=_ARRAY, items=_s(type=_STRING, enum=[
                "boost", "careful", "d8", "durable", "adaptation", "brain", "lifelike"])),
            "boosted": _s(type=_STRING, description="boost: which stat gets +2 (str, dex, con, int, wis, cha)."),
            "adaptation": _s(type=_STRING, description="adaptation: what it is (wings, gills, climbs walls).")},
            required=["character", "name", "kind"]),
    ),
    types.FunctionDeclaration(
        name=CONTINGENCY,
        description=("Contingency: action=hold right after it's cast -- the 5th-level-or-lower spell they hold and its "
                     "trigger (no more words than their level); action=release when the trigger happens or they "
                     "choose -- it takes effect without a roll, on target if it needs one."),
        parameters=_s(type=_OBJECT, properties={
            "character": _s(type=_STRING), "action": _s(type=_STRING, enum=["hold", "release"]),
            "spell": _s(type=_STRING), "trigger": _s(type=_STRING), "target": _s(type=_STRING)},
            required=["character", "action"]),
    ),
    types.FunctionDeclaration(
        name=WEAR_ARMOR,
        description=("A character takes off their armor and shield -- to swim, sleep, sneak, or pass as unarmed -- "
                     "or puts them back on (on=true). It takes time; off, they have only what stacks (a warded "
                     "cloak), no clumsy, and moves for wearing no armor apply."),
        parameters=_s(type=_OBJECT, properties={"character": _s(type=_STRING), "on": _s(type=types.Type.BOOLEAN)},
                      required=["character", "on"]),
    ),
    types.FunctionDeclaration(
        name=SHARE_PIPELEAF,
        description=("A character shares halfling pipeleaf with someone: two uses spent, +1 forward to Parley "
                     "with them."),
        parameters=_s(type=_OBJECT, properties={"character": _s(type=_STRING), "with_whom": _s(type=_STRING)},
                      required=["character", "with_whom"]),
    ),
    types.FunctionDeclaration(
        name=POISON_CHARACTER,
        description=(
            "A character is poisoned -- a blade, a bite, a trap, a drugged cup -- or recovers. Gary keeps it on their "
            "sheet until cured: Bloodweed takes d4 off every damage roll they make; Serpent's Tears makes anything "
            "hurting them roll twice, take the better; Oil of Tagit, Goldenroot and others are yours to narrate as "
            "written. Antitoxin cures one. cured=true when it wears off or is treated."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "poison": _s(type=_STRING, description=(
                "Bloodweed, Serpent's Tears, Oil of Tagit, Goldenroot, or a monster's own ('wyvern venom').")),
                        "effect": _s(type=_STRING, description="For a poison not in the book: what it does."),
                        "cured": _s(type=types.Type.BOOLEAN)},
            required=["character", "poison"],
        ),
    ),
    types.FunctionDeclaration(
        name=SET_GRIP,
        description=(
            "A character carrying both a shield and a two-handed weapon changes what's in hand: 'shield' (the "
            "shield's armor, a one-handed weapon) or 'both hands' (the two-handed weapon, no shield armor). It takes "
            "a moment in the fiction -- slinging the shield, drawing the halberd -- and can be dangerous mid-fight."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "grip": _s(type=_STRING, enum=["shield", "both hands"])},
            required=["character", "grip"],
        ),
    ),
    types.FunctionDeclaration(
        name=PROMOTE_MONSTER,
        description=(
            "A creature stops being an arrow and becomes someone: the spotlight fell on it -- it fled and swore "
            "revenge, Spout Lore found its story, it beat them. Give it a name ('Goblin 3' becomes 'Grizzlegut'), "
            "keeping its HP, and a line on why it matters; it is noted as a threat for the fronts between sessions."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"monster": _s(type=_STRING, description="Its instance name now, e.g. 'Goblin 3'."),
                        "name": _s(type=_STRING), "why": _s(type=_STRING)},
            required=["monster", "name", "why"],
        ),
    ),
    types.FunctionDeclaration(
        name=CREATURE_ATTACKS,
        description=(
            "One creature hurts another -- an ogre swats a goblin, rival factions clash, a charmed beast turns on "
            "its pack. Gary rolls the attacker's damage through the target's armor. For a creature hurting a "
            "character, use monster_attacks."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"attacker": _s(type=_STRING), "target": _s(type=_STRING), "reason": _s(type=_STRING)},
            required=["attacker", "target"],
        ),
    ),
    types.FunctionDeclaration(
        name=WORLD_MAP,
        description=(
            "The campaign map: draw maps, leave blanks. action=place adds a place (kind steading, dungeon, "
            "landmark or terrain, with a note, and the monster setting of what lives there -- Woods, Swamp, Hordes...) -- steadings and dungeons go on by themselves; action=route joins two "
            "places, days apart (a day is a ration), across terrain ('the moor', 'a rocky pass'), or with to= a road "
            "leading off unexplored ('the north road') -- a blank; action=sign marks a front's presence as the "
            "characters would see it ('smoke over Pigton', 'the dead orchard'), near a place -- never the front "
            "itself; action=unsign removes one when its front changes. A journey on a known route takes its days."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "action": _s(type=_STRING, enum=["place", "route", "sign", "unsign"]),
                "name": _s(type=_STRING, description="place: its name. sign/unsign: the sign."),
                "kind": _s(type=_STRING, enum=["steading", "dungeon", "landmark", "terrain"]),
                "note": _s(type=_STRING),
                "setting": _s(type=_STRING, enum=["Caverns", "Depths", "Experiments", "Folk", "Hordes", "Planes",
                                                  "Swamp", "Undead", "Woods"],
                              description="place: the monster setting of what lives there (a marsh: Swamp)."),
                "power": _s(type=types.Type.BOOLEAN, description=(
                    "place: a place of power -- standing stones, a ley-line, a god's altar -- where a Wizard's "
                    "Ritual can draw on it.")),
                "from_place": _s(type=_STRING), "to": _s(type=_STRING),
                "days": _s(type=_INTEGER), "terrain": _s(type=_STRING),
                "near": _s(type=_STRING, description="sign: the place it's near."),
            },
            required=["action"],
        ),
    ),
    types.FunctionDeclaration(
        name=RITUAL,
        description=(
            "A Wizard's Ritual: drawing on a place of power (one marked on the campaign map, where they are) to create "
            "a magical effect. action=start with the goal "
            "and your one to four conditions (it takes days/weeks/months; first you must...; help from...; a lot "
            "of money; only a lesser version; danger from...; disenchanting something). It is kept on their sheet "
            "and shown to you every turn. action=meet marks a condition fulfilled (its number or a word from it); "
            "action=finish when all are met (then give_magic_item if it made one); action=abandon."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING),
                        "action": _s(type=_STRING, enum=["start", "meet", "finish", "abandon"]),
                        "goal": _s(type=_STRING), "conditions": _s(type=_ARRAY, items=_s(type=_STRING)),
                        "condition": _s(type=_STRING), "outcome": _s(type=_STRING)},
            required=["character", "action"],
        ),
    ),
    types.FunctionDeclaration(
        name=GRAFT,
        description=(
            "Blacksmith (Fighter): at a forge, the powers of a magic weapon they carry are grafted onto their "
            "signature weapon, and the magic weapon is destroyed. Gary moves its text, piercing and reach."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"character": _s(type=_STRING), "weapon": _s(type=_STRING), "reason": _s(type=_STRING)},
            required=["character", "weapon"],
        ),
    ),
    types.FunctionDeclaration(
        name=MOUNT,
        description=(
            "A character's mount or beast of burden (a horse, a cart and donkey) is left behind -- stabled, tied "
            "at a dungeon's mouth, tethered while they climb -- or is back with them. Its load counts toward "
            "what they can carry only while it's with them. A mount lost, stolen or killed: take_items."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"actor": _s(type=_STRING), "name": _s(type=_STRING),
                        "with_party": _s(type=types.Type.BOOLEAN), "reason": _s(type=_STRING)},
            required=["actor", "name", "with_party"],
        ),
    ),
    types.FunctionDeclaration(
        name=PAY_FOR,
        description=(
            "A character pays for something off the book's price list that isn't equipment: a stay at an inn "
            "('a night at a civilized inn', 'a week at a peasant inn'), a meal or a feast, an escort, a chirurgeon, "
            "passage, a horse or a cart, a cottage, a bribe, a gift or finery. Gary charges the book's price -- less "
            "their Charisma where the book says so, less a Minstrel's skill in town -- and keeps what is kept (a "
            "horse, a house). For equipment use buy; for something unlisted name a price and use give_coins."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"actor": _s(type=_STRING), "what": _s(type=_STRING),
                        "times": _s(type=_INTEGER, description="How many: nights, meals, days of escort. Default 1."),
                        "reason": _s(type=_STRING)},
            required=["actor", "what"],
        ),
    ),
    types.FunctionDeclaration(
        name=SET_WEATHER,
        description=(
            "Set today's weather, once a day, fitting the season and the place ('hard frost, a white sky, no "
            "wind'), or when the story changes it (a storm rolls in). It is shown to you every turn until the "
            "next day, so keep to it. A Druid with Weather Weaver says what it is at sunrise: use their words."
        ),
        parameters=_s(type=_OBJECT, properties={"weather": _s(type=_STRING)}, required=["weather"]),
    ),
    types.FunctionDeclaration(
        name=RUMMAGE,
        description=(
            "A character pulls a useful mundane item from their adventuring gear -- a torch, rope, chalk, "
            "spikes, a pole: they find what they need and Gary marks off a use (5 to a pack; the last use "
            "empties it). Refused when they have none left -- then they don't have it. Not for weapons, armor "
            "or anything the book lists on its own."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={"actor": _s(type=_STRING),
                        "what": _s(type=_STRING, description="What they pull out: 'a torch', '50 feet of rope'."),
                        "reason": _s(type=_STRING)},
            required=["actor", "what"],
        ),
    ),
    types.FunctionDeclaration(
        name=GIVE_MAGIC_ITEM,
        description=(
            "A character gains a magic item -- spoils, a reward, a ritual's making. One of the book's (name it: "
            "'Vorpal Sword', 'Bag of Holding') comes with the book's text and tags; one you invent needs text "
            "(what it does -- something more interesting than +1 damage) and its tags ('close', '2 weight', "
            "'1 armor', '+1 armor' for a shield, '2 piercing'). It is carried as gear, its weight counts, and its "
            "text is shown to you under them every turn. Magic items are never sold; take_items removes one."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "actor": _s(type=_STRING), "name": _s(type=_STRING),
                "text": _s(type=_STRING, description="Only for one you invent: what it does."),
                "tags": _s(type=_ARRAY, items=_s(type=_STRING), description="Only for one you invent."),
                "reason": _s(type=_STRING),
            },
            required=["actor", "name", "reason"],
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
            "their own, or poured down a friend's throat (target) -- or, if they choose, "
            "removes one debility instead (cure). Bandages heal someone "
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
                "cure": _s(type=_STRING, description="Healing potion only: a debility to remove instead of healing 10, e.g. 'weak'."),
            },
            required=["actor", "item"],
        ),
    ),
    types.FunctionDeclaration(
        name=GIVE_COINS,
        description=(
            "Coin gained or lost outside a sale: a reward, a bribe, a fine, treasure you place. "
            "A creature's own wealth is roll_treasure instead. "
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
                "kind": _s(type=_STRING, enum=["npc", "clue", "promise", "foreshadow", "threat", "general"]),
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
            "a field to leave it unchanged; disposition runs -5 hostile to +5 devoted. "
            "Players see /npcs: everyone the party has met or heard of, with `public` and "
            "how they feel -- never `note`, which is yours."
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
                "note": _s(type=_STRING, description="GM-only, appended to what is already known: "
                                                     "secrets, plans, instincts."),
                "public": _s(type=_STRING, description=(
                    "One line the party knows, shown to players: who they are and how they met, "
                    "e.g. 'apothecary by the temple; insists on every tariff'. No secrets.")),
                "secret": _s(type=types.Type.BOOLEAN, description=(
                    "True for someone the party has not met or heard of -- a hidden villain, a "
                    "plotter offscreen. They stay off /npcs until mentioned without it.")),
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
                "steading": _s(type=_STRING, description=(
                    "The steading they are in, as create_steading named it -- or \"\" when they leave "
                    "for the wilds or a dungeon. What is for sale, prices, Supply and Recruit follow it.")),
                "dungeon": _s(type=_STRING, description=(
                    "In a dungeon or other hostile place explored room by room: its name ('the Barrow of "
                    "Xa'th'al'). Each location you set in it goes on its map. Leaving it: \"\".")),
                "via": _s(type=_STRING, description="In a dungeon: the exit they took to get here ('the iron door')."),
                "exits": _s(type=_ARRAY, items=_s(type=_STRING), description=(
                    "In a dungeon: the ways out of this area they can see ('a narrow stair down', 'a collapsed "
                    "arch north'). Untaken ones stay on the map as blanks.")),
                "setting": _s(type=_STRING, enum=["Caverns", "Depths", "Experiments", "Folk", "Hordes", "Planes",
                                                  "Swamp", "Undead", "Woods"], description=(
                    "A new dungeon's monster setting, from the bestiary: its inhabitants come first from it.")),
            },
            required=["location"],
        ),
    ),
    types.FunctionDeclaration(
        name=INSTANT_NPC,
        description=(
            "Someone becomes important in the moment and you need them now: Gary rolls the book's "
            "Instant NPC tables -- an instinct (what they want) and a knack (how they might get it) -- "
            "and a name not used yet. Make the NPC from them (upsert_npc); change the name if it does "
            "not suit the place."
        ),
        parameters=_s(type=_OBJECT, properties={"role": _s(type=_STRING, description=(
            "Who they are in the moment: 'the captive who got the power cosmic', 'the gate guard'."))},
            required=["role"]),
    ),
    types.FunctionDeclaration(
        name=COMPENDIUM_POWER,
        description=(
            "A compendium move called on in the fiction. weapon_boons: a Bearer's weapon rewards a deed it "
            "desired (boons, at most their bonds with it: heal, knowledge, insight, peace). deity_favor: Divine "
            "Bond marks off a bond with a deity for its clear, decisive favor. dragon_blood: a Dragonslayer "
            "calls on their blood (+Blood forward to Str rolls, then the 1d4 against dragon nature). "
            "drink_heart_blood: Blood +1. dragons_breath: fire on the creatures in its path (targets) and "
            "themselves. faraday: stir (on) or settle (off) the charge in their weapon. found_stronghold: "
            "Landed Gentry's keep (name, 2 benefits, your complication, craft if skilled). Blob: add_minion "
            "(name; a slime kept from one defeated), shape_minion (name, stat), rescoop (a splattered one), "
            "ooze_form (on/off), slime_copy (name; on=false reabsorbs it)."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "power": _s(type=_STRING, enum=["weapon_boons", "deity_favor", "dragon_blood", "drink_heart_blood",
                                                "dragons_breath", "faraday", "found_stronghold", "add_minion",
                                                "shape_minion", "rescoop", "ooze_form", "slime_copy"]),
                "stat": _s(type=_STRING, enum=["str", "dex", "con"], description="shape_minion: the stat made +2."),
                "boons": _s(type=_ARRAY, items=_s(type=_STRING, enum=["heal", "knowledge", "insight", "peace"])),
                "targets": _s(type=_ARRAY, items=_s(type=_STRING)),
                "deity": _s(type=_STRING),
                "on": _s(type=types.Type.BOOLEAN),
                "name": _s(type=_STRING, description="found_stronghold: its name."),
                "benefits": _s(type=_ARRAY, items=_s(type=_STRING, enum=["taxation", "fortified", "garrisoned",
                                                                         "prestigious", "skilled"])),
                "complication": _s(type=_STRING, enum=["infested", "unrest", "disputed"]),
                "craft": _s(type=_STRING),
            },
            required=["character", "power"],
        ),
    ),
    types.FunctionDeclaration(
        name=QUALIFY_COMPENDIUM,
        description=(
            "A character has just met a compendium class's requirement in the fiction, so at their "
            "next level-up they may take its starting move instead of a class move. The classes and "
            "what qualifies: " + describe_requirements()
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "character": _s(type=_STRING),
                "compendium_class": _s(type=_STRING,
                                       enum=list(COMPENDIUM_CLASSES)),
                "reason": _s(type=_STRING, description="What happened, in a few words. Shown to the table."),
            },
            required=["character", "compendium_class", "reason"],
        ),
    ),
    types.FunctionDeclaration(
        name=CREATE_STEADING,
        description=(
            "Make a steading -- a village, town, keep or city -- the first time the party reaches or "
            "hears of it, by the book: its kind's defaults, then one option and one problem by number "
            "(village options: 0 naturally defended, 1 abundant resources, 2 under another's protection, "
            "3 on a major road, 4 a wizard's tower, 5 a religious site; problems: 0 arid land, 1 dedicated "
            "to a deity, 2 recent battle, 3 monster problem, 4 absorbed another village, 5 remote. Towns, "
            "keeps and cities: the error lists theirs). Fill every 'your choice' in details, in order -- "
            "the error says which. Not secret: it shapes what they can buy and who they can hire."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "name": _s(type=_STRING),
                "kind": _s(type=_STRING, enum=["village", "town", "keep", "city"]),
                "option": _s(type=_INTEGER),
                "problem": _s(type=_INTEGER),
                "details": _s(type=_ARRAY, items=_s(type=_STRING),
                              description=("Each 'your choice' in order. Oath, Trade and Enmity name OTHER "
                                           "steadings; Resource and Need name goods; Blight a monster.")),
            },
            required=["name", "kind", "option", "problem"],
        ),
    ),
    types.FunctionDeclaration(
        name=ADJUST_STEADING,
        description=(
            "A steading changes, because the fiction changed it or between sessions by the book's "
            "updates (growth, collapse, want, trade, capture, profit, surplus): step prosperity, "
            "population or defenses up or down, add or remove tags, or change its kind."
        ),
        parameters=_s(
            type=_OBJECT,
            properties={
                "name": _s(type=_STRING),
                "steps": _s(type=_ARRAY, items=_s(type=_STRING), description="e.g. '+prosperity', '-defenses'."),
                "add": _s(type=_ARRAY, items=_s(type=_OBJECT, properties={
                    "tag": _s(type=_STRING), "detail": _s(type=_STRING)}, required=["tag"])),
                "remove": _s(type=_ARRAY, items=_s(type=_STRING), description="Tags to drop, e.g. 'need'."),
                "kind": _s(type=_STRING, enum=["village", "town", "keep", "city"]),
                "reason": _s(type=_STRING),
            },
            required=["name", "reason"],
        ),
    ),
]


def tool_config() -> List[types.Tool]:
    return [types.Tool(function_declarations=DECLARATIONS)]


def known_names() -> frozenset:
    return frozenset(d.name for d in DECLARATIONS)
