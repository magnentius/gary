"""/begin: the first session, run by Gary step by step (opening.py)."""

from __future__ import annotations

import asyncio

import discord
import pytest

from gary import opening
from gary.adapters.discord_bot import GaryClient, OpeningPrompt, PremisePrompt
from gary.fsm import PromptModel
from gary.models import Mutation, MutationKind
from gary.state.backend import InMemoryStateBackend
from gary.state.store import CampaignStore

OFFER = """Here you go:
PREMISE: a lighthouse on a tidal rock | relight the lamp before the smugglers' ship runs aground
- PREMISE: The drowned bell-towers of Old Marrow | ring the bells that call the drowned to rest
**PREMISE**: a glass forest where every tree remembers | find which tree saw the duke's murder
PREMISE: the moving market of the salt flats | win back a stolen stall in the traders' court
PREMISE: one too many | it should be dropped
PREMISE: a place with no activity
PLACE: an old-format line | ignored"""


def test_the_offer_is_read_however_the_model_dresses_it_with_each_activity_beside_its_place():
    places, activities = opening.parse_offer(OFFER)
    assert places == ["a lighthouse on a tidal rock", "The drowned bell-towers of Old Marrow",
                      "a glass forest where every tree remembers", "the moving market of the salt flats"]
    assert activities[0] == "relight the lamp before the smugglers' ship runs aground"
    assert activities[2] == "find which tree saw the duke's murder"
    assert opening.parse_offer("I'd rather not.") == ([], [])


def test_the_prompt_asks_for_a_place_and_an_activity_and_names_the_rejects():
    text = opening.premise_prompt(["a glass forest"])
    assert "a location and a grabby activity" in text and "Call no tools" in text
    assert "PREMISE: <a fantastic place> | <a grabby activity there>" in text and "PLACE:" not in text
    assert "- a glass forest" in text
    assert "passed over" not in opening.premise_prompt()


def test_the_premise_reads_as_the_adventure():
    assert opening.premise("the drowned bell-towers.", "Steal the Choir's Bell") == \
        "The adventure: Steal the Choir's Bell -- at the drowned bell-towers."


def test_the_opening_is_kept_and_undone_a_step_at_a_time(clock):
    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "premise", "starter": 11})], reason="t")
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "characters"})], reason="t")
    assert store.state()["opening"] == {"stage": "characters", "starter": 11}
    store.undo_last()
    assert store.state()["opening"] == {"stage": "premise", "starter": 11}
    store.undo_last()
    assert store.state()["opening"] == {}


# -- the steps, through the client --------------------------------------------------------


class _Response:
    def __init__(self):
        self.private, self.edited, self.deferred = [], False, False

    async def send_message(self, text, ephemeral=False, **kw):
        self.private.append(text)

    async def edit_message(self, **kw):
        self.edited = True

    async def defer(self):
        self.deferred = True


class _User:
    def __init__(self, uid, name="John"):
        self.id, self.display_name, self.mention = uid, name, f"<@{uid}>"


class _Interaction:
    def __init__(self, uid=11):
        self.user, self.channel_id, self.response = _User(uid), 7, _Response()


@pytest.fixture
def table(clock):
    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.SHEET, "Gorm", {"sheet": {
        "name": "Gorm", "class": "Barbarian", "race": "Human", "hp": 23, "max_hp": 23}})], reason="setup")
    sent, ran = [], []

    class _Sender:
        async def send(self, channel_id, text, prompt=None):
            sent.append((text, prompt))

    class _Runner:
        async def run(self, effects):
            ran.extend(effects)

        async def ask(self, effect):
            return OFFER

    class _Director:
        players = {11: "Gorm"}

    class _Client:
        sender, runner, director = _Sender(), _Runner(), _Director()
        _has_model = True
        MAX_SETTING_CHARS = GaryClient.MAX_SETTING_CHARS

    for name in ("_opening", "_set_opening", "_party_sheets", "may_run_opening", "_begin", "offer_premise",
                 "on_premise", "on_opening_step", "_end_session_then_fronts", "_front_work", "_next_session",
                 "_campaign_choice", "_oneshot_step", "_update_steadings"):
        setattr(_Client, name, getattr(GaryClient, name))
    client = _Client()
    client.store = store
    return client, store, sent, ran


