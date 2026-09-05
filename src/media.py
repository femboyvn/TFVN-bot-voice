"""Media lookup and Discord audio source construction."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from itertools import islice
from typing import Any

import discord
import yt_dlp


YTDL_FORMAT_OPTIONS: dict[str, Any] = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "no_warnings": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["android_tv", "ios", "android"],
        }
    },
}

YTDL_SEARCH_OPTIONS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "noplaylist": True,
}

YTDL_PREPARE_OPTIONS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "noplaylist": False,
}

PLAYLIST_LIMIT = 25

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

JUMP_TIMESTAMP_PATTERN = re.compile(
    r"(?P<hours>[0-9]{2}):(?P<minutes>[0-5][0-9]):(?P<seconds>[0-5][0-9])"
)
HTTP_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)

_UNAVAILABLE_AVAILABILITIES = frozenset(
    {
        "needs_auth",
        "premium_only",
        "private",
        "subscriber_only",
    }
)
_UNAVAILABLE_TITLES = frozenset(
    {
        "[deleted video]",
        "[private video]",
        "deleted video",
        "private video",
    }
)


class MediaExtractionError(RuntimeError):
    """Raised when media metadata cannot be extracted."""


@dataclass(frozen=True, slots=True)
class Track:
    title: str
    stream_url: str
    webpage_url: str | None = None
    duration: int | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    duration: int | None = None


@dataclass(frozen=True, slots=True)
class QueuedTrack:
    """Stable media metadata safe to keep in a long-lived playback queue."""

    title: str
    webpage_url: str
    duration: int | None = None


@dataclass(frozen=True, slots=True)
class MediaBatch:
    """One atomic group of queue entries prepared from a user input."""

    items: tuple[QueuedTrack, ...]
    is_playlist: bool = False
    skipped: int = 0
    truncated: bool = False


class MediaService:
    """Runs blocking yt-dlp extraction outside the Discord event loop."""

    async def prepare(
        self,
        query: str,
        *,
        playlist_limit: int = PLAYLIST_LIMIT,
    ) -> MediaBatch:
        """Prepare stable queue metadata without retaining an audio stream URL."""
        normalized_query = query.strip()
        if not normalized_query:
            raise MediaExtractionError("Vui lòng nhập tên bài hát hoặc URL")
        if playlist_limit < 1:
            raise ValueError("playlist_limit must be positive")

        effective_limit = min(playlist_limit, PLAYLIST_LIMIT)
        if not HTTP_URL_PATTERN.match(normalized_query):
            results = await self.search(normalized_query, limit=1)
            if not results:
                raise MediaExtractionError("Không tìm thấy kết quả YouTube")
            first = results[0]
            return MediaBatch(
                items=(
                    QueuedTrack(
                        title=first.title,
                        webpage_url=first.url,
                        duration=first.duration,
                    ),
                )
            )

        try:
            data = await asyncio.to_thread(
                self._prepare_url,
                normalized_query,
                effective_limit,
            )
        except yt_dlp.utils.DownloadError as exc:
            raise MediaExtractionError("Không thể tải media đó") from exc

        return self._batch_from_url_data(
            data,
            source_url=normalized_query,
            playlist_limit=effective_limit,
        )

    async def resolve(self, query: str) -> Track:
        try:
            data = await asyncio.to_thread(self._extract, query)
        except yt_dlp.utils.DownloadError as exc:
            raise MediaExtractionError("Không thể tải media đó") from exc

        if data.get("entries"):
            data = data["entries"][0]

        stream_url = data.get("url")
        if not stream_url:
            raise MediaExtractionError("Nguồn media không cung cấp luồng âm thanh")

        return Track(
            title=data.get("title") or "Không có tiêu đề",
            stream_url=stream_url,
            webpage_url=data.get("webpage_url") or data.get("original_url"),
            duration=_duration_as_int(data.get("duration")),
        )

    async def resolve_queued(self, item: QueuedTrack) -> Track:
        """Resolve a fresh playable stream for an item immediately before use."""
        track = await self.resolve(item.webpage_url)
        return Track(
            title=track.title,
            stream_url=track.stream_url,
            webpage_url=track.webpage_url or item.webpage_url,
            duration=track.duration if track.duration is not None else item.duration,
        )

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        try:
            data = await asyncio.to_thread(self._search, query, limit)
        except yt_dlp.utils.DownloadError as exc:
            raise MediaExtractionError("Tìm kiếm YouTube thất bại") from exc

        if not isinstance(data, dict):
            raise MediaExtractionError("Kết quả tìm kiếm YouTube không hợp lệ")

        results: list[SearchResult] = []
        for entry in data.get("entries") or []:
            if len(results) >= limit:
                break
            if not isinstance(entry, dict):
                continue
            url = entry.get("webpage_url") or entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            if not url.startswith(("http://", "https://")) and entry.get("id"):
                url = f"https://www.youtube.com/watch?v={entry['id']}"
            results.append(
                SearchResult(
                    title=entry.get("title") or "Không có tiêu đề",
                    url=url,
                    duration=_duration_as_int(entry.get("duration")),
                )
            )
        return results

    def create_audio_source(
        self,
        track: Track,
        *,
        volume: float,
        start_at: int | None = None,
    ) -> discord.PCMVolumeTransformer:
        if start_at is not None and start_at < 0:
            raise ValueError("start_at cannot be negative")

        ffmpeg_options = dict(FFMPEG_OPTIONS)
        if start_at:
            ffmpeg_options["before_options"] = (
                f"{ffmpeg_options['before_options']} -ss {start_at}"
            )

        source = discord.FFmpegPCMAudio(track.stream_url, **ffmpeg_options)
        return discord.PCMVolumeTransformer(source, volume=volume)

    @staticmethod
    def _extract(query: str) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS) as ydl:
            return ydl.extract_info(query, download=False)

    @staticmethod
    def _search(query: str, limit: int) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS) as ydl:
            return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    @staticmethod
    def _prepare_url(url: str, playlist_limit: int) -> dict[str, Any]:
        options = dict(YTDL_PREPARE_OPTIONS)
        options["playlistend"] = playlist_limit
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)

    @staticmethod
    def _batch_from_url_data(
        data: dict[str, Any],
        *,
        source_url: str,
        playlist_limit: int,
    ) -> MediaBatch:
        if not isinstance(data, dict):
            raise MediaExtractionError("Nguồn media không có thông tin hợp lệ")

        is_playlist = data.get("_type") in {"multi_video", "playlist"} or (
            "entries" in data
        )
        if not is_playlist:
            if _entry_is_unavailable(data):
                raise MediaExtractionError("Nguồn media không có thông tin hợp lệ")
            item = _queued_track_from_entry(data, fallback_url=source_url)
            if item is None:
                raise MediaExtractionError("Nguồn media không có thông tin hợp lệ")
            return MediaBatch(items=(item,))

        raw_entries = data.get("entries") or ()
        try:
            returned_count = len(raw_entries)
        except TypeError:
            returned_count = None
        try:
            inspected_entries = islice(iter(raw_entries), playlist_limit)
        except TypeError as exc:
            raise MediaExtractionError(
                "Playlist không có thông tin hợp lệ"
            ) from exc
        declared_count = _positive_int(
            data.get("playlist_count") or data.get("n_entries")
        )
        truncated = (
            returned_count is not None and returned_count > playlist_limit
        ) or (
            declared_count is not None
            and declared_count > playlist_limit
        )

        items: list[QueuedTrack] = []
        skipped = 0
        for entry in inspected_entries:
            if not isinstance(entry, dict) or _entry_is_unavailable(entry):
                skipped += 1
                continue
            item = _queued_track_from_entry(entry)
            if item is None:
                skipped += 1
                continue
            items.append(item)

        if not items:
            raise MediaExtractionError("Playlist không có video khả dụng")

        return MediaBatch(
            items=tuple(items),
            is_playlist=True,
            skipped=skipped,
            truncated=truncated,
        )


def format_duration(duration: int | None) -> str:
    if duration is None:
        return ""
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def parse_jump_timestamp(value: str) -> int | None:
    """Parse a strict ``HH:MM:SS`` jump target into whole seconds."""
    match = JUMP_TIMESTAMP_PATTERN.fullmatch(value)
    if match is None:
        return None

    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    return hours * 3600 + minutes * 60 + seconds


def _duration_as_int(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _entry_is_unavailable(entry: dict[str, Any]) -> bool:
    if entry.get("is_unavailable") is True:
        return True

    availability = entry.get("availability")
    if (
        isinstance(availability, str)
        and availability.casefold() in _UNAVAILABLE_AVAILABILITIES
    ):
        return True

    title = entry.get("title")
    return (
        isinstance(title, str)
        and title.strip().casefold() in _UNAVAILABLE_TITLES
    )


def _queued_track_from_entry(
    entry: dict[str, Any],
    *,
    fallback_url: str | None = None,
) -> QueuedTrack | None:
    if not any(
        entry.get(key)
        for key in ("id", "original_url", "title", "url", "webpage_url")
    ):
        return None

    webpage_url = _webpage_url_from_entry(
        entry,
        fallback_url=fallback_url,
    )
    if not webpage_url or not HTTP_URL_PATTERN.match(webpage_url):
        return None

    return QueuedTrack(
        title=entry.get("title") or "Không có tiêu đề",
        webpage_url=webpage_url,
        duration=_duration_as_int(entry.get("duration")),
    )


def _webpage_url_from_entry(
    entry: dict[str, Any],
    *,
    fallback_url: str | None = None,
) -> str | None:
    for key in ("webpage_url", "original_url"):
        value = entry.get(key)
        if isinstance(value, str) and HTTP_URL_PATTERN.match(value):
            return value

    if fallback_url and HTTP_URL_PATTERN.match(fallback_url):
        return fallback_url

    url = entry.get("url")
    if isinstance(url, str) and HTTP_URL_PATTERN.match(url):
        return url

    entry_id = entry.get("id")
    if isinstance(entry_id, str) and entry_id:
        return f"https://www.youtube.com/watch?v={entry_id}"
    return None
