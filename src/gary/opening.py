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

import re
from typing import Dict, List, Sequence, Tuple

STAGES = ("premise", "characters", "introductions", "questions", "bonds", "playing", "done")
#: After the first session the opening is "done" between sessions and "session" during one:
#: /begin starts the next (session_event), /end ends it.

#: What each step's button says, by the stage it leads to.
NEXT_LABEL = {"introductions": "Introductions", "questions": "Hook questions", "bonds": "Bonds",
              "playing": "Begin the adventure"}

OFFERED = 4  # of each: places and activities
MAX_PART = 100  # a select option's label is 100 characters


def premise_prompt(already: Sequence[str] = ()) -> str:
    again = ""
    if already:
        again = ("Already offered and passed over -- offer genuinely different ones:\n"
                 + "\n".join(f"- {a}" for a in already[-8:]) + "\n")
    return (
        "OPENING: the table is starting a new Dungeon World adventure and choosing its premise: a "
        "fantastic location and a grabby activity -- what the characters are there to do. Not a "
        "campaign concept or a world: a place with a strong image, and a job with a clear goal and a "
        "reason it is hard. Examples: places -- 'a lighthouse on the back of a sleeping sea "
        "turtle', 'the drowned bell-towers of Old Marrow'; activities -- 'smuggle a cargo through "
        "the duke's blockade', 'steal back a book from a noble's estate during a masked ball'.\n"
        f"Offer {OFFERED} places and {OFFERED} activities, each able to pair with any of the others, "
        "varied in tone.\n" + again
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


def premise(place: str, activity: str) -> str:
    """The standing premise, as the model will read it every turn."""
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


def questions_event(premise_text: str, party: Sequence[Dict]) -> Dict:
    who = ", ".join(f"{s['name']} ({s.get('race', '')} {s.get('class', '')})" for s in party)
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


def bonds_text() -> str:
    return ("**Bonds.** Now that you know each other, fill in your bonds: press **Write my bonds**, or "
            "**Bonds** on `/sheet`. Each names another character; at least one is enough to start.")


def opening_event(premise_text: str, party: Sequence[Dict] = ()) -> Dict:
    from .bonds import written

    bonds = {s["name"]: [written(b) for b in s.get("bonds") or ()] for s in party if s.get("bonds")}
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


def review_event(threats: Sequence[str] = ()) -> Dict:
    """Between sessions: the book's front work, done after every /end but the first."""
    return {
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
            "Keep to two or three fronts in play. Do nothing if nothing calls for it. Reply to the table with "
            "at most one line, without spoilers."
        ),
    }


def fronts_event(threats: Sequence[str] = ()) -> Dict:
    return {
        "event": "Opening: fronts after the first session",
        "threats_noted": list(threats),
        "instruction": (
            "The first session is over. As the book advises, write the fronts now, from what play turned up: "
            "the threats noted above, the dangerous things mentioned but not dealt with, the people and the "
            "questions the table cared about. Call create_front with scope=campaign once -- the threat that "
            "runs session to session; it may be incomplete, leave blanks -- and with scope=adventure for one "
            "or two more, about what matters here and now. Each has its dangers, impending doom and grim "
            "portents. Fronts are the GM's secret: reply to the table with one line, without spoilers, saying "
            "the world is stirring -- nothing about what the fronts contain."
        ),
    }
