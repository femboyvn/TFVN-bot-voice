"""Voice-channel chat sessions: stay connected and speak monitored messages."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable

import discord

from .player import GuildPlayer, PlayerManager
from .tts import TextToSpeech, play_tts_on_voice_client

log = logging.getLogger(__name__)

# Allow full Discord-length messages (was 300 and cut people off mid-sentence).
DEFAULT_MAX_SPEECH_CHARS = 2000
# gTTS is happier with moderate chunks; long lines are split and spoken in order.
DEFAULT_TTS_CHUNK_CHARS = 400
# Drop backlog rather than speaking minutes of delayed chat.
DEFAULT_SPEECH_QUEUE_MAX = 24
# How long to wait for the voice client to free up (e.g. previous TTS clip).
_BUSY_WAIT_SECONDS = 90.0
_BUSY_POLL = 0.25

PlayerLookup = Callable[[int], GuildPlayer | None]

# Strip media / link / icon noise so TTS does not read "https colon slash slash…".
_URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.)[^\s<>\[\]()\"']+",
)
# Discord custom emoji: <:name:id> / <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
# :shortcode: left after clean_content converts custom emoji
_EMOJI_SHORTCODE_RE = re.compile(r":[a-zA-Z0-9_~+-]{1,64}:")
# Common standalone unicode emoji / symbols (icons) — skip for speech.
_UNICODE_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"  # ZWJ
    "]+",
    flags=re.UNICODE,
)
_WHITESPACE_RE = re.compile(r"\s+")


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


def _truncate_at_word(text: str, max_chars: int) -> str:
    """Truncate at a word boundary when possible (avoid mid-word cutoffs)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    budget = max_chars - 1
    snippet = text[:budget].rstrip()
    if " " in snippet:
        cut = snippet.rsplit(" ", 1)[0].rstrip()
        if cut:
            snippet = cut
    return snippet + "…"


def sanitize_for_speech(content: str) -> str:
    """Remove URLs, GIFs/links, and emoji/icons so TTS only speaks real words.

    Returns stripped text; may be empty when the message was only media/icons.
    """
    text = content or ""
    text = _CUSTOM_EMOJI_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _EMOJI_SHORTCODE_RE.sub(" ", text)
    text = _UNICODE_EMOJI_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def voice_event_announcement(
    *,
    member_is_bot: bool,
    before_channel_id: int | None,
    after_channel_id: int | None,
    bot_channel_id: int | None,
    display_name: str,
    before_self_muted: bool = False,
    after_self_muted: bool = False,
    before_server_muted: bool = False,
    after_server_muted: bool = False,
    before_self_deafened: bool = False,
    after_self_deafened: bool = False,
    before_server_deafened: bool = False,
    after_server_deafened: bool = False,
    announce_join: bool = True,
    announce_leave: bool = False,
    announce_mute: bool = False,
    announce_deafen: bool = False,
) -> str | None:
    """Return the spoken presence line, or None when nothing should be said.

    A join or leave is a channel change into or out of the bot's room.
    Self and server mute or deafen are separate, and each is spoken only
    when the person stays in that room and the matching toggle is on.
    A deafen of one source includes that source's mute, so both changing
    together speaks only the âm line. Removing a server mute or server
    deafen stays silent. Bots and an empty display name stay silent.
    """
    if member_is_bot or bot_channel_id is None:
        return None
    name = sanitize_for_speech(display_name)
    if not name:
        return None
    joined = (
        after_channel_id == bot_channel_id
        and before_channel_id != bot_channel_id
    )
    left = (
        before_channel_id == bot_channel_id
        and after_channel_id != bot_channel_id
    )
    stayed = (
        before_channel_id == bot_channel_id
        and after_channel_id == bot_channel_id
    )
    lines: list[str] = []
    if joined and announce_join:
        lines.append(f"{name} đã tham gia")
    if left and announce_leave:
        lines.append(f"{name} đã rời đi")
    server_deaf_on = (
        stayed and not before_server_deafened and after_server_deafened
    )
    self_deaf_on = (
        stayed and not before_self_deafened and after_self_deafened
    )
    self_deaf_off = (
        stayed and before_self_deafened and not after_self_deafened
    )
    server_mute_on = (
        stayed and not before_server_muted and after_server_muted
    )
    self_mute_on = stayed and not before_self_muted and after_self_muted
    self_mute_off = stayed and before_self_muted and not after_self_muted
    # A deafen from one source also changes that source's mute. Speak only
    # the âm line for that pair. Server and self actions stay independent.
    if server_deaf_on and announce_deafen:
        lines.append(f"{name} đã bị đấm thủng lỗ tai")
    elif server_mute_on and announce_mute:
        lines.append(f"{name} đã bị nhét giẻ vào mồm")
    if self_deaf_on and announce_deafen:
        lines.append(f"{name} đã tắt âm")
    elif self_deaf_off and announce_deafen:
        lines.append(f"{name} đã bật âm")
    elif self_mute_on and announce_mute:
        lines.append(f"{name} đã tắt mic")
    elif self_mute_off and announce_mute:
        lines.append(f"{name} đã bật mic")
    if not lines:
        return None
    return ". ".join(lines)


