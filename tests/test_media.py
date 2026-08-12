from __future__ import annotations

import shlex
import unittest
from unittest.mock import patch

from src.media import (
    MediaService,
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
