"""Per-guild playback queues and lifecycle management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
from pathlib import Path

import discord

from .ducking import DEFAULT_DUCK_LEVEL, DuckingAudioSource
from .media import MediaService, QueuedTrack, Track
from .tts import (
    TTS_FFMPEG_OPTIONS,
    TTSError,
    TextToSpeech,
    normalize_tts_language,
    now_playing_speech,
    play_tts_on_voice_client,
    tts_playback_timeout,
)
from .voice import disconnect_guild_voice_client

log = logging.getLogger(__name__)

IdleCallback = Callable[[int, "GuildPlayer"], Awaitable[None]]
KeepConnected = Callable[[int], bool]


class JumpResult(Enum):
    """Result of asking a guild player to jump within its current track."""

    SUCCESS = auto()
    NOT_PLAYING = auto()
    OUT_OF_RANGE = auto()
    UNKNOWN_DURATION = auto()


class PlaybackState(Enum):
    """User-visible state of a guild's music worker."""

    IDLE = auto()
    LOADING = auto()
    PLAYING = auto()
    PAUSED = auto()


class ControlResult(Enum):
    """Result of a guarded pause or resume request."""

    SUCCESS = auto()
    NOT_PLAYING = auto()
    ALREADY_PAUSED = auto()
    NOT_PAUSED = auto()


class LoopMode(Enum):
    """User-visible looping for the current track or the whole queue."""

    OFF = auto()
    TRACK = auto()
    QUEUE = auto()


HISTORY_LIMIT = 20


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    """Immutable playback data safe for commands and interaction views."""

    current: QueuedTrack | None
    queued: tuple[QueuedTrack, ...]
    state: PlaybackState
    loop_current: bool
    loop_queue: bool = False
    can_previous: bool = False


@dataclass(frozen=True, slots=True)
class GuildAudioSettings:
    """Process-lifetime audio preferences shared within one Discord guild."""

    music_volume: float
    duck_level: float
    tts_language: str
    name_announce: bool = False