def join_announcement_name(
    *,
    member_is_bot: bool,
    before_channel_id: int | None,
    after_channel_id: int | None,
    bot_channel_id: int | None,
    display_name: str,
) -> str | None:
    """Return ``"{name} đã tham gia"``, or None when this update is not a join."""
    return voice_event_announcement(
        member_is_bot=member_is_bot,
        before_channel_id=before_channel_id,
        after_channel_id=after_channel_id,
        bot_channel_id=bot_channel_id,
        display_name=display_name,
        announce_join=True,
    )


def chat_speaker_key(author_id: int | None, author_name: str) -> str | None:
    """Identity used to skip a repeated speaker name on back-to-back chat lines.

    Prefer the Discord user id. Fall back to the display name only when no id
    is available, so two people who share a nickname still get named.
    """
    if author_id is not None:
        return f"id:{author_id}"
    name = (author_name or "").strip()
    if not name:
        return None
    return f"name:{name.casefold()}"


def prepare_chat_speech(
    content: str,
    *,
    author_name: str,
    max_chars: int = DEFAULT_MAX_SPEECH_CHARS,
    announce_name: bool = True,
) -> str | None:
    """Build a plain-speech line for a VC chat message, or None if empty.

    When *announce_name* is True, speaks ``"{name} nói {message}"``.
    When False, speaks only the message body. The session passes False for
    a repeat of the previous spoken chat author, and when name reading is off.
    Skips pure link / GIF / emoji messages after sanitization.
    """
    cleaned = sanitize_for_speech(content)
    if not cleaned:
        return None
    if announce_name:
        # Vietnamese customer-facing speech template.
        name = (author_name or "Ai đó").strip() or "Ai đó"
        prefix = f"{name} nói "
    else:
        prefix = ""
    # Reserve room for the speaker prefix so the body is not over-truncated.
    body_budget = max(1, max_chars - len(prefix))
    if len(cleaned) > body_budget:
        cleaned = _truncate_at_word(cleaned, body_budget)
    return f"{prefix}{cleaned}"


