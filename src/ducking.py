"""Mix secondary TTS over primary music with volume ducking.

Discord voice clients play a single AudioSource. This wrapper keeps music
flowing while a short TTS clip is mixed on top at full level and music is
attenuated (ducked).
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

import discord

log = logging.getLogger(__name__)

# discord.py PCM: 48 kHz, 16-bit stereo, 20 ms → 3840 bytes per frame.
PCM_FRAME_SIZE = 3840
DEFAULT_DUCK_LEVEL = 0.2


class FrameTap(Protocol):
    """Copy music and overlay stems before they are mixed for playback."""

    def on_bot_frame(
        self,
        *,
        music: bytes | None,
        overlay: bytes | None,
        overlay_kind: str | None,
    ) -> None: ...


def _as_discord_frame(data: bytes) -> bytes:
    """Pad/trim to one Discord audio frame so playback does not glitch/cut early."""
    if not data:
        return b""
    if len(data) == PCM_FRAME_SIZE:
        return data
    if len(data) < PCM_FRAME_SIZE:
        return data + b"\x00" * (PCM_FRAME_SIZE - len(data))
    return data[:PCM_FRAME_SIZE]


def mix_pcm16_frames(
    primary: bytes,
    secondary: bytes,
    *,
    duck_level: float,
) -> bytes:
    """Mix two little-endian s16le PCM frames; duck *primary* while *secondary* plays.

    Frames are padded/truncated to the same even length. Samples are clamped.
    """
    if duck_level < 0.0:
        duck_level = 0.0
    if duck_level > 1.0:
        duck_level = 1.0

    length = max(len(primary), len(secondary))
    # Keep even byte length for 16-bit samples.
    length -= length % 2
    if length <= 0:
        return b""

    if len(primary) < length:
        primary = primary + b"\x00" * (length - len(primary))
    elif len(primary) > length:
        primary = primary[:length]
    if len(secondary) < length:
        secondary = secondary + b"\x00" * (length - len(secondary))
    elif len(secondary) > length:
        secondary = secondary[:length]

    out = bytearray(length)
    for offset in range(0, length, 2):
        a = int.from_bytes(primary[offset : offset + 2], "little", signed=True)
        b = int.from_bytes(secondary[offset : offset + 2], "little", signed=True)
        mixed = int(a * duck_level) + b
        if mixed > 32767:
            mixed = 32767
        elif mixed < -32768:
            mixed = -32768
        out[offset : offset + 2] = mixed.to_bytes(2, "little", signed=True)
    return bytes(out)


class DuckingAudioSource(discord.AudioSource):
    """Primary (music) stream with optional secondary (TTS) overlay + ducking."""

    def __init__(
        self,
        primary: discord.AudioSource,
        *,
        duck_level: float = DEFAULT_DUCK_LEVEL,
        tap: FrameTap | None = None,
    ) -> None:
        self.primary = primary
        self.duck_level = duck_level
        self._lock = threading.Lock()
        self._secondary: discord.AudioSource | None = None
        self._secondary_kind: str | None = None
        self._secondary_done: threading.Event | None = None
        self._primary_ended = False
        self._primary_paused = False
        self._tap = tap

    def set_tap(self, tap: FrameTap | None) -> None:
        """Attach or clear a stem tap. Playback bytes do not change."""
        with self._lock:
            self._tap = tap

    @property
    def is_ducking(self) -> bool:
        with self._lock:
            return self._secondary is not None

    @property
    def is_primary_paused(self) -> bool:
        """Whether music reads are frozen while the mixer remains active."""
        with self._lock:
            return self._primary_paused

    def pause_primary(self) -> None:
        """Freeze music without pausing the Discord audio player.

        The mixer continues producing silence (or secondary TTS audio), so a
        voice-session announcement can still be injected while music is paused.
        """
        with self._lock:
            self._primary_paused = True

    def resume_primary(self) -> None:
        """Resume music reads after :meth:`pause_primary`."""
        with self._lock:
            self._primary_paused = False

    def set_duck_level(self, duck_level: float) -> None:
        """Update the music gain used while secondary audio is present."""
        if not 0.0 <= duck_level <= 1.0:
            raise ValueError("duck_level must be between 0 and 1")
        with self._lock:
            self.duck_level = duck_level

    def set_primary_volume(self, volume: float) -> bool:
        """Update an active PCM volume transformer without restarting music."""
        if not 0.0 <= volume <= 2.0:
            raise ValueError("volume must be between 0 and 2")
        with self._lock:
            if not isinstance(self.primary, discord.PCMVolumeTransformer):
                return False
            self.primary.volume = volume
            return True

    def inject_secondary(
        self,
        source: discord.AudioSource,
        *,
        kind: str = "tts",
    ) -> threading.Event:
        """Start mixing *source* over the primary; music is ducked until it ends.

        *kind* is ``tts`` or ``soundboard`` and is copied to the recorder before
        the mix. Returns a threading.Event set when the clip finishes or is replaced.
        """
        done = threading.Event()
        with self._lock:
            previous = self._secondary
            previous_done = self._secondary_done
            self._secondary = source
            self._secondary_kind = kind
            self._secondary_done = done
        if previous is not None:
            with _suppress_cleanup():
                previous.cleanup()
            if previous_done is not None:
                previous_done.set()
        return done

    def clear_secondary(self) -> None:
        with self._lock:
            previous = self._secondary
            previous_done = self._secondary_done
            self._secondary = None
            self._secondary_kind = None
            self._secondary_done = None
        if previous is not None:
            with _suppress_cleanup():
                previous.cleanup()
            if previous_done is not None:
                previous_done.set()

    def read(self) -> bytes:
        with self._lock:
            secondary = self._secondary
            secondary_kind = self._secondary_kind
            duck = self.duck_level if secondary is not None else 1.0
            done_event = self._secondary_done
            primary_paused = self._primary_paused
            tap = self._tap

        primary_data = b""
        if not self._primary_ended and not primary_paused:
            try:
                primary_data = self.primary.read()
            except Exception:
                primary_data = b""
            if not primary_data:
                self._primary_ended = True

        secondary_data = b""
        if secondary is not None:
            try:
                secondary_data = secondary.read()
            except Exception:
                secondary_data = b""
            if not secondary_data:
                with self._lock:
                    if self._secondary is secondary:
                        self._secondary = None
                        self._secondary_kind = None
                        self._secondary_done = None
                with _suppress_cleanup():
                    secondary.cleanup()
                if done_event is not None:
                    done_event.set()
                secondary = None
                secondary_kind = None

        if primary_paused and not self._primary_ended and not secondary_data:
            # Returning b"" ends discord.py playback. Emit one silent frame so
            # secondary audio can still be injected while music stays frozen.
            silent = b"\x00" * PCM_FRAME_SIZE
            self._notify_tap(tap, music=silent, overlay=b"", overlay_kind=None)
            return silent
        if not primary_data and not secondary_data:
            return b""
        if secondary is None or not secondary_data:
            frame = _as_discord_frame(primary_data)
            self._notify_tap(tap, music=primary_data, overlay=b"", overlay_kind=None)
            return frame
        if not primary_data:
            # Music ended mid-TTS: finish speaking alone. A paused primary is
            # silence on the music stem so the take stays aligned.
            frame = _as_discord_frame(secondary_data)
            paused_music = (
                b"\x00" * PCM_FRAME_SIZE
                if primary_paused and not self._primary_ended
                else None
            )
            self._notify_tap(
                tap,
                music=paused_music,
                overlay=secondary_data,
                overlay_kind=secondary_kind,
            )
            return frame
        self._notify_tap(
            tap,
            music=primary_data,
            overlay=secondary_data,
            overlay_kind=secondary_kind,
        )
        return _as_discord_frame(
            mix_pcm16_frames(primary_data, secondary_data, duck_level=duck)
        )

    def _notify_tap(
        self,
        tap: FrameTap | None,
        *,
        music: bytes | None,
        overlay: bytes | None,
        overlay_kind: str | None,
    ) -> None:
        if tap is None:
            return
        try:
            tap.on_bot_frame(
                music=music,
                overlay=overlay,
                overlay_kind=overlay_kind,
            )
        except Exception:
            log.exception("Playback stem tap failed")

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.clear_secondary()
        with _suppress_cleanup():
            self.primary.cleanup()


class _suppress_cleanup:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True
