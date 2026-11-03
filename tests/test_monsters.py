import pytest


def test_the_gm_sees_what_each_kind_of_creature_wants_and_does():
    from gary.monsters import natures, prepare

    added, errors = prepare([{"name": "goblin", "count": 2},
                             {"name": "Bog Hag", "hp": 12, "armor": 1, "damage": "d8",
                              "instinct": "To drown the unwary", "moves": ["Drag someone under"]}], [])
    assert not errors
    goblin, hag = natures(added)  # one line per kind, not per goblin
    assert goblin.startswith("Goblin — instinct: To multiply · moves: Charge!; Call more goblins")
    assert "attacks with Spear (Close, Reach)" in goblin
    assert hag == "Bog Hag — instinct: To drown the unwary · moves: Drag someone under"
    added["Goblin 1"]["hp"] = added["Goblin 2"]["hp"] = 0
    assert natures(added) == [hag]  # the fallen are not worth planning for


def test_a_homebrew_creature_needs_no_nature():
    from gary.monsters import natures, prepare

    added, _ = prepare([{"name": "Mud Thing", "hp": 6, "armor": 0, "damage": "d6"}], [])
    assert natures(added) == []


# -- Making Monsters: the book's questions ---------------------------------------------------

def test_the_books_worked_example_comes_out_as_printed():
    """The spirit of judgement in Making Monsters: solitary, human-sized, no armor, divine
    (+2 damage), magical, armor doesn't help, intelligent, kept alive by more than biology,
    terrifying -- 16 HP, d10+2, ignores armor."""
    from gary.monsters import build

    spirit = build({"organization": "solitary", "size": "human", "defense": "flesh",
                    "known_for": ["divine", "magic"], "divine_favor": "damage", "attack_traits": ["ignores_armor"],
                    "traits": ["intelligent", "unnatural", "terrifying"]})
    assert (spirit["hp"], spirit["armor"], spirit["damage"], spirit["ignores_armor"]) == (16, 0, "d10+2", True)
    assert {"Solitary", "Divine", "Magical", "Intelligent", "Terrifying"} <= set(spirit["tags"])
    assert "a move about its spells" in spirit["write"]


def test_each_answer_moves_the_numbers():
    from gary.monsters import MonsterError, build

    base = {"organization": "group", "size": "human", "defense": "mail"}
    assert (build(base)["hp"], build(base)["damage"], build(base)["armor"]) == (6, "d8", 2)
    assert build({**base, "size": "huge"})["damage"] == "d8+3" and build({**base, "size": "huge"})["hp"] == 14
    assert build({**base, "known_for": ["offense"]})["damage"] == "b[2d8]"
    assert build({**base, "traits": ["abhors_violence"]})["damage"] == "w[2d8]"
    assert build({**base, "attack_traits": ["weak"], "traits": ["devious"]})["damage"] == "d4"  # never below d4
    assert build({**base, "traits": ["ancient"], "organization": "solitary"})["damage"] == "d12"
    assert build({**base, "attack_traits": ["tears_metal"]})["piercing"] == 3
    assert build({**base, "traits": ["amorphous", "cautious"]})["armor"] == 4
    with pytest.raises(MonsterError, match="organization is one of"):
        build({**base, "organization": "swarm"})
    with pytest.raises(MonsterError, match="not wings"):
        build({**base, "traits": ["wings"]})


def test_a_recipe_creature_enters_the_scene_with_an_instinct_and_a_move():
    from gary.monsters import MonsterError, stats_for

    recipe = {"organization": "horde", "size": "small", "defense": "flesh", "attack_traits": ["reach"]}
    record = stats_for({"name": "Salt Scuttler", "recipe": recipe, "instinct": "to encrust prey in salt",
                        "moves": ["Burrow and leap from below"]})
    assert (record["hp"], record["damage"], record["recipe"]) == (3, "d6", True)
    assert "reach" in record["tags"]
    with pytest.raises(MonsterError, match="instinct .* and at least one move"):
        stats_for({"name": "Salt Scuttler", "recipe": recipe})