StateChangeListener = Callable[[int, PlayerSnapshot], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _QueuedEntry:
    metadata: QueuedTrack
    announce_channel: discord.abc.Messageable
    # Old callers may still enqueue a direct stream-only Track. It has no
    # canonical URL that can safely be re-extracted, so retain it as a fallback.
    legacy_track: Track | None = None


@dataclass(slots=True)
class _JumpRequest:
    offset: int
    paused: bool


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
        tts_volume: float | None = None,
        tts_enabled: bool = True,
        duck_level: float = DEFAULT_DUCK_LEVEL,
        on_state_change: StateChangeListener | None = None,
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.media = media
        self.music_volume = volume
        # Runtime music gain is intentionally independent from speech gain.
        self.tts_volume = volume if tts_volume is None else tts_volume
        self.idle_timeout = idle_timeout
        self.on_idle = on_idle
        self.tts = tts
        self.tts_enabled = tts_enabled
        self.duck_level = duck_level
        self.current: QueuedTrack | None = None
        self.loop_current = False
        self.loop_queue = False
        self._waiting: deque[_QueuedEntry] = deque()
        self._queue_ready = asyncio.Event()
        self._history: deque[_QueuedEntry] = deque()
        self._omit_history = False
        self._vote_key: str | None = None
        self._vote_skippers: set[int] = set()
        self._announce_channel: discord.abc.Messageable | None = None
        self._closed = False
        self._mixer: DuckingAudioSource | None = None
        self._music_active = False
        self._pending_jump: _JumpRequest | None = None
        self._skip_requested = False
        self._current_entry: _QueuedEntry | None = None
        self._resolved_track: Track | None = None
        self._resolve_task: asyncio.Task[Track] | None = None
        self._state = PlaybackState.IDLE
        self._activity_version = 0
        self._activity_reservations = 0
        self._state_change_listener = on_state_change
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._task = asyncio.create_task(
            self._player_loop(),
            name=f"guild-player-{guild.id}",
        )

    @property
    def volume(self) -> float:
        """Backward-compatible alias for the music output gain."""
        return self.music_volume

    @volume.setter
    def volume(self, value: float) -> None:
        self.music_volume = value

    def apply_audio_settings(
        self,
        settings: GuildAudioSettings,
        *,
        tts: TextToSpeech | None,
    ) -> None:
        """Apply guild settings to current and future playback immediately."""
        self.music_volume = settings.music_volume
        self.duck_level = settings.duck_level
        self.tts = tts
        mixer = self._mixer
        if mixer is not None:
            mixer.set_primary_volume(settings.music_volume)
            mixer.set_duck_level(settings.duck_level)

    async def enqueue(
        self,
        track: QueuedTrack | Track,
        announce_channel: discord.abc.Messageable,
    ) -> int:
        """Append one item and return the total number of waiting tracks."""
        return await self.enqueue_many((track,), announce_channel)

    async def enqueue_many(
        self,
        tracks: Iterable[QueuedTrack | Track],
        announce_channel: discord.abc.Messageable,
    ) -> int:
        """Append a batch without allowing another producer to interleave it."""
        if self._closed:
            raise RuntimeError("player is closed")

        # Normalize the complete batch before mutating the queue. put_nowait on
        # this unbounded queue contains no await point, making the append atomic
        # relative to other event-loop tasks.
        entries = tuple(
            self._make_queue_entry(track, announce_channel) for track in tracks
        )
        if not entries:
            return len(self._waiting)

        for entry in entries:
            self._put_waiting(entry)
        self._activity_version += 1
        queue_size = len(self._waiting)
        await self._notify_state_change()
        return queue_size

    async def clear_queue(self) -> int:
        """Remove waiting items while leaving the current track untouched."""
        removed = self._drain_queue()
        if removed:
            await self._notify_state_change()
        return removed

    async def stop_music(self) -> bool:
        """Stop the current track and clear waiting music without disconnecting.

        The player worker stays alive so an existing voice-chat session keeps
        running and later music can reuse the same connection. The normal idle
        timeout still owns eventual player retirement.
        """
        if self._closed:
            return False

        had_current = self.current is not None
        removed = self._drain_queue()
        if not had_current and not removed:
            return False

        # Keep draining the queue and requesting the current-track skip in one
        # event-loop turn. Otherwise playback could advance to an entry that
        # Stop was meant to remove. A queue-only stop also restarts an existing
        # idle deadline instead of appearing to disconnect immediately.
        self._activity_version += 1
        self.loop_current = False
        self.loop_queue = False
        skip_requested = self.skip() if had_current else False
        if not skip_requested:
            await self._notify_state_change()
        return True

    @property
    def state(self) -> PlaybackState:
        return self._state

    def touch(self) -> None:
        """Keep an idle player alive for a newly opened or reused controller."""
        if not self._closed:
            self._activity_version += 1

    def reserve_activity(self) -> None:
        """Prevent idle retirement during an accepted connect/enqueue operation."""
        if self._closed:
            raise RuntimeError("player is closed")
        self._activity_reservations += 1
        self._activity_version += 1

    def release_activity(self) -> None:
        """Release a reservation and restart the idle deadline."""
        if self._activity_reservations <= 0:
            raise RuntimeError("player activity is not reserved")
        self._activity_reservations -= 1
        self._activity_version += 1

    def snapshot(self) -> PlayerSnapshot:
        """Return a stable, public view without exposing the asyncio queue."""
        queued = tuple(entry.metadata for entry in self._waiting)
        return PlayerSnapshot(
            current=self.current,
            queued=queued,
            state=self._state,
            loop_current=self.loop_current,
            loop_queue=self.loop_queue,
            can_previous=bool(self._history),
        )

    def pause(self) -> ControlResult:
        """Pause only the active music mixer, never stand-alone TTS."""
        voice_client = self.guild.voice_client
        mixer = getattr(self, "_mixer", None)
        if (
            self._closed
            or self.current is None
            or not self._music_active
            or mixer is None
            or not voice_client
            or not voice_client.is_connected()
        ):
            return ControlResult.NOT_PLAYING
        if mixer.is_primary_paused:
            return ControlResult.ALREADY_PAUSED
        if not voice_client.is_playing():
            return ControlResult.NOT_PLAYING

        mixer.pause_primary()
        self._state = PlaybackState.PAUSED
        self._schedule_state_change()
        return ControlResult.SUCCESS

    def resume(self) -> ControlResult:
        """Resume only music that this player previously paused."""
        voice_client = self.guild.voice_client
        mixer = getattr(self, "_mixer", None)
        if (
            self._closed
            or self.current is None
            or not self._music_active
            or mixer is None
            or not voice_client
            or not voice_client.is_connected()
        ):
            return ControlResult.NOT_PLAYING
        if not mixer.is_primary_paused:
            return ControlResult.NOT_PAUSED
        if not voice_client.is_playing():
            return ControlResult.NOT_PLAYING

        mixer.resume_primary()
        self._state = PlaybackState.PLAYING
        self._schedule_state_change()
        return ControlResult.SUCCESS

    def toggle_loop(self) -> LoopMode:
        """Cycle off → current track → whole queue → off."""
        if self.loop_current:
            self.loop_current = False
            self.loop_queue = True
            mode = LoopMode.QUEUE
        elif self.loop_queue:
            self.loop_queue = False
            mode = LoopMode.OFF
        else:
            self.loop_current = True
            mode = LoopMode.TRACK
        self._schedule_state_change()
        return mode

    def skip(self, *, keep_loop: bool = False) -> bool:
        if not keep_loop:
            self.loop_current = False
        if self._pending_jump is not None:
            self._pending_jump = None
            self._skip_requested = True
            self._schedule_state_change()
            return True

        if (
            self.current is not None
            and self._state is PlaybackState.LOADING
            and not self._skip_requested
        ):
            self._skip_requested = True
            resolve_task = self._resolve_task
            if resolve_task is not None and not resolve_task.done():
                resolve_task.cancel()
            self._schedule_state_change()
            return True

        voice_client = self.guild.voice_client
        if (
            self._skip_requested
            or not self._music_active
            or not voice_client
            or not (voice_client.is_playing() or voice_client.is_paused())
        ):
            return False

        self._skip_requested = True
        voice_client.stop()
        self._schedule_state_change()
        return True

    def previous(self) -> bool:
        """Play the last finished track; put the current song back in front."""
        if self._closed or not self._history:
            return False
        previous = self._history.pop()
        if self._current_entry is not None:
            self._put_waiting(self._current_entry, front=True)
        self._put_waiting(previous, front=True)
        self._omit_history = True
        if self.current is not None:
            return self.skip(keep_loop=True)
        self._activity_version += 1
        self._schedule_state_change()
        return True

    def shuffle_queue(self) -> int:
        """Shuffle waiting tracks only. Returns the waiting count."""
        items = list(self._waiting)
        random.shuffle(items)
        self._waiting.clear()
        self._waiting.extend(items)
        self._activity_version += 1
        self._schedule_state_change()
        return len(items)

    def remove_queued(self, position: int) -> QueuedTrack | None:
        """Remove a 1-based waiting track. Does not touch the current song."""
        if not 1 <= position <= len(self._waiting):
            return None
        entry = self._waiting[position - 1]
        del self._waiting[position - 1]
        self._activity_version += 1
        self._schedule_state_change()
        return entry.metadata

    def move_queued(self, position: int, destination: int) -> bool:
        """Move a 1-based waiting track to another 1-based waiting slot."""
        count = len(self._waiting)
        if not 1 <= position <= count or not 1 <= destination <= count:
            return False
        if position == destination:
            return True
        entry = self._waiting[position - 1]
        del self._waiting[position - 1]
        self._waiting.insert(destination - 1, entry)
        self._activity_version += 1
        self._schedule_state_change()
        return True

    def vote_skip(self, user_id: int, *, voter_count: int) -> tuple[str, int, int]:
        """Record a skip vote. Majority of *voter_count* (at least 1) skips."""
        if self.current is None:
            return ("idle", 0, 0)
        key = self.current.webpage_url
        if self._vote_key != key:
            self._vote_key = key
            self._vote_skippers = set()
        needed = max(1, math.ceil(max(1, voter_count) / 2))
        if user_id in self._vote_skippers:
            return ("already", len(self._vote_skippers), needed)
        self._vote_skippers.add(user_id)
        votes = len(self._vote_skippers)
        if votes >= needed:
            skipped = self.skip()
            self._vote_skippers.clear()
            self._vote_key = None
            return ("skipped" if skipped else "idle", votes, needed)
        return ("voted", votes, needed)

    def jump(self, offset: int) -> JumpResult:
        """Request a restart of the current track at ``offset`` seconds."""
        if self._closed or self.current is None:
            return JumpResult.NOT_PLAYING
        if offset < 0:
            return JumpResult.OUT_OF_RANGE
        resolved_track = getattr(self, "_resolved_track", None)
        duration = (
            resolved_track.duration
            if resolved_track is not None
            else self.current.duration
        )
        if duration is None:
            return JumpResult.UNKNOWN_DURATION
        if offset >= duration:
            return JumpResult.OUT_OF_RANGE
        if self._skip_requested:
            return JumpResult.NOT_PLAYING

        if self._pending_jump is not None:
            self._pending_jump.offset = offset
            self._schedule_state_change()
            return JumpResult.SUCCESS

        voice_client = self.guild.voice_client
        if (
            not self._music_active
            or not voice_client
            or not (voice_client.is_playing() or voice_client.is_paused())
        ):
            return JumpResult.NOT_PLAYING

        self._pending_jump = _JumpRequest(
            offset=offset,
            paused=(
                self._mixer is not None
                and self._mixer.is_primary_paused
            ),
        )
        self._state = PlaybackState.LOADING
        voice_client.stop()
        self._schedule_state_change()
        return JumpResult.SUCCESS

    async def close(self, *, disconnect: bool = True) -> None:
        if self._closed:
            return
        self._closed = True

        voice_client = self.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()

        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

        notification_tasks = tuple(self._notification_tasks)
        for task in notification_tasks:
            task.cancel()
        if notification_tasks:
            await asyncio.gather(*notification_tasks, return_exceptions=True)

        self._reset_playback_state(clear_queue=True)
        await self._notify_state_change()

        if disconnect and voice_client:
            await disconnect_guild_voice_client(
                self.guild,
                expected_client=voice_client,
            )

    async def _player_loop(self) -> None:
        jump_request: _JumpRequest | None = None

        while not self._closed:
            if self._current_entry is None:
                wait_version = self._activity_version
                try:
                    entry = await self._get_waiting(self.idle_timeout)
                except TimeoutError:
                    # Opening/reusing a controller touches an idle player. If
                    # that happened during this wait, grant a fresh full idle
                    # interval rather than disconnecting at the old deadline.
                    if (
                        self._activity_version != wait_version
                        or self._activity_reservations
                        or self._waiting
                    ):
                        continue
                    idle_version = self._activity_version
                    self._state = PlaybackState.IDLE
                    await self._notify_state_change()
                    if (
                        self._activity_version != idle_version
                        or self._activity_reservations
                        or self._waiting
                    ):
                        continue
                    await self.on_idle(self.guild.id, self)
                    return
                self._current_entry = entry
                self.current = entry.metadata
                self._announce_channel = entry.announce_channel
                self._state = PlaybackState.LOADING
                await self._notify_state_change()

            voice_client = self.guild.voice_client
            if not voice_client or not voice_client.is_connected():
                disconnect_version = self._activity_version
                self._reset_playback_state(clear_queue=True)
                jump_request = None
                await self._notify_state_change()
                current_voice = self.guild.voice_client
                if (
                    self._activity_reservations
                    or self._activity_version != disconnect_version
                    or (
                        current_voice is not None
                        and current_voice.is_connected()
                    )
                ):
                    continue
                await self.on_idle(self.guild.id, self)
                return

            if await self._finish_loading_skip():
                jump_request = None
                continue

            if jump_request is None:
                try:
                    self._resolve_task = asyncio.create_task(
                        self._resolve_current(),
                        name=f"guild-player-resolve-{self.guild.id}",
                    )
                    self._resolved_track = await self._resolve_task
                except asyncio.CancelledError:
                    if not self._closed and self._skip_requested:
                        if await self._finish_loading_skip():
                            jump_request = None
                            continue
                    raise
                except Exception as exc:
                    title = self.current.title if self.current is not None else "media"
                    log.warning(
                        "Could not resolve queued track in guild %s: %s",
                        self.guild.id,
                        exc,
                    )
                    escaped_title = discord.utils.escape_markdown(title)
                    await self._send(
                        f"Không thể phát **{escaped_title}**. Đã bỏ qua bài này."
                    )
                    self._finish_current()
                    jump_request = None
                    await self._notify_state_change()
                    continue
                finally:
                    self._resolve_task = None
                # Resolution is observable even though the player remains in
                # LOADING while the optional spoken announcement finishes.
                await self._notify_state_change()
                if await self._finish_loading_skip():
                    jump_request = None
                    continue

            if jump_request is None:
                # TTS failures never block the queue. Status strings are Vietnamese.
                await self._announce_now_playing(self.current)
                if await self._finish_loading_skip():
                    jump_request = None
                    continue

            playback_finished = asyncio.Event()
            try:
                resolved_track = self._resolved_track
                if resolved_track is None:
                    raise RuntimeError("current track has not been resolved")
                if jump_request is None:
                    source = self.media.create_audio_source(
                        resolved_track,
                        volume=self.music_volume,
                    )
                else:
                    source = self.media.create_audio_source(
                        resolved_track,
                        volume=self.music_volume,
                        start_at=jump_request.offset,
                    )
                mixer = DuckingAudioSource(source, duck_level=self.duck_level)
                self._mixer = mixer
                self._music_active = True
                voice_client.play(
                    mixer,
                    after=partial(self._after_playback, playback_finished),
                )
                if jump_request is not None and jump_request.paused:
                    mixer.pause_primary()
                    self._state = PlaybackState.PAUSED
                else:
                    self._state = PlaybackState.PLAYING
                await self._notify_state_change()
            except Exception:
                self._mixer = None
                self._music_active = False
                self._pending_jump = None
                self._skip_requested = False
                log.exception("Could not start playback in guild %s", self.guild.id)
                await self._send("Không thể phát bài trong hàng đợi.")
                self._finish_current()
                jump_request = None
                await self._notify_state_change()
                continue

            await playback_finished.wait()
            self._mixer = None
            self._music_active = False

            if self._closed:
                return
            if self._skip_requested:
                self._skip_requested = False
                self._pending_jump = None
                self._finish_current()
                jump_request = None
                await self._notify_state_change()
                continue
            if self._pending_jump is not None:
                jump_request = self._pending_jump
                self._pending_jump = None
                self._state = PlaybackState.LOADING
                await self._notify_state_change()
                continue

            jump_request = None
            if self.loop_current:
                # A loop replay re-resolves the canonical URL on the next pass.
                self._resolved_track = None
                self._state = PlaybackState.LOADING
            else:
                if self.loop_queue and self._current_entry is not None:
                    self._put_waiting(self._current_entry)
                self._finish_current()
            await self._notify_state_change()

    async def _announce_now_playing(self, track: QueuedTrack | Track) -> None:
        """Send text status and speak the same announcement in the voice channel."""
        title = track.title
        await self._send(f"Đang phát: **{discord.utils.escape_markdown(title)}**")
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
            volume=getattr(self, "tts_volume", self.volume),
            skip_if_busy=True,
        )

    def _file_audio_source(self, path: Path) -> discord.AudioSource:
        """Build a local-file PCM source. The caller owns the file lifetime."""
        source = discord.FFmpegPCMAudio(str(path), **TTS_FFMPEG_OPTIONS)
        return discord.PCMVolumeTransformer(
            source,
            volume=getattr(self, "tts_volume", self.volume),
        )

    async def play_overlay(self, path: Path, *, timeout: float) -> bool:
        """Play a local clip over music (ducked) or standalone if idle.

        Does not delete *path*. Returns False on a missing file, disconnected
        client, or playback failure. A new overlay replaces the previous one.
        """
        voice_client = self.guild.voice_client
        if (
            self._closed
            or not path.is_file()
            or not voice_client
            or not voice_client.is_connected()
        ):
            return False

        try:
            source = await asyncio.to_thread(self._file_audio_source, path)
        except Exception:
            log.exception(
                "Could not open overlay clip in guild %s",
                self.guild.id,
            )
            return False

        mixer = self._mixer
        if mixer is not None and self._music_active:
            if not voice_client.is_playing():
                source.cleanup()
                return False
            try:
                done = mixer.inject_secondary(source)
                await asyncio.wait_for(
                    asyncio.to_thread(done.wait),
                    timeout=timeout,
                )
                return True
            except TimeoutError:
                log.warning(
                    "Overlay clip timed out after %.1fs in guild %s",
                    timeout,
                    self.guild.id,
                )
                mixer.clear_secondary()
                return False
            except Exception:
                log.exception(
                    "Overlay clip failed in guild %s",
                    self.guild.id,
                )
                mixer.clear_secondary()
                return False

        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        finished = asyncio.Event()

        def after(error: Exception | None) -> None:
            if error:
                log.error(
                    "Overlay playback failed in guild %s: %s",
                    self.guild.id,
                    error,
                )
            with contextlib.suppress(RuntimeError):
                self.bot.loop.call_soon_threadsafe(finished.set)

        try:
            voice_client.play(source, after=after)
            await asyncio.wait_for(finished.wait(), timeout=timeout)
            return True
        except TimeoutError:
            log.warning(
                "Standalone overlay timed out after %.1fs in guild %s",
                timeout,
                self.guild.id,
            )
            if voice_client.is_playing():
                voice_client.stop()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(finished.wait(), timeout=2.0)
            return False
        except Exception:
            log.exception(
                "Could not play overlay clip in guild %s",
                self.guild.id,
            )
            with contextlib.suppress(Exception):
                source.cleanup()
            return False

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
                volume=getattr(self, "tts_volume", self.volume),
            )
        except TTSError as exc:
            log.warning("TTS synthesis skipped in guild %s: %s", self.guild.id, exc)
            return False
        except Exception:
            log.exception("Unexpected TTS failure in guild %s", self.guild.id)
            return False

        # Keep volume transformer on TTS; mixer only ducks the music (primary).
        play_timeout = tts_playback_timeout(text)
        try:
            if (
                self._mixer is not mixer
                or not self._music_active
                or not voice_client.is_connected()
                or not voice_client.is_playing()
            ):
                source.cleanup()
                return False

            done = mixer.inject_secondary(source)
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

    def _after_playback(
        self,
        playback_finished: asyncio.Event,
        error: Exception | None,
    ) -> None:
        if error:
            log.error("Playback failed in guild %s: %s", self.guild.id, error)
        with contextlib.suppress(RuntimeError):
            self.bot.loop.call_soon_threadsafe(playback_finished.set)

    async def _send(self, message: str) -> None:
        if self._announce_channel is None:
            return
        try:
            await self._announce_channel.send(message)
        except discord.HTTPException:
            log.warning("Could not send playback status in guild %s", self.guild.id)

    def _make_queue_entry(
        self,
        track: QueuedTrack | Track,
        announce_channel: discord.abc.Messageable,
    ) -> _QueuedEntry:
        if isinstance(track, QueuedTrack):
            return _QueuedEntry(track, announce_channel)
        if isinstance(track, Track):
            metadata = QueuedTrack(
                title=track.title,
                webpage_url=track.webpage_url or track.stream_url,
                duration=track.duration,
            )
            # A stream-only Track is an old API input and cannot be refreshed.
            # Canonical webpage inputs intentionally discard the expiring stream.
            legacy_track = track if track.webpage_url is None else None
            return _QueuedEntry(metadata, announce_channel, legacy_track)
        raise TypeError("tracks must contain QueuedTrack or Track instances")

    async def _resolve_current(self) -> Track:
        entry = self._current_entry
        if entry is None:
            raise RuntimeError("no current queue entry")
        if entry.legacy_track is not None:
            return entry.legacy_track

        track = await self.media.resolve_queued(entry.metadata)
        if self.current is not None and self.current.duration is None:
            self.current = QueuedTrack(
                title=self.current.title,
                webpage_url=self.current.webpage_url,
                duration=track.duration,
            )
        return track

    def _finish_current(self) -> None:
        if not self._omit_history and self._current_entry is not None:
            self._history.append(self._current_entry)
            while len(self._history) > HISTORY_LIMIT:
                self._history.popleft()
        self._omit_history = False
        self.current = None
        self._current_entry = None
        self._resolved_track = None
        self.loop_current = False
        self._pending_jump = None
        self._vote_key = None
        self._vote_skippers.clear()
        # Skip belongs to the current generation. Never carry a late click
        # from an error-reporting await into the following queue item.
        self._skip_requested = False
        self._state = PlaybackState.IDLE

    async def _finish_loading_skip(self) -> bool:
        """Consume a skip requested before Discord playback has started."""
        if not self._skip_requested or self.current is None:
            return False
        self._skip_requested = False
        self._pending_jump = None
        self._finish_current()
        await self._notify_state_change()
        return True

    def _reset_playback_state(self, *, clear_queue: bool) -> None:
        self._finish_current()
        self._skip_requested = False
        self._music_active = False
        self._mixer = None
        if clear_queue:
            self._drain_queue()

    async def _notify_state_change(self) -> None:
        listener = getattr(self, "_state_change_listener", None)
        if listener is None:
            return
        await self._emit_state_change(listener, self.snapshot())

    def _schedule_state_change(self) -> None:
        listener = getattr(self, "_state_change_listener", None)
        if listener is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._emit_state_change(listener, self.snapshot()),
            name=f"guild-player-state-{self.guild.id}",
        )
        tasks = getattr(self, "_notification_tasks", None)
        if tasks is not None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _emit_state_change(
        self,
        listener: StateChangeListener,
        snapshot: PlayerSnapshot,
    ) -> None:
        try:
            await listener(self.guild.id, snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Playback state listener failed in guild %s", self.guild.id)

    def _put_waiting(self, entry: _QueuedEntry, *, front: bool = False) -> None:
        if front:
            self._waiting.appendleft(entry)
        else:
            self._waiting.append(entry)
        self._queue_ready.set()

    async def _get_waiting(self, timeout: float) -> _QueuedEntry:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._waiting:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            self._queue_ready.clear()
            if self._waiting:
                break
            try:
                await asyncio.wait_for(self._queue_ready.wait(), timeout=remaining)
            except TimeoutError:
                if self._waiting:
                    break
                raise
        return self._waiting.popleft()

    def _drain_queue(self) -> int:
        removed = len(self._waiting)
        self._waiting.clear()
        return removed


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
        # Preferences outlive individual idle/stopped GuildPlayer instances.
        self._title_announcement_preferences: dict[int, bool] = {}
        default_language = tts.lang if tts is not None else "vi"
        self._default_audio_settings = GuildAudioSettings(
            music_volume=volume,
            duck_level=duck_level,
            tts_language=default_language,
        )
        self._audio_settings: dict[int, GuildAudioSettings] = {}
        self._guild_tts: dict[int, TextToSpeech] = {}
        self._state_listeners: list[StateChangeListener] = []
        self._lifecycle_locks: dict[int, asyncio.Lock] = {}

    def get(self, guild_id: int) -> GuildPlayer | None:
        return self._players.get(guild_id)

    def audio_settings(self, guild_id: int) -> GuildAudioSettings:
        """Return immutable runtime settings, falling back to startup defaults."""
        return self._audio_settings.get(guild_id, self._default_audio_settings)

    def tts_for_guild(self, guild_id: int) -> TextToSpeech | None:
        """Return a guild-isolated TTS service for current runtime settings."""
        if self.tts is None:
            return None
        settings = self.audio_settings(guild_id)
        service = self._guild_tts.get(guild_id)
        if service is None or service.lang != settings.tts_language:
            service = self.tts.with_language(settings.tts_language)
            self._guild_tts[guild_id] = service
        return service

    def set_audio_settings(
        self,
        guild_id: int,
        settings: GuildAudioSettings,
    ) -> GuildAudioSettings:
        """Validate, persist, and apply one guild's complete audio snapshot."""
        music_volume = float(settings.music_volume)
        duck_level = float(settings.duck_level)
        if not math.isfinite(music_volume) or not 0.0 <= music_volume <= 2.0:
            raise ValueError("music_volume must be between 0 and 2")
        if not math.isfinite(duck_level) or not 0.0 <= duck_level <= 1.0:
            raise ValueError("duck_level must be between 0 and 1")
        language = normalize_tts_language(settings.tts_language)
        normalized = GuildAudioSettings(
            music_volume=music_volume,
            duck_level=duck_level,
            tts_language=language,
            name_announce=bool(settings.name_announce),
        )

        # Commit only after every field has passed validation.
        self._audio_settings[guild_id] = normalized
        self._guild_tts.pop(guild_id, None)
        player = self._players.get(guild_id)
        if player is not None:
            player.apply_audio_settings(
                normalized,
                tts=self.tts_for_guild(guild_id),
            )
        return normalized

    def title_announcements_enabled(self, guild_id: int) -> bool:
        """Return the guild preference for spoken song-title announcements."""
        return self._title_announcement_preferences.get(
            guild_id,
            self.tts_enabled,
        )

    def set_title_announcements(self, guild_id: int, enabled: bool) -> bool:
        """Set and apply a guild's spoken song-title preference."""
        # The environment setting is the master switch; a runtime preference
        # must never re-enable synthesis when TTS is globally unavailable.
        value = bool(enabled and self.tts_enabled)
        self._title_announcement_preferences[guild_id] = value
        player = self._players.get(guild_id)
        if player is not None:
            player.tts_enabled = value
        return value

    def toggle_title_announcements(self, guild_id: int) -> bool:
        """Toggle spoken song-title announcements and return the new value."""
        return self.set_title_announcements(
            guild_id,
            not self.title_announcements_enabled(guild_id),
        )

    def add_state_listener(self, listener: StateChangeListener) -> None:
        """Subscribe an async listener to snapshots from every guild player."""
        if listener not in self._state_listeners:
            self._state_listeners.append(listener)

    def remove_state_listener(self, listener: StateChangeListener) -> None:
        """Unsubscribe a previously registered state listener."""
        with contextlib.suppress(ValueError):
            self._state_listeners.remove(listener)

    async def get_or_create(self, guild: discord.Guild) -> GuildPlayer:
        lock = self._lifecycle_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            player = self._players.get(guild.id)
            if player is None:
                audio_settings = self.audio_settings(guild.id)
                player = GuildPlayer(
                    self.bot,
                    guild,
                    self.media,
                    volume=audio_settings.music_volume,
                    idle_timeout=self.idle_timeout,
                    on_idle=self._remove_idle,
                    tts=self.tts_for_guild(guild.id),
                    tts_volume=self.volume,
                    tts_enabled=self.title_announcements_enabled(guild.id),
                    duck_level=audio_settings.duck_level,
                    on_state_change=self._dispatch_state_change,
                )
                self._players[guild.id] = player
            else:
                player.touch()
            return player

    async def remove(self, guild_id: int, *, disconnect: bool = True) -> bool:
        lock = self._lifecycle_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            player = self._players.pop(guild_id, None)
            if player is None:
                return False
            await player.close(disconnect=disconnect)
            return True

    async def close_all(self) -> None:
        guild_ids = tuple(self._players)
        await asyncio.gather(
            *(self.remove(guild_id) for guild_id in guild_ids),
            return_exceptions=True,
        )

    async def _remove_idle(self, guild_id: int, player: GuildPlayer) -> None:
        lock = self._lifecycle_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if self._players.get(guild_id) is not player:
                return
            self._players.pop(guild_id, None)
            # Active voice-chat sessions stay connected after music idles out.
            # Do not cancel this player task; it is returning from its own wait.
            if self.keep_connected(guild_id):
                return
            voice_client = player.guild.voice_client
            if voice_client:
                await disconnect_guild_voice_client(
                    player.guild,
                    expected_client=voice_client,
                )

    async def _dispatch_state_change(
        self,
        guild_id: int,
        snapshot: PlayerSnapshot,
    ) -> None:
        listeners = tuple(self._state_listeners)
        if not listeners:
            return
        results = await asyncio.gather(
            *(listener(guild_id, snapshot) for listener in listeners),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                log.error(
                    "Player manager state listener failed for guild %s",
                    guild_id,
                    exc_info=(type(result), result, result.__traceback__),
                )
