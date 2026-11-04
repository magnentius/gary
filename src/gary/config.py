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

# "!" is the prefix most Discord bots claim. Sharing it would make Gary answer commands
# meant for another bot in the same server.
#: Internal: how the slash commands address the command dispatcher, and the typed prefix that
#: is now answered with a pointer to the slash commands. Not configurable.
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
    """Parse ``GARY_PLAYERS`` -- ``<discord_user_id>=<character name>`` pairs.

    Without this (or ``/playing``) the model is only ever told a Discord display name,
    and has no character to open a check for. Semicolon-separated, because character
    names contain commas far more often than semicolons.
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
    # Discord's privileged Message Content intent. Off: players speak to Gary with @Gary, a reply
    # to him, or /do -- the only text Discord sends a bot without it. On (GARY_MESSAGE_CONTENT=1,
    # and enabled in the Developer Portal): anything typed in a freeform channel reaches him.
    message_content: bool = False
    command_prefix: str = DEFAULT_COMMAND_PREFIX
    # discord user id -> character name. Empty is allowed: actor
    # validation is then skipped rather than rejecting everything.
    players: Mapping[int, str] = field(default_factory=dict)

    campaign_root: Path = field(default_factory=default_campaign_root)
    backend: str = "file"  # file | firestore
    firestore_campaign_id: str = "default"

    # Empty means run with EchoNarrator: the full Discord path, no model, no spend.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_summary_model: str = "gemini-3.5-flash-lite"
    # §1 requires a timeout on every model call. Without one a hung request
    # blocks the turn forever: no log, no retry, and nothing at the table.
    gemini_timeout_seconds: float = 90.0
    # The previous release, tried once when the narrator is overloaded (a new flash model
    # answered 503 "high demand" for days after release). Empty disables the fallback.
    gemini_fallback_model: str = "gemini-3.7-flash"
    # /picture: the image model, and how many new pictures a day (a kept one shown again is free).
    # About $0.067 an image at 1K with gemini-3.1-flash-image (checked 2026-11-03). 0 turns it off.
    gemini_image_model: str = "gemini-3.1-flash-image"
    images_per_day: int = 30
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
            message_content=str(e.get("GARY_MESSAGE_CONTENT", "")).strip().lower() in ("1", "true", "yes", "on"),
            players=_players(e.get("GARY_PLAYERS")),
            campaign_root=Path(e["GARY_CAMPAIGN_ROOT"]) if e.get("GARY_CAMPAIGN_ROOT")
            else default_campaign_root(),
            backend=e.get("GARY_BACKEND", "file"),
            firestore_campaign_id=e.get("GARY_FIRESTORE_CAMPAIGN_ID", "default"),
            gemini_api_key=e.get("GEMINI_API_KEY", ""),
            gemini_model=e.get("GARY_GEMINI_MODEL", "gemini-3.8-flash"),
            gemini_summary_model=e.get("GARY_GEMINI_SUMMARY_MODEL", "gemini-3.5-flash-lite"),
            gemini_timeout_seconds=float(e.get("GARY_GEMINI_TIMEOUT", 90.0)),
            gemini_fallback_model=e.get("GARY_GEMINI_FALLBACK_MODEL", "gemini-3.7-flash"),
            gemini_image_model=e.get("GARY_GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"),
            images_per_day=int(e.get("GARY_IMAGES_PER_DAY", 30)),
            history_window=int(e.get("GARY_HISTORY_WINDOW", 30)),
            check_ttl_seconds=float(e.get("GARY_CHECK_TTL", 180.0)),
            sweep_interval_seconds=float(e.get("GARY_SWEEP_INTERVAL", 15.0)),
            send_interval_seconds=float(e.get("GARY_SEND_INTERVAL", 1.0)),
            max_effect_depth=int(e.get("GARY_MAX_EFFECT_DEPTH", 4)),
        )
