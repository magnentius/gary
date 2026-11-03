"""Using Monsters: a creature promoted out of the arrows, and creatures hurting each other."""

from __future__ import annotations

from gary.adapters import gemini_tools as T
from gary.fsm import ChangeHp, Director, SendMessage
from gary.models import Mutation, MutationKind
from gary.registry import PendingCheckRegistry
from gary.state.backend import InMemoryStateBackend
from gary.state.history import InMemoryHistoryBackend
from gary.state.store import CampaignStore
from gary.ports import ScriptedDice
from tests.test_gemini import CHANNEL, GOBLIN, call_response, narrator, player, text_response

OGRE = {**GOBLIN, "name": "Ogre", "hp": 10, "damage": "d10+3", "armor": 1}


def _table(clock, dice=(6,)):
    store = CampaignStore(backend=InMemoryStateBackend(), clock=clock)
    store.apply([Mutation(MutationKind.MONSTERS_ADD, "", {"monsters": {
        "Goblin 1": dict(GOBLIN), "Goblin 2": dict(GOBLIN), "Ogre 1": dict(OGRE)}})], reason="setup")
    director = Director(registry=PendingCheckRegistry(clock=clock), clock=clock, game_channels={CHANNEL},
                        dice=ScriptedDice(list(dice)), monsters=lambda: store.state().get("monsters") or {})
    return director, store


async def test_an_ogre_swats_a_goblin(clock):
    director, store = _table(clock)
    n = narrator((director, store, InMemoryHistoryBackend()),
                 call_response(T.CREATURE_ATTACKS, {"attacker": "Ogre 1", "target": "Goblin 1"}),
                 text_response("Crunch."))
    result = await player(n, "we let them fight")
    answer = n.client.calls[1]["contents"][-1].parts[0].function_response.response
    expected = max(0, 6 + 3 - int(GOBLIN.get("armor", 0)))
    assert answer["dealt"] == expected and answer["defeated"] == (int(GOBLIN["hp"]) - expected <= 0)
    assert any(isinstance(e, ChangeHp) and e.name == "Goblin 1" and e.hp == int(GOBLIN["hp"]) - expected
               for e in result.effects)
    assert any(isinstance(e, SendMessage) and "**Ogre 1** hits **Goblin 1**" in e.text for e in result.effects)


def test_a_creature_cannot_hit_itself_or_the_fallen(clock):
    director, store = _table(clock)
    _, refused = director.creature_attacks(channel_id=CHANNEL, attacker="Ogre 1", target="Ogre 1")
    assert not refused["ok"]
    store.apply([Mutation(MutationKind.MONSTER_HP, "Goblin 2", {"hp": 0})], reason="down")
    _, refused = director.creature_attacks(channel_id=CHANNEL, attacker="Ogre 1", target="Goblin 2")
    assert not refused["ok"] and "out of the fight" in refused["error"]
    effects, ok = director.creature_attacks(channel_id=CHANNEL, attacker="goblin 1", target="ogre 1")
    assert ok["ok"] and any(isinstance(e, ChangeHp) and e.name == "Ogre 1" for e in effects)


async def test_a_goblin_that_earned_the_spotlight_gets_a_name(clock):
    director, store = _table(clock)
    store.apply([Mutation(MutationKind.MONSTER_HP, "Goblin 2", {"hp": 1})], reason="wounded")
    n = narrator((director, store, InMemoryHistoryBackend()),
                 call_response(T.PROMOTE_MONSTER, {"monster": "Goblin 2", "name": "Grizzlegut",
                                                   "why": "fled with a burnt hand, swearing revenge"}),
                 text_response("."))
    await player(n, "it gets away")
    monsters = store.state()["monsters"]
    assert "Goblin 2" not in monsters and monsters["Grizzlegut"]["hp"] == 1 and monsters["Grizzlegut"]["named"]
    assert any(note["subject"] == "Grizzlegut" and note["kind"] == "threat" for note in store.state()["notes"])
    store.undo_last(); store.undo_last(); store.undo_last()
    assert "Goblin 2" in store.state()["monsters"] and "Grizzlegut" not in store.state()["monsters"]
