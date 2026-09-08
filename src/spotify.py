"""Spotify URL parsing and catalog lookup.

Spotify does not expose playable audio to third-party bots. This module only
fetches public catalog metadata so the media layer can search YouTube for a
matching stream.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)

SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_OEMBED_URL = "https://open.spotify.com/oembed"
SPOTIFY_HOSTS = frozenset({"open.spotify.com", "play.spotify.com"})
SPOTIFY_SHORT_HOSTS = frozenset({"spotify.link", "spotify.app.link"})
SPOTIFY_KINDS = frozenset({"track", "album", "playlist", "artist"})
_HTTP_TIMEOUT = 15
_TOKEN_REFRESH_SKEW = 60
_USER_AGENT = "TFVN-bot-voice/2.0"

_URI_PATTERN = re.compile(
    r"^spotify:(?P<kind>track|album|playlist|artist):(?P<id>[A-Za-z0-9]+)$",
    re.IGNORECASE,
)
_USER_PLAYLIST_URI_PATTERN = re.compile(
    r"^spotify:user:[^:]+:playlist:(?P<id>[A-Za-z0-9]+)$",
    re.IGNORECASE,
)

HttpJson = Callable[..., tuple[int, Any]]


class SpotifyLookupError(RuntimeError):
    """Raised when Spotify metadata cannot be parsed or fetched."""


@dataclass(frozen=True, slots=True)
class SpotifyRef:
    kind: str
    id: str


@dataclass(frozen=True, slots=True)
class SpotifyTrack:
    title: str
    artists: tuple[str, ...]
    duration: int | None = None

    @property
    def display_title(self) -> str:
        if not self.artists:
            return self.title
        return f"{', '.join(self.artists)} - {self.title}"

    @property
    def search_query(self) -> str:
        if not self.artists:
            return self.title
        return f"{self.artists[0]} - {self.title}"


@dataclass(frozen=True, slots=True)
class SpotifyCollection:
    tracks: tuple[SpotifyTrack, ...]
    is_playlist: bool = False
    skipped: int = 0
    truncated: bool = False


def _hostname(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        return host[4:]
    return host


def is_spotify_input(query: str) -> bool:
    """Return True when *query* is a Spotify URL or URI, including short links."""
    raw = query.strip()
    if not raw:
        return False
    if raw.lower().startswith("spotify:"):
        return True
    host = _hostname(raw)
    return host in SPOTIFY_HOSTS or host in SPOTIFY_SHORT_HOSTS


def parse_spotify_ref(query: str) -> SpotifyRef | None:
    """Parse a Spotify track/album/playlist/artist URL or URI."""
    raw = query.strip()
    if not raw:
        return None

    uri_match = _URI_PATTERN.fullmatch(raw)
    if uri_match is not None:
        return SpotifyRef(uri_match.group("kind").lower(), uri_match.group("id"))

    user_playlist = _USER_PLAYLIST_URI_PATTERN.fullmatch(raw)
    if user_playlist is not None:
        return SpotifyRef("playlist", user_playlist.group("id"))

    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if _hostname(raw) not in SPOTIFY_HOSTS:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].lower().startswith("intl-"):
        parts = parts[1:]
    if parts and parts[0].lower() == "embed":
        parts = parts[1:]
    if (
        len(parts) >= 4
        and parts[0].lower() == "user"
        and parts[2].lower() == "playlist"
    ):
        return SpotifyRef("playlist", parts[3])
    if len(parts) >= 2 and parts[0].lower() in SPOTIFY_KINDS:
        return SpotifyRef(parts[0].lower(), parts[1].split("?")[0])
    return None


def _urllib_json(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            raw = response.read()
            status = response.getcode() or 0
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise SpotifyLookupError("Không thể kết nối Spotify") from exc

    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpotifyLookupError("Spotify trả về dữ liệu không hợp lệ") from exc


class _SpotifyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only while the target stays on a Spotify host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        host = _hostname(newurl)
        if host not in SPOTIFY_HOSTS and host not in SPOTIFY_SHORT_HOSTS:
            raise SpotifyLookupError("Liên kết Spotify không hợp lệ")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _expand_spotify_url(url: str) -> str:
    opener = urllib.request.build_opener(_SpotifyRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT},
        method="GET",
    )
    try:
        with opener.open(request, timeout=_HTTP_TIMEOUT) as response:
            return response.geturl() or url
    except urllib.error.HTTPError as exc:
        final = getattr(exc, "url", None) or getattr(exc, "filename", None) or url
        if isinstance(final, str) and parse_spotify_ref(final) is not None:
            return final
        raise SpotifyLookupError("Không thể đọc Spotify lúc này") from exc
    except urllib.error.URLError as exc:
        raise SpotifyLookupError("Không thể kết nối Spotify") from exc


def _artists_from_payload(payload: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for artist in payload.get("artists") or ():
        if not isinstance(artist, Mapping):
            continue
        name = artist.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(names)


def _duration_from_ms(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value / 1000)
    return None


def _track_from_payload(payload: object) -> SpotifyTrack | None:
    if not isinstance(payload, Mapping):
        return None

    track: object
    if "track" in payload:
        track = payload.get("track")
    else:
        track = payload
    if not isinstance(track, Mapping):
        return None
    if payload.get("is_local") is True or track.get("is_local") is True:
        return None
    track_type = track.get("type")
    if isinstance(track_type, str) and track_type != "track":
        return None

    title = track.get("name")
    if not isinstance(title, str) or not title.strip():
        return None

    return SpotifyTrack(
        title=title.strip(),
        artists=_artists_from_payload(track),
        duration=_duration_from_ms(track.get("duration_ms")),
    )


class SpotifyService:
    """Fetches Spotify catalog metadata using Client Credentials or oEmbed."""

    def __init__(
        self,
        *,
        client_id: str = "",
        client_secret: str = "",
        http_json: HttpJson | None = None,
        expand_url: Callable[[str], str] | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http_json = http_json or _urllib_json
        self._expand_url = expand_url or _expand_spotify_url
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def lookup(
        self,
        query: str,
        *,
        playlist_limit: int,
    ) -> SpotifyCollection:
        if playlist_limit < 1:
            raise ValueError("playlist_limit must be positive")

        resolved_query = query.strip()
        ref = parse_spotify_ref(resolved_query)
        if ref is None and _hostname(resolved_query) in SPOTIFY_SHORT_HOSTS:
            resolved_query = await asyncio.to_thread(
                self._expand_url,
                resolved_query,
            )
            ref = parse_spotify_ref(resolved_query)
        if ref is None:
            raise SpotifyLookupError("Liên kết Spotify không hợp lệ")

        if ref.kind == "track":
            track = await self._lookup_track(ref.id)
            return SpotifyCollection(tracks=(track,))
        if not self.configured:
            raise SpotifyLookupError(
                "Bot chưa được cấu hình để mở album hoặc playlist Spotify."
            )
        if ref.kind == "album":
            return await self._lookup_album(ref.id, playlist_limit)
        if ref.kind == "playlist":
            return await self._lookup_playlist(ref.id, playlist_limit)
        return await self._lookup_artist(ref.id, playlist_limit)

    async def _lookup_track(self, track_id: str) -> SpotifyTrack:
        if self.configured:
            data = await self._api_get(f"/tracks/{track_id}")
            track = _track_from_payload(data)
            if track is None:
                raise SpotifyLookupError("Không tìm thấy bài Spotify này")
            return track

        url = (
            f"{SPOTIFY_OEMBED_URL}?"
            + urllib.parse.urlencode(
                {"url": f"https://open.spotify.com/track/{track_id}"}
            )
        )
        status, data = await asyncio.to_thread(self._http_json, "GET", url)
        if status == 404:
            raise SpotifyLookupError("Không tìm thấy bài Spotify này")
        if status != 200 or not isinstance(data, dict):
            _log.warning("Spotify oEmbed failed for track %s (status %s)", track_id, status)
            raise SpotifyLookupError("Không thể đọc Spotify lúc này")

        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SpotifyLookupError("Không tìm thấy bài Spotify này")
        author = data.get("author_name")
        artists = (author.strip(),) if isinstance(author, str) and author.strip() else ()
        return SpotifyTrack(title=title.strip(), artists=artists)

    async def _lookup_album(
        self,
        album_id: str,
        playlist_limit: int,
    ) -> SpotifyCollection:
        data = await self._api_get(
            f"/albums/{album_id}/tracks",
            {"limit": str(playlist_limit)},
        )
        return self._collection_from_page(data, playlist_limit)

    async def _lookup_playlist(
        self,
        playlist_id: str,
        playlist_limit: int,
    ) -> SpotifyCollection:
        data = await self._api_get(
            f"/playlists/{playlist_id}/tracks",
            {"limit": str(playlist_limit)},
        )
        return self._collection_from_page(data, playlist_limit)

    async def _lookup_artist(
        self,
        artist_id: str,
        playlist_limit: int,
    ) -> SpotifyCollection:
        data = await self._api_get(
            f"/artists/{artist_id}/top-tracks",
            {"market": "US"},
        )
        if not isinstance(data, dict):
            raise SpotifyLookupError("Không thể đọc Spotify lúc này")
        raw_tracks = data.get("tracks") or ()
        if not isinstance(raw_tracks, list):
            raise SpotifyLookupError("Playlist Spotify không có bài khả dụng")
        tracks, skipped = self._tracks_from_entries(raw_tracks[:playlist_limit])
        if not tracks:
            raise SpotifyLookupError("Playlist Spotify không có bài khả dụng")
        return SpotifyCollection(
            tracks=tracks,
            is_playlist=True,
            skipped=skipped,
            truncated=len(raw_tracks) > playlist_limit,
        )

    def _collection_from_page(
        self,
        data: object,
        playlist_limit: int,
    ) -> SpotifyCollection:
        if not isinstance(data, dict):
            raise SpotifyLookupError("Không thể đọc Spotify lúc này")
        raw_items = data.get("items") or ()
        if not isinstance(raw_items, list):
            raise SpotifyLookupError("Playlist Spotify không có bài khả dụng")
        tracks, skipped = self._tracks_from_entries(raw_items[:playlist_limit])
        if not tracks:
            raise SpotifyLookupError("Playlist Spotify không có bài khả dụng")
        total = data.get("total")
        truncated = isinstance(total, int) and total > playlist_limit
        if not truncated and len(raw_items) > playlist_limit:
            truncated = True
        return SpotifyCollection(
            tracks=tracks,
            is_playlist=True,
            skipped=skipped,
            truncated=truncated,
        )

    @staticmethod
    def _tracks_from_entries(
        entries: list[Any],
    ) -> tuple[tuple[SpotifyTrack, ...], int]:
        tracks: list[SpotifyTrack] = []
        skipped = 0
        for entry in entries:
            track = _track_from_payload(entry)
            if track is None:
                skipped += 1
                continue
            tracks.append(track)
        return tuple(tracks), skipped

    async def _api_get(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        retry: bool = True,
    ) -> Any:
        token = await self._bearer_token()
        url = f"{SPOTIFY_API_BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        status, data = await asyncio.to_thread(
            self._http_json,
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if status == 401 and retry:
            self._invalidate_token()
            return await self._api_get(path, params, retry=False)
        if status == 404:
            raise SpotifyLookupError("Không tìm thấy nội dung Spotify này")
        if status == 429:
            raise SpotifyLookupError("Spotify đang bị giới hạn, hãy thử lại sau")
        if status != 200:
            _log.warning("Spotify API %s failed with status %s", path, status)
            raise SpotifyLookupError("Không thể đọc Spotify lúc này")
        return data

    async def _bearer_token(self) -> str:
        if not self.configured:
            raise SpotifyLookupError(
                "Bot chưa được cấu hình để mở album hoặc playlist Spotify."
            )
        async with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._token_expires_at:
                return self._access_token

            credentials = base64.b64encode(
                f"{self._client_id}:{self._client_secret}".encode("utf-8")
            ).decode("ascii")
            body = urllib.parse.urlencode(
                {"grant_type": "client_credentials"}
            ).encode("ascii")
            status, data = await asyncio.to_thread(
                self._http_json,
                "POST",
                SPOTIFY_TOKEN_URL,
                body=body,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            token = data.get("access_token") if isinstance(data, dict) else None
            if status != 200 or not isinstance(token, str) or not token:
                _log.warning("Spotify token request failed with status %s", status)
                raise SpotifyLookupError("Không thể đọc Spotify lúc này")

            expires_in = data.get("expires_in")
            lifetime = (
                int(expires_in)
                if isinstance(expires_in, (int, float)) and expires_in > 0
                else 3600
            )
            self._access_token = token
            self._token_expires_at = now + max(lifetime - _TOKEN_REFRESH_SKEW, 30)
            return token

    def _invalidate_token(self) -> None:
        self._access_token = None
        self._token_expires_at = 0.0