class _Msg:
    def __init__(self, uid=11):
        self.author = _User(uid)


def test_the_whole_opening_in_order(table):
    client, store, sent, ran = table
    asyncio.run(client._begin(_Msg(), 7))
    assert sent[-1][1] == OpeningPrompt(("campaign", "oneshot"))
    assert store.state()["opening"]["starter"] == 11
    asyncio.run(client.on_opening_step(_Interaction(), "campaign"))
    assert isinstance(sent[-1][1], PremisePrompt) and len(sent[-1][1].places) == 4

    # Someone else cannot choose for the table.
    other = _Interaction(uid=99)
    asyncio.run(client.on_opening_step(other, "introductions"))
    assert "running the opening" in other.response.private[0]

    asyncio.run(client.on_premise(_Interaction(), "the drowned bell-towers", "steal the Choir's Bell"))
    assert store.state()["setting"].startswith("The adventure: steal the Choir's Bell")
    assert sent[-1][1] == OpeningPrompt(("introductions",))

    asyncio.run(client.on_opening_step(_Interaction(), "introductions"))
    assert "**Gorm**, Human Barbarian" in sent[-1][0] and sent[-1][1] == OpeningPrompt(("questions",))

    asyncio.run(client.on_opening_step(_Interaction(), "questions"))
    asked = [e for e in ran if isinstance(e, PromptModel)][-1]
    assert asked.payload["event"] == "Opening: hook questions" and "Gorm" in asked.payload["characters"]
    assert sent[-1][1] == OpeningPrompt(("bonds",))

    asyncio.run(client.on_opening_step(_Interaction(), "bonds"))
    assert sent[-1][1] == OpeningPrompt(("mybonds", "playing"))

    store.apply([Mutation(MutationKind.SHEET, "Gorm", {"sheet": {
        **store.state()["sheets"]["Gorm"], "bonds": [{"line": "___ is puny and foolish, but amusing to me.",
                                                      "with": "Thorin"}]}})], reason="bonds")
    asyncio.run(client.on_opening_step(_Interaction(), "playing"))
    assert ran[-1].payload["event"] == "Opening: the first scene"
    assert ran[-1].payload["bonds"] == {"Gorm": ["Thorin is puny and foolish, but amusing to me."]}
    assert "Ask a question right away" in ran[-1].payload["instruction"]
    assert store.state()["opening"]["stage"] == "playing"

    # A step out of order is refused.
    late = _Interaction()
    asyncio.run(client.on_opening_step(late, "bonds"))
    assert "not that step" in late.response.private[0]

    # /begin again, mid-session, says so.
    asyncio.run(client._begin(_Msg(), 7))
    assert "Session 1 is under way" in sent[-1][0]


def test_the_first_end_of_session_writes_the_fronts_then_each_reviews_them(table):
    client, store, sent, ran = table

    async def ended(yeses, fulfilled, resolved):
        return "⭐ End of session"

    client._end_session = ended
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "playing", "starter": 11})], reason="t")
    store.apply([Mutation(MutationKind.NOTE, "the Choir", {"kind": "threat", "body": "wants the bell back"})],
                reason="t")

    async def go():
        line = await client._end_session_then_fronts(7, 1, set(), {})
        await asyncio.sleep(0)
        return line

    assert asyncio.run(go()) == "⭐ End of session"
    assert ran[-1].payload["event"] == "Opening: fronts after the first session"
    assert ran[-1].payload["threats_noted"] == ["the Choir: wants the bell back"]
    assert "scope=campaign" in ran[-1].payload["instruction"]
    assert store.state()["opening"]["stage"] == "done"
    assert store.state()["opening"]["front_work"] is False  # open only while the work runs
    ran.clear()
    asyncio.run(go())  # every later session: the between-session review
    assert ran[-1].payload["event"] == "Between sessions: front work"


