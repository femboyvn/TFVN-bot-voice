"""Unit tests for the shipped TTS transform and audio-source path."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import discord

from src.tts import TTSError, TextToSpeech, now_playing_speech


class _FakeAudioSource(discord.AudioSource):
    """Minimal AudioSource so PCMVolumeTransformer accepts the mock FFmpeg source."""

    def read(self) -> bytes:
        return b"\x00" * discord.player.OpusEncoder.FRAME_SIZE

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        return None


def _write_fake_mp3(text: str, dest: Path) -> None:
    """Local I/O-boundary stand-in: writes non-empty bytes (not real speech)."""
    # Minimal non-empty payload; synthesis quality is out of scope for unit tests.
    dest.write_bytes(b"ID3" + text.encode("utf-8") + b"\x00" * 32)


class NowPlayingSpeechTests(unittest.TestCase):
    def test_builds_plain_phrase(self) -> None:
        self.assertEqual(now_playing_speech("Song Title"), "Đang phát Song Title")

    def test_falls_back_for_blank_title(self) -> None:
        self.assertEqual(now_playing_speech("   "), "Đang phát Không có tiêu đề")


class TextToSpeechTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tts = TextToSpeech(lang="en", synthesizer=_write_fake_mp3)

    def test_synthesize_rejects_empty_text(self) -> None:
        with self.assertRaisesRegex(TTSError, "non-empty"):
            self.tts.synthesize("   ")

    def test_synthesize_returns_nonempty_audio_file(self) -> None:
        path = self.tts.synthesize("Now playing test track")
        try:
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 0)
            self.assertIn(b"Now playing test track", path.read_bytes())
        finally:
            path.unlink(missing_ok=True)

    def test_synthesize_raises_when_backend_writes_empty(self) -> None:
        def empty_writer(_text: str, dest: Path) -> None:
            dest.write_bytes(b"")

        tts = TextToSpeech(synthesizer=empty_writer)
        with self.assertRaisesRegex(TTSError, "empty audio"):
            tts.synthesize("hello")

    def test_synthesize_wraps_backend_exceptions(self) -> None:
        def boom(_text: str, _dest: Path) -> None:
            raise RuntimeError("network down")

        tts = TextToSpeech(synthesizer=boom)
        with self.assertRaisesRegex(TTSError, "TTS synthesis failed"):
            tts.synthesize("hello")

    def test_create_audio_source_returns_volume_transformer_and_path(self) -> None:
        fake_source = _FakeAudioSource()
        with patch(
            "src.tts.discord.FFmpegPCMAudio", return_value=fake_source
        ) as mock_ff:
            source, path = self.tts.create_audio_source("Hello world", volume=0.5)
            try:
                self.assertIsInstance(source, discord.PCMVolumeTransformer)
                self.assertEqual(source.volume, 0.5)
                self.assertIs(source.original, fake_source)
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)
                mock_ff.assert_called_once()
                self.assertEqual(str(path), mock_ff.call_args.args[0])
            finally:
                path.unlink(missing_ok=True)
                source.cleanup()

    def test_create_audio_source_cleans_up_path_if_ffmpeg_fails(self) -> None:
        with patch(
            "src.tts.discord.FFmpegPCMAudio",
            side_effect=RuntimeError("ffmpeg missing"),
        ):
            with self.assertRaises(RuntimeError):
                self.tts.create_audio_source("Hello", volume=0.7)
        # No leftover temp files under the tfd-tts prefix should remain owned by us;
        # synthesize creates then unlinks on FFmpeg failure.


class TextToSpeechDefaultBackendTests(unittest.TestCase):
    """Drive the real default synthesizer entry (gTTS) with a stubbed gTTS class."""

    def test_default_path_calls_gtts_save(self) -> None:
        saved: list[str] = []

        class FakeGTTS:
            def __init__(self, text: str, lang: str) -> None:
                self.text = text
                self.lang = lang

            def save(self, path: str) -> None:
                Path(path).write_bytes(b"fake-gtts-" + self.text.encode())
                saved.append(path)

        tts = TextToSpeech(lang="en")  # no injected synthesizer → default path
        with patch("gtts.gTTS", FakeGTTS):
            path = tts.synthesize("queue announcement")
        try:
            self.assertEqual(len(saved), 1)
            self.assertEqual(path, Path(saved[0]))
            self.assertGreater(path.stat().st_size, 0)
            self.assertIn(b"queue announcement", path.read_bytes())
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
