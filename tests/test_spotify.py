from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from src.spotify import (
    SPOTIFY_TOKEN_URL,
    SpotifyLookupError,
    SpotifyService,
    SpotifyTrack,
    is_spotify_input,
    parse_spotify_ref,
)


class _ScriptedHttp:
    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes | None, dict[str, str] | None]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        self.calls.append((method, url, body, headers))
        if not self.responses:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        return self.responses.pop(0)


class ParseSpotifyRefTests(unittest.TestCase):
    def test_parses_http_urls_and_uris(self) -> None:
        cases = {
            "https://open.spotify.com/track/abc123": ("track", "abc123"),
            "https://open.spotify.com/track/abc123?si=xyz": ("track", "abc123"),
            "https://open.spotify.com/intl-vi/album/alb1": ("album", "alb1"),
            "https://open.spotify.com/embed/playlist/pl1": ("playlist", "pl1"),
            "https://play.spotify.com/artist/art1": ("artist", "art1"),
            "https://www.open.spotify.com/track/abc123": ("track", "abc123"),
            "https://open.spotify.com/user/bob/playlist/pl2": ("playlist", "pl2"),
            "https://open.spotify.com/track/abc123/": ("track", "abc123"),
            "https://open.spotify.com/intl-en/embed/track/abc123": ("track", "abc123"),
            "  spotify:track:abc123  ": ("track", "abc123"),
            "spotify:track:abc123": ("track", "abc123"),
            "spotify:album:alb1": ("album", "alb1"),
            "spotify:playlist:pl1": ("playlist", "pl1"),
            "spotify:artist:art1": ("artist", "art1"),
            "spotify:user:bob:playlist:pl2": ("playlist", "pl2"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                ref = parse_spotify_ref(raw)
                self.assertIsNotNone(ref)
                assert ref is not None
                self.assertEqual((ref.kind, ref.id), expected)

    def test_rejects_non_spotify_and_incomplete_links(self) -> None:
        invalid = (
            "",
            "never gonna give you up",
            "https://www.youtube.com/watch?v=abc",
            "https://open.spotify.com/",
            "https://open.spotify.com/show/abc123",
            "spotify:episode:abc",
            "spotify:",
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_spotify_ref(raw))

    def test_detects_spotify_input_including_short_links(self) -> None:
        self.assertTrue(is_spotify_input("  spotify:track:abc123  "))
        self.assertTrue(is_spotify_input("https://open.spotify.com/show/abc"))
        self.assertTrue(is_spotify_input("https://spotify.link/short"))
        self.assertFalse(is_spotify_input("spotify is cool"))
        self.assertFalse(is_spotify_input("https://example.test/track/abc"))


class SpotifyTrackTests(unittest.TestCase):
    def test_display_and_search_titles(self) -> None:
        track = SpotifyTrack("Song", ("First", "Second"), 210)
        self.assertEqual(track.display_title, "First, Second - Song")
        self.assertEqual(track.search_query, "First - Song")

        untitled = SpotifyTrack("Instrumental", ())
        self.assertEqual(untitled.display_title, "Instrumental")
        self.assertEqual(untitled.search_query, "Instrumental")


class SpotifyLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_track_uses_oembed_without_credentials(self) -> None:
        http = _ScriptedHttp(
            [(200, {"title": "Song", "author_name": "Artist"})]
        )
        service = SpotifyService(http_json=http)

        collection = await service.lookup(
            "https://open.spotify.com/track/abc123",
            playlist_limit=25,
        )

        self.assertFalse(collection.is_playlist)
        self.assertEqual(
            collection.tracks,
            (SpotifyTrack("Song", ("Artist",)),),
        )
        self.assertEqual(len(http.calls), 1)
        self.assertIn("oembed", http.calls[0][1])
        self.assertIn("abc123", http.calls[0][1])

    async def test_track_uses_api_when_configured(self) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (
                    200,
                    {
                        "name": "Song",
                        "type": "track",
                        "duration_ms": 210000,
                        "artists": [{"name": "Artist"}],
                    },
                ),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )

        collection = await service.lookup("spotify:track:abc123", playlist_limit=25)

        self.assertEqual(
            collection.tracks,
            (SpotifyTrack("Song", ("Artist",), 210),),
        )
        self.assertEqual(http.calls[0][0], "POST")
        self.assertEqual(http.calls[0][1], SPOTIFY_TOKEN_URL)
        self.assertIn("/tracks/abc123", http.calls[1][1])
        self.assertEqual(
            http.calls[1][3],
            {"Authorization": "Bearer tok"},
        )

    async def test_playlist_requires_credentials(self) -> None:
        service = SpotifyService(http_json=_ScriptedHttp([]))
        with self.assertRaisesRegex(SpotifyLookupError, "chưa được cấu hình"):
            await service.lookup(
                "https://open.spotify.com/playlist/pl1",
                playlist_limit=25,
            )

    async def test_playlist_skips_unusable_entries_and_marks_truncated(
        self,
    ) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (
                    200,
                    {
                        "total": 40,
                        "items": [
                            {
                                "is_local": True,
                                "track": {"name": "Local", "type": "track"},
                            },
                            {"track": None},
                            {
                                "track": {
                                    "name": "Podcast",
                                    "type": "episode",
                                }
                            },
                            {
                                "track": {
                                    "name": "Keep",
                                    "type": "track",
                                    "duration_ms": 1000,
                                    "artists": [{"name": "A"}],
                                }
                            },
                        ],
                    },
                ),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )

        collection = await service.lookup(
            "https://open.spotify.com/playlist/pl1",
            playlist_limit=25,
        )

        self.assertTrue(collection.is_playlist)
        self.assertTrue(collection.truncated)
        self.assertEqual(collection.skipped, 3)
        self.assertEqual(collection.tracks, (SpotifyTrack("Keep", ("A",), 1),))

    async def test_album_uses_simplified_track_objects(self) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (
                    200,
                    {
                        "total": 1,
                        "items": [
                            {
                                "name": "Cut",
                                "type": "track",
                                "duration_ms": 5000,
                                "artists": [{"name": "Band"}],
                            }
                        ],
                    },
                ),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )

        collection = await service.lookup("spotify:album:alb1", playlist_limit=25)

        self.assertTrue(collection.is_playlist)
        self.assertEqual(
            collection.tracks,
            (SpotifyTrack("Cut", ("Band",), 5),),
        )
        self.assertIn("/albums/alb1/tracks", http.calls[1][1])

    async def test_artist_uses_top_tracks(self) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (
                    200,
                    {
                        "tracks": [
                            {
                                "name": "Hit",
                                "type": "track",
                                "artists": [{"name": "Star"}],
                            }
                        ]
                    },
                ),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )

        collection = await service.lookup("spotify:artist:art1", playlist_limit=25)

        self.assertTrue(collection.is_playlist)
        self.assertEqual(collection.tracks[0].title, "Hit")
        self.assertIn("/artists/art1/top-tracks", http.calls[1][1])

    async def test_retries_once_after_unauthorized_api_response(self) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "old", "expires_in": 3600}),
                (401, {"error": "expired"}),
                (200, {"access_token": "new", "expires_in": 3600}),
                (
                    200,
                    {
                        "name": "Song",
                        "type": "track",
                        "artists": [{"name": "Artist"}],
                    },
                ),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )

        collection = await service.lookup("spotify:track:abc123", playlist_limit=25)

        self.assertEqual(collection.tracks[0].title, "Song")
        self.assertEqual(http.calls[1][3], {"Authorization": "Bearer old"})
        self.assertEqual(http.calls[3][3], {"Authorization": "Bearer new"})

    async def test_reuses_cached_access_token(self) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (
                    200,
                    {
                        "name": "One",
                        "type": "track",
                        "artists": [{"name": "A"}],
                    },
                ),
                (
                    200,
                    {
                        "name": "Two",
                        "type": "track",
                        "artists": [{"name": "B"}],
                    },
                ),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )

        await service.lookup("spotify:track:one", playlist_limit=25)
        await service.lookup("spotify:track:two", playlist_limit=25)

        token_calls = [call for call in http.calls if call[0] == "POST"]
        self.assertEqual(len(token_calls), 1)

    async def test_maps_api_status_codes_to_user_errors(self) -> None:
        not_found = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (404, {}),
            ]
        )
        limited = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (429, {}),
            ]
        )
        with self.assertRaisesRegex(SpotifyLookupError, "Không tìm thấy"):
            await SpotifyService(
                client_id="id",
                client_secret="secret",
                http_json=not_found,
            ).lookup("spotify:track:missing", playlist_limit=25)
        with self.assertRaisesRegex(SpotifyLookupError, "giới hạn"):
            await SpotifyService(
                client_id="id",
                client_secret="secret",
                http_json=limited,
            ).lookup("spotify:track:slow", playlist_limit=25)

    async def test_expands_short_links_before_lookup(self) -> None:
        http = _ScriptedHttp(
            [(200, {"title": "Song", "author_name": "Artist"})]
        )
        service = SpotifyService(
            http_json=http,
            expand_url=lambda _url: "https://open.spotify.com/track/abc123",
        )

        collection = await service.lookup(
            "https://spotify.link/short",
            playlist_limit=25,
        )

        self.assertEqual(collection.tracks[0].title, "Song")

    async def test_rejects_unrecognized_spotify_urls(self) -> None:
        service = SpotifyService(http_json=_ScriptedHttp([]))
        with self.assertRaisesRegex(SpotifyLookupError, "không hợp lệ"):
            await service.lookup(
                "https://open.spotify.com/show/abc123",
                playlist_limit=25,
            )

    async def test_oembed_404_is_missing_track(self) -> None:
        service = SpotifyService(http_json=_ScriptedHttp([(404, {})]))
        with self.assertRaisesRegex(SpotifyLookupError, "Không tìm thấy bài"):
            await service.lookup("spotify:track:missing", playlist_limit=25)

    async def test_oembed_blank_title_is_missing_track(self) -> None:
        service = SpotifyService(
            http_json=_ScriptedHttp(
                [(200, {"title": "   ", "author_name": "Artist"})]
            )
        )
        with self.assertRaisesRegex(SpotifyLookupError, "Không tìm thấy bài"):
            await service.lookup("spotify:track:blank", playlist_limit=25)

    async def test_oembed_non_object_body_is_unavailable(self) -> None:
        service = SpotifyService(http_json=_ScriptedHttp([(200, "nope")]))
        with self.assertRaisesRegex(SpotifyLookupError, "Không thể đọc Spotify lúc này"):
            await service.lookup("spotify:track:bad", playlist_limit=25)

    async def test_oembed_server_error_is_unavailable(self) -> None:
        service = SpotifyService(http_json=_ScriptedHttp([(500, {"error": "nope"})]))
        with self.assertRaisesRegex(SpotifyLookupError, "Không thể đọc Spotify lúc này"):
            await service.lookup("spotify:track:down", playlist_limit=25)

    async def test_empty_playlist_lookup_raises(self) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (200, {"total": 0, "items": []}),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )
        with self.assertRaisesRegex(SpotifyLookupError, "không có bài khả dụng"):
            await service.lookup(
                "https://open.spotify.com/playlist/empty",
                playlist_limit=25,
            )

    async def test_playlist_of_only_unusable_entries_raises(self) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (
                    200,
                    {
                        "total": 2,
                        "items": [
                            {"track": None},
                            {
                                "track": {
                                    "name": "Podcast",
                                    "type": "episode",
                                }
                            },
                        ],
                    },
                ),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )
        with self.assertRaisesRegex(SpotifyLookupError, "không có bài khả dụng"):
            await service.lookup("spotify:playlist:dead", playlist_limit=25)

    async def test_api_track_payload_without_usable_track_raises(self) -> None:
        http = _ScriptedHttp(
            [
                (200, {"access_token": "tok", "expires_in": 3600}),
                (200, {"name": "  ", "type": "track"}),
            ]
        )
        service = SpotifyService(
            client_id="id",
            client_secret="secret",
            http_json=http,
        )
        with self.assertRaisesRegex(SpotifyLookupError, "Không tìm thấy bài"):
            await service.lookup("spotify:track:empty", playlist_limit=25)

    async def test_lookup_rejects_non_positive_playlist_limit(self) -> None:
        service = SpotifyService(http_json=_ScriptedHttp([]))
        with self.assertRaises(ValueError):
            await service.lookup("spotify:track:abc", playlist_limit=0)


class SpotifyHttpHelperTests(unittest.TestCase):
    def test_json_decode_errors_become_lookup_errors(self) -> None:
        from src.spotify import _urllib_json

        class _Response:
            def read(self) -> bytes:
                return b"not-json"

            def getcode(self) -> int:
                return 200

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        with patch("src.spotify.urllib.request.urlopen", return_value=_Response()):
            with self.assertRaisesRegex(SpotifyLookupError, "không hợp lệ"):
                _urllib_json("GET", "https://open.spotify.com/oembed")


if __name__ == "__main__":
    unittest.main()
