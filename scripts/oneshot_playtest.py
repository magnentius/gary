"""A whole one-shot, end to end, through the bot's own /begin flow -- with live Gemini as GM and
two characters played by a model.

    PYTHONPATH=src .venv/bin/python scripts/oneshot_playtest.py --out oneshot.md

It drives GaryClient's real opening code (the mode, the premise, questions, bonds, three scenes,
the level-up break, the epilogue and /end) through stand-in Discord interactions, on the scene
playtest's table: the same store, Director, recording narrator, dice buttons and model players.
Nothing reaches Discord. It costs model calls: a full run is a few hundred.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent))

import playtest  # noqa: E402  (the scene harness: Table, Report, Players, party, Scenario)
from gary.adapters.discord_bot import GaryClient, OpeningPrompt, PremisePrompt  # noqa: E402
from gary.levelling import Advance, move_options  # noqa: E402
from gary.models import Mutation, MutationKind  # noqa: E402
from gary.sheets import xp_needed  # noqa: E402

CHANNEL = playtest.CHANNEL
STARTER = 1001  # Thorin's player runs /begin
CAST = ("Thorin", "Dorn")
#: Player turns per stretch of play: the questions, each scene, the epilogue.
TURNS = {"questions": 4, "scene": 10, "epilogue": 2}


class _User:
    def __init__(self, uid: int, name: str) -> None:
        self.id, self.display_name, self.mention = uid, name, f"<@{uid}>"


class _Response:
    def __init__(self, report: "playtest.Report") -> None:
        self.report = report

    async def send_message(self, text: str = "", **_: Any) -> None:
        self.report.say(f"  *(private: {text})*")

    async def edit_message(self, **_: Any) -> None:
        return None

    async def defer(self, **_: Any) -> None:
        return None


class _Interaction:
    def __init__(self, user: _User, report: "playtest.Report") -> None:
        self.user, self.channel_id, self.response = user, CHANNEL, _Response(report)


class _Msg:
    def __init__(self, user: _User) -> None:
        self.author = user


class _Settings:
    command_prefix = "?dm"


def bot(table: "playtest.Table") -> Any:
    """GaryClient's opening methods on the playtest table: the real flow, no Discord."""
    class _Client:
        _has_model = True
        MAX_SETTING_CHARS = GaryClient.MAX_SETTING_CHARS
        settings = _Settings()

    for name in ("_opening", "_set_opening", "_party_sheets", "may_run_opening", "_begin", "offer_premise",
                 "on_premise", "on_opening_step", "_oneshot_step", "_end_session", "_end_session_then_fronts"):
        setattr(_Client, name, getattr(GaryClient, name))
    client = _Client()
    client.store, client.director, client.runner, client.sender = (
        table.store, table.director, table.runner, table.transport)
    return client


def take(table: "playtest.Table", kind: type) -> Optional[Any]:
    """The newest prompt of this kind the bot posted, taken off the pile."""
    for i in range(len(table.transport.prompts) - 1, -1, -1):
        if isinstance(table.transport.prompts[i], kind):
            return table.transport.prompts.pop(i)
    return None


async def play(table: "playtest.Table", players: "playtest.Players", stretch: str, goals: dict) -> None:
    """A stretch of play: the model players answer Gary, turn about, through the scene harness."""
    scene = table.store.state().get("scene") or {}
    playtest.MAX_LLM_TURNS = TURNS[stretch]
    await playtest.play_with_players(table, playtest.Scenario(
        stretch, table.store.state().get("setting") or "",
        (scene.get("location") or "here", scene.get("summary") or ""), [], goals=goals), players)


def level_everyone(table: "playtest.Table") -> None:
    """The break: each presses Level up -- a stat and an advanced move, the first offered."""
    sheets = table.store.state().get("sheets") or {}
    for name in CAST:
        sheet = sheets[name]
        if int(sheet.get("xp", 0)) < xp_needed(sheet):
            continue
        a = Advance(sheet)
        while a.step() is not None:
            step = a.step()
            if step == "stat":
                a.choose_stat(a.stat_options()[0])
            elif step == "move":
                a.choose_move(move_options(sheet, a.level)[0]["name"])
            elif step == "spell":
                a.choose_spell(a.spell_options()[0].name)
            elif step == "move_spell":
                a.choose_move_spell(a.move_spell_options()[0].name)
            elif step == "move_choice":
                a.choose_move_choice(a.move_choice_options()[0])
            else:
                break
        if a.step() is None:
            new = a.result()
            table.store.apply([Mutation(MutationKind.SHEET, name, {"sheet": new})], reason="playtest levels up")
            table.report.say(f"  🖱️ *{name} levels up* → level {new['level']}, "
                             f"{(new.get('advanced_moves') or ['?'])[-1]}")