def chunk_speech_text(
    text: str,
    *,
    max_chunk_chars: int = DEFAULT_TTS_CHUNK_CHARS,
) -> list[str]:
    """Split speech into ordered chunks for reliable TTS synthesis/playback.

    Prefers sentence / word boundaries so lines are not cut mid-phrase when
    possible. Short text returns a single chunk.
    """
    cleaned = text.strip()
    if not cleaned:
        return []
    if max_chunk_chars <= 0 or len(cleaned) <= max_chunk_chars:
        return [cleaned]

    chunks: list[str] = []
    remaining = cleaned
    while remaining:
        if len(remaining) <= max_chunk_chars:
            chunks.append(remaining)
            break
        window = remaining[:max_chunk_chars]
        split_at = -1
        for sep in (". ", "! ", "? ", "… ", "; ", ", ", " "):
            idx = window.rfind(sep)
            if idx > max_chunk_chars // 4:
                split_at = idx + len(sep)
                break
        if split_at <= 0:
            split_at = max_chunk_chars
        piece = remaining[:split_at].strip()
        if piece:
            chunks.append(piece)
        remaining = remaining[split_at:].lstrip()
    return chunks


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
        get_player: PlayerLookup | None = None,
        max_speech_chars: int = DEFAULT_MAX_SPEECH_CHARS,
        tts_chunk_chars: int = DEFAULT_TTS_CHUNK_CHARS,
        queue_max: int = DEFAULT_SPEECH_QUEUE_MAX,
        name_announce: bool = False,
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.voice_channel_id = voice_channel.id
        self.voice_channel_name = voice_channel.name
        self.tts = tts
        self.volume = volume
        self.get_player = get_player or (lambda _gid: None)
        self.max_speech_chars = max_speech_chars
        self.tts_chunk_chars = tts_chunk_chars
        # Guild default is off: speak the message body only.
        self.name_announce = bool(name_announce)
        # Last chat author whose line was actually queued. A repeat skips the name.
        self._last_chat_speaker_key: str | None = None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_max)
        self._closed = False
        self._task = asyncio.create_task(
            self._speak_loop(),
            name=f"voice-session-{guild.id}",
        )

    @property
    def active(self) -> bool:
        return not self._closed

    def set_name_announce(self, enabled: bool) -> bool:
        """Enable or disable speaker-name prefix in TTS. Returns the new value.

        Turning the prefix on or off starts a new speaker run, so the next
        chat line says the name again when reading is on.
        """
        enabled = bool(enabled)
        if enabled != self.name_announce:
            self._last_chat_speaker_key = None
        self.name_announce = enabled
        return self.name_announce

    def rebind_channel(
        self,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        self.voice_channel_id = voice_channel.id
        self.voice_channel_name = voice_channel.name
        # A new room is a new conversation; the next line names its sender.
        self._last_chat_speaker_key = None

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
        author_id: int | None = None,
    ) -> bool:
        """Filter a Discord message and queue speech if it belongs to this session.

        When name reading is on, the first queued line from a person is
        ``"{name} nói {message}"``. Later queued lines from that same person
        omit the name until someone else is spoken, the room changes, or the
        setting is toggled. Messages that are not spoken do not change the run.
        """
        if not is_session_chat_message(
            author_is_bot=author_is_bot,
            guild_id=guild_id,
            channel_id=channel_id,
            session_voice_channel_id=self.voice_channel_id,
            content=content,
            command_prefix=command_prefix,
        ):
            return False
        speaker_key = chat_speaker_key(author_id, author_name)
        repeat_speaker = (
            self.name_announce
            and speaker_key is not None
            and speaker_key == self._last_chat_speaker_key
        )
        speech = prepare_chat_speech(
            content,
            author_name=author_name,
            max_chars=self.max_speech_chars,
            announce_name=self.name_announce and not repeat_speaker,
        )
        if speech is None:
            return False
        # Enqueue in order so long messages finish fully instead of one truncated clip.
        segments = chunk_speech_text(speech, max_chunk_chars=self.tts_chunk_chars)
        if not segments:
            return False
        queued_any = False
        for segment in segments:
            if self.enqueue_speech(segment):
                queued_any = True
        if queued_any and speaker_key is not None:
            self._last_chat_speaker_key = speaker_key
        return queued_any

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

        # Prefer ducking over the current track so music keeps playing underneath.
        player = self.get_player(self.guild.id)
        if player is not None:
            ducked = await player.speak_over_music(text)
            if ducked:
                return

        # No active music mixer: wait for a free client, then play TTS alone.
        waited = 0.0
        while (
            not self._closed
            and voice_client.is_connected()
            and (voice_client.is_playing() or voice_client.is_paused())
            and waited < _BUSY_WAIT_SECONDS
        ):
            # Music may have started while we were synthesizing earlier; retry duck.
            if player is not None and await player.speak_over_music(text):
                return
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
        players: PlayerManager | None = None,
    ) -> None:
        self.bot = bot
        self.tts = tts
        self.volume = volume
        self.players = players
        self._sessions: dict[int, VoiceSession] = {}

    def bind_players(self, players: PlayerManager) -> None:
        """Attach the player manager after construction (breaks init cycles)."""
        self.players = players

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

        players = self.players
        guild_tts = (
            players.tts_for_guild(guild.id)
            if players is not None
            else None
        )
        name_announce = False
        if players is not None:
            name_announce = players.audio_settings(guild.id).name_announce
        session = VoiceSession(
            self.bot,
            guild,
            voice_channel,
            guild_tts or self.tts,
            volume=self.volume,
            get_player=(players.get if players is not None else None),
            name_announce=name_announce,
        )
        self._sessions[guild.id] = session
        return session

    def refresh_tts_language(self, guild_id: int) -> bool:
        """Apply the current guild language to an already-running session."""
        session = self._sessions.get(guild_id)
        players = self.players
        if session is None or not session.active or players is None:
            return False
        tts = players.tts_for_guild(guild_id)
        if tts is None:
            return False
        session.tts = tts
        return True

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
