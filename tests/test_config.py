"""Configuration (ARCHITECTURE.md §1, §9.3).

Every check here fails at boot rather than at runtime. A misconfigured Drelm that starts
successfully and then silently ignores the table is the worst outcome available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drelm.config import DEFAULT_AVRAE_BOT_ID, ConfigError, Settings


def settings(**kw) -> Settings:
    base = dict(discord_token="tok", game_channels=frozenset({555}))
    base.update(kw)
    return Settings(**base)


def test_defaults_are_the_verified_values():
    s = settings()
    assert s.avrae_bot_id == 261302296103747584
    assert DEFAULT_AVRAE_BOT_ID == 261302296103747584
    assert not s.command_prefix.startswith("!")


def test_missing_token_is_fatal():
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        settings(discord_token="")


def test_no_game_channels_is_fatal():
    """Otherwise Drelm joins the server, looks healthy, and ignores every message."""
    with pytest.raises(ConfigError, match="ignore every message"):
        settings(game_channels=frozenset())


def test_prefix_colliding_with_avrae_is_fatal():
    """Avrae owns '!'. Sharing it means answering commands meant for the dice bot."""
    with pytest.raises(ConfigError, match="collides with Avrae"):
        settings(command_prefix="!dm ")


def test_freeform_channel_outside_game_channels_is_fatal():
    """A freeform channel Drelm does not watch is silently inert -- catch the typo."""
    with pytest.raises(ConfigError, match="not game channels"):
        settings(freeform_channels=frozenset({999}))


def test_unknown_backend_is_fatal():
    with pytest.raises(ConfigError, match="unknown backend"):
        settings(backend="postgres")


def test_from_env_parses_channel_lists():
    s = Settings.from_env(
        {
            "DISCORD_TOKEN": "tok",
            "DRELM_GAME_CHANNELS": "111, 222 333",
            "DRELM_FREEFORM_CHANNELS": "222",
        }
    )
    assert s.game_channels == frozenset({111, 222, 333})
    assert s.freeform_channels == frozenset({222})


def test_from_env_rejects_a_non_snowflake():
    with pytest.raises(ConfigError, match="not a Discord snowflake"):
        Settings.from_env({"DISCORD_TOKEN": "tok", "DRELM_GAME_CHANNELS": "111,abc"})


def test_from_env_overrides():
    s = Settings.from_env(
        {
            "DISCORD_TOKEN": "tok",
            "DRELM_GAME_CHANNELS": "555",
            "DRELM_AVRAE_BOT_ID": "12345",
            "DRELM_BACKEND": "firestore",
            "DRELM_CHECK_TTL": "300",
            "DRELM_CAMPAIGN_ROOT": "/srv/drelm",
        }
    )
    assert s.avrae_bot_id == 12345
    assert s.backend == "firestore"
    assert s.check_ttl_seconds == 300.0
    assert s.campaign_root == Path("/srv/drelm")


def test_settings_are_immutable():
    """Config is read once at boot; nothing accumulates during a session."""
    s = settings()
    with pytest.raises(Exception):
        s.game_channels = frozenset({1})  # type: ignore[misc]
