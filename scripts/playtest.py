"""Headless playtest: real rules, real Gemini, a throwaway campaign, no Discord.

Plays scripted scenes the way a table would. Player lines go in as messages; every button
Gary posts is "clicked" by the player it belongs to -- Roll, an option, Roll damage,
Choose spells, a debility to shed, an item to drop -- so a session runs end to end. The
whole thing is written to a Markdown transcript, with what the model called and what it
was told, and a list of anything worth a look: tool errors, refused moves, stalls,
runaway loops, exceptions.

    set -a && . ~/.config/gary/env && set +a
    PYTHONPATH=src .venv/bin/python scripts/playtest.py fight --out /tmp/fight.md
    PYTHONPATH=src .venv/bin/python scripts/playtest.py all --seed 7
    PYTHONPATH=src .venv/bin/python scripts/playtest.py town --llm-players --out /tmp/town.md

With --llm-players a second model plays the characters instead of the scripted lines: each
has a goal for the scene, reads what Gary just said and answers in character, so a scene
that escalates is played through rather than left behind. Whoever Gary addressed last
speaks next; the scene ends when every character says their goal is done, or at the turn
limit.

It spends real Gemini calls (a scenario is a few dozen turns on Flash). The campaign is
in memory and vanishes with the process. What it cannot see is Discord itself: how a
button or a dropdown looks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gary import choices, spells
from gary.steadings import here
from gary.adapters.gemini import GeminiNarrator
from gary.adapters.runner import STALL_MESSAGE, EffectRunner
from gary.fsm import (
    ChoicePrompt, CurePrompt, DamagePrompt, Director, OverloadPrompt, ReactPrompt, RollPrompt, SpellsPrompt,
)
from gary.models import Mutation, MutationKind
from gary.ports import RandomDice
from gary.registry import PendingCheckRegistry
from gary.sheets import Draft
from gary.state.backend import InMemoryStateBackend
from gary.state.history import InMemoryHistoryBackend
from gary.state.store import CampaignStore

CHANNEL = 4242
#: A scene's player line can set off a chain of buttons; past this, something is looping.
MAX_CLICKS_PER_LINE = 25


# -- the party ---------------------------------------------------------------------------


def _character(name: str, cls: str, race: str, order: Sequence[str], gear: Dict[int, Sequence[int]],
               alignment: str, choices: Sequence[str] = (), signature: Optional[tuple] = None) -> dict:
    """A character made through the same Draft as `/new`, so it is valid by construction."""
    d = Draft()
    d.choose_class(cls)
    d.choose_race(race)
    for stat in order:
        d.assign(stat, d.next_score())
    for group, picks in gear.items():
        d.choose_gear(group, picks)
    if signature:
        base, rng, enhancements = signature
        d.choose_signature_base(base)
        d.choose_signature_range(rng)
        d.choose_signature_enhancements(enhancements)
    d.choose_alignment(alignment)
    for value in choices:
        d.choose_class_choice(value)
    d.choose_name(name)
    sheet = d.build()
    if spells.caster_class(sheet):
        prep = spells.Preparation(sheet)
        while prep.step() is not None:
            prep.choose(_spell_picks(prep))
        sheet.update(prep.changes())
    return sheet


def _spell_picks(prep: "spells.Preparation") -> List[str]:
    fewest, most = prep.counts()
    picks, total = [], 0
    for s in prep.options():
        if len(picks) == most:
            break
        if prep.step() == "prepare" and total + s.level > spells.budget(prep.sheet):
            continue
        picks.append(s.name)
        total += s.level
    return picks[:max(fewest, len(picks))]


def party() -> List[Tuple[int, dict]]:
    thorin = _character(
        "Thorin", "cleric", "Dwarf", ("wis", "con", "str", "cha", "dex"), {0: (1,), 1: (0,), 2: (0,)},
        "Good", choices=("Krugon the Bleak", "Healing and Restoration", "Petition: Offering"),
    )
    dorn = _character(
        "Dorn", "fighter", "Human", ("str", "con", "dex", "wis", "cha"), {0: (0,), 1: (0, 3)},
        "Good", choices=("Ancient",), signature=("Sword", "close", (1, 3)),
    )
    return [(1001, thorin), (1002, dorn)]


# -- scenarios ---------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    setting: str
    scene: Tuple[str, str]
    lines: List[Tuple[str, str]]  # (character, what their player says)
    # Sheet changes before the scene starts, by character: coin for a town, wounds for a rest.
    setup: Dict[str, dict] = field(default_factory=dict)
    # For --llm-players: what each character is after in this scene.
    goals: Dict[str, str] = field(default_factory=dict)


SCENARIOS: Dict[str, Scenario] = {
    "fight": Scenario(
        "fight",
        "Low fantasy borderlands. Goblin raiders harry the old road; the town of Fenwick is a "
        "day's walk east.",
        ("The old road, Fenwick Woods", "A broken-down cart blocks the road at dusk. Thick brush on "
                                        "both sides; a shallow ditch to the north."),
        [
            ("Dorn", "We walk up to the broken cart. I keep my hand on my sword."),
            ("Thorin", "I look around carefully — is anyone hiding near the cart?"),
            ("Dorn", "If anything comes out of the brush I charge it."),
            ("Thorin", "I call on Krugon to bless Dorn before the fight."),
            ("Dorn", "I attack the nearest goblin with my sword!"),
            ("Thorin", "I stand over Dorn and defend him from the goblins."),
            ("Dorn", "I keep swinging at whatever is left."),
            ("Thorin", "If one of them is pinned or knocked down, I bring my hammer down on it."),
            ("Dorn", "Are any of them still standing? I finish them off."),
        ],
        goals={"Dorn": "Get past the ambush alive and kill or drive off every goblin, then search the bodies.",
               "Thorin": "Keep Dorn alive: bless him, defend him, heal him if he is hurt."},
    ),
    "discern": Scenario(
        "discern",
        "Low fantasy borderlands. An old barrow-crypt under the hill; something lairs there.",
        ("The barrow-crypt", "A low stone hall lit by one guttering torch. Bones heaped by a sealed "
                             "sarcophagus; a great shape breathes slowly in the dark beyond it."),
        [
            ("Thorin", "Before anyone moves, I study the hall carefully. What here is not what it "
                       "appears to be?"),
            ("Dorn", "I keep watch on that breathing shape while Thorin looks."),
            ("Thorin", "Knowing that, I act on it — I go for what we just learned."),
            ("Dorn", "I strike the thing while it is still unaware of us."),
            ("Thorin", "I fight beside Dorn until it is down."),
        ],
        goals={"Thorin": "Study the crypt closely before acting, then use exactly what you learn to "
                         "beat whatever lairs here.",
               "Dorn": "Wait for Thorin's read of the room, then strike the creature using it."},
    ),
    "hire": Scenario(
        "hire",
        "Low fantasy borderlands. Fenwick is a walled market town, the only one for days.",
        ("Fenwick, the Drowned Rat", "Smoky common room at dusk; sellswords and out-of-work guards "
                                     "drink in the corners."),
        [
            ("Dorn", "I stand on a table and announce we're hiring a guard for a barrow delve — good "
                     "pay and a share of whatever we find."),
            ("Dorn", "I hire the best of them."),
            ("Thorin", "On the way out of town, Dorn orders our new guard to scout the barrow "
                       "entrance alone while we wait."),
        ],
        setup={"Dorn": {"coins": 200}},
        goals={"Dorn": "Hire a capable guard for the barrow (offer good pay and a share), then send "
                       "them ahead into danger.",
               "Thorin": "Help Dorn find a trustworthy hireling and make sure they are paid."},
    ),
    "camp": Scenario(
        "camp",
        "Low fantasy borderlands. Goblin raiders harry the old road.",
        ("A ruined chapel off the old road", "Roofless chapel, walls mostly standing, the door can be "
                                             "barred. Quiet. The goblins are dead or fled."),
        [
            ("Thorin", "I kneel at the broken altar and pray to Krugon for an hour."),
            ("Dorn", "We bar the door and make camp here for the night."),
        ],
        setup={"Dorn": {"hp": 14}, "Thorin": {"hp": 9}},
        goals={"Thorin": "Pray to Krugon for an hour to renew your spells, then rest the night.",
               "Dorn": "Make the chapel safe and get a night's sleep so you both heal."},
    ),
    "journey": Scenario(
        "journey",
        "Low fantasy borderlands. The town of Fenwick lies three days east through the Greyfen marsh.",
        ("The edge of the Greyfen", "Reed beds and black water stretch east under low cloud. No road."),
        [
            ("Dorn", "We set out for Fenwick through the marsh. I'll scout ahead; Thorin keeps track "
                     "of our supplies."),
            ("Dorn", "Whatever comes at us out there, I put myself between it and Thorin."),
            ("Thorin", "We press on toward Fenwick as fast as we can."),
        ],
        goals={"Dorn": "Reach Fenwick through the marsh. You scout ahead.",
               "Thorin": "Reach Fenwick with supplies to spare. You keep track of rations."},
    ),
    "town": Scenario(
        "town",
        "Low fantasy borderlands. Fenwick is a walled market town, the only one for days.",
        ("Fenwick, market square", "Busy square at midday: stalls, a temple of the Dawn Mother, the "
                                   "Drowned Rat inn on the corner."),
        [
            ("Thorin", "I buy a healing potion from the apothecary's stall."),
            ("Thorin", "Then I go looking for something rarer — a holy relic, blessed by a saint."),
            ("Dorn", "We earned it. I throw a big party at the Drowned Rat and spend 200 coins on it."),
            ("Thorin", "We take rooms at the Drowned Rat and rest properly for three days."),
        ],
        setup={"Dorn": {"coins": 300, "hp": 12, "debilities": ["weak"]}, "Thorin": {"coins": 120}},
        goals={"Thorin": "Buy a healing potion, then try to find a rare holy relic for sale.",
               "Dorn": "Throw a big party at the Drowned Rat inn (spend 200 coins), then rest "
                       "there for a few days until you are healed."},
    ),
}


# -- the table ---------------------------------------------------------------------------


@dataclass
class Report:
    lines: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    tool_calls: Dict[str, int] = field(default_factory=dict)
    forwards_granted: int = 0
    forwards_used: int = 0

    def say(self, text: str) -> None:
        self.lines.append(text)

    def flag(self, text: str) -> None:
        self.flags.append(text)
        self.lines.append(f"> ⚠️ {text}")


class Transport:
    """Stands in for Discord: what Gary sends is written down, and prompts are kept for the
    players to click."""

    def __init__(self, report: Report) -> None:
        self.report = report
        self.prompts: List[Any] = []
        self.heard: List[str] = []  # everything Gary has said, for the players to read

    async def send(self, channel_id: int, text: str, prompt: Optional[object] = None) -> None:
        if text == STALL_MESSAGE:
            self.report.flag("Gary stalled (the model failed or said nothing).")
        self.heard.append(text)
        if text.startswith("➕ "):
            self.report.forwards_granted += 1
        self.report.say(f"**Gary:** {text}")
        if prompt is not None:
            self.prompts.append(prompt)
            self.report.say(f"  `[{type(prompt).__name__}]`")


class RecordingNarrator(GeminiNarrator):
    """The real narrator, with every tool call and its answer written down."""

    report: Report

    def _dispatch(self, channel_id, calls, effects):
        deferred, responses = super()._dispatch(channel_id, calls, effects)
        # In order per tool: two calls to one tool in a turn each get their own answer.
        answers: Dict[str, List[Any]] = {}
        for r in responses:
            if getattr(r, "function_response", None):
                answers.setdefault(r.function_response.name, []).append(r.function_response.response)
        for call in calls:
            args = dict(call.args or {})
            self.report.tool_calls[call.name] = self.report.tool_calls.get(call.name, 0) + 1
            if call.name == "make_move" and args.get("forward"):
                self.report.forwards_used += 1
            queue = answers.get(call.name) or []
            answer = queue.pop(0) if queue else None
            shown = json.dumps(args, ensure_ascii=False)
            self.report.say(f"  🔧 `{call.name}` {shown}" + (f" → `{json.dumps(answer, ensure_ascii=False)[:300]}`"
                                                           if answer is not None else " (deferred)"))
            if isinstance(answer, dict) and answer.get("ok") is False:
                why = answer.get("error") or "; ".join(answer.get("refused") or ()) or "no reason given"
                self.report.flag(f"`{call.name}` refused: {why}")
        return deferred, responses

    async def narrate(self, *, channel_id, kind, payload):
        if kind == "system_note":
            self.report.flag(f"move refused back to the model: {payload.get('error')}")
        return await super().narrate(channel_id=channel_id, kind=kind, payload=payload)


class Table:
    def __init__(self, seed: int, report: Report, client: Any = None) -> None:
        self.report = report
        if client is None:
            from google import genai

            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.rng = random.Random(seed)
        self.store = CampaignStore(backend=InMemoryStateBackend(), clock=time.time)
        self.registry = PendingCheckRegistry(clock=time.time, ttl_seconds=3600)
        self.director = Director(
            registry=self.registry, clock=time.time, game_channels={CHANNEL},
            freeform_channels={CHANNEL}, dice=RandomDice(random.Random(seed)),
            sheet_for=lambda a: (self.store.state().get("sheets") or {}).get(a),
            monsters=lambda: self.store.state().get("monsters") or {},
            steading=lambda: here(self.store.state()),
        )
        self.narrator = RecordingNarrator(
            client=client, director=self.director,
            store=self.store, history_backend=InMemoryHistoryBackend(), summarize=False,
            model=os.environ.get("GARY_GEMINI_MODEL") or "gemini-3.8-flash",
        )
        self.narrator.report = report
        self.transport = Transport(report)
        self.runner = EffectRunner(self.transport, self.narrator, store=self.store)
        self.users: Dict[str, int] = {}

    def seat(self, user_id: int, sheet: dict) -> None:
        self.store.apply([Mutation(MutationKind.SHEET, sheet["name"], {"sheet": sheet}),
                          Mutation(MutationKind.PLAYER_CHARACTER, str(user_id), {"actor": sheet["name"]})],
                         reason=f"playtest seats {sheet['name']}")
        self.director.set_player_character(user_id, sheet["name"])
        self.users[sheet["name"]] = user_id

    def prepare(self, scenario: Scenario) -> None:
        sheets = self.store.state().get("sheets") or {}
        for actor, changes in scenario.setup.items():
            self.store.apply([Mutation(MutationKind.SHEET, actor, {"sheet": {**sheets[actor], **changes}})],
                             reason=f"playtest sets up {actor}")

    def set_stage(self, scenario: Scenario) -> None:
        """A fresh scene: the creatures of the last one are gone, and nobody is mid-move."""
        for check in list(self.registry.open_checks(CHANNEL)):
            self.registry.consume(check.check_id)
        self.store.apply([Mutation(MutationKind.MONSTERS_REMOVE, "", {}),
                          Mutation(MutationKind.SETTING, "", {"text": scenario.setting}),
                          Mutation(MutationKind.SCENE, "", {"location": scenario.scene[0],
                                                            "summary": scenario.scene[1]})],
                         reason="playtest stage")

    async def play(self, actor: str, text: str) -> None:
        self.report.say(f"\n**{actor} (player):** {text}")
        effects = self.director.on_player_message(
            channel_id=CHANNEL, user_id=self.users[actor], display_name=f"{actor}'s player",
            content=text, mentions_bot=True,
        )
        await self.runner.run(effects)
        await self.click_everything()

    async def click_everything(self) -> None:
        clicks = 0
        while self.transport.prompts:
            clicks += 1
            if clicks > MAX_CLICKS_PER_LINE:
                self.report.flag(f"more than {MAX_CLICKS_PER_LINE} buttons from one line; stopping.")
                self.transport.prompts.clear()
                return
            prompt = self.transport.prompts.pop(0)
            effects = self.click(prompt)
            if effects:
                await self.runner.run(effects)

    def _owner(self, check_id: str) -> Optional[int]:
        return next((c.user_id for c in self.registry.open_checks(CHANNEL) if c.check_id == check_id), None)

    def click(self, prompt: Any) -> list:
        d = self.director
        if isinstance(prompt, RollPrompt):
            user = self._owner(prompt.check_id)
            self.report.say("  🖱️ *Roll*")
            return d.roll(channel_id=CHANNEL, user_id=user, check_id=prompt.check_id) if user else []
        if isinstance(prompt, ChoicePrompt):
            user = self._owner(prompt.check_id)
            picks = sorted(self.rng.sample(range(len(prompt.options)), prompt.count))
            self.report.say(f"  🖱️ *chooses* {[prompt.options[p] for p in picks]}")
            return d.choose(channel_id=CHANNEL, user_id=user, check_id=prompt.check_id, picks=picks) if user else []
        if isinstance(prompt, DamagePrompt):
            user = self._owner(prompt.check_id)
            self.report.say("  🖱️ *Roll damage*")
            return d.roll_damage(channel_id=CHANNEL, user_id=user, check_id=prompt.check_id) if user else []
        if isinstance(prompt, OverloadPrompt):
            user = self._owner(prompt.check_id)
            index, name = prompt.items[0]
            self.report.say(f"  🖱️ *Drop {name}*")
            return d.drop_and_roll(channel_id=CHANNEL, user_id=user, check_id=prompt.check_id, index=index)
        if isinstance(prompt, CurePrompt):
            debility = prompt.debilities[0]
            self.report.say(f"  🖱️ *Shed {debility}*")
            return d.cure(channel_id=CHANNEL, user_id=prompt.user_id, debility=debility)
        if isinstance(prompt, SpellsPrompt):
            sheet = (self.store.state().get("sheets") or {}).get(prompt.actor)
            prep = spells.Preparation(sheet)
            while prep.step() is not None:
                prep.choose(_spell_picks(prep))
            changes = prep.changes()
            self.store.apply([Mutation(MutationKind.SHEET, prompt.actor, {"sheet": {**sheet, **changes}})],
                             reason="playtest chooses spells")
            self.report.say(f"  🖱️ *Choose spells* → {changes['spells']}")
            return []
        if isinstance(prompt, ReactPrompt):
            # Optional: a table may always take the blow. The harness does, and says so.
            self.report.say("  🛡️ *Reduce damage offered* — taken as it fell")
            return []
        self.report.flag(f"no way to click a {type(prompt).__name__}")
        return []


# -- players played by a model -----------------------------------------------------------

PLAYER_MODEL = "gemini-3.5-flash-lite"

#: The most turns one --llm-players scene runs: enough for a fight and its aftermath.
MAX_LLM_TURNS = 14

_PLAYER_BRIEF = """\
You are a player at a Dungeon World table, playing {name}: {sheet}.
Your goal in this scene: {goal}

