"""/playing and /new tag the player's server nickname with their character."""

from __future__ import annotations

from types import SimpleNamespace

import discord

from gary.adapters.discord_bot import tag_nickname, tagged_nickname


def test_the_tag_is_added_replaced_and_fits_32():
    assert tagged_nickname("Magnentius", "Slymin") == "Magnentius (Slymin)"
    assert tagged_nickname("Magnentius (Slymin)", "Dorn") == "Magnentius (Dorn)"
    long = tagged_nickname("A" * 40, "Kethra")
    assert len(long) == 32 and long.endswith(" (Kethra)")


class Member(SimpleNamespace):
    async def edit(self, nick, reason=None):
        if self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        self.nick = nick


def member(id=5, nick=None, owner=1, fail=False):
    return Member(id=id, nick=nick, display_name="Magnentius", guild=SimpleNamespace(owner_id=owner), fail=fail)


async def test_a_player_is_tagged_but_the_owner_and_a_refusal_are_left_alone():
    m = member()
    await tag_nickname(m, "Slymin")
    assert m.nick == "Magnentius (Slymin)"
    owner = member(id=1)
    await tag_nickname(owner, "Slymin")
    assert owner.nick is None
    refused = member(fail=True)
    await tag_nickname(refused, "Slymin")  # logged, not raised
    assert refused.nick is None
