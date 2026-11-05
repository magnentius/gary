"""Starting an adventure (`/begin`): the table's first session, run step by step.

Dungeon World's first session prepares little and builds the world at the table. Gary runs
the recipe from "My Recipe for Starting Adventures" (Spouting Lore, 2020), which keeps the
book's low prep and adds structure for players to build on:

    premise        a fantastic place and a grabby activity -- Gemini offers some of each
    characters     everyone makes one with /new
    introductions  name, class, look; the table asks each other questions
    questions      hook questions, each to one character, asserting as much as asking
    bonds          now that the characters know each other
    opening        a tight frame in the middle of things, a soft move, "What do you do?"
    playing        and after the first /end, fronts from what play turned up

The stage is kept in state (``opening``) by the OPENING mutation, so a restart resumes it
and /undo can take a step back. This module holds the words: what the model is asked at
each step, and reading back its offer of places and activities.
"""

from __future__ import annotations

import random
import re
from typing import Dict, List, Optional, Sequence, Tuple

STAGES = ("premise", "characters", "introductions", "questions", "bonds", "playing", "done")
#: After the first session the opening is "done" between sessions and "session" during one:
#: /begin starts the next (session_event), /end ends it.

#: What each step's button says, by the stage it leads to.
NEXT_LABEL = {"introductions": "Introductions", "questions": "Hook questions", "bonds": "Bonds",
              "playing": "Begin the adventure", "carryon": "Carry on in this world",
              "finale": "Close the campaign with an epilogue",
              "campaign": "A campaign", "oneshot": "A one-shot (about 4 hours)", "scene": "Next scene",
              "break": "Level-up break", "epilogue": "Epilogue", "continue": "Continue as a campaign"}

#: One-shot mode follows "Dragonslaying on a Timetable" (John Aegard, 2014): the characters
#: already travel together toward one Final Destination; questions build the setting, the
#: premises and the opposition; three scenes -- arrival, a branch, the finale -- with a level-up
#: break before the last; then an epilogue. No fronts: the book notes that in a one-shot "the
#: longer cycles of levels and bonds don't really kick in".
ONESHOT_SCENES = 3

OFFERED = 4  # of each: places and activities

#: Ingredients for a premise, a few drawn at random each time it is asked for. Fixed examples were
#: echoed: shown "a silver mine", "drowned bell-towers" and "poisoning the abbey's well", three games
#: running brought salt mines, phantom church bells, flooding crypts and poisoning saboteurs.
PLACE_KINDS = (
    "market town", "fishing village", "river ford", "toll bridge", "manor house", "hill fort", "border keep",
    "monastery", "pilgrim shrine", "lighthouse", "harbour", "river barge", "merchant ship", "island", "quarry",
    "mine", "logging camp", "vineyard", "orchard", "watermill", "windmill", "tannery", "brewery", "crossroads inn",
    "travelling fair", "circus", "theatre", "bathhouse", "prison", "courthouse", "library", "college",
    "graveyard", "barrow field", "standing stones", "ruined temple", "mountain pass", "glacier hut", "fen village",
    "salt marsh", "heath", "deep forest", "hunting lodge", "caravan route", "oasis", "duke's palace", "sewers",
    "beekeepers' hamlet", "horse fair", "shipyard", "smugglers' cove", "goblin market", "dwarf-hold gate",
    "elven ruin", "wizard's tower", "giant's causeway", "hermitage", "plague village", "garrison town",
)
TROUBLES = (
    "a debt come due", "a wedding", "a feud", "a disappearance", "a curse", "a festival", "an election",
    "a disputed succession", "a strike", "a pilgrimage", "a trial", "a duel", "an inheritance", "a broken treaty",
    "a haunting", "a monster's lair", "an escaped prisoner", "a lost heir", "a forbidden romance", "a rival crew",
    "a forged deed", "a stolen relic", "a bandit toll", "a sick beast", "a hostage", "a bet", "a race",
    "a tax revolt", "a false prophet", "a witch hunt", "a counterfeit coin", "a runaway apprentice",
    "a buried secret", "a dragon's demand", "a lost map", "a siege", "an embassy", "a ransom", "a heresy",
)
SEASONS = ("the spring thaw", "high summer", "harvest time", "the first frost", "deep winter", "a long drought",
           "the autumn rains", "midsummer's eve", "the dead of night all season")
