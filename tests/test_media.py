from __future__ import annotations

import shlex
import unittest
from unittest.mock import patch

from src.media import (
    PLAYLIST_LIMIT,
    MediaBatch,
    MediaExtractionError,
    MediaService,
    QueuedTrack,
    Track,
    format_duration,
    parse_jump_timestamp,
)


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


if __name__ == "__main__":
    unittest.main()
