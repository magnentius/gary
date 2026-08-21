"""Configuration (ARCHITECTURE.md §1, §9.3).

Every check here fails at boot rather than at runtime. A misconfigured Gary that starts
successfully and then silently ignores the table is the worst outcome available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gary.config import DEFAULT_AVRAE_BOT_ID, ConfigError, Settings


def settings(**kw) -> Settings:
    base = dict(discord_token="tok", game_channels=frozenset({555}))
    base.update(kw)
    return Settings(**base)


def test_defaults_are_the_verified_values():
    s = settings()
    assert s.avrae_bot_id == 261302296103747584
    assert DEFAULT_AVRAE_BOT_ID == 261302296103747584
    assert not s.command_prefix.startswith("!")
    # Must track adapters/gemini.py; a drift between them is a silent misconfiguration.
    assert s.gemini_model == "gemini-3.7-flash"
    assert s.gemini_summary_model == "gemini-3.5-flash-lite"


def test_config_defaults_match_the_adapter_defaults():
    from gary.adapters.gemini import DEFAULT_MODEL, DEFAULT_SUMMARY_MODEL

    s = settings()
    assert s.gemini_model == DEFAULT_MODEL
    assert s.gemini_summary_model == DEFAULT_SUMMARY_MODEL


def test_missing_token_is_fatal():
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        settings(discord_token="")


def test_no_game_channels_is_fatal():
    """Otherwise Gary joins the server, looks healthy, and ignores every message."""
    with pytest.raises(ConfigError, match="ignore every message"):
        settings(game_channels=frozenset())


def test_prefix_colliding_with_avrae_is_fatal():
    """Avrae owns '!'. Sharing it means answering commands meant for the dice bot."""
    with pytest.raises(ConfigError, match="collides with Avrae"):
        settings(command_prefix="!dm ")


def test_freeform_channel_outside_game_channels_is_fatal():
    """A freeform channel Gary does not watch is silently inert -- catch the typo."""
    with pytest.raises(ConfigError, match="not game channels"):
        settings(freeform_channels=frozenset({999}))


def test_unknown_backend_is_fatal():
    with pytest.raises(ConfigError, match="unknown backend"):
        settings(backend="postgres")


def test_from_env_parses_channel_lists():
    s = Settings.from_env(
        {
            "DISCORD_TOKEN": "tok",
            "GARY_GAME_CHANNELS": "111, 222 333",
            "GARY_FREEFORM_CHANNELS": "222",
        }
    )
    assert s.game_channels == frozenset({111, 222, 333})
    assert s.freeform_channels == frozenset({222})


def test_from_env_rejects_a_non_snowflake():
    with pytest.raises(ConfigError, match="not a Discord snowflake"):
        Settings.from_env({"DISCORD_TOKEN": "tok", "GARY_GAME_CHANNELS": "111,abc"})


def test_from_env_overrides():
    s = Settings.from_env(
        {
            "DISCORD_TOKEN": "tok",
            "GARY_GAME_CHANNELS": "555",
            "GARY_AVRAE_BOT_ID": "12345",
            "GARY_BACKEND": "firestore",
            "GARY_CHECK_TTL": "300",
            "GARY_CAMPAIGN_ROOT": "/srv/gary",
        }
    )
    assert s.avrae_bot_id == 12345
    assert s.backend == "firestore"
    assert s.check_ttl_seconds == 300.0
    assert s.campaign_root == Path("/srv/gary")


def test_settings_are_immutable():
    """Config is read once at boot; nothing accumulates during a session."""
    s = settings()
    with pytest.raises(Exception):
        s.game_channels = frozenset({1})  # type: ignore[misc]


def test_players_mapping_parses():
    s = Settings.from_env({
        "DISCORD_TOKEN": "t", "GARY_GAME_CHANNELS": "1",
        "GARY_PLAYERS": "111=Caspian Varr; 222=Kethra Vane",
    })
    assert s.players == {111: "Caspian Varr", 222: "Kethra Vane"}


def test_players_is_optional():
    s = Settings.from_env({"DISCORD_TOKEN": "t", "GARY_GAME_CHANNELS": "1"})
    assert s.players == {}


def test_malformed_players_entry_fails_at_boot():
    """Fail at boot, not three minutes into a check that cannot correlate."""
    with pytest.raises(ConfigError):
        Settings.from_env({
            "DISCORD_TOKEN": "t", "GARY_GAME_CHANNELS": "1",
            "GARY_PLAYERS": "Caspian Varr",
        })