MAX_PART = 100  # a select option's label is 100 characters


def premise_prompt(already: Sequence[str] = (), rng: Optional[random.Random] = None) -> str:
    rng = rng or random.Random()
    places, troubles = rng.sample(PLACE_KINDS, 6), rng.sample(TROUBLES, 6)
    again = ""
    if already:
        again = ("Already offered and passed over -- offer genuinely different ones:\n"
                 + "\n".join(f"- {a}" for a in already[-8:]) + "\n")
    return (
        "OPENING: the table is starting a new Dungeon World adventure and choosing its premise: a "
        "location and a grabby activity -- what the characters are there to do. Not a campaign concept "
        "or a world: a place with a strong image, and a job with a clear goal and a reason it is hard.\n"
        "PLACES are grounded: a real kind of place with one memorable twist. Not titanic creatures' "
        "remains, not anything floating, hanging or suspended, not lakes of molten anything.\n"
        "ACTIVITIES have a clear goal and a complication. At least one must be winnable without "
        "bloodshed -- a negotiation, a delivery, a mystery, a rescue by wits or stealth, an escort, "
        "keeping a peace.\n"
        f"This time, draw on these (most of your offers should use one; reach past the first idea): kinds of "
        f"place -- {', '.join(places)}; troubles -- {', '.join(troubles)}; the season -- {rng.choice(SEASONS)}.\n"
        f"Offer {OFFERED} places and {OFFERED} activities, each able to pair with any of the others, "
        "varied in tone -- some light, some grim. No two may share an image or a central verb: at most "
        "one theft, one rescue, one escort, and no repeated motif.\n" + again
        + f"Reply with exactly {OFFERED * 2} lines and nothing else, in this form:\n"
        + "\n".join(["PLACE: <a fantastic location>"] * OFFERED + ["ACTIVITY: <a grabby activity>"] * OFFERED)
        + f"\nEach under {MAX_PART} characters. No preamble, no numbering. Call no tools."
    )


_LINE = re.compile(r"^\s*(?:[-*>\d.)\s]*)\**(PLACE|ACTIVITY)\**\s*[:\-]\s*(.+?)\s*$", re.IGNORECASE)


def parse_offer(text: str) -> Tuple[List[str], List[str]]:
    """The places and activities in the model's reply, as many as it gave (at most OFFERED)."""
    places: List[str] = []
    activities: List[str] = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        value = m[2].strip().strip('"').strip()[:MAX_PART]
        if not value:
            continue
        (places if m[1].upper() == "PLACE" else activities).append(value)
    return places[:OFFERED], activities[:OFFERED]


def premise(place: str, activity: str, oneshot: bool = False) -> str:
    """The standing premise, as the model will read it every turn."""
    if oneshot:
        return (f"A one-shot: {activity.strip().rstrip('.')} -- and it ends at {place.strip().rstrip('.')}, "
                "the final destination.")
    return f"The adventure: {activity.strip().rstrip('.')} -- at {place.strip().rstrip('.')}."


def introductions(party: Sequence[Dict]) -> str:
    lines = ["**Introductions.** Each of you, tell the table who your character is and what they look like "
             "— bring the look below to life. Ask each other anything. (Bonds come later.)"]
    from .choices import look_of

    for sheet in party:
        look = look_of(sheet)
        lines.append(f"• **{sheet['name']}**, {sheet.get('race', '')} {sheet.get('class', '')}".rstrip()
                     + (f" — {look}" if look else ""))
    return "\n".join(lines)