async def run(seed: int, out: Path) -> None:
    from google import genai

    report = playtest.Report()
    table = playtest.Table(seed, report)
    client = bot(table)
    players = playtest.Players(genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                                            http_options=genai.types.HttpOptions(timeout=90_000)))
    users = {}
    for uid, sheet in playtest.party():
        if sheet["name"] in CAST:
            table.seat(uid, sheet)
            users[sheet["name"]] = _User(uid, f"{sheet['name']}'s player")
    starter = next(u for u in users.values() if u.id == STARTER)
    act = _Interaction(starter, report)

    async def step(name: str) -> None:
        report.say(f"\n### ▶ {name}\n")
        await client.on_opening_step(act, name)
        await table.click_everything_but_opening()

    try:
        report.say("\n## /begin\n")
        await client._begin(_Msg(starter), CHANNEL)
        assert take(table, OpeningPrompt), "no mode buttons"
        await step("oneshot")
        offer = take(table, PremisePrompt)
        if offer is None:
            raise RuntimeError("no premise was offered")
        report.say(f"  🖱️ *premise*: {offer.activities[0]} — at {offer.places[0]}")
        await client.on_premise(act, offer.places[0], offer.activities[0])
        take(table, OpeningPrompt)

        await step("questions")
        take(table, OpeningPrompt)
        await play(table, players, "questions", {
            n: "Answer the Game Master's questions about your character and the premise, vividly and in "
               "character. When you've answered, say DONE." for n in CAST})
        await step("bonds")
        take(table, OpeningPrompt)
        await step("playing")
        premise = table.store.state().get("setting") or ""
        goal = f"Play the one-shot to its end: {premise}. Work with your companion; deal with danger first."
        for n in (1, 2, 3):
            report.say(f"\n## Scene {n}\n")
            take(table, OpeningPrompt)
            await play(table, players, "scene", {c: goal for c in CAST})
            if n == 1:
                await step("scene")
            elif n == 2:
                take(table, OpeningPrompt)
                await step("break")
                level_everyone(table)
                take(table, OpeningPrompt)
                await step("scene")
        take(table, OpeningPrompt)
        await step("epilogue")
        await play(table, players, "epilogue", {
            c: "Tell how your character's story ends, in a sentence or two, then say DONE." for c in CAST})
        report.say("\n## /end\n")
        line = await client._end_session_then_fronts(CHANNEL, 2, frozenset(), {})
        report.say(f"**Gary:** {line}")
        await asyncio.sleep(0.5)  # the chronicle is posted by a task
        stage = (table.store.state().get("opening") or {}).get("stage")
        if stage != "finished":
            report.flag(f"the one-shot ended at stage {stage!r}, not 'finished'")
    except Exception:  # noqa: BLE001 - a crash is a finding
        report.flag(f"the one-shot crashed:\n```\n{traceback.format_exc()}```")
    out.write_text(playtest.render(report, ["one-shot"], seed).replace("# Gary playtest — one-shot",
                                                                         "# Gary playtest — a whole one-shot", 1))
    print(f"{len(report.flags)} flag(s); transcript at {out}")


def _click_everything_but_opening(self: "playtest.Table"):
    """Click the dice and choice buttons; leave the opening's own buttons for the driver."""
    async def go() -> None:
        keep = [p for p in self.transport.prompts if isinstance(p, (OpeningPrompt, PremisePrompt))]
        self.transport.prompts[:] = [p for p in self.transport.prompts if p not in keep]
        await self.click_everything()
        self.transport.prompts[:0] = keep
    return go()


playtest.Table.click_everything_but_opening = _click_everything_but_opening


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=Path("oneshot.md"))
    args = parser.parse_args()
    asyncio.run(run(args.seed, args.out))


if __name__ == "__main__":
    main()
