"""Mix secondary TTS over primary music with volume ducking.

Discord voice clients play a single AudioSource. This wrapper keeps music
flowing while a short TTS clip is mixed on top at full level and music is
attenuated (ducked).
"""

from __future__ import annotations

import threading

import discord

# discord.py PCM: 48 kHz, 16-bit stereo, 20 ms → 3840 bytes per frame.
PCM_FRAME_SIZE = 3840
DEFAULT_DUCK_LEVEL = 0.2


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
    ) -> None:
        self.primary = primary
        self.duck_level = duck_level
        self._lock = threading.Lock()
        self._secondary: discord.AudioSource | None = None
        self._secondary_done: threading.Event | None = None
        self._primary_ended = False

    @property
    def is_ducking(self) -> bool:
        with self._lock:
            return self._secondary is not None

    def inject_secondary(self, source: discord.AudioSource) -> threading.Event:
        """Start mixing *source* over the primary; music is ducked until it ends.

        Returns a threading.Event set when the secondary clip finishes (or is replaced).
        """
        done = threading.Event()
        with self._lock:
            previous = self._secondary
            previous_done = self._secondary_done
            self._secondary = source
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
            self._secondary_done = None
        if previous is not None:
            with _suppress_cleanup():
                previous.cleanup()
            if previous_done is not None:
                previous_done.set()

    def read(self) -> bytes:
        with self._lock:
            secondary = self._secondary
            duck = self.duck_level if secondary is not None else 1.0
            done_event = self._secondary_done

        primary_data = b""
        if not self._primary_ended:
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
                        self._secondary_done = None
                with _suppress_cleanup():
                    secondary.cleanup()
                if done_event is not None:
                    done_event.set()
                secondary = None

        if not primary_data and not secondary_data:
            return b""
        if secondary is None or not secondary_data:
            return primary_data
        if not primary_data:
            # Music ended mid-TTS: finish speaking alone.
            return secondary_data
        return mix_pcm16_frames(primary_data, secondary_data, duck_level=duck)

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
