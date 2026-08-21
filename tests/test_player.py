from __future__ import annotations

import asyncio
import contextlib
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import discord

from src.ducking import DuckingAudioSource
from src.media import MediaExtractionError, QueuedTrack, Track
from src.player import (
    ControlResult,
    GuildPlayer,
    JumpResult,
    PlaybackState,
    PlayerManager,
    PlayerSnapshot,
)
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
        self.player.current = None
        self.player._closed = False
        self.player._music_active = False
        self.player._mixer = None
        self.player._pending_jump = None
        self.player._skip_requested = False
        self.player._resolved_track = None
        self.player._state = PlaybackState.IDLE

    def test_toggle_loop_changes_state(self) -> None:
        self.assertTrue(self.player.toggle_loop())
        self.assertFalse(self.player.toggle_loop())

    def test_skip_stops_active_voice_client_and_disables_loop(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        self.player.guild.voice_client = voice_client
        self.player.loop_current = True
        self.player._music_active = True

        self.assertTrue(self.player.skip())
        self.assertFalse(self.player.loop_current)
        self.assertTrue(self.player._skip_requested)
        voice_client.stop.assert_called_once_with()

    def test_skip_returns_false_when_idle(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = False
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client

        self.assertFalse(self.player.skip())
        voice_client.stop.assert_not_called()

    def test_jump_stops_current_track_and_records_offset(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Long song",
            stream_url="https://example.test/song",
            duration=4000,
        )
        self.player._music_active = True

        result = self.player.jump(3723)

        self.assertIs(result, JumpResult.SUCCESS)
        self.assertEqual(self.player._pending_jump.offset, 3723)
        self.assertFalse(self.player._pending_jump.paused)
        voice_client.stop.assert_called_once_with()

    def test_latest_jump_wins_while_restart_is_pending(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Long song",
            stream_url="https://example.test/song",
            duration=120,
        )
        self.player._music_active = True

        self.assertIs(self.player.jump(10), JumpResult.SUCCESS)
        self.assertIs(self.player.jump(30), JumpResult.SUCCESS)

        self.assertEqual(self.player._pending_jump.offset, 30)
        voice_client.stop.assert_called_once_with()

    def test_jump_preserves_paused_state(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Paused song",
            stream_url="https://example.test/song",
            duration=120,
        )
        self.player._music_active = True
        self.player._mixer = DuckingAudioSource(_FakeAudioSource())
        self.player._mixer.pause_primary()

        self.assertIs(self.player.jump(30), JumpResult.SUCCESS)

        self.assertTrue(self.player._pending_jump.paused)
        voice_client.stop.assert_called_once_with()

    def test_jump_rejects_nonexistent_offsets(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Short song",
            stream_url="https://example.test/song",
            duration=30,
        )
        self.player._music_active = True

        for offset in (-1, 30, 31):
            with self.subTest(offset=offset):
                self.assertIs(self.player.jump(offset), JumpResult.OUT_OF_RANGE)

        voice_client.stop.assert_not_called()

    def test_jump_rejects_track_with_unknown_duration(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Live stream",
            stream_url="https://example.test/live",
        )
        self.player._music_active = True

        self.assertIs(self.player.jump(10), JumpResult.UNKNOWN_DURATION)
        voice_client.stop.assert_not_called()

    def test_jump_does_not_control_now_playing_tts(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Announced song",
            stream_url="https://example.test/song",
            duration=120,
        )

        self.assertIs(self.player.jump(10), JumpResult.NOT_PLAYING)
        voice_client.stop.assert_not_called()

    def test_skip_overrides_pending_jump(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Long song",
            stream_url="https://example.test/song",
            duration=120,
        )
        self.player._music_active = True
        self.assertIs(self.player.jump(10), JumpResult.SUCCESS)

        self.assertTrue(self.player.skip())

        self.assertIsNone(self.player._pending_jump)
        self.assertTrue(self.player._skip_requested)
        voice_client.stop.assert_called_once_with()

    def test_pause_and_resume_are_guarded_and_typed(self) -> None:
        voice_client = Mock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = True
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Music",
            stream_url="https://example.test/music",
        )
        self.player._music_active = True
        self.player._mixer = DuckingAudioSource(_FakeAudioSource())

        self.assertIs(self.player.pause(), ControlResult.SUCCESS)
        self.assertIs(self.player.state, PlaybackState.PAUSED)
        self.assertTrue(self.player._mixer.is_primary_paused)
        voice_client.pause.assert_not_called()

        self.assertIs(self.player.pause(), ControlResult.ALREADY_PAUSED)
        self.assertIs(self.player.resume(), ControlResult.SUCCESS)
        self.assertIs(self.player.state, PlaybackState.PLAYING)
        self.assertFalse(self.player._mixer.is_primary_paused)
        voice_client.resume.assert_not_called()

        self.assertIs(self.player.resume(), ControlResult.NOT_PAUSED)

    def test_pause_does_not_control_standalone_tts(self) -> None:
        voice_client = Mock()
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = True
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client
        self.player.current = Track(
            title="Still loading",
            stream_url="https://example.test/music",
        )

        self.assertIs(self.player.pause(), ControlResult.NOT_PLAYING)
        voice_client.pause.assert_not_called()


class _ControlledVoiceClient:
    """Voice-client fake whose playback callbacks are fired by the test."""

    def __init__(self) -> None:
        self.connected = True
        self.playing = False
        self.paused = False
        self.stop_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0
        self.callbacks: list = []
        self.sources: list[discord.AudioSource] = []
        self.play_notifications: asyncio.Queue[None] = asyncio.Queue()
        self._current_callback = None

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing

    def is_paused(self) -> bool:
        return self.paused

    def play(self, source: discord.AudioSource, *, after=None) -> None:
        self.sources.append(source)
        self.callbacks.append(after)
        self._current_callback = after
        self.playing = True
        self.paused = False
        self.play_notifications.put_nowait(None)

    def pause(self) -> None:
        self.pause_calls += 1
        self.playing = False
        self.paused = True

    def resume(self) -> None:
        self.resume_calls += 1
        self.playing = True
        self.paused = False

    def stop(self) -> None:
        self.stop_calls += 1
        self.finish_current()

    def finish_current(self) -> None:
        callback = self._current_callback
        self._current_callback = None
        self.playing = False
        self.paused = False
        if callback is not None:
            callback(None)


class GuildPlayerJumpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_jump_restarts_same_track_without_consuming_queue(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        guild = Mock()
        guild.id = 101
        voice_client = _ControlledVoiceClient()
        guild.voice_client = voice_client
        media = Mock()
        media.create_audio_source.side_effect = (
            lambda *args, **kwargs: _FakeAudioSource()
        )
        idle = AsyncMock()
        channel = AsyncMock()
        first = Track(
            title="First",
            stream_url="https://example.test/first",
            duration=120,
        )
        second = Track(
            title="Second",
            stream_url="https://example.test/second",
            duration=180,
        )
        player = GuildPlayer(
            bot,
            guild,
            media,
            volume=0.5,
            idle_timeout=0.05,
            on_idle=idle,
            tts=None,
            tts_enabled=False,
        )

        try:
            await player.enqueue(first, channel)
            await player.enqueue(second, channel)
            await asyncio.wait_for(
                voice_client.play_notifications.get(),
                timeout=1.0,
            )
            stale_callback = voice_client.callbacks[0]

            self.assertIs(player.pause(), ControlResult.SUCCESS)
            self.assertIs(player.jump(10), JumpResult.SUCCESS)
            self.assertIs(player.jump(30), JumpResult.SUCCESS)
            self.assertEqual(voice_client.stop_calls, 1)

            await asyncio.wait_for(
                voice_client.play_notifications.get(),
                timeout=1.0,
            )
            self.assertEqual(player.current.title, first.title)
            self.assertEqual(player._queue.qsize(), 1)
            self.assertEqual(
                media.create_audio_source.call_args_list[:2],
                [
                    call(first, volume=0.5),
                    call(first, volume=0.5, start_at=30),
                ],
            )
            self.assertEqual(channel.send.await_count, 1)
            self.assertTrue(player._mixer.is_primary_paused)
            self.assertEqual(voice_client.pause_calls, 0)
            self.assertIs(player.resume(), ControlResult.SUCCESS)

            # A stale callback belongs only to its original playback generation.
            stale_callback(None)
            await asyncio.sleep(0)
            self.assertEqual(len(voice_client.callbacks), 2)

            voice_client.finish_current()
            await asyncio.wait_for(
                voice_client.play_notifications.get(),
                timeout=1.0,
            )
            self.assertEqual(player.current.title, second.title)
            self.assertEqual(
                media.create_audio_source.call_args_list[2],
                call(second, volume=0.5),
            )

            voice_client.finish_current()
            await asyncio.wait_for(player._task, timeout=1.0)
            idle.assert_awaited_once()
        finally:
            await player.close(disconnect=False)


class GuildPlayerQueueTests(unittest.IsolatedAsyncioTestCase):
    def _make_player(
        self,
        *,
        media: Mock | None = None,
        voice_client: _ControlledVoiceClient | None = None,
        idle: AsyncMock | None = None,
        listener: AsyncMock | None = None,
    ) -> tuple[GuildPlayer, Mock, _ControlledVoiceClient, AsyncMock]:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        guild = Mock()
        guild.id = 202
        controlled_voice = voice_client or _ControlledVoiceClient()
        guild.voice_client = controlled_voice
        media_service = media or Mock()
        idle_callback = idle or AsyncMock()
        player = GuildPlayer(
            bot,
            guild,
            media_service,
            volume=0.5,
            idle_timeout=10.0,
            on_idle=idle_callback,
            tts=None,
            tts_enabled=False,
            on_state_change=listener,
        )
        return player, media_service, controlled_voice, idle_callback

    async def test_enqueue_many_snapshot_and_clear_are_waiting_only(self) -> None:
        player, _, _, _ = self._make_player()
        channel = AsyncMock()
        items = tuple(
            QueuedTrack(
                title=f"Song {index}",
                webpage_url=f"https://example.test/{index}",
                duration=index,
            )
            for index in range(3)
        )

        try:
            position = await player.enqueue_many(items, channel)
            snapshot = player.snapshot()

            self.assertEqual(position, 3)
            self.assertEqual(snapshot.queued, items)
            self.assertIsNone(snapshot.current)
            self.assertIs(snapshot.state, PlaybackState.IDLE)
            with self.assertRaises(FrozenInstanceError):
                snapshot.loop_current = True  # type: ignore[misc]

            self.assertEqual(await player.clear_queue(), 3)
            self.assertEqual(player.snapshot().queued, ())
            self.assertEqual(await player.clear_queue(), 0)
        finally:
            await player.close(disconnect=False)

    async def test_clear_queue_keeps_current_track_playing(self) -> None:
        media = Mock()
        media.resolve_queued = AsyncMock(
            return_value=Track(
                title="Current",
                stream_url="https://stream.test/current",
                duration=60,
            )
        )
        media.create_audio_source.return_value = _FakeAudioSource()
        player, _, voice_client, _ = self._make_player(media=media)
        channel = AsyncMock()
        current = QueuedTrack("Current", "https://youtube.test/current", 60)
        waiting = (
            QueuedTrack("Second", "https://youtube.test/second", 70),
            QueuedTrack("Third", "https://youtube.test/third", 80),
        )

        try:
            await player.enqueue(current, channel)
            await asyncio.wait_for(voice_client.play_notifications.get(), timeout=1.0)
            await player.enqueue_many(waiting, channel)

            self.assertEqual(await player.clear_queue(), 2)
            snapshot = player.snapshot()
            self.assertEqual(snapshot.current, current)
            self.assertEqual(snapshot.queued, ())
            self.assertIs(snapshot.state, PlaybackState.PLAYING)
            self.assertTrue(voice_client.is_playing())
        finally:
            await player.close(disconnect=False)

    async def test_loop_re_resolves_canonical_url_before_each_replay(self) -> None:
        media = Mock()
        first_stream = Track(
            title="Fresh one",
            stream_url="https://stream.test/one",
            duration=90,
        )
        second_stream = Track(
            title="Fresh two",
            stream_url="https://stream.test/two",
            duration=90,
        )
        media.resolve_queued = AsyncMock(side_effect=(first_stream, second_stream))
        media.create_audio_source.side_effect = lambda *_args, **_kwargs: _FakeAudioSource()
        player, _, voice_client, _ = self._make_player(media=media)
        channel = AsyncMock()
        queued = QueuedTrack("Loop me", "https://youtube.test/loop", 90)

        try:
            await player.enqueue(queued, channel)
            await asyncio.wait_for(voice_client.play_notifications.get(), timeout=1.0)
            self.assertTrue(player.toggle_loop())
            voice_client.finish_current()
            await asyncio.wait_for(voice_client.play_notifications.get(), timeout=1.0)

            self.assertEqual(media.resolve_queued.await_count, 2)
            self.assertEqual(
                media.create_audio_source.call_args_list,
                [
                    call(first_stream, volume=0.5),
                    call(second_stream, volume=0.5),
                ],
            )
            self.assertEqual(player.current, queued)
        finally:
            await player.close(disconnect=False)

    async def test_resolution_failure_announces_and_continues(self) -> None:
        media = Mock()
        playable = Track(
            title="Playable",
            stream_url="https://stream.test/good",
            duration=45,
        )
        media.resolve_queued = AsyncMock(
            side_effect=(MediaExtractionError("gone"), playable)
        )
        media.create_audio_source.return_value = _FakeAudioSource()
        player, _, voice_client, _ = self._make_player(media=media)
        channel = AsyncMock()
        failed = QueuedTrack("Unavailable", "https://youtube.test/bad")
        good = QueuedTrack("Good", "https://youtube.test/good", 45)

        try:
            await player.enqueue_many((failed, good), channel)
            await asyncio.wait_for(voice_client.play_notifications.get(), timeout=1.0)

            self.assertEqual(player.current, good)
            media.create_audio_source.assert_called_once_with(playable, volume=0.5)
            messages = [item.args[0] for item in channel.send.await_args_list]
            self.assertTrue(any("Không thể phát" in item for item in messages))
            self.assertTrue(any("Unavailable" in item for item in messages))
            self.assertTrue(any("Đang phát" in item and "Good" in item for item in messages))
        finally:
            await player.close(disconnect=False)

    async def test_skip_during_failure_message_does_not_skip_following_track(
        self,
    ) -> None:
        media = Mock()
        playable = Track(
            title="Playable",
            stream_url="https://stream.test/good",
            duration=45,
        )
        media.resolve_queued = AsyncMock(
            side_effect=(MediaExtractionError("gone"), playable)
        )
        media.create_audio_source.return_value = _FakeAudioSource()
        player, _, voice_client, _ = self._make_player(media=media)
        failure_message_started = asyncio.Event()
        release_failure_message = asyncio.Event()
        channel = AsyncMock()

        async def send(message: str) -> None:
            if "Không thể phát" in message:
                failure_message_started.set()
                await release_failure_message.wait()

        channel.send = AsyncMock(side_effect=send)
        failed = QueuedTrack("Unavailable", "https://youtube.test/bad")
        good = QueuedTrack("Good", "https://youtube.test/good", 45)

        try:
            await player.enqueue_many((failed, good), channel)
            await asyncio.wait_for(failure_message_started.wait(), timeout=1.0)
            self.assertTrue(player.skip())
            release_failure_message.set()
            await asyncio.wait_for(
                voice_client.play_notifications.get(),
                timeout=1.0,
            )

            self.assertEqual(player.current, good)
            media.create_audio_source.assert_called_once_with(
                playable,
                volume=0.5,
            )
        finally:
            release_failure_message.set()
            await player.close(disconnect=False)

    async def test_skip_cancels_slow_resolution_and_plays_next(self) -> None:
        media = Mock()
        resolution_started = asyncio.Event()
        resolution_cancelled = asyncio.Event()
        allow_hung_resolution = asyncio.Event()
        good_stream = Track(
            title="Good",
            stream_url="https://stream.test/good",
            duration=45,
        )

        async def resolve(item: QueuedTrack) -> Track:
            if item.title == "Slow":
                resolution_started.set()
                try:
                    await allow_hung_resolution.wait()
                except asyncio.CancelledError:
                    resolution_cancelled.set()
                    raise
                raise AssertionError("slow resolution should have been cancelled")
            return good_stream

        media.resolve_queued = AsyncMock(side_effect=resolve)
        media.create_audio_source.return_value = _FakeAudioSource()
        player, _, voice_client, _ = self._make_player(media=media)
        channel = AsyncMock()
        slow = QueuedTrack("Slow", "https://youtube.test/slow")
        good = QueuedTrack("Good", "https://youtube.test/good", 45)

        try:
            await player.enqueue_many((slow, good), channel)
            await asyncio.wait_for(resolution_started.wait(), timeout=1.0)
            self.assertIs(player.state, PlaybackState.LOADING)
            self.assertTrue(player.skip())
            await asyncio.wait_for(resolution_cancelled.wait(), timeout=1.0)
            await asyncio.wait_for(
                voice_client.play_notifications.get(),
                timeout=1.0,
            )

            self.assertEqual(player.current, good)
            self.assertEqual(player.snapshot().queued, ())
            media.create_audio_source.assert_called_once_with(
                good_stream,
                volume=0.5,
            )
        finally:
            allow_hung_resolution.set()
            await player.close(disconnect=False)

    async def test_disconnect_clears_current_and_all_waiting_tracks(self) -> None:
        voice_client = _ControlledVoiceClient()
        voice_client.connected = False
        idle = AsyncMock()
        player, _, _, _ = self._make_player(
            voice_client=voice_client,
            idle=idle,
        )
        channel = AsyncMock()
        tracks = (
            QueuedTrack("One", "https://youtube.test/one"),
            QueuedTrack("Two", "https://youtube.test/two"),
        )

        await player.enqueue_many(tracks, channel)
        await asyncio.wait_for(player._task, timeout=1.0)

        snapshot = player.snapshot()
        self.assertIsNone(snapshot.current)
        self.assertEqual(snapshot.queued, ())
        self.assertIs(snapshot.state, PlaybackState.IDLE)
        idle.assert_awaited_once_with(202, player)
        await player.close(disconnect=False)

    async def test_reconnect_during_disconnect_notification_keeps_player(self) -> None:
        disconnected = _ControlledVoiceClient()
        disconnected.connected = False
        reconnected = _ControlledVoiceClient()
        idle = AsyncMock()
        reconnected_during_notification = asyncio.Event()

        async def listener(_guild_id: int, snapshot: PlayerSnapshot) -> None:
            if (
                snapshot.state is PlaybackState.IDLE
                and snapshot.current is None
                and not snapshot.queued
            ):
                player.guild.voice_client = reconnected
                reconnected_during_notification.set()

        player, _, _, _ = self._make_player(
            voice_client=disconnected,
            idle=idle,
            listener=listener,
        )
        channel = AsyncMock()
        try:
            await player.enqueue(
                QueuedTrack("Lost", "https://youtube.test/lost"),
                channel,
            )
            await asyncio.wait_for(
                reconnected_during_notification.wait(),
                timeout=1.0,
            )
            await asyncio.sleep(0)

            idle.assert_not_awaited()
            self.assertFalse(player._task.done())
            self.assertIs(player.guild.voice_client, reconnected)
        finally:
            await player.close(disconnect=False)

    async def test_state_listener_observes_loading_play_pause_and_stop(self) -> None:
        media = Mock()
        media.resolve_queued = AsyncMock(
            return_value=Track(
                title="State song",
                stream_url="https://stream.test/state",
                duration=20,
            )
        )
        media.create_audio_source.return_value = _FakeAudioSource()
        listener = AsyncMock()
        player, _, voice_client, _ = self._make_player(
            media=media,
            listener=listener,
        )
        channel = AsyncMock()

        await player.enqueue(
            QueuedTrack("State song", "https://youtube.test/state", 20),
            channel,
        )
        await asyncio.wait_for(voice_client.play_notifications.get(), timeout=1.0)
        self.assertIs(player.pause(), ControlResult.SUCCESS)
        self.assertIs(player.resume(), ControlResult.SUCCESS)
        await asyncio.sleep(0)
        await player.close(disconnect=False)

        states = [item.args[1].state for item in listener.await_args_list]
        self.assertIn(PlaybackState.LOADING, states)
        self.assertIn(PlaybackState.PLAYING, states)
        self.assertIn(PlaybackState.PAUSED, states)
        self.assertIs(states[-1], PlaybackState.IDLE)


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
        self.assertIn("Đang phát", sent)
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
        self.assertIn("Đang phát", sent_messages)
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

    async def test_speak_over_paused_music_keeps_primary_frozen(self) -> None:
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

        class _CountingMusic(discord.AudioSource):
            def __init__(self) -> None:
                self.reads = 0

            def read(self) -> bytes:
                self.reads += 1
                return b"\x01\x00" * 8

            def cleanup(self) -> None:
                return None

        music = _CountingMusic()
        mixer = DuckingAudioSource(music, duck_level=0.2)
        mixer.pause_primary()
        player = object.__new__(GuildPlayer)
        player.bot = Mock()
        player.bot.loop = asyncio.get_running_loop()
        player.guild = Mock()
        player.guild.id = 1
        player.volume = 0.7
        player.tts = TextToSpeech(synthesizer=_write_fake_mp3)
        player._mixer = mixer
        player._music_active = True
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
        self.assertTrue(mixer.is_primary_paused)
        self.assertEqual(music.reads, 0)


class PlayerIdleSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_during_idle_notification_keeps_player_alive(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        media = Mock()
        media.create_audio_source.return_value = _FakeAudioSource()
        guild = Mock()
        guild.id = 70
        voice_client = _ControlledVoiceClient()
        guild.voice_client = voice_client
        on_idle = AsyncMock()
        idle_notification = asyncio.Event()
        release_notification = asyncio.Event()

        async def listener(_guild_id: int, snapshot: PlayerSnapshot) -> None:
            if snapshot.state is PlaybackState.IDLE and not snapshot.queued:
                idle_notification.set()
                await release_notification.wait()

        player = GuildPlayer(
            bot,
            guild,
            media,
            volume=0.5,
            idle_timeout=0.02,
            on_idle=on_idle,
            tts=None,
            tts_enabled=False,
            on_state_change=listener,
        )
        channel = AsyncMock()
        try:
            await asyncio.wait_for(idle_notification.wait(), timeout=1.0)
            await player.enqueue(
                Track("Late", "https://stream.test/late", duration=10),
                channel,
            )
            release_notification.set()
            await asyncio.wait_for(
                voice_client.play_notifications.get(),
                timeout=1.0,
            )
            on_idle.assert_not_awaited()
            self.assertEqual(player.current.title, "Late")
        finally:
            release_notification.set()
            await player.close(disconnect=False)

    async def test_touch_restarts_an_in_progress_idle_deadline(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        media = Mock()
        manager = PlayerManager(
            bot,
            media,
            volume=0.5,
            idle_timeout=0.2,
            tts=None,
            tts_enabled=False,
        )
        guild = Mock()
        guild.id = 71
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.disconnect = AsyncMock()
        guild.voice_client = voice_client

        player = await manager.get_or_create(guild)
        await asyncio.sleep(0.1)
        self.assertIs(await manager.get_or_create(guild), player)
        await asyncio.sleep(0.15)

        self.assertIs(manager.get(guild.id), player)
        self.assertFalse(player._task.done())
        await manager.remove(guild.id, disconnect=False)

    async def test_activity_reservation_blocks_idle_retirement(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        manager = PlayerManager(
            bot,
            Mock(),
            volume=0.5,
            idle_timeout=0.02,
            tts=None,
            tts_enabled=False,
        )
        guild = Mock()
        guild.id = 73
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.disconnect = AsyncMock()
        guild.voice_client = voice_client

        player = await manager.get_or_create(guild)
        player.reserve_activity()
        try:
            await asyncio.sleep(0.08)
            self.assertIs(manager.get(guild.id), player)
            self.assertFalse(player._task.done())
            voice_client.disconnect.assert_not_awaited()
        finally:
            player.release_activity()
            await manager.remove(guild.id, disconnect=False)

    async def test_create_waits_for_previous_player_close(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        media = Mock()
        manager = PlayerManager(
            bot,
            media,
            volume=0.5,
            idle_timeout=10.0,
            tts=None,
            tts_enabled=False,
        )
        guild = Mock()
        guild.id = 72
        voice_client = MagicMock()
        voice_client.is_connected.return_value = True
        voice_client.disconnect = AsyncMock()
        guild.voice_client = voice_client
        old_player = await manager.get_or_create(guild)
        close_started = asyncio.Event()
        allow_close = asyncio.Event()
        original_close = old_player.close

        async def blocked_close(*, disconnect: bool = True) -> None:
            close_started.set()
            await allow_close.wait()
            await original_close(disconnect=disconnect)

        with patch.object(old_player, "close", new=blocked_close):
            removing = asyncio.create_task(manager.remove(guild.id))
            await asyncio.wait_for(close_started.wait(), timeout=1.0)
            creating = asyncio.create_task(manager.get_or_create(guild))
            await asyncio.sleep(0)
            self.assertFalse(creating.done())
            allow_close.set()
            self.assertTrue(await asyncio.wait_for(removing, timeout=1.0))
            new_player = await asyncio.wait_for(creating, timeout=1.0)

        self.assertIsNot(new_player, old_player)
        self.assertIs(manager.get(guild.id), new_player)
        await manager.remove(guild.id, disconnect=False)

    async def test_manager_fans_out_player_state_snapshots(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        media = Mock()
        media.resolve_queued = AsyncMock(
            return_value=Track(
                title="Managed",
                stream_url="https://stream.test/managed",
                duration=30,
            )
        )
        media.create_audio_source.return_value = _FakeAudioSource()
        manager = PlayerManager(
            bot,
            media,
            volume=0.5,
            idle_timeout=10.0,
            tts=None,
            tts_enabled=False,
        )
        listener = AsyncMock()
        manager.add_state_listener(listener)
        guild = Mock()
        guild.id = 77
        voice_client = _ControlledVoiceClient()
        guild.voice_client = voice_client
        channel = AsyncMock()

        player = await manager.get_or_create(guild)
        await player.enqueue(
            QueuedTrack("Managed", "https://youtube.test/managed", 30),
            channel,
        )
        await asyncio.wait_for(voice_client.play_notifications.get(), timeout=1.0)
        await manager.remove(77, disconnect=False)

        self.assertGreaterEqual(listener.await_count, 3)
        self.assertTrue(
            all(item.args[0] == 77 for item in listener.await_args_list)
        )
        self.assertTrue(
            all(
                isinstance(item.args[1], PlayerSnapshot)
                for item in listener.await_args_list
            )
        )
        self.assertIs(listener.await_args_list[-1].args[1].state, PlaybackState.IDLE)

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

        player = await manager.get_or_create(guild)
        # Wait for idle timeout with empty queue.
        await asyncio.wait_for(player._task, timeout=2.0)
        keep.assert_called()
        voice_client.disconnect.assert_not_called()
        self.assertIsNone(manager.get(99))


if __name__ == "__main__":
    unittest.main()
