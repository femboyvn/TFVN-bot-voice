"""Per-guild custom soundboard: JSON index, MP3 files, and URL ingest.

Durable copies live on local disk, or on Cloudflare R2 when configured.
The server keeps a play cache of MP3s and drops files unused for the
configured TTL (default 7 days) only when R2 is the source of truth.
Network and FFmpeg work is injectable so tests never need the real sites
or encoder.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .media import MediaURLBlockedError, _validate_url
from .spotify import is_spotify_input

log = logging.getLogger(__name__)

INDEX_VERSION = 1
INDEX_FILENAME = "index.json"
SECONDS_PER_DAY = 86400
SOUND_ID_RE = re.compile(r"^[0-9a-f]{8}$")
MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 32
MIN_DURATION_MS = 200
HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = "TFVN-bot-voice/2.0"
FETCH_CHUNK_SIZE = 64 * 1024

SoundKind = Literal["myinstants", "youtube", "direct"]

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)
MYINSTANTS_HOSTS = frozenset({"myinstants.com", "www.myinstants.com"})

_OG_AUDIO_RE = re.compile(
    r"<meta\b[^>]*\b(?:property|name)=['\"]og:audio['\"][^>]*\bcontent=['\"]([^'\"]+)['\"]"
    r"|"
    r"<meta\b[^>]*\bcontent=['\"]([^'\"]+)['\"][^>]*\b(?:property|name)=['\"]og:audio['\"]",
    re.IGNORECASE,
)
_MEDIA_MP3_RE = re.compile(
    r"https?://(?:www\.)?myinstants\.com/media/sounds/[^\s\"'<>]+\.mp3",
    re.IGNORECASE,
)
_AUDIO_MAGIC = (
    b"ID3",
    b"OggS",
    b"fLaC",
    b"RIFF",
    bytes([0xFF, 0xFB]),
    bytes([0xFF, 0xF3]),
    bytes([0xFF, 0xF2]),
)

FetchFn = Callable[[str, int], tuple[bytes, str]]
YtdlpFn = Callable[[str, Path, int], Path]
EncodeFn = Callable[[Path, Path, int], int]


class ObjectStore(Protocol):
    """Bytes in/out for a remote bucket. Keys are relative (``guild/file``)."""

    def get_bytes(self, key: str) -> bytes | None: ...

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
    ) -> None: ...

    def delete_key(self, key: str) -> None: ...


class DictObjectStore:
    """In-memory object store used by tests as a stand-in for R2."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def get_bytes(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
    ) -> None:
        self.objects[key] = data
        self.content_types[key] = content_type

    def delete_key(self, key: str) -> None:
        self.objects.pop(key, None)
        self.content_types.pop(key, None)


def remote_object_key(prefix: str, relative: str) -> str:
    """Join an optional bucket prefix with a guild-relative object key."""
    prefix = prefix.strip("/")
    relative = relative.lstrip("/")
    if not relative:
        raise ValueError("object key cannot be empty")
    return f"{prefix}/{relative}" if prefix else relative


class S3ObjectStore:
    """S3-compatible backend (Cloudflare R2) around an injected boto3 client."""

    def __init__(
        self,
        client: object,
        bucket: str,
        *,
        prefix: str = "soundboard",
    ) -> None:
        if not bucket:
            raise ValueError("bucket is required")
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    @classmethod
    def from_settings(cls, settings: object) -> S3ObjectStore:
        """Build a live R2 client from validated application settings."""
        import boto3
        from botocore.config import Config as BotoConfig

        endpoint = str(getattr(settings, "r2_endpoint"))
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=str(getattr(settings, "r2_access_key_id")),
            aws_secret_access_key=str(getattr(settings, "r2_secret_access_key")),
            region_name="auto",
            config=BotoConfig(signature_version="s3v4"),
        )
        return cls(
            client,
            str(getattr(settings, "r2_bucket")),
            prefix=str(getattr(settings, "r2_prefix") or "soundboard"),
        )

    def _full(self, key: str) -> str:
        return remote_object_key(self._prefix, key)

    def get_bytes(self, key: str) -> bytes | None:
        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=self._full(key),
            )
            body = response["Body"]
            return body.read()
        except Exception as exc:
            if _is_missing_object(exc):
                return None
            raise SoundboardError("Không tải được âm thanh từ kho.") from exc

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
    ) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=self._full(key),
                Body=data,
                ContentType=content_type,
            )
        except Exception as exc:
            raise SoundboardError("Không lưu được âm thanh lên kho.") from exc

    def delete_key(self, key: str) -> None:
        try:
            self._client.delete_object(
                Bucket=self._bucket,
                Key=self._full(key),
            )
        except Exception as exc:
            if _is_missing_object(exc):
                return
            raise SoundboardError("Không xóa được âm thanh trên kho.") from exc