def questions_event(premise_text: str, party: Sequence[Dict], oneshot: bool = False) -> Dict:
    who = ", ".join(f"{s['name']} ({s.get('race', '')} {s.get('class', '')})" for s in party)
    if oneshot:
        return {
            "event": "One-shot: questions",
            "premise": premise_text,
            "characters": who,
            "instruction": (
                "A one-shot starts now; there is no world yet, and the table builds it. The characters already know "
                "each other and are travelling to the same place: the final destination in the premise. Ask "
                "questions, each to one CHARACTER by name, that build three things: the SETTING (ask the characters "
                "tied to places first -- a Druid's land, a Ranger's wilds, a Barbarian's homeland, a Thief's city "
                "-- then the others), the PREMISES (what each of them is doing and why; several premises are fine, "
                "even at odds with one another, but every one must lead to the final destination, and every "
                "character must have a stake in at least one), and the OPPOSITION (who or what guards the "
                "destination; ask one question that names the final enemy). Draw on their class choices -- appetites, "
                "lore, deities, poisons. Assert as much as you ask; ask several questions about one thing to give it "
                "edges. Do not narrate a scene yet. As answers come in, record each as fact (upsert_npc, record_note) "
                "and follow up until the premises are clear. Do not create fronts."
                + _outsider_instruction(outsiders(party))
            ),
        }
    return {
        "event": "Opening: hook questions",
        "premise": premise_text,
        "characters": who,
        "instruction": (
            "The adventure starts now, and the world is built at the table. Ask hook questions that pin down "
            "the premise -- its motive, stakes, urgency, dangers, details and complications -- and how the "
            "characters know each other and came together, as the book asks during introductions. Ask one to three "
            "per character -- start with one each -- each addressed to a CHARACTER by name, never to the "
            "group. Assert as much as you ask, so each question has hard edges ('Mara, the smuggler who "
            "hired you betrayed your brother once. Why do you trust her now?'). Ask for something that "
            "matters, not just a name; get personal -- their past, feelings and ties. Leave room for their "
            "answer. Do not narrate a scene or start play yet. As answers come in on later turns, record "
            "each as fact (upsert_npc for people, record_note for places, dangers and lore), build on it, "
            "and ask follow-ups until the core of the premise is clear -- leave some questions open for play."
            + _outsider_instruction(outsiders(party))
        ),
    }


def outsiders(party: Sequence[Dict]) -> List[str]:
    """Characters with the Barbarian's Outsider: "At the beginning of each session, the GM will
    ask you something about your homeland, why you left, or what you left behind. If you answer
    them, mark XP." """
    from .class_moves import TEXTS

    return [s["name"] for s in party
            if TEXTS.get(str(s.get("class", "")).lower(), {}).get("race_move") == "Outsider"]


def _outsider_instruction(names: Sequence[str]) -> str:
    if not names:
        return ""
    return (f" {' and '.join(names)} {'is an Outsider' if len(names) == 1 else 'are Outsiders'}: ask each "
            "something about their homeland, why they left, or what they left behind. When one answers, "
            "call mark_xp for them with reason 'Outsider'.")


def session_event(number: int, party: Sequence[Dict]) -> Dict:
    """The start of every session after the first."""
    return {
        "event": f"Session {number} begins",
        "instruction": (
            "A new session is starting; the players have been away. In a few sentences, recap where things "
            "stand -- what happened last time, where the party is, what is unresolved -- from the scene, the "
            "notes and the story so far; no new events, nothing about fronts."
            + _outsider_instruction(outsiders(party))
            + " Then reframe the current scene with a detail or two, and end on 'What do you do?' to a "
              "specific character."
        ),
    }


def death_notice(state: Dict, name: str) -> str:
    """What the table is told when a character dies: the dead stay dead here. In a campaign their
    player makes the next character at level 1, as the book says; in a one-shot their part is over."""
    current = state.get("opening") or {}
    if current.get("mode") == "oneshot":
        return (f"⚰️ **{name}** is dead, and stays dead. This is a one-shot: there's no new character -- "
                f"{name}'s player can still help tell the story, and their ending is part of the epilogue.")
    return (f"⚰️ **{name}** is dead, and stays dead. {name}'s player: make your next character with `/new` "
            "(level 1). They can be a hireling stepping up, someone the party meets, or someone with a reason "
            f"to finish what {name} started.")


