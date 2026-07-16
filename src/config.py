"""Environment-backed application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when required application configuration is invalid."""


def _read_float(environment: Mapping[str, str], name: str, default: float) -> float:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _read_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _read_bool(environment: Mapping[str, str], name: str, default: bool) -> bool:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false)")


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings with conservative production defaults."""

    discord_token: str = field(repr=False)
    command_prefix: str = "!tfd "
    log_level: str = "INFO"
    default_volume: float = 0.7
    voice_connect_timeout: float = 20.0
    voice_connect_retries: int = 3
    player_idle_timeout: float = 300.0
    tts_enabled: bool = True
    tts_lang: str = "en"

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> Settings:
        if environment is None:
            load_dotenv()
            environment = os.environ

        token = environment.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigurationError("DISCORD_TOKEN is required")

        settings = cls(
            discord_token=token,
            command_prefix=environment.get("COMMAND_PREFIX", "!tfd "),
            log_level=environment.get("LOG_LEVEL", "INFO").upper(),
            default_volume=_read_float(environment, "DEFAULT_VOLUME", 0.7),
            voice_connect_timeout=_read_float(
                environment, "VOICE_CONNECT_TIMEOUT", 20.0
            ),
            voice_connect_retries=_read_int(environment, "VOICE_CONNECT_RETRIES", 3),
            player_idle_timeout=_read_float(environment, "PLAYER_IDLE_TIMEOUT", 300.0),
            tts_enabled=_read_bool(environment, "TTS_ENABLED", True),
            tts_lang=environment.get("TTS_LANG", "en").strip() or "en",
        )
        settings._validate()
        return settings

    def _validate(self) -> None:
        if not self.command_prefix:
            raise ConfigurationError("COMMAND_PREFIX cannot be empty")
        if not 0.0 <= self.default_volume <= 2.0:
            raise ConfigurationError("DEFAULT_VOLUME must be between 0 and 2")
        if self.voice_connect_timeout <= 0:
            raise ConfigurationError("VOICE_CONNECT_TIMEOUT must be positive")
        if self.voice_connect_retries < 1:
            raise ConfigurationError("VOICE_CONNECT_RETRIES must be at least 1")
        if self.player_idle_timeout <= 0:
            raise ConfigurationError("PLAYER_IDLE_TIMEOUT must be positive")
        if not self.tts_lang:
            raise ConfigurationError("TTS_LANG cannot be empty")
