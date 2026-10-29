"""Multi-track recording clock, MP3 zip, and R2 sync."""

from __future__ import annotations

import struct
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import discord

from src.ducking import DuckingAudioSource, mix_pcm16_frames
from src.recording import (
    MONO_FRAME_BYTES,
    SILENCE_FRAME,
    RecordingService,
    TakeTap,
    TappedAudioSource,
    TrackEntry,
    TrackWriter,
    completion_message,
    start_ffmpeg_encoder,
)
from src.soundboard import S3ObjectStore


def _tone(sample: int) -> bytes:
    return struct.pack("<960h", *([sample] * 960))


class MemoryEncoder:
    def __init__(self, path: Path, bitrate: str) -> None:
        self.path = path
        self.bitrate = bitrate
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, pcm: bytes) -> None:
        self.chunks.append(bytes(pcm))

    def close(self) -> None:
        self.closed = True
        self.path.write_bytes(b"ID3" + b"".join(self.chunks)[:8])

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


def _stem_name(filename: str) -> str:
    if ".part-" in filename:
        return filename.split(".part-", 1)[0]
    return Path(filename).stem


def _join_bytes(parts: list[Path], dest: Path) -> None:
    dest.write_bytes(b"".join(path.read_bytes() for path in parts))
    for path in parts:
        path.unlink()


def _factory(bucket: list[MemoryEncoder]):
    def factory(path: Path, bitrate: str) -> MemoryEncoder:
        encoder = MemoryEncoder(path, bitrate)
        bucket.append(encoder)
        return encoder

    return factory


class _ConstantSource(discord.AudioSource):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def cleanup(self) -> None:
        return None


class _BrokenEncoder(MemoryEncoder):
    def write(self, pcm: bytes) -> None:
        raise BrokenPipeError("closed")


class TrackWriterTests(unittest.TestCase):
    def test_index_five_is_preceded_by_silence(self) -> None:
        encoders: list[MemoryEncoder] = []
        with tempfile.TemporaryDirectory() as tmp:
            writer = TrackWriter(Path(tmp) / "music.mp3", "192k", _factory(encoders))
            writer.write_at(5, _tone(1000))
            self.assertEqual(encoders[0].data[: MONO_FRAME_BYTES * 5], SILENCE_FRAME * 5)
            self.assertEqual(
                encoders[0].data[MONO_FRAME_BYTES * 5 :],
                _tone(1000),
            )

    def test_same_index_keeps_both_frames_in_order(self) -> None:
        encoders: list[MemoryEncoder] = []
        with tempfile.TemporaryDirectory() as tmp:
            writer = TrackWriter(Path(tmp) / "tts.mp3", "128k", _factory(encoders))
            first = _tone(100)
            second = _tone(200)
            writer.write_at(0, first)
            writer.write_at(0, second)
            self.assertEqual(encoders[0].data, first + second)

    def test_broken_pipe_still_advances_and_keeps_later_audio(self) -> None:
        created: list[MemoryEncoder] = []

        def factory(path: Path, bitrate: str) -> MemoryEncoder:
            encoder: MemoryEncoder = (
                _BrokenEncoder(path, bitrate) if not created else MemoryEncoder(path, bitrate)
            )
            created.append(encoder)
            return encoder

        with tempfile.TemporaryDirectory() as tmp:
            writer = TrackWriter(
                Path(tmp) / "music.mp3",
                "192k",
                factory,
                chunk_frames=1,
                part_joiner=_join_bytes,
            )
            writer.write_at(2, _tone(4))
            self.assertEqual(writer.next_index, 3)
            self.assertGreater(len(created), 1)
            self.assertTrue(any(encoder.chunks for encoder in created))

    def test_partial_filename_is_encoded_as_mp3(self) -> None:
        with patch("src.recording.subprocess.Popen") as popen:
            stderr = Mock()
            stderr.read.return_value = b""
            popen.return_value.stdin = unittest.mock.Mock()
            popen.return_value.stderr = stderr
            path = Path("/tmp/music.part-00000.partial")
            start_ffmpeg_encoder(path, "192k")
            argv = popen.call_args.args[0]
        self.assertEqual(argv[-3:], ["-f", "mp3", str(path)])