def newcomer_event(name: str, fallen: Sequence[str]) -> Dict:
    """A campaign character made after a death: the GM brings them in."""
    return {"event": "A new character joins", "character": name, "replacing": list(fallen), "instruction": (
        f"{name} is a new character, made after {' and '.join(fallen)} died. Bring them into the story at the next "
        "natural moment, as the book suggests: a hireling who steps up, someone the party meets where they are, or "
        "someone with a reason to finish what the fallen started. Ask their player a question or two to tie them "
        "in. The dead stay dead.")}


def doomed(party: Sequence[Dict]) -> List[str]:
    """Characters marked as Death's own and not yet dead (a Last Breath miss)."""
    return [s["name"] for s in party if s.get("deaths_mark") and not s.get("dead")]


def with_doom(event: Dict, party: Sequence[Dict]) -> Dict:
    """A scene, the epilogue or a new session, with anyone marked as Death's own still alive: "you'll
    cross the threshold soon. The GM will tell you when." -- now is when."""
    names = doomed(party)
    if not names:
        return event
    who = " and ".join(names)
    return {**event, "deaths_own": names, "instruction": event.get("instruction", "") + (
        f" {who} {'is' if len(names) == 1 else 'are'} marked as Death's own: the time has come. Let it happen in "
        "this scene, at a moment that matters -- a last stand, a sacrifice, a quiet passing -- and call death_comes. "
        "Only something extraordinary -- a bargain with Death himself -- spares them, and that has a price. Once "
        "dead, they stay dead.")}


def bonds_text() -> str:
    return ("**Bonds.** Now that you know each other, fill in your bonds: press **Write my bonds**, or "
            "**Bonds** on `/sheet`. Each names another character; at least one is enough to start.")


def opening_event(premise_text: str, party: Sequence[Dict] = (), oneshot: bool = False) -> Dict:
    from .bonds import written

    bonds = {s["name"]: [written(b) for b in s.get("bonds") or ()] for s in party if s.get("bonds")}
    if oneshot:
        return {
            "event": "One-shot: scene 1 of 3, getting underway",
            "premise": premise_text,
            "bonds": bonds,
            "instruction": (
                "This one-shot has three scenes: this arrival, a branch in the road, and a finale at the final "
                "destination; Gary moves on when the table is ready. Remind the players once, briefly, that this is a "
                "conversation and they can name a move when they see one. Start the party one or two places short of "
                "the destination, at the moment they first see their enemy -- not already fighting. Make the enemy a "
                "mob of six to ten weak creatures drawn from the answers, strange rather than goblins or orcs, with a "
                "move that entangles and a way of moving -- flying, leaping or burrowing: add_monsters with a recipe "
                "(organization horde, defense flesh, and an adaptation for the way it moves). Fold in a bond or two. Call set_scene. Reward Spout Lore, Discern Realities and "
                "the like with answers that can win the fight, even on a 7-9; let a well-leveraged Parley work. Then "
                "ask 'What do you do?' to a specific character."
            ),
        }
    return {
        "event": "Opening: the first scene",
        "premise": premise_text,
        "bonds": bonds,
        "instruction": (
            "First, the bonds above: they are what the characters think of each other. Pick one or two that "
            "stand out and fold them into the scene. Then frame the opening tightly, from everything the table "
            "has established: a tense situation that demands action -- at the threshold of the premise or in "
            "the middle of it, as the book has it ('outside the entrance to a dungeon, ambushed in a fetid "
            "swamp'). Say who is there, where, when, and what they are doing, with no more than three sensory "
            "details. Call set_scene for it (and add_monsters if a threat is present, advance_time if the hour "
            "matters). Make a soft GM move -- show a danger coming, offer an opportunity with a cost. Ask a "
            "question right away, to a specific character, that the situation raises ('who is leading the "
            "ambush against you?'), and end on 'What do you do?'. Note the questions the setup implies (will "
            "they get the cargo through?) and drive play toward answering them. Threats you introduce are "
            "noted with record_note kind=threat: fronts come after this session."
        ),
    }


