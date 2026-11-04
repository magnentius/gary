"""A campaign across two sessions, end to end, through the bot's own /begin and /end flow -- with
live Gemini as GM and three characters played by a model.

    PYTHONPATH=src .venv/bin/python scripts/campaign_playtest.py --out campaign.md

What a one-shot never reaches: the campaign opening (introductions, questions, bonds), the fronts
written after the first session, the next session's recap with everything carried over, and the
between-session work after it -- fronts advanced or ended, steadings updated by their tags. Between
the stretches of play it checks what should hold and flags what doesn't. Nothing reaches Discord. It
costs model calls: a run is a few hundred.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent))

import oneshot_playtest as oneshot  # noqa: E402  (the stand-in client and interactions)
import playtest  # noqa: E402  (the scene harness: Table, Report, Players, party, Scenario)
from gary import fronts as front_rules  # noqa: E402
from gary import steadings as steading_rules  # noqa: E402
from gary.adapters.discord_bot import GaryClient, OpeningPrompt, PremisePrompt  # noqa: E402
from gary.worldmap import render as render_world  # noqa: E402

CHANNEL = playtest.CHANNEL
STARTER = 1002  # Dorn's player runs /begin
CAST = ("Dorn", "Nim", "Thorin")
#: Player turns per stretch of play.
TURNS = {"questions": 6, "session 1": 16, "session 2": 16}


def bot(table: "playtest.Table") -> Any:
    """The one-shot harness's stand-in client, with the campaign's between-session methods too."""
    client = oneshot.bot(table)
    for name in ("_next_session", "_update_steadings", "_front_work", "_campaign_choice"):
        setattr(type(client), name, getattr(GaryClient, name))
    return client


async def play(table: "playtest.Table", players: "playtest.Players", stretch: str, goals: Dict[str, str]) -> None:
    scene = table.store.state().get("scene") or {}
    playtest.MAX_LLM_TURNS = TURNS[stretch]
    await playtest.play_with_players(table, playtest.Scenario(
        stretch, table.store.state().get("setting") or "",
        (scene.get("location") or "here", scene.get("summary") or ""), [], goals=goals), players)


async def end_session(client: Any, report: "playtest.Report", yeses: int) -> None:
    """/end, and the tasks it starts -- the chronicle, the front work -- run to the end. Only those:
    a Gemini call abandoned by the narrator's timeout is left to die on its own and may never finish,
    so waiting on every task hangs the run (it did, for an hour)."""
    before = asyncio.all_tasks()
    line = await client._end_session_then_fronts(CHANNEL, yeses, frozenset(), {})
    report.say(f"**Gary:** {line}")
    started = asyncio.all_tasks() - before - {asyncio.current_task()}
    if started:
        _, late = await asyncio.wait(started, timeout=600)
        if late:
            report.flag(f"{len(late)} task(s) started by /end still running after 10 minutes")


def fronts_line(state: Dict[str, Any]) -> Dict[str, tuple]:
    """Each danger's name -> (portents reached, ended), to compare across the break."""
    return {d["name"]: (d.get("reached", 0), d.get("ended", ""))
            for f in (state.get("fronts") or {}).values() for d in f.get("dangers") or ()}


