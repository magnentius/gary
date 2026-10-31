

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
