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
from typing import FrozenSet, Optional

# Verified against the Discord App Directory. Kept overridable because hardcoding an ID
# that turns out to be wrong is exactly how v1 of this design shipped a dead parser.
DEFAULT_AVRAE_BOT_ID = 261302296103747584

# Avrae owns "!". A collision would make Drelm answer commands meant for the dice bot.
DEFAULT_COMMAND_PREFIX = "?dm "


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


@dataclass(frozen=True)
class Settings:
    discord_token: str
    game_channels: FrozenSet[int]
    freeform_channels: FrozenSet[int] = field(default_factory=frozenset)
    avrae_bot_id: int = DEFAULT_AVRAE_BOT_ID
    command_prefix: str = DEFAULT_COMMAND_PREFIX

    campaign_root: Path = Path("./campaign")
    backend: str = "file"  # file | firestore
    firestore_campaign_id: str = "default"

    # Empty means run with EchoNarrator: the full Discord path, no model, no spend.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_summary_model: str = "gemini-3.5-flash-lite"
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
                "DRELM_GAME_CHANNELS is empty; Drelm would join the server and ignore "
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

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        e = os.environ if env is None else env
        return cls(
            discord_token=e.get("DISCORD_TOKEN", ""),
            game_channels=_ids(e.get("DRELM_GAME_CHANNELS")),
            freeform_channels=_ids(e.get("DRELM_FREEFORM_CHANNELS")),
            avrae_bot_id=int(e.get("DRELM_AVRAE_BOT_ID", DEFAULT_AVRAE_BOT_ID)),
            command_prefix=e.get("DRELM_COMMAND_PREFIX", DEFAULT_COMMAND_PREFIX),
            campaign_root=Path(e.get("DRELM_CAMPAIGN_ROOT", "./campaign")),
            backend=e.get("DRELM_BACKEND", "file"),
            firestore_campaign_id=e.get("DRELM_FIRESTORE_CAMPAIGN_ID", "default"),
            gemini_api_key=e.get("GEMINI_API_KEY", ""),
            gemini_model=e.get("DRELM_GEMINI_MODEL", "gemini-3.6-flash"),
            gemini_summary_model=e.get("DRELM_GEMINI_SUMMARY_MODEL", "gemini-3.5-flash-lite"),
            history_window=int(e.get("DRELM_HISTORY_WINDOW", 30)),
            check_ttl_seconds=float(e.get("DRELM_CHECK_TTL", 180.0)),
            sweep_interval_seconds=float(e.get("DRELM_SWEEP_INTERVAL", 15.0)),
            send_interval_seconds=float(e.get("DRELM_SEND_INTERVAL", 1.0)),
            max_effect_depth=int(e.get("DRELM_MAX_EFFECT_DEPTH", 4)),
        )