def test_begin_mid_opening_shows_the_next_step(table):
    client, store, sent, ran = table
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "bonds", "starter": 11})], reason="t")
    asyncio.run(client._begin(_Msg(), 7))
    assert sent[-1][1] == OpeningPrompt(("mybonds", "playing"))


def test_later_sessions_begin_with_a_recap_the_outsiders_question_and_level_ups(table):
    client, store, sent, ran = table
    store.apply([Mutation(MutationKind.SHEET, "Gorm", {"sheet": {
        **store.state()["sheets"]["Gorm"], "level": 1, "xp": 8}})], reason="xp")
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "done", "starter": 11, "session": 1})], reason="t")
    asyncio.run(client._begin(_Msg(uid=42), 7))  # anyone may start a session
    assert store.state()["opening"]["stage"] == "session" and store.state()["opening"]["session"] == 2
    assert sent[-1][0].startswith("🎲 **Session 2.** Gorm: you can level up")
    event = ran[-1].payload
    assert event["event"] == "Session 2 begins" and "recap" in event["instruction"]
    assert "Gorm is an Outsider" in event["instruction"] and "mark_xp" in event["instruction"]

    async def ended(yeses, fulfilled, resolved):
        return "⭐ End of session"

    client._end_session = ended

    async def end():
        await client._end_session_then_fronts(7, 0, set(), {})
        await asyncio.sleep(0)

    asyncio.run(end())
    assert store.state()["opening"]["stage"] == "done"
    assert ran[-1].payload["event"] == "Between sessions: front work"


def _ender(client):
    async def ended(yeses, fulfilled, resolved):
        return "⭐ End of session"

    client._end_session = ended

    async def end():
        await client._end_session_then_fronts(7, 0, set(), {})
        await asyncio.sleep(0)

    return end


def test_when_the_campaign_front_is_over_the_table_carries_on_or_closes(table):
    client, store, sent, ran = table
    end = _ender(client)
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "done", "session": 4, "campaign_over": True})],
                reason="t")
    asyncio.run(client._begin(_Msg(), 7))
    assert "campaign front is over" in sent[-1][0] and sent[-1][1] == OpeningPrompt(("carryon", "finale"))

    # Carry on: the next session starts, and the review after it writes a new campaign front.
    asyncio.run(client.on_opening_step(_Interaction(uid=42), "carryon"))
    assert store.state()["opening"]["stage"] == "session" and store.state()["opening"]["session"] == 5
    asyncio.run(end())
    assert ran[-1].payload["new_campaign_front"] and "write a new campaign front" in ran[-1].payload["instruction"]
    assert not store.state()["opening"]["new_campaign_front"]

    # Or close it: an epilogue, then the chronicle, and the campaign is finished.
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "done", "campaign_over": True})], reason="t")
    asyncio.run(client.on_opening_step(_Interaction(uid=42), "finale"))
    assert store.state()["opening"]["stage"] == "epilogue"
    assert ran[-1].payload["event"] == "The campaign's last session: an epilogue" and "Gorm" in ran[-1].payload["characters"]
    asyncio.run(end())
    assert store.state()["opening"]["stage"] == "finished"
    assert sent[-1][0].startswith("📖 **The chronicle of this campaign**") and "**Gorm**" not in sent[-1][0]
    assert "• Gorm, Human Barbarian" in sent[-1][0]
    asyncio.run(client._begin(_Msg(), 7))
    assert "This campaign is finished" in sent[-1][0]