class TakeFinalizeTests(unittest.TestCase):
    def test_zip_contains_bot_mp3s_and_only_speakers_who_sent_audio(self) -> None:
        encoders: list[MemoryEncoder] = []
        with tempfile.TemporaryDirectory() as tmp:
            service = RecordingService(
                Mock(
                    recording_data_dir=tmp,
                    recording_max_minutes=120,
                    recording_retention_days=7,
                    recording_max_bytes=2_000_000_000,
                ),
                encoder_factory=_factory(encoders),
            )
            take = service.start(
                guild_id=9,
                voice_channel_id=3,
                text_channel_id=4,
                starter_id=8,
                voice_channel_name="Phòng",
            )
            take.submit_bot("music", _tone(1), index=0)
            take.submit_bot("tts", _tone(2), index=0)
            take.submit_bot("soundboard", _tone(3), index=0)
            take.submit_speaker(15, "Lan", _tone(4), index=1)
            finished = service.stop(9)

            self.assertIsNotNone(finished)
            assert finished is not None
            self.assertTrue(finished.zip_path.is_file())
            with zipfile.ZipFile(finished.zip_path) as archive:
                names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "manifest.json",
                    "music.mp3",
                    "tts.mp3",
                    "soundboard.mp3",
                    "user-15.mp3",
                },
            )
            self.assertTrue(all(encoder.closed for encoder in encoders))
            kinds = {_stem_name(encoder.path.name): encoder.bitrate for encoder in encoders}
            self.assertEqual(kinds["music"], "192k")
            self.assertEqual(kinds["tts"], "128k")
            self.assertNotIn(".pcm", "".join(names))
            self.assertNotIn(".flac", "".join(names))

    def test_music_tts_and_soundboard_stay_unmixed(self) -> None:
        primary = struct.pack("<4h", 1000, 1000, 1000, 1000)
        secondary = struct.pack("<4h", 50, 50, 50, 50)
        seen: list[tuple[bytes | None, bytes | None, str | None]] = []

        class Tap:
            def on_bot_frame(self, *, music, overlay, overlay_kind) -> None:
                seen.append((music, overlay, overlay_kind))

        mixer = DuckingAudioSource(
            _ConstantSource([primary]),
            duck_level=0.5,
            tap=Tap(),
        )
        mixer.inject_secondary(_ConstantSource([secondary]), kind="soundboard")
        mixed = mixer.read()
        self.assertEqual(
            mixed,
            mix_pcm16_frames(primary, secondary, duck_level=0.5)
            + bytes(3840 - 8),
        )
        self.assertEqual(seen, [(primary, secondary, "soundboard")])

    def test_standalone_wrapper_copies_onto_its_kind(self) -> None:
        encoders: list[MemoryEncoder] = []
        with tempfile.TemporaryDirectory() as tmp:
            service = RecordingService(
                Mock(recording_data_dir=tmp),
                encoder_factory=_factory(encoders),
            )
            take = service.start(
                guild_id=1,
                voice_channel_id=2,
                text_channel_id=None,
                starter_id=None,
                voice_channel_name="Phòng",
            )
            source = TappedAudioSource(
                _ConstantSource([_tone(9), b""]),
                TakeTap(take),
                "tts",
            )
            self.assertEqual(source.read(), _tone(9))
            self.assertEqual(source.read(), b"")
            service.stop(1)
            tts = next(
                encoder for encoder in encoders if encoder.path.name.startswith("tts")
            )
            self.assertIn(_tone(9), tts.data)


