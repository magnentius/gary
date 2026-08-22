"""Runtime configuration (ARCHITECTURE.md §1, §11).

Config is read once at boot and treated as immutable. Nothing here accumulates during a
session, which is what lets a replacement process behave identically to the one it
replaced.

Secrets come from the environment, which on the VM means Secret Manager injected by the
service account — never a file in the repo (§9.3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Optional

# Verified against the Discord App Directory. Kept overridable because hardcoding an ID
# that turns out to be wrong is exactly how v1 of this design shipped a dead parser.
DEFAULT_AVRAE_BOT_ID = 261302296103747584

# Avrae owns "!". A collision would make Gary answer commands meant for the dice bot.
DEFAULT_COMMAND_PREFIX = "?dm "

#: Where a campaign lives when nothing says otherwise.
#:
#: Deliberately outside the working directory. The campaign is the journal, the snapshot
#: and every channel's history -- the one thing in this project that exists nowhere else
#: and that §7.3 cannot undo. Defaulting it to ``./campaign`` put it inside a git
#: checkout, where `rm -rf gary && git clone ...` is an ordinary thing to do to a repo
#: and would take a year of play with it.
#:
#: ``XDG_DATA_HOME`` is honoured where it is set; the fallback is its specified default.
#: This is the same path inside the container and out, except that the image overrides it
#: to ``/campaign`` so the bind mount has somewhere obvious to land.
def default_campaign_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "gary" / "campaign"


#: A campaign left where the old default used to put it. Checked at boot so an upgrade
#: cannot look like the campaign having vanished -- see `Settings.stranded_campaign`.
LEGACY_CAMPAIGN_ROOT = Path("./campaign")


class ConfigError(RuntimeError):
    """Configuration is missing or unusable. Always fatal at boot, never at runtime."""


def _ids(raw: Optional[str]) -> FrozenSet[int]:
    if not raw:
        return frozenset()
    out = set()
    for part in raw.replace(",", " ").split():
        try:
            out.add(int(part))
        except ValueError:
            raise ConfigError(f"not a Discord snowflake: {part!r}")
    return frozenset(out)


def _players(raw: Optional[str]) -> "Mapping[int, str]":
    """Parse ``GARY_PLAYERS`` -- ``<discord_user_id>=<Avrae character name>`` pairs.

    Without this the model is only ever told a Discord display name, so ``open_check``
    names an actor Avrae has never heard of and the roll silently fails to correlate
    (ARCHITECTURE.md §6.2). Semicolon-separated, because character names contain commas
    far more often than semicolons.
    """
    if not raw:
        return {}
    out: Dict[int, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        user_id, sep, actor = part.partition("=")
        if not sep or not actor.strip():
            raise ConfigError(
                f"GARY_PLAYERS entry {part!r} is not '<user_id>=<character name>'"
            )
        try:
            out[int(user_id.strip())] = actor.strip()
        except ValueError:
            raise ConfigError(f"not a Discord snowflake: {user_id.strip()!r}")
    return out


@dataclass(frozen=True)
class Settings:
    discord_token: str
    game_channels: FrozenSet[int]
    freeform_channels: FrozenSet[int] = field(default_factory=frozenset)
    avrae_bot_id: int = DEFAULT_AVRAE_BOT_ID
    # Avrae's built-in coinpurse. Override only to use a workshop alias instead
    # (e.g. "coins"); the native command needs nothing installed.
    coins_command: str = "game coins"
    command_prefix: str = DEFAULT_COMMAND_PREFIX
    # discord user id -> Avrae character name. Empty is allowed: actor
    # validation is then skipped rather than rejecting everything.
    players: Mapping[int, str] = field(default_factory=dict)

    campaign_root: Path = field(default_factory=default_campaign_root)
    backend: str = "file"  # file | firestore
    firestore_campaign_id: str = "default"

    # Empty means run with EchoNarrator: the full Discord path, no model, no spend.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.7-flash"
    gemini_summary_model: str = "gemini-3.5-flash-lite"
    # §1 requires a timeout on every model call. Without one a hung request
    # blocks the turn forever: no log, no retry, and nothing at the table.
    gemini_timeout_seconds: float = 90.0
    # TEMPORARY: gemini-3.7-flash was answering 503 "high demand" repeatedly on
    # 2026-08-21, eight days after release. Empty disables the fallback.
    gemini_fallback_model: str = "gemini-3.6-flash"
    history_window: int = 30

    check_ttl_seconds: float = 180.0
    sweep_interval_seconds: float = 15.0
    send_interval_seconds: float = 1.0
    max_effect_depth: int = 4

    def __post_init__(self) -> None:
        if not self.discord_token:
            raise ConfigError("DISCORD_TOKEN is empty")
        if not self.game_channels:
            raise ConfigError(
                "GARY_GAME_CHANNELS is empty; Gary would join the server and ignore "
                "every message"
            )
        if self.command_prefix.startswith("!"):
            raise ConfigError(
                f"command prefix {self.command_prefix!r} collides with Avrae's '!'"
            )
        if self.backend not in ("file", "firestore"):
            raise ConfigError(f"unknown backend {self.backend!r}")
        stray = self.freeform_channels - self.game_channels
        if stray:
            raise ConfigError(
                f"freeform channels are not game channels: {sorted(stray)}"
            )

    def stranded_campaign(self) -> Optional[Path]:
        """A campaign sitting where the old default used to put it, and now unread.

        The default moved out of the working directory, and the failure that move can
        cause is worse than the one it fixes: Gary starts, finds nothing at the new path,
        and opens an empty campaign. That is indistinguishable from a fresh install, so
        nobody would think to look. Detected rather than migrated -- moving somebody's
        journal without being asked is not a thing a boot sequence should do.
        """
        legacy = LEGACY_CAMPAIGN_ROOT.resolve()
        if not (legacy / "journal.ndjson").exists():
            return None
        try:
            if legacy == self.campaign_root.resolve():
                return None  # it *is* the configured root; nothing stranded
        except OSError:  # pragma: no cover - unresolvable path
            pass
        return legacy

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        e = os.environ if env is None else env
        return cls(
            discord_token=e.get("DISCORD_TOKEN", ""),
            game_channels=_ids(e.get("GARY_GAME_CHANNELS")),
            freeform_channels=_ids(e.get("GARY_FREEFORM_CHANNELS")),
            avrae_bot_id=int(e.get("GARY_AVRAE_BOT_ID", DEFAULT_AVRAE_BOT_ID)),
            coins_command=e.get("GARY_COINS_COMMAND", "game coins"),
            command_prefix=e.get("GARY_COMMAND_PREFIX", DEFAULT_COMMAND_PREFIX),
            players=_players(e.get("GARY_PLAYERS")),
            campaign_root=Path(e["GARY_CAMPAIGN_ROOT"]) if e.get("GARY_CAMPAIGN_ROOT")
            else default_campaign_root(),
            backend=e.get("GARY_BACKEND", "file"),
            firestore_campaign_id=e.get("GARY_FIRESTORE_CAMPAIGN_ID", "default"),
            gemini_api_key=e.get("GEMINI_API_KEY", ""),
            gemini_model=e.get("GARY_GEMINI_MODEL", "gemini-3.7-flash"),
            gemini_summary_model=e.get("GARY_GEMINI_SUMMARY_MODEL", "gemini-3.5-flash-lite"),
            gemini_timeout_seconds=float(e.get("GARY_GEMINI_TIMEOUT", 90.0)),
            gemini_fallback_model=e.get("GARY_GEMINI_FALLBACK_MODEL", "gemini-3.6-flash"),
            history_window=int(e.get("GARY_HISTORY_WINDOW", 30)),
            check_ttl_seconds=float(e.get("GARY_CHECK_TTL", 180.0)),
            sweep_interval_seconds=float(e.get("GARY_SWEEP_INTERVAL", 15.0)),
            send_interval_seconds=float(e.get("GARY_SEND_INTERVAL", 1.0)),
            max_effect_depth=int(e.get("GARY_MAX_EFFECT_DEPTH", 4)),
        )
