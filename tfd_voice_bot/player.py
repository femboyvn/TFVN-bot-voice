"""Per-guild playback queues and lifecycle management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

import discord

from .media import MediaService, Track

log = logging.getLogger(__name__)

IdleCallback = Callable[[int, "GuildPlayer"], Awaitable[None]]


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
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.media = media
        self.volume = volume
        self.idle_timeout = idle_timeout
        self.on_idle = on_idle
        self.current: Track | None = None
        self.loop_current = False
        self._queue: asyncio.Queue[Track] = asyncio.Queue()
        self._playback_finished = asyncio.Event()
        self._announce_channel: discord.abc.Messageable | None = None
        self._closed = False
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

            try:
                source = self.media.create_audio_source(self.current, volume=self.volume)
                voice_client.play(source, after=self._after_playback)
            except Exception:
                log.exception("Could not start playback in guild %s", self.guild.id)
                await self._send("Could not start the queued track.")
                self.current = None
                continue

            await self._send(f"Now playing: **{discord.utils.escape_markdown(self.current.title)}**")
            await self._playback_finished.wait()

    def _after_playback(self, error: Exception | None) -> None:
        if error:
            log.error("Playback failed in guild %s: %s", self.guild.id, error)
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
    ) -> None:
        self.bot = bot
        self.media = media
        self.volume = volume
        self.idle_timeout = idle_timeout
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
            )
            self._players[guild.id] = player
        return player

    async def remove(self, guild_id: int) -> bool:
        player = self._players.pop(guild_id, None)
        if player is None:
            return False
        await player.close()
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
        voice_client = player.guild.voice_client
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)