class SyncAndRetentionTests(unittest.TestCase):
    def _service(self, root: str, remote: object | None = None) -> RecordingService:
        return RecordingService(
            Mock(
                recording_data_dir=root,
                recording_max_minutes=120,
                recording_retention_days=7,
                recording_max_bytes=2_000_000_000,
            ),
            remote=remote,  # type: ignore[arg-type]
            encoder_factory=_factory([]),
        )

    def test_r2_upload_runs_only_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain = self._service(tmp)
            take = plain.start(
                guild_id=4,
                voice_channel_id=1,
                text_channel_id=2,
                starter_id=3,
                voice_channel_name="Phòng",
            )
            take.submit_speaker(6, "Minh", _tone(7), index=0)
            finished = plain.stop(4)
            assert finished is not None
            skipped = plain.sync(finished)
            self.assertFalse(skipped.synced)
            self.assertFalse(skipped.error)

            client = Mock()
            client.generate_presigned_url.return_value = "https://example.test/take.zip"
            store = S3ObjectStore(client, "bucket", prefix="recordings")
            synced = self._service(tmp, store).sync(finished)
            self.assertTrue(synced.synced)
            self.assertEqual(synced.url, "https://example.test/take.zip")
            client.upload_file.assert_called_once()
            key = client.upload_file.call_args.args[2]
            self.assertEqual(key, f"recordings/4/{finished.session_id}/take.zip")

    def test_failed_upload_keeps_the_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = Mock()
            client.upload_file.side_effect = OSError("denied")
            store = S3ObjectStore(client, "bucket", prefix="recordings")
            service = self._service(tmp, store)
            finished = service.stop(
                service.start(
                    guild_id=4,
                    voice_channel_id=1,
                    text_channel_id=2,
                    starter_id=3,
                    voice_channel_name="Phòng",
                ).guild_id
            )
            assert finished is not None
            result = service.sync(finished)
            self.assertTrue(result.error)
            self.assertFalse(result.synced)
            self.assertTrue(finished.zip_path.is_file())
            text = completion_message(
                finished.tracks,
                finished.duration_seconds,
                r2_synced=False,
                r2_error=True,
                presigned_url=None,
                attached=False,
                too_large=True,
            )
            self.assertIn("Đã lưu trên máy. Chưa đồng bộ lên R2.", text)
            self.assertIn(
                "File lớn hơn giới hạn Discord. Bản ghi nằm trên ổ ghi âm của bot.",
                text,
            )

    def test_retention_deletes_old_directories_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "1" / "old"
            fresh = root / "1" / "fresh"
            old.mkdir(parents=True)
            fresh.mkdir(parents=True)
            (old / "take.zip").write_bytes(b"zip")
            (fresh / "take.zip").write_bytes(b"zip")
            expired = datetime.now(timezone.utc) - timedelta(days=8)
            for path in (old, old / "take.zip"):
                ts = expired.timestamp()
                path.touch()
                import os

                os.utime(path, (ts, ts))
            removed = RecordingService(
                Mock(
                    recording_data_dir=tmp,
                    recording_retention_days=7,
                    recording_max_minutes=120,
                    recording_max_bytes=2_000_000_000,
                )
            ).purge_local()
            self.assertIn(old, removed)
            self.assertTrue(fresh.is_dir())

    def test_completion_message_includes_sync_and_link(self) -> None:
        text = completion_message(
            (
                TrackEntry("music", "music.mp3", None, "nhạc"),
                TrackEntry("speaker", "user-1.mp3", 1, "Lan"),
            ),
            65,
            r2_synced=True,
            r2_error=False,
            presigned_url="https://example.test/take.zip",
            attached=False,
            too_large=True,
        )
        self.assertIn("Đã lưu bản ghi (1:05).", text)
        self.assertIn("Track: nhạc, Lan.", text)
        self.assertIn("Đã đồng bộ lên R2.", text)
        self.assertIn("https://example.test/take.zip", text)

    def test_recovered_message_names_closed_chunks(self) -> None:
        text = completion_message(
            (TrackEntry("music", "music.mp3", None, "nhạc"),),
            30,
            r2_synced=False,
            r2_error=False,
            presigned_url=None,
            attached=True,
            too_large=False,
            recovered=True,
            closed_chunks=4,
        )
        self.assertIn("Đã khôi phục bản ghi dở (0:30).", text)
        self.assertIn("Đã giữ 4 đoạn MP3 đã đóng.", text)
        self.assertIn("Đoạn cuối chưa đóng có thể bị mất vì bot dừng giữa chừng.", text)