def _settings(world: Optional[Dict]) -> Tuple[str, List[str]]:
    """The bestiary's monster settings, and the map's places that have one: "Consult a monster setting to
    populate a front or when you want a threat that is only tangentially related to one of your fronts"."""
    from . import dungeons

    places = [f"{p['name']}: {p['setting']}" for p in ((world or {}).get("places") or {}).values() if p.get("setting")]
    return dungeons.catalog(), places


_SETTINGS_ADVICE = (
    "Populate dangers from the monster settings (monster_settings; places_by_setting says what lives where on "
    "the map): a danger's creatures come from the setting of where it lurks, and a setting also gives threats "
    "only tangentially related to a front -- the undead in the ruins a cult has taken -- blocks in the heroes' "
    "path. "
)


def review_event(threats: Sequence[str] = (), new_campaign_front: bool = False,
                 steadings_changed: Sequence[str] = (), steadings_to_check: Sequence[str] = (),
                 world: Optional[Dict] = None) -> Dict:
    """Between sessions: the book's front work, and its steading updates, after every /end but the first."""
    catalog, places = _settings(world)
    return {
        "monster_settings": catalog,
        "places_by_setting": places,
        "new_campaign_front": new_campaign_front,
        "steadings_changed_by_gary": list(steadings_changed),
        "steadings_to_check": list(steadings_to_check),
        "event": "Between sessions: front work",
        "threats_noted": list(threats),
        "instruction": (
            "The session is over; this is the GM's time between sessions, out of play. Look over the fronts "
            "and what happened. Fronts continue apace whether the characters are there or not: advance "
            "(advance_portent) any danger the party neglected or did not see. End dangers that were dealt "
            "with (end_danger). Revise what is still to come where play changed it (revise_front). Move an "
            "unresolved danger you like from an adventure front up to the campaign front, or a campaign "
            "danger that has come to bear into an adventure front (move_danger). If an adventure front has "
            "been resolved -- or the party has set off after something new -- write a new adventure front "
            "(create_front scope=adventure), inspired by the campaign front and the threats noted above. "
            "Then the steadings, as the book updates the campaign map between sessions: Gary has applied what "
            "their tags decide (steadings_changed_by_gary); go through steadings_to_check and, where the fiction "
            "says a condition holds, apply it with adjust_steading. "
            + _SETTINGS_ADVICE +
            "Keep to two or three fronts in play. Update the campaign map's signs of the fronts as they change "
            "(world_map sign/unsign: a burning village, refugees on the road -- as the characters would see it, "
            "never the front's name). Do nothing if nothing calls for it. "
            + ("The campaign front is over and the table chose to carry on: write a new campaign front "
               "(create_front scope=campaign) from what the last one left behind. " if new_campaign_front else "")
            + "Reply to the table with at most one line, without spoilers."
        ),
    }


def fronts_event(threats: Sequence[str] = (), world: Optional[Dict] = None) -> Dict:
    catalog, places = _settings(world)
    return {
        "monster_settings": catalog,
        "places_by_setting": places,
        "event": "Opening: fronts after the first session",
        "threats_noted": list(threats),
        "instruction": (
            "The first session is over. As the book advises, write the fronts now, from what play turned up: "
            "the threats noted above, the dangerous things mentioned but not dealt with, the people and the "
            "questions the table cared about. Call create_front with scope=campaign once -- the threat that "
            "runs session to session; it may be incomplete, leave blanks -- and with scope=adventure for one "
            "or two more, about what matters here and now. Each has its dangers, impending doom and grim "
            "portents. " + _SETTINGS_ADVICE + "Every steading on the campaign map needs its record: "
            "create_steading for any that has none, as play showed it. Put a sign of each front on the campaign map (world_map action=sign), as the characters would see "
            "it -- the burning village, the dead orchard -- never the front itself. Fronts are the GM's secret: "
            "reply to the table with one line, without spoilers, saying "
            "the world is stirring -- nothing about what the fronts contain."
        ),
    }


# -- the end of a campaign -------------------------------------------------------------------

