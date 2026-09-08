from __future__ import annotations

import shlex
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.media import (
    PLAYLIST_LIMIT,
    MediaBatch,
    MediaExtractionError,
    MediaService,
    MediaURLBlockedError,
    QueuedTrack,
    SearchResult,
    Track,
    _validate_url,
    format_duration,
    parse_jump_timestamp,
    pick_youtube_match,
)
from src.spotify import SpotifyCollection, SpotifyLookupError, SpotifyTrack


class FormatDurationTests(unittest.TestCase):
    def test_formats_minutes(self) -> None:
        self.assertEqual(format_duration(185), "3:05")

    def test_formats_hours(self) -> None:
        self.assertEqual(format_duration(3723), "1:02:03")

    def test_handles_unknown_duration(self) -> None:
        self.assertEqual(format_duration(None), "")


class ParseJumpTimestampTests(unittest.TestCase):
    def test_parses_strict_hh_mm_ss(self) -> None:
        cases = {
            "00:00:00": 0,
            "00:00:59": 59,
            "00:59:59": 3599,
            "01:02:03": 3723,
            "99:59:59": 359999,
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_jump_timestamp(value), expected)

    def test_rejects_invalid_timestamps(self) -> None:
        invalid_values = (
            "",
            "1:02:03",
            "001:02:03",
            "01:2:03",
            "01:02:3",
            "01:60:00",
            "01:00:60",
            "-1:02:03",
            "01:02",
            "01:02:03:04",
            " 01:02:03 ",
            "01:02:03x",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                self.assertIsNone(parse_jump_timestamp(value))


class MediaPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.media = MediaService()

    async def test_plain_query_uses_first_youtube_search_result(self) -> None:
        search_data = {
            "entries": [
                {
                    "id": "first-id",
                    "title": "First result",
                    "url": "first-id",
                    "duration": 123,
                },
                {
                    "id": "second-id",
                    "title": "Second result",
                    "url": "second-id",
                },
            ]
        }

        with (
            patch.object(MediaService, "_search", return_value=search_data) as search,
            patch.object(MediaService, "_prepare_url") as prepare_url,
        ):
            batch = await self.media.prepare("  nhạc thư giãn  ")

        self.assertEqual(
            batch,
            MediaBatch(
                items=(
                    QueuedTrack(
                        title="First result",
                        webpage_url=(
                            "https://www.youtube.com/watch?v=first-id"
                        ),
                        duration=123,
                    ),
                )
            ),
        )
        search.assert_called_once_with("nhạc thư giãn", 1)
        prepare_url.assert_not_called()

    async def test_direct_url_uses_metadata_extraction_not_search(self) -> None:
        url = "https://www.youtube.com/watch?v=video-id"
        metadata = {
            "id": "video-id",
            "title": "Direct video",
            "webpage_url": url,
            "duration": 42,
        }

        with (
            patch.object(
                MediaService,
                "_prepare_url",
                return_value=metadata,
            ) as prepare_url,
            patch.object(MediaService, "_search") as search,
        ):
            batch = await self.media.prepare(url)

        self.assertEqual(
            batch.items,
            (QueuedTrack("Direct video", url, 42),),
        )
        self.assertFalse(batch.is_playlist)
        prepare_url.assert_called_once_with(url, PLAYLIST_LIMIT)
        search.assert_not_called()

    async def test_direct_url_keeps_input_url_instead_of_temporary_stream(
        self,
    ) -> None:
        url = "https://example.test/watch/track"
        metadata = {
            "title": "Generic media",
            "url": "https://cdn.example.test/temporary-stream",
        }

        with patch.object(
            MediaService,
            "_prepare_url",
            return_value=metadata,
        ):
            batch = await self.media.prepare(url)

        self.assertEqual(batch.items[0].webpage_url, url)

    async def test_playlist_preserves_order_and_skips_unavailable_entries(
        self,
    ) -> None:
        playlist_url = "https://www.youtube.com/playlist?list=example"
        metadata = {
            "_type": "playlist",
            "entries": [
                {"id": "a", "title": "A", "url": "a", "duration": 1},
                None,
                {
                    "id": "private",
                    "title": "Private",
                    "url": "private",
                    "availability": "private",
                },
                {"title": "Missing URL"},
                {
                    "id": "b",
                    "title": "B",
                    "url": "https://youtu.be/b",
                    "duration": 2,
                },
            ],
        }

        with patch.object(
            MediaService,
            "_prepare_url",
            return_value=metadata,
        ):
            batch = await self.media.prepare(playlist_url)

        self.assertEqual([item.title for item in batch.items], ["A", "B"])
        self.assertEqual(
            [item.webpage_url for item in batch.items],
            ["https://www.youtube.com/watch?v=a", "https://youtu.be/b"],
        )
        self.assertTrue(batch.is_playlist)
        self.assertEqual(batch.skipped, 3)
        self.assertFalse(batch.truncated)

    async def test_playlist_is_capped_at_first_twenty_five_entries(self) -> None:
        playlist_url = "https://www.youtube.com/playlist?list=large"
        entries = [
            {"id": str(index), "title": f"Track {index}", "url": str(index)}
            for index in range(30)
        ]

        with patch.object(
            MediaService,
            "_prepare_url",
            return_value={"_type": "playlist", "entries": entries},
        ) as prepare_url:
            batch = await self.media.prepare(
                playlist_url,
                playlist_limit=100,
            )

        self.assertEqual(len(batch.items), PLAYLIST_LIMIT)
        self.assertEqual(batch.items[-1].title, "Track 24")
        self.assertTrue(batch.truncated)
        prepare_url.assert_called_once_with(playlist_url, PLAYLIST_LIMIT)

    async def test_declared_playlist_size_marks_extractor_limited_result(self) -> None:
        playlist_url = "https://www.youtube.com/playlist?list=large"
        metadata = {
            "_type": "playlist",
            "playlist_count": 100,
            "entries": [
                {"id": str(index), "title": str(index), "url": str(index)}
                for index in range(PLAYLIST_LIMIT)
            ],
        }

        with patch.object(
            MediaService,
            "_prepare_url",
            return_value=metadata,
        ):
            batch = await self.media.prepare(playlist_url)

        self.assertTrue(batch.truncated)

    async def test_prepare_rejects_blank_malformed_and_empty_inputs(self) -> None:
        with self.assertRaisesRegex(MediaExtractionError, "Vui lòng nhập"):
            await self.media.prepare("   ")

        url = "https://example.test/media"
        with patch.object(MediaService, "_prepare_url", return_value={}):
            with self.assertRaisesRegex(MediaExtractionError, "thông tin hợp lệ"):
                await self.media.prepare(url)

        with patch.object(MediaService, "_prepare_url", return_value=None):
            with self.assertRaisesRegex(MediaExtractionError, "thông tin hợp lệ"):
                await self.media.prepare(url)

        with patch.object(
            MediaService,
            "_prepare_url",
            return_value={"_type": "playlist", "entries": []},
        ):
            with self.assertRaisesRegex(MediaExtractionError, "video khả dụng"):
                await self.media.prepare(url)

        with patch.object(
            MediaService,
            "_prepare_url",
            return_value={"_type": "playlist", "entries": 42},
        ):
            with self.assertRaisesRegex(MediaExtractionError, "thông tin hợp lệ"):
                await self.media.prepare(url)

    async def test_prepare_plain_query_requires_a_search_result(self) -> None:
        with patch.object(
            MediaService,
            "_search",
            return_value={"entries": []},
        ):
            with self.assertRaisesRegex(MediaExtractionError, "Không tìm thấy"):
                await self.media.prepare("không tồn tại")

    async def test_queued_item_is_resolved_lazily_with_a_fresh_stream(self) -> None:
        webpage_url = "https://www.youtube.com/watch?v=lazy"
        metadata = {
            "id": "lazy",
            "title": "Queued title",
            "webpage_url": webpage_url,
            "duration": 300,
        }
        resolved = {
            "title": "Fresh title",
            "url": "https://stream.example.test/fresh",
            "webpage_url": webpage_url,
        }

        with (
            patch.object(
                MediaService,
                "_prepare_url",
                return_value=metadata,
            ),
            patch.object(MediaService, "_extract", return_value=resolved) as extract,
        ):
            batch = await self.media.prepare(webpage_url)
            extract.assert_not_called()
            track = await self.media.resolve_queued(batch.items[0])

        extract.assert_called_once_with(webpage_url)
        self.assertEqual(track.title, "Fresh title")
        self.assertEqual(track.stream_url, "https://stream.example.test/fresh")
        self.assertEqual(track.webpage_url, webpage_url)
        self.assertEqual(track.duration, 300)

    async def test_search_returns_five_and_skips_partial_entries(self) -> None:
        entries = [
            None,
            {"title": "Missing URL"},
            {"id": "first", "title": "First", "url": "first"},
            *(
                {
                    "id": str(index),
                    "title": f"Result {index}",
                    "webpage_url": f"https://youtu.be/{index}",
                }
                for index in range(2, 8)
            ),
        ]
        with patch.object(
            MediaService,
            "_search",
            return_value={"entries": entries},
        ):
            results = await self.media.search("test", limit=5)

        self.assertEqual(len(results), 5)
        self.assertEqual(results[0].url, "https://www.youtube.com/watch?v=first")
        self.assertEqual(results[-1].title, "Result 5")

    async def test_search_rejects_invalid_top_level_metadata(self) -> None:
        with patch.object(MediaService, "_search", return_value=None):
            with self.assertRaisesRegex(MediaExtractionError, "không hợp lệ"):
                await self.media.search("test")


class AudioSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.media = MediaService()
        self.track = Track(
            title="Example",
            stream_url="https://example.test/audio",
        )

    def test_create_audio_source_adds_ffmpeg_seek_offset(self) -> None:
        with (
            patch("src.media.discord.FFmpegPCMAudio") as ffmpeg,
            patch("src.media.discord.PCMVolumeTransformer") as transformer,
        ):
            self.media.create_audio_source(
                self.track,
                volume=0.25,
                start_at=3723,
            )

        ffmpeg.assert_called_once()
        args, kwargs = ffmpeg.call_args
        self.assertEqual(args, (self.track.stream_url,))
        before_options = shlex.split(kwargs["before_options"])
        seek_index = before_options.index("-ss")
        self.assertEqual(before_options[seek_index + 1], "3723")
        self.assertIn("-reconnect", before_options)
        self.assertEqual(kwargs["options"], "-vn")
        transformer.assert_called_once_with(ffmpeg.return_value, volume=0.25)

    def test_create_audio_source_without_offset_does_not_seek(self) -> None:
        with (
            patch("src.media.discord.FFmpegPCMAudio") as ffmpeg,
            patch("src.media.discord.PCMVolumeTransformer"),
        ):
            self.media.create_audio_source(self.track, volume=0.5)

        _, kwargs = ffmpeg.call_args
        self.assertNotIn("-ss", shlex.split(kwargs["before_options"]))

    def test_create_audio_source_rejects_negative_offset(self) -> None:
        with self.assertRaises(ValueError):
            self.media.create_audio_source(
                self.track,
                volume=0.5,
                start_at=-1,
            )


class URLValidationTests(unittest.TestCase):
    """SSRF prevention via _validate_url."""

    def test_blocks_loopback_ip_literal(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            _validate_url("http://127.0.0.1/latest/meta-data/")

    def test_blocks_ipv6_loopback(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            _validate_url("http://[::1]/something")

    def test_blocks_private_ip_10(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            _validate_url("http://10.0.0.1/internal")

    def test_blocks_private_ip_172(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            _validate_url("http://172.16.0.1/internal")

    def test_blocks_private_ip_192(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            _validate_url("http://192.168.1.1/internal")

    def test_blocks_link_local_metadata_endpoint(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            _validate_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_hostname_resolving_to_private_ip(self) -> None:
        fake_result = [(2, 1, 6, "", ("127.0.0.1", 0))]
        with patch("src.media.socket.getaddrinfo", return_value=fake_result):
            with self.assertRaises(MediaURLBlockedError):
                _validate_url("http://evil.example.com/steal")

    def test_allows_public_url(self) -> None:
        fake_result = [(2, 1, 6, "", ("142.250.80.46", 0))]
        with patch("src.media.socket.getaddrinfo", return_value=fake_result):
            _validate_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_allows_dns_failure_to_pass_through(self) -> None:
        """Unresolvable hostnames are let through for yt-dlp to handle."""
        import socket as _sock

        with patch(
            "src.media.socket.getaddrinfo",
            side_effect=_sock.gaierror("Name or service not known"),
        ):
            _validate_url("https://nonexistent.example.test/video")

    def test_blocks_url_without_hostname(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            _validate_url("http:///no-host")


class URLValidationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Verify that prepare() and resolve() call _validate_url for URLs."""

    def setUp(self) -> None:
        self.media = MediaService()

    async def test_prepare_blocks_private_url(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            await self.media.prepare("http://169.254.169.254/latest/meta-data/")

    async def test_resolve_blocks_private_url(self) -> None:
        with self.assertRaises(MediaURLBlockedError):
            await self.media.resolve("http://10.0.0.1/internal")

    async def test_resolve_skips_validation_for_plain_queries(self) -> None:
        """Non-URL queries (search terms) should not trigger URL validation."""
        fake_data = {
            "title": "Song",
            "url": "https://stream.example.test/audio",
            "webpage_url": "https://www.youtube.com/watch?v=test",
        }
        with patch.object(MediaService, "_extract", return_value=fake_data):
            track = await self.media.resolve("ytsearch1:some song")
        self.assertEqual(track.title, "Song")


class SpotifyPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.spotify = MagicMock()
        self.media = MediaService(spotify=self.spotify)

    async def test_spotify_track_is_matched_to_youtube_and_skips_ytdlp(self) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(SpotifyTrack("Song", ("Artist",), 200),),
            )
        )
        youtube = [SearchResult("YT title", "https://youtu.be/matched", 198)]

        with (
            patch.object(self.media, "search", new=AsyncMock(return_value=youtube)) as search,
            patch.object(MediaService, "_prepare_url") as prepare_url,
        ):
            batch = await self.media.prepare("  https://open.spotify.com/track/abc  ")

        self.assertEqual(
            batch,
            MediaBatch(
                items=(
                    QueuedTrack(
                        "Artist - Song",
                        "https://youtu.be/matched",
                        198,
                    ),
                )
            ),
        )
        self.spotify.lookup.assert_awaited_once_with(
            "https://open.spotify.com/track/abc",
            playlist_limit=PLAYLIST_LIMIT,
        )
        search.assert_awaited_once_with("Artist - Song", limit=5)
        prepare_url.assert_not_called()

    async def test_spotify_uri_does_not_go_through_plain_youtube_search(self) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(SpotifyTrack("Song", ("Artist",)),),
            )
        )
        with (
            patch.object(
                self.media,
                "search",
                new=AsyncMock(
                    return_value=[
                        SearchResult(
                            "Artist - Song (Official Audio)",
                            "https://youtu.be/x",
                            10,
                        ),
                    ]
                ),
            ),
            patch.object(MediaService, "_search") as raw_search,
        ):
            await self.media.prepare("spotify:track:abc123")

        raw_search.assert_not_called()

    async def test_spotify_playlist_skips_unmatched_youtube_results(self) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(
                    SpotifyTrack("Keep", ("A",), 10),
                    SpotifyTrack("Miss", ("B",), 20),
                ),
                is_playlist=True,
                skipped=1,
                truncated=True,
            )
        )

        async def search(query: str, *, limit: int = 5) -> list[SearchResult]:
            if query.startswith("A -"):
                return [SearchResult("YT Keep", "https://youtu.be/keep", 11)]
            return []

        with patch.object(self.media, "search", side_effect=search):
            batch = await self.media.prepare(
                "https://open.spotify.com/playlist/pl1"
            )

        self.assertEqual(
            [item.webpage_url for item in batch.items],
            ["https://youtu.be/keep"],
        )
        self.assertEqual(batch.items[0].title, "A - Keep")
        self.assertTrue(batch.is_playlist)
        self.assertTrue(batch.truncated)
        self.assertEqual(batch.skipped, 2)

    async def test_spotify_single_track_without_youtube_match_fails(self) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(SpotifyTrack("Song", ("Artist",)),),
            )
        )
        with patch.object(self.media, "search", new=AsyncMock(return_value=[])):
            with self.assertRaisesRegex(MediaExtractionError, "Không tìm thấy"):
                await self.media.prepare("spotify:track:abc")

    async def test_spotify_lookup_errors_surface_as_media_errors(self) -> None:
        self.spotify.lookup = AsyncMock(
            side_effect=SpotifyLookupError("Liên kết Spotify không hợp lệ")
        )
        with self.assertRaisesRegex(MediaExtractionError, "không hợp lệ"):
            await self.media.prepare("https://open.spotify.com/show/abc")

    async def test_spotify_playlist_picks_official_over_karaoke_and_hour_mix(
        self,
    ) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(
                    SpotifyTrack("Blinding Lights", ("The Weeknd",), 200),
                ),
                is_playlist=True,
            )
        )
        results = [
            SearchResult(
                "Blinding Lights karaoke version",
                "https://youtu.be/kara",
                201,
            ),
            SearchResult(
                "The Weeknd - Blinding Lights (Official Audio)",
                "https://youtu.be/official",
                200,
            ),
            SearchResult(
                "Blinding Lights 10 hour",
                "https://youtu.be/hour",
                36000,
            ),
        ]
        with patch.object(
            self.media,
            "search",
            new=AsyncMock(return_value=results),
        ) as search:
            batch = await self.media.prepare(
                "https://open.spotify.com/playlist/pl1"
            )

        self.assertEqual(batch.items[0].webpage_url, "https://youtu.be/official")
        self.assertEqual(batch.items[0].title, "The Weeknd - Blinding Lights")
        self.assertTrue(batch.is_playlist)
        search.assert_awaited_once_with("The Weeknd - Blinding Lights", limit=5)

    async def test_spotify_playlist_skips_tracks_with_only_bad_youtube_hits(
        self,
    ) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(
                    SpotifyTrack("Keep", ("Artist",), 180),
                    SpotifyTrack("Miss", ("Artist",), 180),
                ),
                is_playlist=True,
            )
        )

        async def search(query: str, *, limit: int = 5) -> list[SearchResult]:
            if "Keep" in query:
                return [
                    SearchResult(
                        "Artist - Keep (Official Audio)",
                        "https://youtu.be/keep",
                        181,
                    )
                ]
            return [
                SearchResult("Miss 1 hour mix", "https://youtu.be/mix", 3600),
                SearchResult("Miss karaoke", "https://youtu.be/kara", 40),
            ]

        with patch.object(self.media, "search", side_effect=search):
            batch = await self.media.prepare(
                "https://open.spotify.com/playlist/pl1"
            )

        self.assertEqual(
            [item.webpage_url for item in batch.items],
            ["https://youtu.be/keep"],
        )
        self.assertEqual(batch.skipped, 1)
        self.assertTrue(batch.is_playlist)

    async def test_spotify_playlist_all_unmatched_fails_unlike_partial_skip(
        self,
    ) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(SpotifyTrack("Miss", ("Artist",), 180),),
                is_playlist=True,
            )
        )
        with patch.object(self.media, "search", new=AsyncMock(return_value=[])):
            with self.assertRaisesRegex(
                MediaExtractionError,
                "Playlist không có video khả dụng",
            ):
                await self.media.prepare("https://open.spotify.com/playlist/pl1")

    async def test_first_youtube_query_error_falls_through_to_alternate_query(
        self,
    ) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(SpotifyTrack("Song", ("Artist",), 180),),
            )
        )
        queries: list[str] = []

        async def search(query: str, *, limit: int = 5) -> list[SearchResult]:
            queries.append(query)
            if query == "Artist - Song":
                raise MediaExtractionError("Tìm kiếm YouTube thất bại")
            return [
                SearchResult(
                    "Artist - Song (Official Audio)",
                    "https://youtu.be/alt",
                    180,
                )
            ]

        with patch.object(self.media, "search", side_effect=search):
            batch = await self.media.prepare("spotify:track:abc")

        self.assertEqual(queries, ["Artist - Song", "Song Artist"])
        self.assertEqual(batch.items[0].webpage_url, "https://youtu.be/alt")
        self.assertFalse(batch.is_playlist)

    async def test_poor_first_youtube_hits_fall_through_to_alternate_query(
        self,
    ) -> None:
        self.spotify.lookup = AsyncMock(
            return_value=SpotifyCollection(
                tracks=(SpotifyTrack("Song", ("Artist",), 180),),
                is_playlist=True,
            )
        )
        queries: list[str] = []

        async def search(query: str, *, limit: int = 5) -> list[SearchResult]:
            queries.append(query)
            if query == "Artist - Song":
                return [
                    SearchResult(
                        "unrelated 10 hour mix",
                        "https://youtu.be/bad",
                        36000,
                    )
                ]
            return [
                SearchResult(
                    "Artist - Song (Official Audio)",
                    "https://youtu.be/good",
                    181,
                )
            ]

        with patch.object(self.media, "search", side_effect=search):
            batch = await self.media.prepare(
                "https://open.spotify.com/playlist/pl1"
            )

        self.assertEqual(queries, ["Artist - Song", "Song Artist"])
        self.assertEqual(batch.items[0].webpage_url, "https://youtu.be/good")
        self.assertTrue(batch.is_playlist)
        self.assertEqual(batch.skipped, 0)


class YoutubeMatchTests(unittest.TestCase):
    def test_prefers_duration_and_artist_over_first_result(self) -> None:
        track = SpotifyTrack("Stay", ("The Kid LAROI", "Justin Bieber"), 141)
        karaoke = SearchResult("Stay karaoke", "https://youtu.be/kara", 142)
        official = SearchResult(
            "The Kid LAROI Justin Bieber Stay Official Audio",
            "https://youtu.be/official",
            141,
            uploader="The Kid LAROI - Topic",
        )
        hour_mix = SearchResult("Stay 1 hour", "https://youtu.be/hour", 3600)

        picked = pick_youtube_match(track, (karaoke, official, hour_mix))

        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/official")

    def test_rejects_candidates_with_wildly_different_duration(self) -> None:
        track = SpotifyTrack("Song", ("Artist",), 200)
        picked = pick_youtube_match(
            track,
            (
                SearchResult("Artist - Song full album", "https://youtu.be/album", 3400),
            ),
        )
        self.assertIsNone(picked)

    def test_ignores_cover_when_original_is_present(self) -> None:
        track = SpotifyTrack("drivers license", ("Olivia Rodrigo",), 242)
        cover = SearchResult(
            "drivers license cover",
            "https://youtu.be/cover",
            240,
        )
        original = SearchResult(
            "Olivia Rodrigo - drivers license (Official Video)",
            "https://youtu.be/orig",
            242,
        )
        picked = pick_youtube_match(track, (cover, original))
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/orig")

    def test_matches_vietnamese_title_tokens(self) -> None:
        track = SpotifyTrack("Nơi Này Có Anh", ("Sơn Tùng M-TP",), 260)
        picked = pick_youtube_match(
            track,
            (
                SearchResult(
                    "Sơn Tùng M-TP - Nơi Này Có Anh",
                    "https://youtu.be/vn",
                    261,
                ),
            ),
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/vn")

    def test_returns_none_when_results_are_empty(self) -> None:
        track = SpotifyTrack("Song", ("Artist",), 120)
        self.assertIsNone(pick_youtube_match(track, ()))

    def test_skips_blank_youtube_titles_and_uses_next_candidate(self) -> None:
        track = SpotifyTrack("Song", ("Artist",), 180)
        picked = pick_youtube_match(
            track,
            (
                SearchResult("   ", "https://youtu.be/blank", 180),
                SearchResult("", "https://youtu.be/empty", 180),
                SearchResult("Artist - Song", "https://youtu.be/ok", 181),
            ),
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/ok")

    def test_missing_durations_still_match_on_title_and_artist(self) -> None:
        track = SpotifyTrack("Song", ("Artist",))
        picked = pick_youtube_match(
            track,
            (
                SearchResult(
                    "Artist - Song Official Audio",
                    "https://youtu.be/ok",
                ),
            ),
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/ok")

    def test_does_not_penalize_remix_when_spotify_title_already_says_remix(
        self,
    ) -> None:
        track = SpotifyTrack("Song (Remix)", ("Artist",), 200)
        studio = SearchResult("Artist - Song", "https://youtu.be/studio", 200)
        remix = SearchResult(
            "Artist - Song Remix Official Audio",
            "https://youtu.be/remix",
            200,
        )
        picked = pick_youtube_match(track, (studio, remix))
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/remix")

    def test_does_not_penalize_live_when_spotify_title_already_says_live(
        self,
    ) -> None:
        track = SpotifyTrack("Song (Live)", ("Artist",), 200)
        studio = SearchResult("Artist - Song", "https://youtu.be/studio", 200)
        live = SearchResult(
            "Artist - Song Live Official Audio",
            "https://youtu.be/live",
            200,
        )
        picked = pick_youtube_match(track, (studio, live))
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/live")

    def test_does_not_penalize_karaoke_when_spotify_title_already_says_karaoke(
        self,
    ) -> None:
        track = SpotifyTrack("Song Karaoke", ("Artist",), 200)
        studio = SearchResult("Artist - Song", "https://youtu.be/studio", 200)
        karaoke = SearchResult(
            "Artist - Song Karaoke Official Audio",
            "https://youtu.be/kara",
            200,
        )
        picked = pick_youtube_match(track, (studio, karaoke))
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/kara")

    def test_short_title_without_artist_or_close_duration_is_rejected(self) -> None:
        track = SpotifyTrack("Stay", ("Artist",), 180)
        picked = pick_youtube_match(
            track,
            (
                SearchResult(
                    "Stay night lofi mix",
                    "https://youtu.be/lofi",
                    210,
                ),
            ),
        )
        self.assertIsNone(picked)

    def test_short_title_with_close_duration_can_match(self) -> None:
        track = SpotifyTrack("Stay", ("Artist",), 141)
        picked = pick_youtube_match(
            track,
            (SearchResult("Stay", "https://youtu.be/stay", 141),),
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.url, "https://youtu.be/stay")


if __name__ == "__main__":
    unittest.main()
