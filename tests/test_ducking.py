"""Tests for PCM ducking / TTS overlay mixer."""

from __future__ import annotations

import struct
import threading
import unittest

import discord

from src.ducking import DuckingAudioSource, mix_pcm16_frames


def _frame_from_sample(sample: int, frames: int = 10) -> bytes:
    """Build a short stereo-ish PCM buffer (2 bytes per sample, mono packed as s16)."""
    return struct.pack("<" + "h" * frames, *([sample] * frames))


class MixPcmTests(unittest.TestCase):
    def test_duck_reduces_primary_when_secondary_present(self) -> None:
        # primary = 10000, secondary = 0, duck 0.2 → ~2000
        primary = _frame_from_sample(10000, 4)
        secondary = _frame_from_sample(0, 4)
        mixed = mix_pcm16_frames(primary, secondary, duck_level=0.2)
        samples = struct.unpack("<" + "h" * 4, mixed)
        self.assertEqual(samples[0], 2000)

    def test_adds_secondary_on_top(self) -> None:
        primary = _frame_from_sample(1000, 2)
        secondary = _frame_from_sample(500, 2)
        mixed = mix_pcm16_frames(primary, secondary, duck_level=0.5)
        samples = struct.unpack("<hh", mixed)
        # 1000 * 0.5 + 500 = 1000
        self.assertEqual(samples[0], 1000)

    def test_clamps_overflow(self) -> None:
        primary = _frame_from_sample(30000, 1)
        secondary = _frame_from_sample(20000, 1)
        mixed = mix_pcm16_frames(primary, secondary, duck_level=1.0)
        (sample,) = struct.unpack("<h", mixed)
        self.assertEqual(sample, 32767)


class _ConstantSource(discord.AudioSource):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def cleanup(self) -> None:
        return None


class _RepeatingSource(discord.AudioSource):
    def __init__(self, chunk: bytes) -> None:
        self.chunk = chunk

    def read(self) -> bytes:
        return self.chunk

    def cleanup(self) -> None:
        return None


class DuckingAudioSourceTests(unittest.TestCase):
    def test_passes_through_primary_without_secondary(self) -> None:
        chunk = _frame_from_sample(100, 8)
        mixer = DuckingAudioSource(_ConstantSource([chunk, b""]), duck_level=0.2)
        first = mixer.read()
        # Mixer pads to a full Discord frame so real playback does not cut early.
        self.assertEqual(first[: len(chunk)], chunk)
        self.assertEqual(len(first), 3840)
        self.assertEqual(mixer.read(), b"")

    def test_ducks_primary_while_secondary_active(self) -> None:
        primary_chunk = _frame_from_sample(10000, 4)
        secondary_chunk = _frame_from_sample(0, 4)
        mixer = DuckingAudioSource(
            _ConstantSource([primary_chunk, primary_chunk, b""]),
            duck_level=0.2,
        )
        done = mixer.inject_secondary(_ConstantSource([secondary_chunk, b""]))
        first = mixer.read()
        samples = struct.unpack("<" + "h" * 4, first[:8])
        self.assertEqual(samples[0], 2000)
        # Secondary ends on next read; music continues at full level.
        second = mixer.read()
        samples2 = struct.unpack("<" + "h" * 4, second[:8])
        self.assertEqual(samples2[0], 10000)
        self.assertTrue(done.is_set())

    def test_inject_replaces_previous_secondary(self) -> None:
        mixer = DuckingAudioSource(
            _ConstantSource([_frame_from_sample(0, 2)] * 5),
            duck_level=0.2,
        )
        first_done = mixer.inject_secondary(_ConstantSource([_frame_from_sample(1, 2)] * 10))
        second_done = mixer.inject_secondary(_ConstantSource([_frame_from_sample(2, 2), b""]))
        self.assertTrue(first_done.is_set())
        mixer.read()
        mixer.read()
        self.assertTrue(second_done.is_set())

    def test_paused_primary_is_frozen_while_secondary_keeps_playing(self) -> None:
        primary = _ConstantSource(
            [_frame_from_sample(1000, 2), _frame_from_sample(2000, 2), b""]
        )
        mixer = DuckingAudioSource(primary, duck_level=0.2)
        mixer.pause_primary()

        silence = mixer.read()
        self.assertEqual(silence, b"\x00" * 3840)
        self.assertEqual(len(primary._chunks), 3)

        done = mixer.inject_secondary(
            _ConstantSource([_frame_from_sample(500, 2), b""])
        )
        speech = mixer.read()
        self.assertEqual(struct.unpack("<h", speech[:2])[0], 500)
        self.assertEqual(len(primary._chunks), 3)

        self.assertEqual(mixer.read(), b"\x00" * 3840)
        self.assertTrue(done.is_set())
        self.assertEqual(len(primary._chunks), 3)

        mixer.resume_primary()
        resumed = mixer.read()
        self.assertEqual(struct.unpack("<h", resumed[:2])[0], 1000)
        self.assertEqual(len(primary._chunks), 2)

    def test_live_volume_and_duck_updates_change_active_mix(self) -> None:
        primary = discord.PCMVolumeTransformer(
            _ConstantSource([_frame_from_sample(10000, 4)] * 2),
            volume=0.5,
        )
        mixer = DuckingAudioSource(primary, duck_level=0.2)
        mixer.inject_secondary(
            _ConstantSource([_frame_from_sample(0, 4)] * 2)
        )

        first = struct.unpack("<h", mixer.read()[:2])[0]
        self.assertEqual(first, 1000)

        self.assertTrue(mixer.set_primary_volume(1.0))
        mixer.set_duck_level(0.5)
        second = struct.unpack("<h", mixer.read()[:2])[0]
        self.assertEqual(second, 5000)

    def test_live_updates_are_safe_during_audio_thread_reads(self) -> None:
        primary = discord.PCMVolumeTransformer(
            _RepeatingSource(_frame_from_sample(10000, 4)),
            volume=0.5,
        )
        mixer = DuckingAudioSource(primary, duck_level=0.2)
        mixer.inject_secondary(_RepeatingSource(_frame_from_sample(0, 4)))
        failures: list[BaseException] = []

        def update_settings() -> None:
            try:
                for index in range(500):
                    mixer.set_primary_volume(1.0 if index % 2 else 0.5)
                    mixer.set_duck_level(0.8 if index % 2 else 0.2)
            except BaseException as exc:  # pragma: no cover - assertion below
                failures.append(exc)

        updater = threading.Thread(target=update_settings)
        updater.start()
        samples = [struct.unpack("<h", mixer.read()[:2])[0] for _ in range(500)]
        updater.join(timeout=2.0)

        self.assertFalse(updater.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(samples)
        self.assertTrue(all(0 <= sample <= 8000 for sample in samples))

    def test_live_update_validation_and_non_transformer_result(self) -> None:
        mixer = DuckingAudioSource(_RepeatingSource(_frame_from_sample(1)))

        self.assertFalse(mixer.set_primary_volume(0.5))
        for invalid in (-0.1, 2.1):
            with self.subTest(volume=invalid):
                with self.assertRaises(ValueError):
                    mixer.set_primary_volume(invalid)
        for invalid in (-0.1, 1.1):
            with self.subTest(duck_level=invalid):
                with self.assertRaises(ValueError):
                    mixer.set_duck_level(invalid)


if __name__ == "__main__":
    unittest.main()