class ChunkRecoveryTests(unittest.TestCase):
    def _service(
        self,
        root: str,
        *,
        chunk_frames: int = 2,
        chunk_seconds: float = 15,
    ) -> RecordingService:
        return RecordingService(
            Mock(
                recording_data_dir=root,
                recording_max_minutes=120,
                recording_retention_days=7,
                recording_max_bytes=2_000_000_000,
            ),
            encoder_factory=_factory([]),
            chunk_frames=chunk_frames,
            chunk_seconds=chunk_seconds,
            part_joiner=_join_bytes,
        )

    def test_stop_joins_sealed_chunks_into_one_mp3(self) -> None:
        encoders: list[MemoryEncoder] = []
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            service._factory = _factory(encoders)
            take = service.start(
                guild_id=3,
                voice_channel_id=1,
                text_channel_id=2,
                starter_id=4,
                voice_channel_name="Phòng",
            )
            for index in range(5):
                take.submit_bot("music", _tone(index + 1), index=index)
            finished = service.stop(3)
            assert finished is not None
            music = [
                encoder for encoder in encoders if encoder.path.name.startswith("music")
            ]
            self.assertEqual(len(music), 3)
            self.assertTrue(all(encoder.closed for encoder in music))
            self.assertFalse(list(finished.zip_path.parent.glob("*.part-*.mp3")))
            with zipfile.ZipFile(finished.zip_path) as archive:
                self.assertIn("music.mp3", archive.namelist())

    def test_crash_keeps_sealed_chunks_and_drops_the_open_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            take = service.start(
                guild_id=9,
                voice_channel_id=3,
                text_channel_id=4,
                starter_id=8,
                voice_channel_name="Phòng",
            )
            for index in range(5):
                take.submit_bot("music", _tone(index + 1), index=index)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                sealed = list(take.directory.glob("music.part-*.mp3"))
                if len(sealed) >= 2:
                    break
                time.sleep(0.01)
            else:
                self.fail("sealed chunks were not published")
            take._finalized = True
            take._stop.set()
            take._thread.join(timeout=2)
            self.assertFalse(take._thread.is_alive())
            service._takes.pop(take.guild_id)
            part_files = sorted(take.directory.glob("music.part-*.mp3"))
            self.assertGreaterEqual(len(part_files), 2)
            expected = b"".join(path.read_bytes() for path in part_files)
            (take.directory / "music.part-00009.partial").write_bytes(b"BROKEN")

            found = service.recover_local()
            self.assertEqual(len(found), 1)
            finished = found[0]
            self.assertTrue(finished.recovered)
            self.assertEqual(finished.text_channel_id, 4)
            self.assertGreaterEqual(finished.closed_chunks, 2)
            with zipfile.ZipFile(finished.zip_path) as archive:
                payload = archive.read("music.mp3")
                names = archive.namelist()
            self.assertEqual(payload, expected)
            self.assertNotIn(b"BROKEN", payload)
            self.assertNotIn("music.part-00009.partial", names)
            self.assertEqual(len(service.recover_local()), 1)
            service.mark_reported(finished)
            self.assertEqual(service.recover_local(), [])

    def test_wall_clock_seals_a_short_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp, chunk_frames=10_000, chunk_seconds=0.05)
            take = service.start(
                guild_id=2,
                voice_channel_id=1,
                text_channel_id=None,
                starter_id=None,
                voice_channel_name="Phòng",
            )
            try:
                take.submit_bot("music", _tone(3), index=0)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if list(take.directory.glob("music.part-*.mp3")):
                        break
                    time.sleep(0.01)
                else:
                    self.fail("wall clock did not seal the open chunk")
            finally:
                service.abort(2)

    def test_partial_only_session_is_not_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "1" / "session"
            session.mkdir(parents=True)
            (session / "music.part-00000.partial").write_bytes(b"BROKEN")
            service = self._service(tmp)
            self.assertEqual(service.recover_local(), [])
            self.assertTrue((session / "music.part-00000.partial").is_file())


if __name__ == "__main__":
    unittest.main()
