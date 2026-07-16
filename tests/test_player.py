from __future__ import annotations

import asyncio
import contextlib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord

from src.ducking import DuckingAudioSource
from src.media import Track
from src.player import GuildPlayer, PlayerManager
from src.tts import TTSError, TextToSpeech, now_playing_speech


class _FakeAudioSource(discord.AudioSource):
    def read(self) -> bytes:
        return b"\x00" * 3840

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        return None


def _write_fake_mp3(text: str, dest: Path) -> None:
    dest.write_bytes(b"ID3" + text.encode("utf-8") + b"\x00" * 16)


class GuildPlayerControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.player = object.__new__(GuildPlayer)
        self.player.loop_current = False
        self.player.guild = Mock()

    def test_toggle_loop_changes_state(self) -> None:
        self.assertTrue(self.player.toggle_loop())
        self.assertFalse(self.player.toggle_loop())

    def test_skip_stops_active_voice_client_and_disables_loop(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        self.player.guild.voice_client = voice_client
        self.player.loop_current = True

        self.assertTrue(self.player.skip())
        self.assertFalse(self.player.loop_current)
        voice_client.stop.assert_called_once_with()

    def test_skip_returns_false_when_idle(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = False
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client

        self.assertFalse(self.player.skip())
        voice_client.stop.assert_not_called()


class GuildPlayerAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    """Integration of text announce + TTS on the shipped GuildPlayer path."""

    def setUp(self) -> None:
        self.player = object.__new__(GuildPlayer)
        self.player.bot = Mock()
        self.player.bot.loop = asyncio.get_event_loop()
        self.player.guild = Mock()
        self.player.guild.id = 42
        self.player.volume = 0.7
        self.player.tts = TextToSpeech(synthesizer=_write_fake_mp3)
        self.player.tts_enabled = True
        self.player._announce_channel = AsyncMock()
        self.player._send = AsyncMock()  # type: ignore[method-assign]

    async def test_announce_now_playing_sends_text_and_speaks(self) -> None:
        track = Track(title="Cool Song", stream_url="https://example.com/a.mp3")
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = False
        voice_client.is_paused.return_value = False

        def play_and_finish(source, *, after=None):
            if after is not None:
                after(None)

        voice_client.play.side_effect = play_and_finish
        self.player.guild.voice_client = voice_client

        fake_ffmpeg = _FakeAudioSource()
        with patch("src.tts.discord.FFmpegPCMAudio", return_value=fake_ffmpeg):
            await self.player._announce_now_playing(track)

        self.player._send.assert_awaited_once()
        sent = self.player._send.await_args.args[0]
        self.assertIn("Now playing", sent)
        self.assertIn("Cool Song", sent)

        voice_client.play.assert_called_once()
        played_source = voice_client.play.call_args.args[0]
        self.assertIsInstance(played_source, discord.PCMVolumeTransformer)

    async def test_announce_skips_tts_when_disabled(self) -> None:
        self.player.tts_enabled = False
        track = Track(title="Muted", stream_url="https://example.com/a.mp3")
        voice_client = MagicMock()
        self.player.guild.voice_client = voice_client

        await self.player._announce_now_playing(track)

        self.player._send.assert_awaited_once()
        voice_client.play.assert_not_called()

    async def test_speak_tts_failure_does_not_raise(self) -> None:
        boom = TextToSpeech(
            synthesizer=lambda _t, _p: (_ for _ in ()).throw(RuntimeError("no net"))
        )
        self.player.tts = boom
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = False
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client

        # Must not raise — player loop relies on soft-fail TTS.
        await self.player._speak_tts(now_playing_speech("x"))
        voice_client.play.assert_not_called()

    async def test_speak_tts_handles_tts_error(self) -> None:
        class FailTTS(TextToSpeech):
            def create_audio_source(self, text: str, *, volume: float):
                raise TTSError("empty")

        self.player.tts = FailTTS(synthesizer=_write_fake_mp3)
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = False
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client

        await self.player._speak_tts("hello")
        voice_client.play.assert_not_called()

    async def test_player_loop_plays_music_after_tts_and_keeps_queue(self) -> None:
        """Full loop: TTS announce → music play; second track still dequeued."""
        media = Mock()
        music_source = _FakeAudioSource()
        media.create_audio_source.return_value = music_source

        tts = TextToSpeech(synthesizer=_write_fake_mp3)
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        guild = Mock()
        guild.id = 7

        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = False
        voice_client.is_paused.return_value = False
        guild.voice_client = voice_client

        play_calls: list[str] = []

        def play_side_effect(source, *, after=None):
            # Announce TTS is PCMVolumeTransformer; music is wrapped in DuckingAudioSource.
            if isinstance(source, DuckingAudioSource):
                play_calls.append("music")
                self.assertIs(source.primary, music_source)
            else:
                play_calls.append("tts")
            if after is not None:
                after(None)

        voice_client.play.side_effect = play_side_effect

        idle = AsyncMock()
        player = GuildPlayer(
            bot,
            guild,
            media,
            volume=0.5,
            idle_timeout=0.5,
            on_idle=idle,
            tts=tts,
            tts_enabled=True,
        )
        channel = AsyncMock()
        track = Track(title="First", stream_url="https://example.com/1.mp3")

        fake_ffmpeg = _FakeAudioSource()
        with patch("src.tts.discord.FFmpegPCMAudio", return_value=fake_ffmpeg):
            await player.enqueue(track, channel)
            # Allow the player loop to process one track and idle out.
            await asyncio.wait_for(player._task, timeout=3.0)

        self.assertIn("tts", play_calls)
        self.assertIn("music", play_calls)
        # Order: speak first, then music.
        self.assertLess(play_calls.index("tts"), play_calls.index("music"))
        channel.send.assert_awaited()
        sent_messages = " ".join(
            call.args[0] for call in channel.send.await_args_list if call.args
        )
        self.assertIn("Now playing", sent_messages)
        self.assertIn("First", sent_messages)
        media.create_audio_source.assert_called()
        idle.assert_awaited()

    async def test_player_loop_continues_music_when_tts_fails(self) -> None:
        media = Mock()
        music_source = _FakeAudioSource()
        media.create_audio_source.return_value = music_source

        def fail_synth(_text: str, _dest: Path) -> None:
            raise RuntimeError("gTTS unavailable")

        tts = TextToSpeech(synthesizer=fail_synth)
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        guild = Mock()
        guild.id = 8
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = False
        voice_client.is_paused.return_value = False

        def play_music(source, *, after=None):
            if after is not None:
                after(None)

        voice_client.play.side_effect = play_music
        guild.voice_client = voice_client

        idle = AsyncMock()
        player = GuildPlayer(
            bot,
            guild,
            media,
            volume=0.5,
            idle_timeout=0.5,
            on_idle=idle,
            tts=tts,
            tts_enabled=True,
        )
        channel = AsyncMock()
        track = Track(title="Survives", stream_url="https://example.com/1.mp3")
        await player.enqueue(track, channel)
        await asyncio.wait_for(player._task, timeout=3.0)

        media.create_audio_source.assert_called_once()
        voice_client.play.assert_called()
        # Music still plays (via ducking mixer) despite TTS failure.
        self.assertTrue(
            any(
                isinstance(call.args[0], DuckingAudioSource)
                and call.args[0].primary is music_source
                for call in voice_client.play.call_args_list
            )
        )
        channel.send.assert_awaited()

    async def test_speak_over_music_injects_tts_into_mixer(self) -> None:
        class _FiniteSource(discord.AudioSource):
            def __init__(self) -> None:
                self._left = 2

            def read(self) -> bytes:
                if self._left <= 0:
                    return b""
                self._left -= 1
                return b"\x00" * 16

            def cleanup(self) -> None:
                return None

        music = _FakeAudioSource()
        mixer = DuckingAudioSource(music, duck_level=0.2)
        player = object.__new__(GuildPlayer)
        player.bot = Mock()
        player.bot.loop = asyncio.get_running_loop()
        player.guild = Mock()
        player.guild.id = 1
        player.volume = 0.7
        player.tts = TextToSpeech(synthesizer=_write_fake_mp3)
        player._mixer = mixer
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = True
        player.guild.voice_client = voice_client

        finite = _FiniteSource()

        async def pump_mixer() -> None:
            # Drive the mixer so the injected secondary can finish.
            for _ in range(10):
                await asyncio.sleep(0)
                if mixer.read() == b"" and not mixer.is_ducking:
                    break

        with patch("src.tts.discord.FFmpegPCMAudio", return_value=finite):
            pump = asyncio.create_task(pump_mixer())
            try:
                ok = await asyncio.wait_for(
                    player.speak_over_music("hello world"),
                    timeout=2.0,
                )
            finally:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
        self.assertTrue(ok)


class PlayerIdleSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_does_not_disconnect_when_session_keeps_connected(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        media = Mock()
        keep = Mock(return_value=True)
        manager = PlayerManager(
            bot,
            media,
            volume=0.5,
            idle_timeout=0.05,
            tts=None,
            tts_enabled=False,
            keep_connected=keep,
        )
        guild = Mock()
        guild.id = 99
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        guild.voice_client = voice_client

        player = manager.get_or_create(guild)
        # Wait for idle timeout with empty queue.
        await asyncio.wait_for(player._task, timeout=2.0)
        keep.assert_called()
        voice_client.disconnect.assert_not_called()
        self.assertIsNone(manager.get(99))


if __name__ == "__main__":
    unittest.main()
