"""Per-guild playback queues and lifecycle management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import discord

from .ducking import DEFAULT_DUCK_LEVEL, DuckingAudioSource
from .media import MediaService, Track
from .tts import (
    TTSError,
    TextToSpeech,
    now_playing_speech,
    play_tts_on_voice_client,
    tts_playback_timeout,
)

log = logging.getLogger(__name__)

IdleCallback = Callable[[int, "GuildPlayer"], Awaitable[None]]
KeepConnected = Callable[[int], bool]


class GuildPlayer:
    """Owns the playback queue and worker task for one Discord guild."""

    def __init__(
        self,
        bot: discord.Client,
        guild: discord.Guild,
        media: MediaService,
        *,
        volume: float,
        idle_timeout: float,
        on_idle: IdleCallback,
        tts: TextToSpeech | None = None,
        tts_enabled: bool = True,
        duck_level: float = DEFAULT_DUCK_LEVEL,
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.media = media
        self.volume = volume
        self.idle_timeout = idle_timeout
        self.on_idle = on_idle
        self.tts = tts
        self.tts_enabled = tts_enabled
        self.duck_level = duck_level
        self.current: Track | None = None
        self.loop_current = False
        self._queue: asyncio.Queue[Track] = asyncio.Queue()
        self._playback_finished = asyncio.Event()
        self._announce_channel: discord.abc.Messageable | None = None
        self._closed = False
        self._mixer: DuckingAudioSource | None = None
        self._task = asyncio.create_task(
            self._player_loop(),
            name=f"guild-player-{guild.id}",
        )

    async def enqueue(
        self,
        track: Track,
        announce_channel: discord.abc.Messageable,
    ) -> int:
        self._announce_channel = announce_channel
        await self._queue.put(track)
        return self._queue.qsize()

    def toggle_loop(self) -> bool:
        self.loop_current = not self.loop_current
        return self.loop_current

    def skip(self) -> bool:
        voice_client = self.guild.voice_client
        if not voice_client or not (voice_client.is_playing() or voice_client.is_paused()):
            return False
        self.loop_current = False
        voice_client.stop()
        return True

    async def close(self, *, disconnect: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self.loop_current = False
        self.current = None
        self._drain_queue()

        voice_client = self.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()

        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

        if disconnect and voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)

    async def _player_loop(self) -> None:
        while not self._closed:
            self._playback_finished.clear()

            if self.current is None or not self.loop_current:
                try:
                    self.current = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=self.idle_timeout,
                    )
                except TimeoutError:
                    await self.on_idle(self.guild.id, self)
                    return

            voice_client = self.guild.voice_client
            if not voice_client or not voice_client.is_connected():
                self.current = None
                continue

            # Text + spoken announcements before music; TTS failures never block the queue.
            # Status strings are Vietnamese (customer UI).
            await self._announce_now_playing(self.current)

            try:
                source = self.media.create_audio_source(self.current, volume=self.volume)
                mixer = DuckingAudioSource(source, duck_level=self.duck_level)
                self._mixer = mixer
                voice_client.play(mixer, after=self._after_playback)
            except Exception:
                self._mixer = None
                log.exception("Could not start playback in guild %s", self.guild.id)
                await self._send("Không thể phát bài trong hàng đợi.")
                self.current = None
                continue

            await self._playback_finished.wait()
            self._mixer = None

    async def _announce_now_playing(self, track: Track) -> None:
        """Send text status and speak the same announcement in the voice channel."""
        title = track.title
        await self._send(
            f"Đang phát: **{discord.utils.escape_markdown(title)}**"
        )
        if not self.tts_enabled or self.tts is None:
            return
        await self._speak_tts(now_playing_speech(title))

    async def _speak_tts(self, text: str) -> None:
        """Play TTS audio on the guild voice client; never raise into the player loop."""
        voice_client = self.guild.voice_client
        if not voice_client or not voice_client.is_connected() or self.tts is None:
            return
        await play_tts_on_voice_client(
            self.bot,
            voice_client,
            self.tts,
            text,
            volume=self.volume,
            skip_if_busy=True,
        )

    async def speak_over_music(self, text: str) -> bool:
        """Duck music and mix TTS over it. Returns False if music is not playing."""
        mixer = self._mixer
        voice_client = self.guild.voice_client
        if (
            mixer is None
            or self.tts is None
            or not voice_client
            or not voice_client.is_connected()
            or not voice_client.is_playing()
        ):
            return False

        audio_path: Path | None = None
        try:
            source, audio_path = await asyncio.to_thread(
                self.tts.create_audio_source,
                text,
                volume=self.volume,
            )
        except TTSError as exc:
            log.warning("TTS synthesis skipped in guild %s: %s", self.guild.id, exc)
            return False
        except Exception:
            log.exception("Unexpected TTS failure in guild %s", self.guild.id)
            return False

        # Keep volume transformer on TTS; mixer only ducks the music (primary).
        done = mixer.inject_secondary(source)
        play_timeout = tts_playback_timeout(text)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(done.wait),
                timeout=play_timeout,
            )
            return True
        except TimeoutError:
            log.warning(
                "Ducked TTS timed out after %.1fs in guild %s",
                play_timeout,
                self.guild.id,
            )
            mixer.clear_secondary()
            return False
        finally:
            # FFmpeg may still hold the file briefly after the secondary ends.
            await asyncio.sleep(0.05)
            if audio_path is not None:
                with contextlib.suppress(OSError):
                    audio_path.unlink(missing_ok=True)

    def _after_playback(self, error: Exception | None) -> None:
        if error:
            log.error("Playback failed in guild %s: %s", self.guild.id, error)
        self._mixer = None
        self.bot.loop.call_soon_threadsafe(self._playback_finished.set)

    async def _send(self, message: str) -> None:
        if self._announce_channel is None:
            return
        try:
            await self._announce_channel.send(message)
        except discord.HTTPException:
            log.warning("Could not send playback status in guild %s", self.guild.id)

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return


class PlayerManager:
    """Creates and disposes guild players without exposing global state."""

    def __init__(
        self,
        bot: discord.Client,
        media: MediaService,
        *,
        volume: float,
        idle_timeout: float,
        tts: TextToSpeech | None = None,
        tts_enabled: bool = True,
        duck_level: float = DEFAULT_DUCK_LEVEL,
        keep_connected: KeepConnected | None = None,
    ) -> None:
        self.bot = bot
        self.media = media
        self.volume = volume
        self.idle_timeout = idle_timeout
        self.tts = tts
        self.tts_enabled = tts_enabled
        self.duck_level = duck_level
        self.keep_connected = keep_connected or (lambda _guild_id: False)
        self._players: dict[int, GuildPlayer] = {}

    def get(self, guild_id: int) -> GuildPlayer | None:
        return self._players.get(guild_id)

    def get_or_create(self, guild: discord.Guild) -> GuildPlayer:
        player = self._players.get(guild.id)
        if player is None:
            player = GuildPlayer(
                self.bot,
                guild,
                self.media,
                volume=self.volume,
                idle_timeout=self.idle_timeout,
                on_idle=self._remove_idle,
                tts=self.tts,
                tts_enabled=self.tts_enabled,
                duck_level=self.duck_level,
            )
            self._players[guild.id] = player
        return player

    async def remove(self, guild_id: int, *, disconnect: bool = True) -> bool:
        player = self._players.pop(guild_id, None)
        if player is None:
            return False
        await player.close(disconnect=disconnect)
        return True

    async def close_all(self) -> None:
        players = list(self._players.values())
        self._players.clear()
        await asyncio.gather(
            *(player.close() for player in players),
            return_exceptions=True,
        )

    async def _remove_idle(self, guild_id: int, player: GuildPlayer) -> None:
        if self._players.get(guild_id) is not player:
            return
        self._players.pop(guild_id, None)
        # Active voice-chat sessions stay connected after the music queue idles out.
        # Do not cancel the player task here — it is mid-return from the idle wait.
        if self.keep_connected(guild_id):
            return
        voice_client = player.guild.voice_client
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)