def snapshot(table: "playtest.Table") -> Dict[str, tuple]:
    return {n: (s.get("hp"), s.get("xp"), s.get("level"), len(s.get("gear") or ()))
            for n, s in (table.store.state().get("sheets") or {}).items()}


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
            users[sheet["name"]] = oneshot._User(uid, f"{sheet['name']}'s player")
    starter = next(u for u in users.values() if u.id == STARTER)
    act = oneshot._Interaction(starter, report)

    async def step(name: str) -> None:
        report.say(f"\n### ▶ {name}\n")
        await client.on_opening_step(act, name)
        await table.click_everything_but_opening()

    def save() -> None:
        """The transcript so far: a run that dies or hangs still leaves what it played."""
        out.write_text(playtest.render(report, ["campaign"], seed).replace(
            "# Gary playtest — campaign", "# Gary playtest — a campaign, two sessions", 1))

    def stage() -> str:
        return str((table.store.state().get("opening") or {}).get("stage") or "")

    try:
        # -- session 1: the campaign's opening ------------------------------------------------
        report.say("\n## Session 1: /begin\n")
        await client._begin(oneshot._Msg(starter), CHANNEL)
        assert oneshot.take(table, OpeningPrompt), "no mode buttons"
        await step("campaign")
        offer = oneshot.take(table, PremisePrompt)
        if offer is None:
            raise RuntimeError("no premise was offered")
        report.say(f"  🖱️ *premise*: {offer.activities[0]} — at {offer.places[0]}")
        await client.on_premise(act, offer.places[0], offer.activities[0])
        oneshot.take(table, OpeningPrompt)
        await step("introductions")
        oneshot.take(table, OpeningPrompt)
        await step("questions")
        oneshot.take(table, OpeningPrompt)
        await play(table, players, "questions", {
            n: "Answer the Game Master's questions about your character and the premise, vividly and in "
               "character. When you've answered, say DONE." for n in CAST})
        await step("bonds")
        oneshot.take(table, OpeningPrompt)
        await step("playing")
        if stage() != "playing":
            report.flag(f"after the opening the stage is {stage()!r}, not 'playing'")
        premise = (table.store.state().get("opening") or {}).get("premise") or ""
        await play(table, players, "session 1", {
            "Dorn": f"Pursue the premise: {premise}. Ask the locals about any danger you hear of, and go after it.",
            "Nim": "Find out who profits from the trouble here, and pocket something valuable on the way.",
            "Thorin": "Help the townsfolk, heal anyone hurt, and learn what threatens the place."})

        save()
        # -- the end of session 1: XP, then the fronts -------------------------------------------
        report.say("\n## /end (session 1)\n")
        xp_before = {n: v[1] for n, v in snapshot(table).items()}
        await end_session(client, report, 2)
        state = table.store.state()
        if stage() != "done":
            report.flag(f"after the first /end the stage is {stage()!r}, not 'done'")
        if any(snapshot(table)[n][1] <= xp_before[n] for n in CAST if n in xp_before):
            report.flag("not everyone gained XP at the first /end")
        scopes = [f.get("scope") for f in (state.get("fronts") or {}).values()]
        if "campaign" not in scopes:
            report.flag(f"no campaign front written after session 1 (fronts: {scopes})")
        if "adventure" not in scopes:
            report.flag(f"no adventure front written after session 1 (fronts: {scopes})")
        if not ((state.get("world") or {}).get("signs")):
            report.flag("no signs of the fronts on the campaign map after session 1")
        report.say("\n### Fronts after session 1\n\n" + "\n".join(front_rules.render(state.get("fronts") or {})))

        save()
        # -- session 2: the recap, everything carried over ----------------------------------------
        report.say("\n## Session 2: /begin\n")
        carried = snapshot(table)
        portents = fronts_line(table.store.state())
        await client._begin(oneshot._Msg(starter), CHANNEL)
        await table.click_everything_but_opening()
        opened = table.store.state().get("opening") or {}
        if (opened.get("stage"), int(opened.get("session") or 0)) != ("session", 2):
            report.flag(f"session 2 didn't start: stage {opened.get('stage')!r}, session {opened.get('session')!r}")
        for name, (hp, xp, level, _) in carried.items():
            now = snapshot(table).get(name)
            if now and (now[0], now[2]) != (hp, level):
                report.flag(f"{name} changed between sessions without play: HP {hp}→{now[0]}, level {level}→{now[2]}")
        await play(table, players, "session 2", {
            "Dorn": "Pick up where you left off: deal with the danger you learned of last session.",
            "Nim": "Follow the money you heard about last session, and don't get caught.",
            "Thorin": "Keep everyone alive, and make sure the town learns what you found."})

        save()
        # -- the end of session 2: the between-session review ------------------------------------
        report.say("\n## /end (session 2)\n")
        towns = {k: steading_rules.describe(v) for k, v in (table.store.state().get("steadings") or {}).items()}
        await end_session(client, report, 1)
        state = table.store.state()
        if stage() != "done":
            report.flag(f"after the second /end the stage is {stage()!r}, not 'done'")
        if not towns:
            report.flag("no steading was ever created, so the between-session steading update had nothing to do")
        after = fronts_line(state)
        moved = {n: (portents.get(n), v) for n, v in after.items() if portents.get(n) != v}
        report.say("\n### Fronts after session 2\n\n" + "\n".join(front_rules.render(state.get("fronts") or {})))
        report.say("\n**Dangers changed between the sessions' ends:** "
                   + ("; ".join(f"{n}: {a} → {b}" for n, (a, b) in moved.items()) or "none"))
        report.say("\n### Steadings, before and after the second /end\n\n" + ("\n".join(
            f"- before: {towns.get(k, '(new)')}\n  after: {steading_rules.describe(v)}"
            for k, v in (state.get("steadings") or {}).items()) or "none"))
        report.say("\n### Campaign map, as the GM sees it\n\n" + (render_world(state.get("world")) or "none"))
    except Exception:  # noqa: BLE001 - a crash is a finding
        report.flag(f"the campaign crashed:\n```\n{traceback.format_exc()}```")
    save()
    print(f"{len(report.flags)} flag(s); transcript at {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", type=Path, default=Path("campaign.md"))
    args = parser.parse_args()
    asyncio.run(run(args.seed, args.out))


if __name__ == "__main__":
    main()
