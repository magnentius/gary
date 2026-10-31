"""Configuration (ARCHITECTURE.md §1, §9.3).

Every check here fails at boot rather than at runtime. A misconfigured Gary that starts
successfully and then silently ignores the table is the worst outcome available.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gary.config import ConfigError, Settings


def settings(**kw) -> Settings:
    base = dict(discord_token="tok", game_channels=frozenset({555}))
    base.update(kw)
    return Settings(**base)


def test_defaults_are_the_verified_values():
    s = settings()
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


def test_the_command_prefix_is_no_longer_configurable():
    """The commands are slash commands; the old prefix is internal and the variable is ignored."""
    s = Settings.from_env({"DISCORD_TOKEN": "t", "GARY_GAME_CHANNELS": "1", "GARY_COMMAND_PREFIX": "!gm "})
    assert s.command_prefix == "?dm "

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
            "GARY_BACKEND": "firestore",
            "GARY_CHECK_TTL": "300",
            "GARY_CAMPAIGN_ROOT": "/srv/gary",
        }
    )
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


# -- where the campaign lives -------------------------------------------------------------


def test_the_default_campaign_root_is_outside_the_working_directory():
    """It used to be ./campaign, which put the one irreplaceable thing in the project
    inside a git checkout -- and `rm -rf gary && git clone ...` is an ordinary thing to
    do to a repo."""
    from gary.config import default_campaign_root

    root = default_campaign_root()
    assert root.is_absolute()
    assert Path.cwd() not in root.parents and root != Path.cwd()
    assert root.parts[-2:] == ("gary", "campaign")


def test_xdg_data_home_is_honoured(monkeypatch):
    from gary.config import default_campaign_root

    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg")
    assert default_campaign_root() == Path("/tmp/xdg/gary/campaign")


def test_an_empty_xdg_data_home_falls_back_rather_than_rooting_at_slash(monkeypatch):
    """`XDG_DATA_HOME=` is set-but-empty, and the spec says treat that as unset. Reading
    it literally would put the campaign at /gary/campaign."""
    from gary.config import default_campaign_root

    monkeypatch.setenv("XDG_DATA_HOME", "")
    assert default_campaign_root() == Path.home() / ".local/share/gary/campaign"


def test_an_explicit_campaign_root_still_wins(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("GARY_GAME_CHANNELS", "1")
    monkeypatch.setenv("GARY_CAMPAIGN_ROOT", "/srv/fen-marches")
    assert Settings.from_env().campaign_root == Path("/srv/fen-marches")


def test_an_empty_campaign_root_falls_back_instead_of_writing_to_cwd(monkeypatch):
    """`GARY_CAMPAIGN_ROOT=` in a .env is the easy way to get an empty string, and
    Path("") is "." -- the working directory, which is what the move exists to avoid."""
    from gary.config import default_campaign_root

    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("GARY_GAME_CHANNELS", "1")
    monkeypatch.setenv("GARY_CAMPAIGN_ROOT", "")
    assert Settings.from_env().campaign_root == default_campaign_root()


def test_a_campaign_at_the_old_default_is_reported_not_migrated(tmp_path, monkeypatch):
    """Starting fresh where a campaign already exists elsewhere looks exactly like a new
    install, so it has to be said out loud. Moving it unasked is not a boot-time job."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "campaign").mkdir()
    (tmp_path / "campaign" / "journal.ndjson").write_text("{}\n")

    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("GARY_GAME_CHANNELS", "1")
    monkeypatch.setenv("GARY_CAMPAIGN_ROOT", str(tmp_path / "elsewhere"))
    stranded = Settings.from_env().stranded_campaign()
    assert stranded == (tmp_path / "campaign").resolve()
    # Reported only: the journal is left exactly where it was.
    assert (tmp_path / "campaign" / "journal.ndjson").exists()


def test_nothing_is_stranded_when_the_old_path_is_the_configured_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "campaign").mkdir()
    (tmp_path / "campaign" / "journal.ndjson").write_text("{}\n")

    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("GARY_GAME_CHANNELS", "1")
    monkeypatch.setenv("GARY_CAMPAIGN_ROOT", "./campaign")
    assert Settings.from_env().stranded_campaign() is None


def test_an_empty_directory_at_the_old_path_is_not_a_stranded_campaign(tmp_path, monkeypatch):
    """A bare ./campaign with no journal is leftover scaffolding, not somebody's game."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "campaign").mkdir()

    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("GARY_GAME_CHANNELS", "1")
    monkeypatch.setenv("GARY_CAMPAIGN_ROOT", str(tmp_path / "elsewhere"))
    assert Settings.from_env().stranded_campaign() is None