def test_the_chronicle_tells_what_became_of_everyone():
    state = {"setting": "The adventure: steal the bell -- at the drowned towers.", "opening": {"session": 6},
             "sheets": {"Kael": {"name": "Kael", "race": "Dwarf", "class": "Wizard", "level": 10, "retired": True,
                                 "former_classes": ["Fighter"]},
                        "Ash": {"name": "Ash", "race": "Elf", "class": "Thief", "level": 4, "dead": True}},
             "fronts": {"c": {"name": "The Choir", "scope": "campaign",
                              "dangers": [{"name": "The Bell", "ended": "rung, and the towers fell"}]}},
             "npcs": {"m": {"name": "Mara", "known": True}, "v": {"name": "Vex", "known": False}}}
    text = opening.chronicle(state)
    assert "6 sessions." in text
    assert "• Kael, Dwarf Wizard 10, once a Fighter — retired to safety" in text
    assert "• Ash, Elf Thief 4 — died" in text
    assert "• The Choir (campaign front) — The Bell: rung, and the towers fell" in text
    assert "People met:** Mara" in text and "Vex" not in text


def test_a_copy_is_kept_before_a_reset(tmp_path):
    root = tmp_path / "campaign"
    (root / "history").mkdir(parents=True)
    (root / "snapshot.json").write_text("{}")
    (root / ".gary.lock").write_text("1")

    class _Settings:
        campaign_root, backend = root, "file"

    client = type("C", (), {"settings": _Settings()})()
    kept = GaryClient._keep_a_copy(client)
    assert kept.parent == root / "kept" and (kept / "snapshot.json").exists() and (kept / "history").is_dir()
    assert not (kept / ".gary.lock").exists()
    again = GaryClient._keep_a_copy(client)
    assert not (again / "kept").exists()  # earlier copies are not copied into later ones
    _Settings.backend = "firestore"
    assert GaryClient._keep_a_copy(client) is None


def test_a_one_shot_runs_three_scenes_a_break_and_an_epilogue(table):
    client, store, sent, ran = table
    end = _ender(client)
    asyncio.run(client._begin(_Msg(), 7))
    asyncio.run(client.on_opening_step(_Interaction(), "oneshot"))
    assert store.state()["opening"]["mode"] == "oneshot" and "final destination" in sent[-1][0]
    asyncio.run(client.on_premise(_Interaction(), "the Glass Spire", "steal the Choir's Bell"))
    assert "it ends at the Glass Spire, the final destination" in store.state()["setting"]
    assert sent[-1][1] == OpeningPrompt(("questions",))  # no introductions: they travel together

    asyncio.run(client.on_opening_step(_Interaction(), "questions"))
    assert ran[-1].payload["event"] == "One-shot: questions" and "ONE question" in ran[-1].payload["instruction"]
    asyncio.run(client.on_opening_step(_Interaction(), "bonds"))
    asyncio.run(client.on_opening_step(_Interaction(), "playing"))
    assert ran[-1].payload["event"].startswith("One-shot: scene 1 of 3")
    assert sent[-1][1] == OpeningPrompt(("scene",))

    early = _Interaction()
    asyncio.run(client.on_opening_step(early, "break"))  # not before scene 2
    assert "isn't open now" in early.response.private[0]

    asyncio.run(client.on_opening_step(_Interaction(), "scene"))
    assert ran[-1].payload["event"] == "One-shot: scene 2 of 3, the branch" and sent[-1][1] == OpeningPrompt(("break",))
    asyncio.run(client.on_opening_step(_Interaction(), "break"))
    gorm = store.state()["sheets"]["Gorm"]
    from gary.sheets import xp_needed

    assert int(gorm["xp"]) >= xp_needed(gorm)  # everyone gains a level at the break
    asyncio.run(client.on_opening_step(_Interaction(), "scene"))
    assert ran[-1].payload["event"] == "One-shot: scene 3 of 3, the finale" and sent[-1][1] == OpeningPrompt(("epilogue",))
    asyncio.run(client.on_opening_step(_Interaction(), "epilogue"))
    assert ran[-1].payload["event"] == "One-shot: the epilogue" and store.state()["opening"]["stage"] == "epilogue"

    asyncio.run(end())
    assert store.state()["opening"]["stage"] == "finished" and "The one-shot is over" in sent[-1][0]
    assert not store.state().get("fronts")

    # And it can carry on as a campaign: that session is the campaign's first, so fronts follow it.
    asyncio.run(client._begin(_Msg(), 7))
    assert sent[-1][1] == OpeningPrompt(("continue",))
    asyncio.run(client.on_opening_step(_Interaction(), "continue"))
    assert store.state()["opening"]["mode"] == "campaign" and store.state()["opening"]["stage"] == "playing"
    asyncio.run(end())
    assert ran[-1].payload["event"] == "Opening: fronts after the first session"


