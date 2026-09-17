"""Environment-backed application configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .tts import normalize_tts_language

DEFAULT_SOUNDBOARD_DATA_DIR = "data/soundboard"
DEFAULT_SOUNDBOARD_MAX_SECONDS = 12
DEFAULT_SOUNDBOARD_MAX_SOUNDS = 40
DEFAULT_SOUNDBOARD_MAX_BYTES = 1_500_000
DEFAULT_SOUNDBOARD_CACHE_DAYS = 7
DEFAULT_R2_PREFIX = "soundboard"
DEFAULT_PLAYLIST_DB_PATH = "data/playlists/playlists.db"
MIN_SOUNDBOARD_SECONDS = 1
MAX_SOUNDBOARD_SECONDS = 30
MIN_SOUNDBOARD_SOUNDS = 1
MAX_SOUNDBOARD_SOUNDS = 100
MIN_SOUNDBOARD_BYTES = 64 * 1024
MAX_SOUNDBOARD_BYTES = 5 * 1024 * 1024
MIN_SOUNDBOARD_CACHE_DAYS = 0
MAX_SOUNDBOARD_CACHE_DAYS = 365


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
    tts_lang: str = "vi"
    # Music gain (0–1) while session/chat TTS is mixed over a playing track.
    music_duck_level: float = 0.2
    # Optional Spotify Web API credentials. Track URLs still work via oEmbed
    # without them; albums, playlists, and artist links require both values.
    spotify_client_id: str = ""
    spotify_client_secret: str = field(default="", repr=False)
    soundboard_data_dir: str = DEFAULT_SOUNDBOARD_DATA_DIR
    soundboard_max_seconds: int = DEFAULT_SOUNDBOARD_MAX_SECONDS
    soundboard_max_sounds: int = DEFAULT_SOUNDBOARD_MAX_SOUNDS
    soundboard_max_bytes: int = DEFAULT_SOUNDBOARD_MAX_BYTES
    soundboard_cache_days: int = DEFAULT_SOUNDBOARD_CACHE_DAYS
    playlist_db_path: str = DEFAULT_PLAYLIST_DB_PATH
    playlist_max_per_user: int = 20
    playlist_max_tracks: int = 100
    playlist_max_server: int = 20
    playlist_backup_minutes: int = 0
    r2_account_id: str = ""
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = field(default="", repr=False)
    r2_bucket: str = ""
    r2_prefix: str = DEFAULT_R2_PREFIX

    @property
    def r2_configured(self) -> bool:
        """True when Cloudflare R2 object storage is fully configured."""
        return bool(self.r2_bucket)

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> Settings:
        if environment is None:
            load_dotenv()
            environment = os.environ

        token = environment.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigurationError("DISCORD_TOKEN is required")

        raw_tts_lang = environment.get("TTS_LANG", "vi").strip() or "vi"
        try:
            tts_lang = normalize_tts_language(raw_tts_lang)
        except ValueError as exc:
            raise ConfigurationError(
                "TTS_LANG must be a supported gTTS language code"
            ) from exc

        spotify_client_id = environment.get("SPOTIFY_CLIENT_ID", "").strip()
        spotify_client_secret = environment.get("SPOTIFY_CLIENT_SECRET", "").strip()
        if bool(spotify_client_id) != bool(spotify_client_secret):
            raise ConfigurationError(
                "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must both be set"
            )

        r2_account_id = environment.get("R2_ACCOUNT_ID", "").strip()
        r2_endpoint = environment.get("R2_ENDPOINT", "").strip()
        r2_access_key_id = environment.get("R2_ACCESS_KEY_ID", "").strip()
        r2_secret_access_key = environment.get("R2_SECRET_ACCESS_KEY", "").strip()
        r2_bucket = environment.get("R2_BUCKET", "").strip()
        r2_prefix = (
            environment.get("R2_PREFIX", DEFAULT_R2_PREFIX).strip()
            or DEFAULT_R2_PREFIX
        )
        r2_fields = (
            r2_account_id,
            r2_endpoint,
            r2_access_key_id,
            r2_secret_access_key,
            r2_bucket,
        )
        if any(r2_fields) and not (
            r2_bucket
            and r2_access_key_id
            and r2_secret_access_key
            and (r2_account_id or r2_endpoint)
        ):
            raise ConfigurationError(
                "R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and "
                "R2_ACCOUNT_ID or R2_ENDPOINT must all be set together"
            )
        if r2_bucket and not r2_endpoint:
            r2_endpoint = f"https://{r2_account_id}.r2.cloudflarestorage.com"

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
            tts_lang=tts_lang,
            music_duck_level=_read_float(environment, "MUSIC_DUCK_LEVEL", 0.2),
            spotify_client_id=spotify_client_id,
            spotify_client_secret=spotify_client_secret,
            soundboard_data_dir=(
                environment.get("SOUNDBOARD_DATA_DIR", DEFAULT_SOUNDBOARD_DATA_DIR).strip()
                or DEFAULT_SOUNDBOARD_DATA_DIR
            ),
            soundboard_max_seconds=_read_int(
                environment,
                "SOUNDBOARD_MAX_SECONDS",
                DEFAULT_SOUNDBOARD_MAX_SECONDS,
            ),
            soundboard_max_sounds=_read_int(
                environment,
                "SOUNDBOARD_MAX_SOUNDS",
                DEFAULT_SOUNDBOARD_MAX_SOUNDS,
            ),
            soundboard_max_bytes=_read_int(
                environment,
                "SOUNDBOARD_MAX_BYTES",
                DEFAULT_SOUNDBOARD_MAX_BYTES,
            ),
            soundboard_cache_days=_read_int(
                environment,
                "SOUNDBOARD_CACHE_DAYS",
                DEFAULT_SOUNDBOARD_CACHE_DAYS,
            ),
            r2_account_id=r2_account_id,
            r2_endpoint=r2_endpoint,
            r2_access_key_id=r2_access_key_id,
            r2_secret_access_key=r2_secret_access_key,
            r2_bucket=r2_bucket,
            r2_prefix=r2_prefix,
            playlist_db_path=(
                environment.get("PLAYLIST_DB_PATH", DEFAULT_PLAYLIST_DB_PATH).strip()
                or DEFAULT_PLAYLIST_DB_PATH
            ),
            playlist_max_per_user=_read_int(environment, "PLAYLIST_MAX_PER_USER", 20),
            playlist_max_tracks=_read_int(environment, "PLAYLIST_MAX_TRACKS", 100),
            playlist_max_server=_read_int(environment, "PLAYLIST_MAX_SERVER", 20),
            playlist_backup_minutes=_read_int(environment, "PLAYLIST_BACKUP_MINUTES", 0),
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
        try:
            normalize_tts_language(self.tts_lang)
        except ValueError as exc:
            raise ConfigurationError(
                "TTS_LANG must be a supported gTTS language code"
            ) from exc
        if not 0.0 <= self.music_duck_level <= 1.0:
            raise ConfigurationError("MUSIC_DUCK_LEVEL must be between 0 and 1")
        if not self.soundboard_data_dir.strip():
            raise ConfigurationError("SOUNDBOARD_DATA_DIR cannot be empty")
        try:
            Path(self.soundboard_data_dir)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("SOUNDBOARD_DATA_DIR is not a valid path") from exc
        if not MIN_SOUNDBOARD_SECONDS <= self.soundboard_max_seconds <= MAX_SOUNDBOARD_SECONDS:
            raise ConfigurationError(
                "SOUNDBOARD_MAX_SECONDS must be between "
                f"{MIN_SOUNDBOARD_SECONDS} and {MAX_SOUNDBOARD_SECONDS}"
            )
        if not MIN_SOUNDBOARD_SOUNDS <= self.soundboard_max_sounds <= MAX_SOUNDBOARD_SOUNDS:
            raise ConfigurationError(
                "SOUNDBOARD_MAX_SOUNDS must be between "
                f"{MIN_SOUNDBOARD_SOUNDS} and {MAX_SOUNDBOARD_SOUNDS}"
            )
        if not MIN_SOUNDBOARD_BYTES <= self.soundboard_max_bytes <= MAX_SOUNDBOARD_BYTES:
            raise ConfigurationError(
                "SOUNDBOARD_MAX_BYTES must be between "
                f"{MIN_SOUNDBOARD_BYTES} and {MAX_SOUNDBOARD_BYTES}"
            )
        if not (
            MIN_SOUNDBOARD_CACHE_DAYS
            <= self.soundboard_cache_days
            <= MAX_SOUNDBOARD_CACHE_DAYS
        ):
            raise ConfigurationError(
                "SOUNDBOARD_CACHE_DAYS must be between "
                f"{MIN_SOUNDBOARD_CACHE_DAYS} and {MAX_SOUNDBOARD_CACHE_DAYS}"
            )
        if self.r2_bucket and not self.r2_endpoint:
            raise ConfigurationError("R2_ENDPOINT cannot be empty when R2 is enabled")
        try:
            path = Path(self.playlist_db_path)
            if (
                not self.playlist_db_path.strip()
                or "\x00" in self.playlist_db_path
                or path.name in {"", ":memory:"}
                or path.is_dir()
            ):
                raise ValueError("expected a database file path")
        except (TypeError, ValueError, OSError) as exc:
            raise ConfigurationError("PLAYLIST_DB_PATH must be a file path") from exc
        if not 1 <= self.playlist_max_per_user <= 100:
            raise ConfigurationError("PLAYLIST_MAX_PER_USER must be between 1 and 100")
        if not 1 <= self.playlist_max_tracks <= 1000:
            raise ConfigurationError("PLAYLIST_MAX_TRACKS must be between 1 and 1000")
        if not 1 <= self.playlist_max_server <= 100:
            raise ConfigurationError("PLAYLIST_MAX_SERVER must be between 1 and 100")
        if not 0 <= self.playlist_backup_minutes <= 10080:
            raise ConfigurationError("PLAYLIST_BACKUP_MINUTES must be between 0 and 10080")
        if self.playlist_backup_minutes and not self.r2_configured:
            raise ConfigurationError("PLAYLIST_BACKUP_MINUTES requires R2 configuration")