def _is_missing_object(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code") or "")
        if code in {"NoSuchKey", "404", "NotFound", "NoSuchBucket"}:
            return True
    name = type(exc).__name__
    return name in {"NoSuchKey", "404"}


class SoundboardError(RuntimeError):
    """Requester-facing soundboard failure. ``str(exc)`` is Vietnamese UI copy."""


class SoundboardCorruptError(SoundboardError):
    """The guild index exists but cannot be parsed; refuse mutations."""


@dataclass(frozen=True, slots=True)
class SoundboardEntry:
    """One saved clip in a guild library."""

    id: str
    name: str
    mp3: str
    source_url: str
    duration_ms: int
    added_by: int
    added_at: str


def format_clip_duration(duration_ms: int) -> str:
    """Render a short clip length for Vietnamese confirmations."""
    seconds = max(0.0, duration_ms / 1000.0)
    if seconds < 10:
        rendered = f"{seconds:.1f}".rstrip("0").rstrip(".")
        return f"{rendered}s"
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def normalize_sound_name(value: str) -> str:
    """Trim and validate a clip name unique-key."""
    name = unicodedata.normalize("NFC", value).strip()
    if not MIN_NAME_LENGTH <= len(name) <= MAX_NAME_LENGTH:
        raise SoundboardError("Tên âm thanh phải từ 2 đến 32 ký tự.")
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise SoundboardError("Tên âm thanh không hợp lệ.")
    return name


def can_delete_sound(
    entry: SoundboardEntry,
    *,
    user_id: int,
    manage_guild: bool,
) -> bool:
    """True when the clicker added the clip or can manage the server."""
    return bool(manage_guild) or entry.added_by == user_id


def classify_soundboard_url(url: str) -> SoundKind:
    """Return the ingest kind for a user-supplied URL.

    Raises :class:`SoundboardError` for Spotify, playlists, or blocked hosts.
    """
    normalized = url.strip()
    if not normalized:
        raise SoundboardError("Vui lòng dán URL MyInstants, YouTube, hoặc tệp âm thanh.")
    if is_spotify_input(normalized):
        raise SoundboardError(
            "Spotify dùng cho hàng đợi nhạc, không dùng cho bảng âm thanh."
        )
    try:
        _validate_url(normalized)
    except MediaURLBlockedError as exc:
        raise SoundboardError(str(exc)) from exc

    parsed = urlparse(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise SoundboardError("URL không hợp lệ.")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if host in MYINSTANTS_HOSTS:
        return "myinstants"
    if host in YOUTUBE_HOSTS:
        if "/playlist" in path:
            raise SoundboardError("Bảng âm thanh không nhận playlist.")
        return "youtube"
    return "direct"


def parse_myinstants_audio_url(html: str) -> str | None:
    """Extract the MP3 URL from a MyInstants instant page."""
    match = _OG_AUDIO_RE.search(html)
    if match:
        found = match.group(1) or match.group(2)
        if found:
            return found.strip()
    media = _MEDIA_MP3_RE.search(html)
    if media:
        return media.group(0)
    return None


def ffmpeg_encode_command(src: Path, dest: Path, max_seconds: int) -> list[str]:
    """Build the FFmpeg argv that trims and encodes a clip to MP3."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-t",
        str(max_seconds),
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "libmp3lame",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "128k",
        str(dest),
    ]


def ffprobe_duration_command(path: Path) -> list[str]:
    """Build the ffprobe argv that reports duration in seconds."""
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]


def looks_like_audio(data: bytes, content_type: str) -> bool:
    """True when headers or magic bytes indicate an audio payload."""
    if not data:
        return False
    media_type = content_type.lower().split(";", 1)[0].strip()
    if media_type.startswith("audio/"):
        return True
    if data.startswith(b"ftyp") or b"ftyp" in data[:12]:
        return True
    return any(data.startswith(magic) for magic in _AUDIO_MAGIC)


def default_fetch_bytes(url: str, max_bytes: int) -> tuple[bytes, str]:
    """HTTP GET with a hard size cap. Used for MyInstants pages and direct audio."""
    try:
        _validate_url(url)
    except MediaURLBlockedError as exc:
        raise SoundboardError(str(exc)) from exc
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT},
        method="GET",
    )
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            content_type = str(response.headers.get("Content-Type") or "")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(FETCH_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SoundboardError("Tệp âm thanh quá lớn.")
                chunks.append(chunk)
    except SoundboardError:
        raise
    except Exception as exc:
        raise SoundboardError("Không tải được âm thanh từ URL đó.") from exc
    return b"".join(chunks), content_type


def default_ytdlp_download(url: str, dest_dir: Path, max_seconds: int) -> Path:
    """Download the first ``max_seconds`` of YouTube audio into *dest_dir*."""
    import yt_dlp

    def _ranges(_info: object, _ydl: object) -> list[dict[str, float]]:
        return [{"start_time": 0.0, "end_time": float(max_seconds)}]

    dest_dir.mkdir(parents=True, exist_ok=True)
    template = str(dest_dir / "source.%(ext)s")
    options = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": template,
        "overwrites": True,
        "download_ranges": _ranges,
        "force_keyframes_at_cuts": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
    except Exception as exc:
        raise SoundboardError("Không tải được âm thanh YouTube.") from exc

    matches = sorted(
        path
        for path in dest_dir.iterdir()
        if path.is_file() and path.name.startswith("source.")
    )
    if not matches:
        raise SoundboardError("Không tải được âm thanh YouTube.")
    return matches[0]


def default_ffmpeg_encode(src: Path, dest: Path, max_seconds: int) -> int:
    """Encode *src* to MP3 and return duration in milliseconds."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = ffmpeg_encode_command(src, dest, max_seconds)
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise SoundboardError("Không mã hóa được âm thanh.") from exc
    except subprocess.CalledProcessError as exc:
        raise SoundboardError("Không mã hóa được âm thanh.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SoundboardError("Mã hóa âm thanh quá lâu.") from exc

    probe = ffprobe_duration_command(dest)
    try:
        result = subprocess.run(
            probe,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SoundboardError("Không đọc được độ dài âm thanh.") from exc
    try:
        seconds = float(result.stdout.strip())
    except ValueError as exc:
        raise SoundboardError("Không đọc được độ dài âm thanh.") from exc
    if not (seconds > 0):
        raise SoundboardError("Âm thanh quá ngắn.")
    return int(seconds * 1000)


class SoundboardStore:
    """Atomic JSON + MP3 library, one directory per guild.

    When *remote* is set (R2), that bucket is the source of truth and the
    local directory is a play cache. Unused cached MP3s older than
    *cache_ttl_seconds* are deleted. With no remote, local files are durable
    and are never expired.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_sounds: int,
        remote: ObjectStore | None = None,
        cache_ttl_seconds: float = 0.0,
    ) -> None:
        self.root = root
        self.max_sounds = max_sounds
        self.remote = remote
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def guild_dir(self, guild_id: int) -> Path:
        """Return the resolved guild directory, jailed under :attr:`root`."""
        if not isinstance(guild_id, int) or guild_id < 0:
            raise SoundboardError("Máy chủ không hợp lệ.")
        root = self.root.expanduser().resolve()
        path = (root / str(guild_id)).resolve()
        if not path.is_relative_to(root):
            raise SoundboardError("Đường dẫn không hợp lệ.")
        return path

    def index_path(self, guild_id: int) -> Path:
        return self.guild_dir(guild_id) / INDEX_FILENAME

    def mp3_path(self, guild_id: int, entry: SoundboardEntry) -> Path | None:
        """Return the cached clip path when it exists and is not expired."""
        path = self._cached_mp3_path(guild_id, entry)
        if path is None or not path.is_file():
            return None
        if self._cache_expired(path):
            return None
        return path

    async def ensure_playable(
        self,
        guild_id: int,
        entry: SoundboardEntry,
    ) -> Path | None:
        """Return a local MP3 path, hydrating from R2 and touching last-used."""
        async with self._lock(guild_id):
            return await asyncio.to_thread(
                self._ensure_playable_locked,
                guild_id,
                entry,
            )

    async def list(self, guild_id: int) -> tuple[SoundboardEntry, ...]:
        async with self._lock(guild_id):
            return await asyncio.to_thread(self._list_locked, guild_id)

    async def get(self, guild_id: int, sound_id: str) -> SoundboardEntry | None:
        entries = await self.list(guild_id)
        for entry in entries:
            if entry.id == sound_id:
                return entry
        return None

    async def commit_new(
        self,
        guild_id: int,
        entry: SoundboardEntry,
        source_mp3: Path,
    ) -> SoundboardEntry:
        """Move *source_mp3* into the guild dir and append *entry* to the index."""
        async with self._lock(guild_id):
            return await asyncio.to_thread(
                self._commit_new_locked,
                guild_id,
                entry,
                source_mp3,
            )

    async def remove(self, guild_id: int, sound_id: str) -> SoundboardEntry | None:
        async with self._lock(guild_id):
            return await asyncio.to_thread(self._remove_locked, guild_id, sound_id)

    def _remote_key(self, guild_id: int, name: str) -> str:
        return f"{guild_id}/{name}"

    def _cached_mp3_path(
        self,
        guild_id: int,
        entry: SoundboardEntry,
    ) -> Path | None:
        if not SOUND_ID_RE.fullmatch(entry.id):
            return None
        if entry.mp3 != f"{entry.id}.mp3":
            return None
        directory = self.guild_dir(guild_id)
        path = (directory / entry.mp3).resolve()
        if not path.is_relative_to(directory):
            return None
        return path

    def _cache_expired(self, path: Path) -> bool:
        if self.remote is None or self.cache_ttl_seconds <= 0:
            return False
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return True
        return age >= self.cache_ttl_seconds

    def _touch(self, path: Path) -> None:
        now = time.time()
        with contextlib.suppress(OSError):
            os.utime(path, (now, now))

    def _evict_expired(self, guild_id: int) -> int:
        if self.remote is None or self.cache_ttl_seconds <= 0:
            return 0
        directory = self.guild_dir(guild_id)
        if not directory.is_dir():
            return 0
        removed = 0
        for path in directory.glob("*.mp3"):
            if self._cache_expired(path):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _list_locked(self, guild_id: int) -> tuple[SoundboardEntry, ...]:
        self._evict_expired(guild_id)
        return self._load(guild_id)

    def _ensure_playable_locked(
        self,
        guild_id: int,
        entry: SoundboardEntry,
    ) -> Path | None:
        self._evict_expired(guild_id)
        path = self._cached_mp3_path(guild_id, entry)
        if path is None:
            return None
        if path.is_file() and not self._cache_expired(path):
            self._touch(path)
            return path
        if self.remote is None:
            return None
        data = self.remote.get_bytes(self._remote_key(guild_id, entry.mp3))
        if not data:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            prefix=f".{entry.id}.",
            suffix=".mp3",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        self._touch(path)
        return path

    def _load(self, guild_id: int) -> tuple[SoundboardEntry, ...]:
        if self.remote is not None:
            try:
                raw = self.remote.get_bytes(
                    self._remote_key(guild_id, INDEX_FILENAME)
                )
            except SoundboardError:
                raise
            except Exception as exc:
                log.warning(
                    "R2 index fetch failed for guild %s: %s",
                    guild_id,
                    exc,
                )
                raw = None
            if raw is not None:
                entries = _parse_index_bytes(raw)
                with contextlib.suppress(OSError, SoundboardError):
                    self._write_index(guild_id, entries, sync_remote=False)
                return entries
        return self._load_local(guild_id)

    def _load_local(self, guild_id: int) -> tuple[SoundboardEntry, ...]:
        path = self.index_path(guild_id)
        if not path.is_file():
            return ()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SoundboardCorruptError(
                "Thư viện âm thanh bị hỏng. Hãy liên hệ quản trị viên."
            ) from exc
        return _parse_index_bytes(raw)

    def _commit_new_locked(
        self,
        guild_id: int,
        entry: SoundboardEntry,
        source_mp3: Path,
    ) -> SoundboardEntry:
        entries = self._load(guild_id)
        if len(entries) >= self.max_sounds:
            raise SoundboardError(
                f"Thư viện đã đủ {self.max_sounds} âm thanh."
            )
        folded = entry.name.casefold()
        if any(existing.name.casefold() == folded for existing in entries):
            raise SoundboardError("Đã có âm thanh trùng tên.")
        if any(existing.id == entry.id for existing in entries):
            raise SoundboardError("Không lưu được âm thanh. Hãy thử lại.")
        if not SOUND_ID_RE.fullmatch(entry.id) or entry.mp3 != f"{entry.id}.mp3":
            raise SoundboardError("Không lưu được âm thanh. Hãy thử lại.")

        directory = self.guild_dir(guild_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = (directory / entry.mp3).resolve()
        if not destination.is_relative_to(directory):
            raise SoundboardError("Đường dẫn không hợp lệ.")
        updated = entries + (entry,)
        if self.remote is not None:
            data = source_mp3.read_bytes()
            mp3_key = self._remote_key(guild_id, entry.mp3)
            self.remote.put_bytes(
                mp3_key,
                data,
                content_type="audio/mpeg",
            )
            try:
                self.remote.put_bytes(
                    self._remote_key(guild_id, INDEX_FILENAME),
                    _index_bytes(updated),
                    content_type="application/json",
                )
            except Exception:
                with contextlib.suppress(Exception):
                    self.remote.delete_key(mp3_key)
                raise
        os.replace(source_mp3, destination)
        try:
            self._write_index(guild_id, updated, sync_remote=False)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        self._touch(destination)
        return entry

    def _remove_locked(self, guild_id: int, sound_id: str) -> SoundboardEntry | None:
        if not SOUND_ID_RE.fullmatch(sound_id):
            return None
        entries = self._load(guild_id)
        kept: list[SoundboardEntry] = []
        removed: SoundboardEntry | None = None
        for entry in entries:
            if entry.id == sound_id and removed is None:
                removed = entry
            else:
                kept.append(entry)
        if removed is None:
            return None
        kept_entries = tuple(kept)
        if self.remote is not None:
            self.remote.put_bytes(
                self._remote_key(guild_id, INDEX_FILENAME),
                _index_bytes(kept_entries),
                content_type="application/json",
            )
            with contextlib.suppress(SoundboardError):
                self.remote.delete_key(self._remote_key(guild_id, removed.mp3))
        self._write_index(guild_id, kept_entries, sync_remote=False)
        cached = self._cached_mp3_path(guild_id, removed)
        if cached is not None:
            cached.unlink(missing_ok=True)
        else:
            leftover = self.guild_dir(guild_id) / f"{removed.id}.mp3"
            leftover.unlink(missing_ok=True)
        return removed

    def _write_index(
        self,
        guild_id: int,
        entries: tuple[SoundboardEntry, ...],
        *,
        sync_remote: bool = True,
    ) -> None:
        directory = self.guild_dir(guild_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = _index_bytes(entries)
        if sync_remote and self.remote is not None:
            self.remote.put_bytes(
                self._remote_key(guild_id, INDEX_FILENAME),
                payload,
                content_type="application/json",
            )
        index = directory / INDEX_FILENAME
        handle, tmp_name = tempfile.mkstemp(
            prefix=".index.",
            suffix=".tmp",
            dir=str(directory),
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, index)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


def _index_payload(entries: tuple[SoundboardEntry, ...]) -> dict[str, object]:
    return {
        "version": INDEX_VERSION,
        "sounds": [
            {
                "id": entry.id,
                "name": entry.name,
                "mp3": entry.mp3,
                "source_url": entry.source_url,
                "duration_ms": entry.duration_ms,
                "added_by": entry.added_by,
                "added_at": entry.added_at,
            }
            for entry in entries
        ],
    }


def _index_bytes(entries: tuple[SoundboardEntry, ...]) -> bytes:
    return (
        json.dumps(_index_payload(entries), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _parse_index_bytes(raw: bytes) -> tuple[SoundboardEntry, ...]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SoundboardCorruptError(
            "Thư viện âm thanh bị hỏng. Hãy liên hệ quản trị viên."
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        raise SoundboardCorruptError(
            "Thư viện âm thanh bị hỏng. Hãy liên hệ quản trị viên."
        )
    sounds = payload.get("sounds")
    if not isinstance(sounds, list):
        raise SoundboardCorruptError(
            "Thư viện âm thanh bị hỏng. Hãy liên hệ quản trị viên."
        )
    entries: list[SoundboardEntry] = []
    for item in sounds:
        parsed = _entry_from_json(item)
        if parsed is not None:
            entries.append(parsed)
    return tuple(entries)


def _entry_from_json(item: object) -> SoundboardEntry | None:
    if not isinstance(item, dict):
        return None
    sound_id = item.get("id")
    name = item.get("name")
    mp3 = item.get("mp3")
    source_url = item.get("source_url")
    duration_ms = item.get("duration_ms")
    added_by = item.get("added_by")
    added_at = item.get("added_at")
    if not isinstance(sound_id, str) or not SOUND_ID_RE.fullmatch(sound_id):
        return None
    if not isinstance(name, str) or not name:
        return None
    if mp3 != f"{sound_id}.mp3":
        return None
    if not isinstance(source_url, str) or not source_url:
        return None
    if not isinstance(duration_ms, int) or duration_ms < 0:
        return None
    if not isinstance(added_by, int):
        return None
    if not isinstance(added_at, str) or not added_at:
        return None
    return SoundboardEntry(
        id=sound_id,
        name=name,
        mp3=mp3,
        source_url=source_url,
        duration_ms=duration_ms,
        added_by=added_by,
        added_at=added_at,
    )


class SoundboardIngest:
    """Download and transcode a user URL into a local MP3."""

    def __init__(
        self,
        *,
        fetch: FetchFn | None = None,
        ytdlp: YtdlpFn | None = None,
        encode: EncodeFn | None = None,
    ) -> None:
        self._fetch = fetch or default_fetch_bytes
        self._ytdlp = ytdlp or default_ytdlp_download
        self._encode = encode or default_ffmpeg_encode

    def materialize(
        self,
        url: str,
        dest_mp3: Path,
        *,
        max_seconds: int,
        max_bytes: int,
    ) -> int:
        """Write an encoded MP3 to *dest_mp3* and return duration in milliseconds."""
        kind = classify_soundboard_url(url)
        raw_limit = max(max_bytes * 8, 8 * 1024 * 1024)
        dest_mp3.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="tfd-soundboard-",
            dir=str(dest_mp3.parent),
        ) as tmp:
            tmpdir = Path(tmp)
            source = self._download_source(
                url,
                kind,
                tmpdir,
                max_seconds=max_seconds,
                raw_limit=raw_limit,
            )
            encoded = tmpdir / "encoded.mp3"
            duration_ms = self._encode(source, encoded, max_seconds)
            if duration_ms < MIN_DURATION_MS:
                raise SoundboardError("Âm thanh quá ngắn.")
            size = encoded.stat().st_size
            if size > max_bytes:
                raise SoundboardError("Tệp âm thanh quá lớn.")
            os.replace(encoded, dest_mp3)
            return duration_ms

    def _download_source(
        self,
        url: str,
        kind: SoundKind,
        tmpdir: Path,
        *,
        max_seconds: int,
        raw_limit: int,
    ) -> Path:
        if kind == "youtube":
            return self._ytdlp(url, tmpdir, max_seconds)
        if kind == "myinstants":
            audio_url = self._myinstants_mp3_url(url, raw_limit)
            data, content_type = self._fetch(audio_url, raw_limit)
            if not looks_like_audio(data, content_type):
                raise SoundboardError("URL MyInstants không phải tệp âm thanh.")
            path = tmpdir / "source.bin"
            path.write_bytes(data)
            return path
        data, content_type = self._fetch(url, raw_limit)
        if not looks_like_audio(data, content_type):
            raise SoundboardError("URL không phải tệp âm thanh.")
        path = tmpdir / "source.bin"
        path.write_bytes(data)
        return path

    def _myinstants_mp3_url(self, url: str, raw_limit: int) -> str:
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        if "/media/sounds/" in path and path.endswith(".mp3"):
            return url
        data, _content_type = self._fetch(url, min(raw_limit, 1_000_000))
        html = data.decode("utf-8", errors="replace")
        audio_url = parse_myinstants_audio_url(html)
        if not audio_url:
            raise SoundboardError("Không tìm thấy âm thanh MyInstants.")
        try:
            _validate_url(audio_url)
        except MediaURLBlockedError as exc:
            raise SoundboardError(str(exc)) from exc
        return audio_url


class SoundboardService:
    """High-level add/list/remove used by commands and the panel."""

    def __init__(
        self,
        store: SoundboardStore,
        ingest: SoundboardIngest,
        *,
        max_seconds: int,
        max_bytes: int,
    ) -> None:
        self.store = store
        self.ingest = ingest
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes

    @classmethod
    def from_settings(cls, settings: object) -> SoundboardService:
        root = Path(str(getattr(settings, "soundboard_data_dir")))
        max_sounds = int(getattr(settings, "soundboard_max_sounds"))
        max_seconds = int(getattr(settings, "soundboard_max_seconds"))
        max_bytes = int(getattr(settings, "soundboard_max_bytes"))
        remote: ObjectStore | None = None
        cache_ttl_seconds = 0.0
        if getattr(settings, "r2_bucket", ""):
            remote = S3ObjectStore.from_settings(settings)
            days = int(getattr(settings, "soundboard_cache_days", 7))
            cache_ttl_seconds = float(days) * SECONDS_PER_DAY
        return cls(
            SoundboardStore(
                root,
                max_sounds=max_sounds,
                remote=remote,
                cache_ttl_seconds=cache_ttl_seconds,
            ),
            SoundboardIngest(),
            max_seconds=max_seconds,
            max_bytes=max_bytes,
        )

    async def ensure_playable(
        self,
        guild_id: int,
        entry: SoundboardEntry,
    ) -> Path | None:
        """Hydrate the local play cache and return a path FFmpeg can read."""
        return await self.store.ensure_playable(guild_id, entry)

    async def list(self, guild_id: int) -> tuple[SoundboardEntry, ...]:
        return await self.store.list(guild_id)

    async def add_sound(
        self,
        guild_id: int,
        *,
        name: str,
        url: str,
        added_by: int,
    ) -> SoundboardEntry:
        normalized = normalize_sound_name(name)
        classify_soundboard_url(url)
        existing = await self.store.list(guild_id)
        if len(existing) >= self.store.max_sounds:
            raise SoundboardError(
                f"Thư viện đã đủ {self.store.max_sounds} âm thanh."
            )
        if any(entry.name.casefold() == normalized.casefold() for entry in existing):
            raise SoundboardError("Đã có âm thanh trùng tên.")

        sound_id = uuid.uuid4().hex[:8]
        directory = self.store.guild_dir(guild_id)
        directory.mkdir(parents=True, exist_ok=True)
        # Stage on the destination volume so os.replace stays atomic.
        handle, tmp_name = tempfile.mkstemp(
            prefix=f"sb-{sound_id}-",
            suffix=".mp3",
            dir=str(directory),
        )
        os.close(handle)
        tmp_path = Path(tmp_name)
        try:
            duration_ms = await asyncio.to_thread(
                self.ingest.materialize,
                url.strip(),
                tmp_path,
                max_seconds=self.max_seconds,
                max_bytes=self.max_bytes,
            )
            entry = SoundboardEntry(
                id=sound_id,
                name=normalized,
                mp3=f"{sound_id}.mp3",
                source_url=url.strip(),
                duration_ms=duration_ms,
                added_by=added_by,
                added_at=datetime.now(timezone.utc).isoformat(),
            )
            try:
                return await self.store.commit_new(guild_id, entry, tmp_path)
            except SoundboardError:
                tmp_path.unlink(missing_ok=True)
                raise
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    async def remove_sound(
        self,
        guild_id: int,
        sound_id: str,
        *,
        user_id: int,
        manage_guild: bool,
    ) -> SoundboardEntry:
        entry = await self.store.get(guild_id, sound_id)
        if entry is None:
            raise SoundboardError("Âm thanh không còn tồn tại.")
        if not can_delete_sound(
            entry,
            user_id=user_id,
            manage_guild=manage_guild,
        ):
            raise SoundboardError("Bạn chỉ xóa được âm thanh do bạn thêm.")
        removed = await self.store.remove(guild_id, sound_id)
        if removed is None:
            raise SoundboardError("Âm thanh không còn tồn tại.")
        return removed