def test_a_one_shot_ended_early_still_closes_with_its_chronicle(table):
    client, store, sent, ran = table
    store.apply([Mutation(MutationKind.OPENING, "", {"stage": "playing", "mode": "oneshot", "scene": 2,
                                                      "starter": 11})], reason="t")
    asyncio.run(_ender(client)())
    assert store.state()["opening"]["stage"] == "finished" and sent[-1][0].startswith("📖")


def test_a_death_tells_the_table_whats_next_campaign_or_one_shot():
    assert "make your next character with `/new` (level 1)" in opening.death_notice({"opening": {"mode": "campaign"}}, "Dorn")
    one = opening.death_notice({"opening": {"mode": "oneshot"}}, "Dorn")
    assert "there's no new character" in one and "/new" not in one
    assert "The dead stay dead" in opening.newcomer_event("Kael", ["Dorn"])["instruction"]


def test_no_new_characters_once_a_one_shot_is_under_way(table):
    client, store, sent, ran = table
    client._finish_character = GaryClient._finish_character.__get__(client)
    store.apply([Mutation(MutationKind.OPENING, "", {"mode": "oneshot", "stage": "playing"})], reason="mid-game")

    class _I:
        user = _User(12)

    refused = asyncio.run(client._finish_character(_I(), {"name": "Kael", "class": "Fighter"}, 7))
    assert "no new characters once play has begun" in refused


def test_the_prompt_draws_fresh_ingredients_and_shows_no_fixed_examples():
    """Fixed examples were echoed: three games of salt mines, flooding crypts and poisoning saboteurs."""
    import random

    one, two = (opening.premise_prompt(rng=random.Random(seed)) for seed in (1, 2))
    assert "This time, draw on these" in one and one != two
    assert opening.premise_prompt(rng=random.Random(1)) == one  # the draw, not the wording, varies
    for echoed in ("silver mine", "bell-towers", "abbey's well", "poisoning"):
        assert echoed not in one
    drawn = one.split("kinds of place -- ")[1].split(";")[0].split(", ")
    assert len(drawn) == 6 and set(drawn) <= set(opening.PLACE_KINDS)


def test_a_long_premise_runs_on_into_the_options_description_instead_of_being_cut_off():
    from gary.adapters.discord_bot import wrapped_option

    text = "A flooded monastic library where scholars pole flatboats among the drowned stacks"
    option = wrapped_option(text, "0")
    assert len(option.label) <= 45 and f"{option.label} {option.description}" == text
    assert wrapped_option("A short place", "1").description is None


def test_a_premise_is_one_option_its_place_the_label_and_its_activity_the_line_under_it():
    from gary.adapters.discord_bot import PremiseView

    async def build():
        return PremiseView(PremisePrompt(("a tidal lighthouse", "a glass forest"),
                                         ("relight the lamp", "find the witness tree")))

    view = asyncio.run(build())
    (select,) = [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert [(o.label, o.description) for o in select.options] == [
        ("a tidal lighthouse", "relight the lamp"), ("a glass forest", "find the witness tree")]
