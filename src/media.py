"""Media lookup and Discord audio source construction."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Any
from urllib.parse import urlparse

import discord
import yt_dlp

from .spotify import (
    SpotifyLookupError,
    SpotifyService,
    SpotifyTrack,
    is_spotify_input,
)

_log = logging.getLogger(__name__)

_SPOTIFY_MATCH_CONCURRENCY = 4
_SPOTIFY_SEARCH_LIMIT = 5
_MIN_YOUTUBE_MATCH_SCORE = 25
_TITLE_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_PARENTHETICAL_RE = re.compile(r"[\(\[\{].*?[\)\]\}]")
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring)\.?\b.*", re.IGNORECASE)
_TOKEN_STOPWORDS = frozenset({"a", "an", "and", "of", "the", "to"})
_PENALTY_PHRASES = (
    "karaoke",
    "cover",
    "nightcore",
    "slowed",
    "reverb",
    "sped up",
    "speed up",
    "8d",
    "mashup",
    "mash up",
    "remix",
    "bootleg",
    "instrumental",
    "piano",
    "live",
    "concert",
    "1 hour",
    "10 hour",
    "hour version",
    "full album",
)
_BONUS_PHRASES = (
    "official audio",
    "official video",
    "official music video",
    "lyric video",
    "audio",
)


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


class MediaURLBlockedError(MediaExtractionError):
    """Raised when a user-supplied URL targets a blocked network address."""


def _validate_url(url: str) -> None:
    """Block URLs that resolve to private, loopback, or reserved addresses.

    Prevents server-side request forgery (SSRF) by verifying that the
    hostname in *url* does not resolve to an internal or cloud-metadata
    IP address before handing the URL to yt-dlp.

    Raises :class:`MediaURLBlockedError` if the URL is unsafe.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        raise MediaURLBlockedError("URL không hợp lệ")

    if not hostname:
        raise MediaURLBlockedError("URL không hợp lệ")

    # Try direct IP literal first (avoids unnecessary DNS lookup).
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        # Hostname is not an IP literal – resolve via DNS.
        try:
            resolved = socket.getaddrinfo(
                hostname, None, proto=socket.IPPROTO_TCP
            )
        except socket.gaierror:
            # Cannot resolve – let yt-dlp handle the error naturally.
            return
        if not resolved:
            return
        addr = ipaddress.ip_address(resolved[0][4][0])

    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        _log.warning("Blocked SSRF attempt to %s (%s)", hostname, addr)
        raise MediaURLBlockedError("URL này không được hỗ trợ")


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
    uploader: str | None = None


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

    def __init__(self, spotify: SpotifyService | None = None) -> None:
        self._spotify = spotify if spotify is not None else SpotifyService()

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
        if is_spotify_input(normalized_query):
            if HTTP_URL_PATTERN.match(normalized_query):
                _validate_url(normalized_query)
            return await self._prepare_spotify(normalized_query, effective_limit)
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

        _validate_url(normalized_query)
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
        if HTTP_URL_PATTERN.match(query):
            _validate_url(query)
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
            uploader = entry.get("channel") or entry.get("uploader")
            results.append(
                SearchResult(
                    title=entry.get("title") or "Không có tiêu đề",
                    url=url,
                    duration=_duration_as_int(entry.get("duration")),
                    uploader=uploader if isinstance(uploader, str) else None,
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

    async def _prepare_spotify(
        self,
        query: str,
        playlist_limit: int,
    ) -> MediaBatch:
        try:
            collection = await self._spotify.lookup(
                query,
                playlist_limit=playlist_limit,
            )
        except SpotifyLookupError as exc:
            raise MediaExtractionError(str(exc)) from exc

        matched = await self._match_spotify_tracks(collection.tracks)
        items: list[QueuedTrack] = []
        youtube_skipped = 0
        for item in matched:
            if item is None:
                youtube_skipped += 1
                continue
            items.append(item)

        if not items:
            if collection.is_playlist:
                raise MediaExtractionError("Playlist không có video khả dụng")
            raise MediaExtractionError("Không tìm thấy kết quả YouTube")

        return MediaBatch(
            items=tuple(items),
            is_playlist=collection.is_playlist,
            skipped=collection.skipped + youtube_skipped,
            truncated=collection.truncated,
        )

    async def _match_spotify_tracks(
        self,
        tracks: tuple[SpotifyTrack, ...],
    ) -> list[QueuedTrack | None]:
        semaphore = asyncio.Semaphore(_SPOTIFY_MATCH_CONCURRENCY)

        async def match(track: SpotifyTrack) -> QueuedTrack | None:
            async with semaphore:
                result = await self._search_youtube_match(track)
            if result is None:
                _log.info(
                    "No YouTube match for Spotify track %r",
                    track.display_title,
                )
                return None
            return QueuedTrack(
                title=track.display_title,
                webpage_url=result.url,
                duration=(
                    result.duration
                    if result.duration is not None
                    else track.duration
                ),
            )

        return list(await asyncio.gather(*(match(track) for track in tracks)))

    async def _search_youtube_match(
        self,
        track: SpotifyTrack,
    ) -> SearchResult | None:
        queries = [track.search_query]
        if track.artists:
            alternate = f"{track.title} {track.artists[0]}".strip()
            if alternate and alternate.casefold() != track.search_query.casefold():
                queries.append(alternate)

        for query in queries:
            try:
                results = await self.search(query, limit=_SPOTIFY_SEARCH_LIMIT)
            except MediaExtractionError:
                _log.info(
                    "YouTube search failed for Spotify track %r",
                    track.display_title,
                )
                continue
            picked = pick_youtube_match(track, results)
            if picked is not None:
                return picked
        return None


def pick_youtube_match(
    track: SpotifyTrack,
    results: Sequence[SearchResult],
) -> SearchResult | None:
    """Pick the YouTube result that best matches a Spotify catalog track.

    Scoring uses title/artist tokens, duration closeness, and penalties for
    karaoke/cover/live/remix uploads. Returns ``None`` when no candidate is
    confident enough; playlist matching should skip that track.
    """
    best: tuple[int, int, SearchResult] | None = None
    for index, result in enumerate(results):
        score = _youtube_match_score(track, result, index)
        if score is None:
            continue
        ranked = (score, -index, result)
        if best is None or ranked[0] > best[0] or (
            ranked[0] == best[0] and ranked[1] > best[1]
        ):
            best = ranked
    if best is None or best[0] < _MIN_YOUTUBE_MATCH_SCORE:
        return None
    return best[2]


def _youtube_match_score(
    track: SpotifyTrack,
    result: SearchResult,
    index: int,
) -> int | None:
    result_title = result.title.strip()
    if not result_title:
        return None

    duration_score = _duration_match_score(track.duration, result.duration)
    if duration_score is None:
        return None

    haystack = _fold_text(f"{result_title} {result.uploader or ''}")
    haystack_tokens = set(haystack.split())
    title_core = _core_title(track.title)
    title_tokens = _significant_tokens(title_core)
    artist_tokens = _significant_tokens(
        _fold_text(" ".join(track.artists))
    )

    score = duration_score + max(0, 5 - index)
    if title_core and _contains_phrase(haystack, title_core):
        score += 30
    if title_tokens:
        matched_title = sum(1 for token in title_tokens if token in haystack_tokens)
        score += int(25 * matched_title / len(title_tokens))
        if matched_title == len(title_tokens):
            score += 10
    if artist_tokens:
        matched_artists = sum(1 for token in artist_tokens if token in haystack_tokens)
        if matched_artists:
            score += 10 + int(15 * matched_artists / len(artist_tokens))

    source_blob = _fold_text(f"{track.title} {' '.join(track.artists)}")
    result_blob = _fold_text(result_title)
    for phrase in _PENALTY_PHRASES:
        if _contains_phrase(result_blob, phrase) and not _contains_phrase(
            source_blob, phrase
        ):
            score -= 30
    for phrase in _BONUS_PHRASES:
        if _contains_phrase(result_blob, phrase):
            score += 8
            break
    uploader = _fold_text(result.uploader or "")
    if uploader.endswith("topic") or "vevo" in uploader.split():
        score += 12

    if len(title_tokens) <= 1 and not artist_tokens and duration_score < 20:
        return None
    if len(title_tokens) <= 1 and artist_tokens:
        if not any(token in haystack_tokens for token in artist_tokens):
            if duration_score < 30:
                return None
    return score


def _duration_match_score(
    expected: int | None,
    actual: int | None,
) -> int | None:
    if expected is None or actual is None:
        return 0
    if expected < 0 or actual < 0:
        return None
    delta = abs(expected - actual)
    limit = max(45, int(expected * 0.25))
    if delta > limit:
        return None
    if delta <= 5:
        return 35
    if delta <= 12:
        return 22
    if delta <= 25:
        return 10
    return 0


def _fold_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_TITLE_TOKEN_RE.findall(text))


def _contains_phrase(haystack: str, phrase: str) -> bool:
    needle = _fold_text(phrase)
    if not needle or not haystack:
        return False
    return f" {needle} " in f" {haystack} "


def _core_title(value: str) -> str:
    stripped = _PARENTHETICAL_RE.sub(" ", value)
    stripped = _FEAT_RE.sub(" ", stripped)
    return _fold_text(stripped)


def _significant_tokens(value: str) -> tuple[str, ...]:
    tokens = []
    seen: set[str] = set()
    for token in value.split():
        if token in _TOKEN_STOPWORDS or token in seen:
            continue
        if len(token) < 2 and not token.isdigit():
            continue
        seen.add(token)
        tokens.append(token)
    return tuple(tokens)


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
