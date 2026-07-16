"""Voice-channel chat sessions: stay connected and speak monitored messages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

import discord

from .tts import TextToSpeech, play_tts_on_voice_client

log = logging.getLogger(__name__)

# Bound speech length so gTTS stays responsive.
DEFAULT_MAX_SPEECH_CHARS = 300
# Drop backlog rather than speaking minutes of delayed chat.
DEFAULT_SPEECH_QUEUE_MAX = 12
# How long to wait for the voice client to free up (e.g. previous TTS clip).
_BUSY_WAIT_SECONDS = 90.0
_BUSY_POLL = 0.25


def is_session_chat_message(
    *,
    author_is_bot: bool,
    guild_id: int | None,
    channel_id: int,
    session_voice_channel_id: int,
    content: str,
    command_prefix: str,
) -> bool:
    """Return True when a message should be read aloud in an active session."""
    if author_is_bot or guild_id is None:
        return False
    if channel_id != session_voice_channel_id:
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if command_prefix and stripped.startswith(command_prefix):
        return False
    return True


def prepare_chat_speech(
    content: str,
    *,
    author_name: str,
    max_chars: int = DEFAULT_MAX_SPEECH_CHARS,
) -> str | None:
    """Build a plain-speech line for a VC chat message, or None if empty."""
    cleaned = content.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip() + "…"
    name = (author_name or "Someone").strip() or "Someone"
    return f"{name} says {cleaned}"


class VoiceSession:
    """One guild's join-session: monitors a voice channel's text chat via TTS."""

    def __init__(
        self,
        bot: discord.Client,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        tts: TextToSpeech,
        *,
        volume: float,
        max_speech_chars: int = DEFAULT_MAX_SPEECH_CHARS,
        queue_max: int = DEFAULT_SPEECH_QUEUE_MAX,
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.voice_channel_id = voice_channel.id
        self.voice_channel_name = voice_channel.name
        self.tts = tts
        self.volume = volume
        self.max_speech_chars = max_speech_chars
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_max)
        self._closed = False
        self._task = asyncio.create_task(
            self._speak_loop(),
            name=f"voice-session-{guild.id}",
        )

    @property
    def active(self) -> bool:
        return not self._closed

    def rebind_channel(
        self,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        self.voice_channel_id = voice_channel.id
        self.voice_channel_name = voice_channel.name

    def enqueue_speech(self, text: str) -> bool:
        """Queue TTS text. Drops oldest entry if the queue is full. Returns False if closed."""
        if self._closed:
            return False
        cleaned = text.strip()
        if not cleaned:
            return False
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
            log.warning(
                "Session speech queue full in guild %s; dropped oldest line",
                self.guild.id,
            )
        try:
            self._queue.put_nowait(cleaned)
        except asyncio.QueueFull:
            return False
        return True

    def offer_chat_message(
        self,
        *,
        author_is_bot: bool,
        author_name: str,
        guild_id: int | None,
        channel_id: int,
        content: str,
        command_prefix: str,
    ) -> bool:
        """Filter a Discord message and queue speech if it belongs to this session."""
        if not is_session_chat_message(
            author_is_bot=author_is_bot,
            guild_id=guild_id,
            channel_id=channel_id,
            session_voice_channel_id=self.voice_channel_id,
            content=content,
            command_prefix=command_prefix,
        ):
            return False
        speech = prepare_chat_speech(
            content,
            author_name=author_name,
            max_chars=self.max_speech_chars,
        )
        if speech is None:
            return False
        return self.enqueue_speech(speech)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Wake the worker so it can exit promptly.
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _speak_loop(self) -> None:
        while not self._closed:
            try:
                text = await self._queue.get()
            except asyncio.CancelledError:
                raise
            if text is None or self._closed:
                self._queue.task_done()
                return
            try:
                await self._speak_when_ready(text)
            finally:
                self._queue.task_done()

    async def _speak_when_ready(self, text: str) -> None:
        voice_client = self.guild.voice_client
        if not isinstance(voice_client, discord.VoiceClient):
            return
        if not voice_client.is_connected():
            return

        waited = 0.0
        while (
            not self._closed
            and voice_client.is_connected()
            and (voice_client.is_playing() or voice_client.is_paused())
            and waited < _BUSY_WAIT_SECONDS
        ):
            await asyncio.sleep(_BUSY_POLL)
            waited += _BUSY_POLL

        if self._closed or not voice_client.is_connected():
            return
        if voice_client.is_playing() or voice_client.is_paused():
            log.info(
                "Skipping session TTS in guild %s (voice still busy after wait)",
                self.guild.id,
            )
            return

        await play_tts_on_voice_client(
            self.bot,
            voice_client,
            self.tts,
            text,
            volume=self.volume,
            skip_if_busy=True,
        )


class SessionManager:
    """Tracks per-guild voice chat sessions."""

    def __init__(
        self,
        bot: discord.Client,
        tts: TextToSpeech,
        *,
        volume: float,
    ) -> None:
        self.bot = bot
        self.tts = tts
        self.volume = volume
        self._sessions: dict[int, VoiceSession] = {}

    def get(self, guild_id: int) -> VoiceSession | None:
        return self._sessions.get(guild_id)

    def is_active(self, guild_id: int) -> bool:
        session = self._sessions.get(guild_id)
        return session is not None and session.active

    def start(
        self,
        guild: discord.Guild,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> VoiceSession:
        existing = self._sessions.get(guild.id)
        if existing is not None and existing.active:
            existing.rebind_channel(voice_channel)
            return existing

        session = VoiceSession(
            self.bot,
            guild,
            voice_channel,
            self.tts,
            volume=self.volume,
        )
        self._sessions[guild.id] = session
        return session

    async def stop(self, guild_id: int) -> bool:
        session = self._sessions.pop(guild_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(
            *(session.close() for session in sessions),
            return_exceptions=True,
        )

    def keep_connected(self, guild_id: int) -> bool:
        """Used by the player idle path so sessions are not kicked out of VC."""
        return self.is_active(guild_id)