Each turn you are shown what the Game Master just said. Answer as {name}'s player, in one
or two sentences: what {name} does or says now. Stay in character and in the fiction; react
to what is happening, especially to any threat aimed at you. Do not narrate outcomes, roll
dice or name game moves — just say what you try. Pursue your goal, but deal with danger
first. When your goal is done and nothing threatens you, answer exactly: DONE"""


class Players:
    """The characters, each played by a model with a goal."""

    def __init__(self, client: Any, model: str = PLAYER_MODEL) -> None:
        self.client = client
        self.model = model

    async def line(self, name: str, sheet_line: str, goal: str, heard: Sequence[str]) -> str:
        from google.genai import types

        brief = _PLAYER_BRIEF.format(name=name, sheet=sheet_line, goal=goal)
        recent = "\n\n".join(heard[-6:]) or "(The scene opens.)"
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=[types.Content(role="user", parts=[types.Part.from_text(
                text=f"The Game Master:\n{recent}\n\nWhat does {name} do?")])],
            config=types.GenerateContentConfig(system_instruction=brief),
        )
        parts = response.candidates[0].content.parts if response.candidates else []
        text = " ".join(p.text for p in parts if getattr(p, "text", None)).strip()
        return " ".join(text.split())[:400]


def _addressed(heard: Sequence[str], names: Sequence[str], last: Optional[str]) -> str:
    """Who Gary turned to last: the character named latest in what was just said, or the
    other one if nobody was."""
    for text in reversed(heard[-3:]):
        found = [(text.rfind(n), n) for n in names if n in text]
        if found:
            return max(found)[1]
    others = [n for n in names if n != last]
    return others[0] if others else names[0]


async def play_with_players(table: "Table", scenario: Scenario, players: Players) -> None:
    from gary.sheets import describe

    names = list(scenario.goals)
    done: set = set()
    last: Optional[str] = None
    opening = f"{scenario.scene[0]}. {scenario.scene[1]}"
    table.transport.heard.append(opening)
    for _ in range(MAX_LLM_TURNS):
        live = [n for n in names if n not in done]
        if not live:
            return
        actor = _addressed(table.transport.heard, live, last)
        sheet = (table.store.state().get("sheets") or {}).get(actor) or {}
        # The whole character, so the player prays to the right god and casts what they hold.
        known = " · ".join(line for line in (describe(sheet, with_name=False), choices.describe(sheet),
                                              spells.describe(sheet)) if line)
        try:
            text = await players.line(actor, known, scenario.goals[actor],
                                      table.transport.heard)
        except Exception:  # noqa: BLE001
            table.report.flag(f"the player model failed for {actor}:\n```\n{traceback.format_exc()}```")
            return
        if not text:
            continue
        # "DONE" alone ends their scene; tacked onto a line, the line is played first.
        finished = text.rstrip(" .!").upper().endswith("DONE")
        text = text.rstrip(" .!")[:-4].rstrip() if finished else text
        if text:
            last = actor
            try:
                await table.play(actor, text)
            except Exception:  # noqa: BLE001
                table.report.flag(f"exception on {actor}'s line {text!r}:\n```\n{traceback.format_exc()}```")
        if finished:
            done.add(actor)
            table.report.say(f"\n*{actor}'s player is done.*")
    table.report.say(f"\n*Turn limit ({MAX_LLM_TURNS}) reached.*")


# -- running -----------------------------------------------------------------------------


async def run(names: Sequence[str], seed: int, client: Any = None,
              scenarios: Optional[Dict[str, Scenario]] = None, players: Optional[Players] = None) -> Report:
    scenarios = scenarios or SCENARIOS
    report = Report()
    table = Table(seed, report, client)
    for user_id, sheet in party():
        table.seat(user_id, sheet)
    for name in names:
        scenario = scenarios[name]
        report.say(f"\n## Scenario: {name}\n")
        table.set_stage(scenario)
        table.prepare(scenario)
        if players is not None:
            await play_with_players(table, scenario, players)
            continue
        for actor, text in scenario.lines:
            try:
                await table.play(actor, text)
            except Exception:  # noqa: BLE001 - a crash is a finding, not the end of the run
                report.flag(f"exception on {actor}'s line {text!r}:\n```\n{traceback.format_exc()}```")
    if report.forwards_granted and not report.forwards_used:
        report.flag(f"{report.forwards_granted} forward(s) granted and none used: did the model "
                    "ever set forward=true when acting on one?")
    stats = table.runner.stats
    if stats.depth_exceeded:
        report.flag(f"effect depth exceeded {stats.depth_exceeded} time(s): the model looped.")
    report.say("\n## Final sheets\n")
    from gary.sheets import describe

    for sheet in (table.store.state().get("sheets") or {}).values():
        report.say(f"- {describe(sheet)}")
    return report


def render(report: Report, names: Sequence[str], seed: int) -> str:
    head = [f"# Gary playtest — {', '.join(names)} (seed {seed})", ""]
    head.append(f"**Flags: {len(report.flags)}**")
    head += [f"- {f.splitlines()[0]}" for f in report.flags] or ["- none"]
    head.append("")
    head.append(f"**Forwards:** {report.forwards_granted} granted, {report.forwards_used} used")
    head.append("**Tool calls:** " + (", ".join(f"{k} ×{v}" for k, v in sorted(report.tool_calls.items()))
                                      or "none"))
    return "\n".join(head + report.lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=[*SCENARIOS, "all"])
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, default=None, help="Transcript path (default: stdout).")
    parser.add_argument("--llm-players", action="store_true",
                        help="A model plays the characters toward their goals, instead of the script.")
    parser.add_argument("--player-model", default=PLAYER_MODEL)
    args = parser.parse_args()
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY is not set. `set -a && . ~/.config/gary/env && set +a` first.")
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    players = None
    if args.llm_players:
        from google import genai

        players = Players(genai.Client(api_key=os.environ["GEMINI_API_KEY"]), args.player_model)
    report = asyncio.run(run(names, args.seed, players=players))
    text = render(report, names, args.seed)
    if args.out:
        args.out.write_text(text)
        print(f"{len(report.flags)} flag(s); transcript at {args.out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