def scene_event(scene: int) -> Dict:
    """One-shot scenes 2 and 3."""
    if scene == 2:
        return {
            "event": "One-shot: scene 2 of 3, the branch",
            "instruction": (
                "Wrap up the arrival in a sentence. Now offer three ways to the final destination, each a place "
                "inspired by the answers (a drowned road, a smugglers' tunnel, a hermit's tower) and say what each "
                "promises and risks; call set_scene and let the party choose. On the way, put them against something "
                "that must be outwitted rather than fought -- a creature whose gaze petrifies, a sphinx's terms, a "
                "puzzle-lock with teeth -- and let the route offer an ally, a secret or an item that will matter at "
                "the end. End on 'What do you do?'."
            ),
        }
    return {
        "event": "One-shot: scene 3 of 3, the finale",
        "instruction": (
            "The party reaches the final destination. Bring on the big enemy the answers named -- a solitary "
            "creature that is more than a bag of hit points: several parts or heads, a weak spot, terrain to use "
            "(height, water, fire, onlookers). Play it hard: characters may die and meet Death. Keep the premises "
            "alive in the fight -- tempt each character with their own goal at the worst moment. When the fight is "
            "over, let the premises that pulled against each other come to a head before anyone leaves. End on "
            "'What do you do?'."
        ),
    }


def epilogue_event(party: Sequence[Dict], oneshot: bool = False) -> Dict:
    """The campaign front is over and the table chose to close the campaign -- or a one-shot ends."""
    names = ", ".join(s["name"] for s in party)
    if oneshot:
        return {
            "event": "One-shot: the epilogue",
            "characters": names,
            "instruction": (
                "The one-shot is over. Go round the table: ask each character, by name, what becomes of them now, "
                "saving for last the one whose story changed the most; let each player tell their own ending and add "
                "a detail that honours it. Then give a line or two of epilogue for the people and places the table "
                "cared about most, and close the story. No rolls."
            ),
        }
    return {
        "event": "The campaign's last session: an epilogue",
        "characters": names,
        "instruction": (
            "The campaign front is over and the table has chosen to end the campaign here. As the book says of a "
            "resolved front, take time over the aftermath: in a few sentences, show how the world has changed -- "
            "what came to pass, what was stopped, who rose and who fell. Then ask each character, by name, one at "
            "a time, what becomes of them now; let each player tell their own ending and add a detail that "
            "honours it. When everyone has answered, close the story in a short final passage. No new threats, "
            "no rolls unless a player asks for one."
        ),
    }


def chronicle(state: Dict) -> str:
    """The campaign in brief, for the table to keep: the premise, how many sessions, the
    characters and what became of them, the fronts and how they ended, the people met."""
    from .npcs import known

    lines = ["📖 **The chronicle of this campaign**"]
    if state.get("setting"):
        lines.append(f"*{state['setting']}*")
    sessions = int((state.get("opening") or {}).get("session") or 0)
    if sessions:
        lines.append(f"{sessions} session{'s' if sessions != 1 else ''}.")
    sheets = list((state.get("sheets") or {}).values())
    if sheets:
        lines.append("**The heroes**")
    for s in sheets:
        fate = ("died" if s.get("dead") else "retired to safety" if s.get("retired")
                else "became a mentor" if s.get("mentor") else "")
        was = f", once a {' and '.join(s['former_classes'])}" if s.get("former_classes") else ""
        lines.append(f"• {s['name']}, {s.get('race', '')} {s.get('class', '')} {s.get('level', 1)}{was}"
                     + (f" — {fate}" if fate else ""))
    fronts = list((state.get("fronts") or {}).values())
    if fronts:
        lines.append("**The threats**")
    for f in fronts:
        ends = []
        for d in f.get("dangers") or ():
            ended = str(d.get("ended") or "")
            ends.append(f"{d['name']}: " + (ended or "still at large"))
        lines.append(f"• {f['name']}" + (" (campaign front)" if f.get("scope") == "campaign" else "")
                     + (" — " + "; ".join(ends) if ends else ""))
    people = [n["name"] for n in known(state)]
    if people:
        lines.append("**People met:** " + ", ".join(people[:20]) + ("…" if len(people) > 20 else ""))
    text = "\n".join(lines)
    return text if len(text) <= 1900 else text[:1890].rsplit("\n", 1)[0] + "\n…"
