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
    playing        and after the first /endsession, fronts from what play turned up

The stage is kept in state (``opening``) by the OPENING mutation, so a restart resumes it
and /undo can take a step back. This module holds the words: what the model is asked at
each step, and reading back its offer of places and activities.
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence, Tuple

STAGES = ("premise", "characters", "introductions", "questions", "bonds", "playing", "done")

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
             "— their eyes, their body, what they wear. Ask each other anything. (Bonds come later.)"]
    for sheet in party:
        lines.append(f"• **{sheet['name']}**, {sheet.get('race', '')} {sheet.get('class', '')}".rstrip())
    return "\n".join(lines)


def questions_event(premise_text: str, party: Sequence[Dict]) -> Dict:
    who = ", ".join(f"{s['name']} ({s.get('race', '')} {s.get('class', '')})" for s in party)
    return {
        "event": "Opening: hook questions",
        "premise": premise_text,
        "characters": who,
        "instruction": (
            "The adventure starts now, and the world is built at the table. Ask hook questions that pin down "
            "the premise: its motive, stakes, urgency, dangers, details and complications. Ask one to three "
            "per character -- start with one each -- each addressed to a CHARACTER by name, never to the "
            "group. Assert as much as you ask, so each question has hard edges ('Mara, the smuggler who "
            "hired you betrayed your brother once. Why do you trust her now?'). Ask for something that "
            "matters, not just a name; get personal -- their past, feelings and ties. Leave room for their "
            "answer. Do not narrate a scene or start play yet. As answers come in on later turns, record "
            "each as fact (upsert_npc for people, record_note for places, dangers and lore), build on it, "
            "and ask follow-ups until the core of the premise is clear -- leave some questions open for play."
        ),
    }


def bonds_text() -> str:
    return ("**Bonds.** Now that you know each other, fill in your bonds: press **Write my bonds**, or "
            "**Bonds** on `/sheet`. Each names another character; at least one is enough to start.")


def opening_event(premise_text: str) -> Dict:
    return {
        "event": "Opening: the first scene",
        "premise": premise_text,
        "instruction": (
            "Frame the opening scene tightly, from everything the table has established. Start at the "
            "threshold of the premise or in the middle of the action. Say who is there, where, when, and what "
            "they are doing, with no more than three sensory details. Call set_scene for it (and "
            "add_monsters if a threat is present, advance_time if the hour matters). Then make a soft GM "
            "move -- show a danger coming, offer an opportunity with a cost -- and end on 'What do you do?' "
            "to a specific character. Note the questions the setup implies (will they get the cargo through?) "
            "and drive play toward answering them."
        ),
    }


def fronts_event() -> Dict:
    return {
        "event": "Opening: fronts after the first session",
        "instruction": (
            "The first session is over. As the book advises, write the campaign's fronts now, from what play "
            "turned up: the threats, the people and the questions the table cared about. Call create_front "
            "for one or two, each with its dangers, impending doom and grim portents. Fronts are the GM's "
            "secret: reply to the table with one line, without spoilers, saying the world is stirring -- "
            "nothing about what the fronts contain."
        ),
    }
