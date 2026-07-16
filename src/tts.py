"""Text-to-speech synthesis for voice-channel announcements.

Keeps synthesis (text → audio file) separate from Discord connect/play so unit
tests can exercise the real transform without a voice client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import discord

log = logging.getLogger(__name__)

# Local temp files need no reconnect flags (unlike remote media streams).
TTS_FFMPEG_OPTIONS: dict[str, str] = {
    "options": "-vn",
}

# Base cap for short clips; longer text gets a scaled timeout (see helper below).
TTS_PLAYBACK_TIMEOUT = 90.0
TTS_PLAYBACK_TIMEOUT_MAX = 300.0


def tts_playback_timeout(text: str) -> float:
    """Seconds to allow for speaking *text* (scales with length; Vietnamese is slower)."""
    # ~0.12s per character + headroom for synthesis/FFmpeg startup.
    scaled = 12.0 + len(text) * 0.14
    return max(TTS_PLAYBACK_TIMEOUT, min(TTS_PLAYBACK_TIMEOUT_MAX, scaled))

Synthesizer = Callable[[str, Path], None]


class TTSError(RuntimeError):
    """Raised when speech cannot be synthesized into playable audio."""


def now_playing_speech(title: str) -> str:
    """Plain-speech phrase for now-playing announcements (Vietnamese UI)."""
    cleaned = (title or "").strip() or "Không có tiêu đề"
    return f"Đang phát {cleaned}"


def _gtts_synthesize(text: str, dest: Path, *, lang: str) -> None:
    """Write MP3 speech for *text* to *dest* using Google Translate TTS."""
    from gtts import gTTS

    gTTS(text=text, lang=lang).save(str(dest))


class TextToSpeech:
    """Converts short announcement text into Discord-playable audio.

    The default backend is gTTS (network). Tests may inject a *synthesizer*
    that writes audio bytes locally so the real ``synthesize`` / source path
    is exercised without network I/O.
    """

    def __init__(
        self,
        *,
        lang: str = "en",
        synthesizer: Synthesizer | None = None,
    ) -> None:
        self.lang = lang
        self._synthesizer = synthesizer

    def synthesize(self, text: str) -> Path:
        """Turn non-empty *text* into a temporary audio file.

        Returns a path the caller owns and must delete when finished.
        Raises :class:`TTSError` on empty input or failed generation.
        """
        cleaned = text.strip()
        if not cleaned:
            raise TTSError("TTS text must be non-empty")

        fd, raw_path = tempfile.mkstemp(prefix="tfd-tts-", suffix=".mp3")
        os.close(fd)
        dest = Path(raw_path)
        try:
            synthesizer = self._synthesizer or self._default_synthesize
            synthesizer(cleaned, dest)
            if not dest.is_file() or dest.stat().st_size == 0:
                raise TTSError("TTS produced empty audio")
        except TTSError:
            dest.unlink(missing_ok=True)
            raise
        except Exception as exc:
            dest.unlink(missing_ok=True)
            raise TTSError(f"TTS synthesis failed: {exc}") from exc
        return dest

    def create_audio_source(
        self,
        text: str,
        *,
        volume: float,
    ) -> tuple[discord.PCMVolumeTransformer, Path]:
        """Build a volume-transformed FFmpeg source for *text*.

        Returns ``(source, path)`` so the caller can clean up the temp file
        after playback finishes (or fails).
        """
        path = self.synthesize(text)
        try:
            source = discord.FFmpegPCMAudio(str(path), **TTS_FFMPEG_OPTIONS)
            return discord.PCMVolumeTransformer(source, volume=volume), path
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _default_synthesize(self, text: str, dest: Path) -> None:
        _gtts_synthesize(text, dest, lang=self.lang)


async def play_tts_on_voice_client(
    bot: discord.Client,
    voice_client: discord.VoiceClient,
    tts: TextToSpeech,
    text: str,
    *,
    volume: float,
    timeout: float | None = None,
    skip_if_busy: bool = True,
) -> bool:
    """Synthesize *text* and play it on *voice_client*.

    Returns True if playback was started and finished (or timed out after stop).
    Never raises into callers for synthesis/play failures — returns False instead.
    """
    if not voice_client.is_connected():
        return False

    if skip_if_busy and (voice_client.is_playing() or voice_client.is_paused()):
        log.warning("Voice client busy; skipping TTS")
        return False

    play_timeout = timeout if timeout is not None else tts_playback_timeout(text)

    audio_path: Path | None = None
    try:
        source, audio_path = await asyncio.to_thread(
            tts.create_audio_source,
            text,
            volume=volume,
        )
    except TTSError as exc:
        log.warning("TTS synthesis skipped: %s", exc)
        return False
    except Exception:
        log.exception("Unexpected TTS failure")
        return False

    finished = asyncio.Event()

    def after(error: Exception | None) -> None:
        if error:
            log.error("TTS playback failed: %s", error)
        bot.loop.call_soon_threadsafe(finished.set)

    try:
        # Re-check after synthesis (music may have started meanwhile).
        if skip_if_busy and (voice_client.is_playing() or voice_client.is_paused()):
            log.warning("Voice client became busy; skipping TTS")
            return False
        voice_client.play(source, after=after)
        try:
            await asyncio.wait_for(finished.wait(), timeout=play_timeout)
        except TimeoutError:
            log.warning("TTS playback timed out after %.1fs", play_timeout)
            if voice_client.is_playing():
                voice_client.stop()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(finished.wait(), timeout=2.0)
        return True
    except Exception:
        log.exception("Could not play TTS")
        return False
    finally:
        if audio_path is not None:
            with contextlib.suppress(OSError):
                audio_path.unlink(missing_ok=True)
