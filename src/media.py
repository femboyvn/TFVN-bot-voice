"""Media lookup and Discord audio source construction."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


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


class MediaService:
    """Runs blocking yt-dlp extraction outside the Discord event loop."""

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

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        try:
            data = await asyncio.to_thread(self._search, query, limit)
        except yt_dlp.utils.DownloadError as exc:
            raise MediaExtractionError("Tìm kiếm YouTube thất bại") from exc

        results: list[SearchResult] = []
        for entry in (data.get("entries") or [])[:limit]:
            url = entry.get("webpage_url") or entry.get("url")
            if not url:
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
    ) -> discord.PCMVolumeTransformer:
        source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS)
        return discord.PCMVolumeTransformer(source, volume=volume)

    @staticmethod
    def _extract(query: str) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS) as ydl:
            return ydl.extract_info(query, download=False)

    @staticmethod
    def _search(query: str, limit: int) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS) as ydl:
            return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)


def format_duration(duration: int | None) -> str:
    if duration is None:
        return ""
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _duration_as_int(value: object) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    return None
