"""Per-guild multi-track recorder.

Each take writes 48 kHz mono MP3 stems that share one 20 ms clock: music,
TTS, soundboard, and one file per speaking member. PCM exists only as the
bytes fed to FFmpeg. A stem is sealed into a finished MP3 chunk every few
seconds, so a crash keeps those chunks and loses only the open tail. The
session directory stores the assembled MP3 files and ``take.zip``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import discord

from .ducking import FrameTap

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
FRAME_SECONDS = 0.02
MONO_FRAME_BYTES = 1920
STEREO_FRAME_BYTES = 3840
SILENCE_FRAME = b"\x00" * MONO_FRAME_BYTES
MUSIC_BITRATE = "192k"
SPEECH_BITRATE = "128k"
BOT_KINDS = ("music", "tts", "soundboard")
_QUEUE_MAX = 500
_SILENCE_CHUNK_FRAMES = 50
_INCOMPLETE_MAX_AGE_SECONDS = 3600
# A sealed chunk is a finished MP3. A crash can lose only the open chunk.
CHUNK_SECONDS = 15.0
CHUNK_FRAMES = int(CHUNK_SECONDS / FRAME_SECONDS)
_PART_NAME = re.compile(
    r"^(?P<stem>.+)\.part-(?P<index>\d+)-(?P<frames>\d+)\.mp3$"
)
_NO_ITEM = object()
_TRACK_LABELS = {"music": "nhạc", "tts": "TTS", "soundboard": "soundboard"}


class RecordingError(RuntimeError):
    """Requester-facing recording failure. ``str(exc)`` is Vietnamese UI copy."""


class RecordingBusy(RecordingError):
    """A guild already has a take open."""


class PcmEncoder(Protocol):
    """Consumes mono s16le frames and closes them into one audio file."""

    def write(self, pcm: bytes) -> None: ...

    def close(self) -> None: ...


class EncoderFactory(Protocol):
    def __call__(self, path: Path, bitrate: str) -> PcmEncoder: ...


class PartJoiner(Protocol):
    """Join finished MP3 chunks into one stem file."""

    def __call__(self, parts: Sequence[Path], dest: Path) -> None: ...


class _NullEncoder:
    def write(self, pcm: bytes) -> None:
        return None

    def close(self) -> None:
        return None


class _FFmpegEncoder:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._failed = False
        self._stderr_buf = ""
        self._drain_thread = threading.Thread(
            target=self._drain_stderr,
            name="mp3-stderr",
            daemon=True,
        )
        self._drain_thread.start()

    def write(self, pcm: bytes) -> None:
        if self._failed:
            raise BrokenPipeError(self._stderr_text() or "MP3 encoder already stopped")
        stdin = self._process.stdin
        if stdin is None or self._process.poll() is not None:
            self._failed = True
            raise BrokenPipeError(self._stderr_text() or "MP3 encoder is not running")
        try:
            stdin.write(pcm)
        except (BrokenPipeError, OSError) as exc:
            self._failed = True
            detail = self._stderr_text()
            raise BrokenPipeError(detail or "MP3 encoder pipe closed") from exc

    def close(self) -> None:
        stdin = self._process.stdin
        if stdin is not None:
            try:
                stdin.close()
            except (BrokenPipeError, OSError):
                self._failed = True
        try:
            code = self._process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self._process.kill()
            log.warning("Stopped an MP3 encoder that did not exit")
            return
        if code != 0:
            detail = self._stderr_text()
            log.warning(
                "MP3 encoder exited with status %s%s",
                code,
                f": {detail}" if detail else "",
            )

    def _drain_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        kept = b""
        try:
            while True:
                piece = stderr.read(1024)
                if not piece:
                    break
                kept = (kept + piece)[-4096:]
        except (OSError, ValueError):
            return
        self._stderr_buf = kept.decode("utf-8", "replace").strip()[-500:]

    def _stderr_text(self) -> str:
        """Return ffmpeg's error text once the process has exited."""
        if self._process.poll() is None:
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return self._stderr_buf
        self._drain_thread.join(timeout=1)
        return self._stderr_buf


def start_ffmpeg_encoder(path: Path, bitrate: str) -> PcmEncoder:
    """Encode mono s16le on stdin to a constant-bitrate MP3 at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            # The live file ends in ``.partial``. Without an explicit format,
            # ffmpeg rejects that extension and the next write hits a dead pipe.
            "-f",
            "mp3",
            str(path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return _FFmpegEncoder(process)


def join_mp3_parts(parts: Sequence[Path], dest: Path) -> None:
    """Join finished chunks into *dest*. One chunk is renamed into place."""
    if not parts:
        raise ValueError("no MP3 chunks to join")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        os.replace(parts[0], dest)
        return
    list_path = dest.with_name(dest.stem + ".concat.txt")
    lines = []
    for part in parts:
        escaped = part.resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(dest),
            ],
            check=False,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    finally:
        list_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"ffmpeg concat failed: {detail}")
    for part in parts:
        part.unlink(missing_ok=True)


def to_mono_frame(pcm: bytes) -> bytes:
    """Return one 20 ms mono frame. Stereo input is averaged to mono."""
    if not pcm:
        return SILENCE_FRAME
    if len(pcm) == STEREO_FRAME_BYTES or (
        len(pcm) % 4 == 0 and len(pcm) != MONO_FRAME_BYTES
    ):
        mono = _downmix_stereo(pcm)
    elif len(pcm) % 2 == 0:
        mono = pcm
    else:
        mono = pcm[:-1]
    if len(mono) < MONO_FRAME_BYTES:
        return mono + bytes(MONO_FRAME_BYTES - len(mono))
    return mono[:MONO_FRAME_BYTES]


def _downmix_stereo(pcm: bytes) -> bytes:
    usable = len(pcm) - (len(pcm) % 4)
    mixed = bytearray(usable // 2)
    for src in range(0, usable, 4):
        left = int.from_bytes(pcm[src : src + 2], "little", signed=True)
        right = int.from_bytes(pcm[src + 2 : src + 4], "little", signed=True)
        sample = (left + right) // 2
        dest = src // 2
        mixed[dest : dest + 2] = sample.to_bytes(2, "little", signed=True)
    return bytes(mixed)


class TrackWriter:
    """One MP3 stem built from finished chunks that share frame 0."""

    def __init__(
        self,
        path: Path,
        bitrate: str,
        factory: EncoderFactory,
        *,
        chunk_frames: int = CHUNK_FRAMES,
        part_joiner: PartJoiner = join_mp3_parts,
    ) -> None:
        self.path = path
        self.bitrate = bitrate
        self.next_index = 0
        self.received_audio = False
        self.parts: list[tuple[str, int]] = []
        self._factory = factory
        self._chunk_frames = max(1, chunk_frames)
        self._joiner = part_joiner
        self._part_number = 0
        self._part_frames = 0
        self._encoder: PcmEncoder | None = None
        self._partial: Path | None = None

    def write_at(self, index: int, pcm: bytes) -> None:
        """Write *pcm* at *index*, filling any hole in front with silence.

        A frame that lands on an index already written is appended next, so
        two packets in the same 20 ms bucket are both kept, in arrival order.
        A full chunk is closed into its own MP3 before the next frame.
        """
        if index > self.next_index:
            self._write_silence(index - self.next_index, rotate=True)
            self.next_index = index
        self._emit(to_mono_frame(pcm), rotate=True)
        self.next_index += 1
        self.received_audio = True

    def pad_to(self, index: int) -> None:
        """Extend the file with silence so it reaches *index* frames.

        The tail stays on the open encoder. Live audio is already sealed in
        earlier chunks, so this long silence fill is not rotated.
        """
        if self._part_frames >= self._chunk_frames:
            self._seal()
        if index > self.next_index:
            self._write_silence(index - self.next_index, rotate=False)
            self.next_index = index
        if self.next_index == 0:
            self._emit(SILENCE_FRAME, rotate=False)
            self.next_index = 1

    def seal_open(self) -> bool:
        """Close the open chunk when it already holds audio."""
        if self._encoder is None or self._part_frames <= 0:
            return False
        return self._seal()

    def close(self) -> None:
        """Seal the tail and join every finished chunk into the stem file."""
        if self._part_frames > 0:
            self._seal()
        else:
            self._drop_open_encoder()
        self._assemble()

    def abandon(self) -> None:
        """Close the live encoder without publishing or joining chunks."""
        self._drop_open_encoder()

    def _ensure(self) -> None:
        if self._encoder is not None:
            return
        partial = self.path.with_name(
            f"{self.path.stem}.part-{self._part_number:05d}.partial"
        )
        self._partial = partial
        try:
            self._encoder = self._factory(partial, self.bitrate)
        except Exception:
            log.exception("Could not start the MP3 encoder for %s", self.path.name)
            self._encoder = _NullEncoder()

    def _emit(self, pcm: bytes, *, rotate: bool) -> None:
        offset = 0
        while offset < len(pcm):
            self._ensure()
            assert self._encoder is not None
            if rotate:
                room = self._chunk_frames - self._part_frames
                if room <= 0:
                    self._seal()
                    self._ensure()
                    assert self._encoder is not None
                    room = self._chunk_frames
                piece = pcm[offset : offset + room * MONO_FRAME_BYTES]
            else:
                piece = pcm[offset:]
            try:
                self._encoder.write(piece)
            except OSError as exc:
                self._fail_open_encoder(exc)
            self._part_frames += len(piece) // MONO_FRAME_BYTES
            offset += len(piece)
            if rotate and self._part_frames >= self._chunk_frames:
                self._seal()

    def _write_silence(self, frames: int, *, rotate: bool) -> None:
        remaining = frames
        while remaining > 0:
            count = min(remaining, _SILENCE_CHUNK_FRAMES)
            self._emit(SILENCE_FRAME * count, rotate=rotate)
            remaining -= count

    def _seal(self) -> bool:
        encoder = self._encoder
        partial = self._partial
        frames = self._part_frames
        number = self._part_number
        self._encoder = None
        self._partial = None
        self._part_frames = 0
        if encoder is None or partial is None or frames <= 0:
            return False
        try:
            encoder.close()
        except Exception:
            log.exception("Could not close MP3 chunk %s", partial.name)
            partial.unlink(missing_ok=True)
            return False
        try:
            if not partial.is_file() or partial.stat().st_size <= 0:
                partial.unlink(missing_ok=True)
                return False
            with partial.open("rb") as handle:
                os.fsync(handle.fileno())
            durable_name = f"{self.path.stem}.part-{number:05d}-{frames}.mp3"
            os.replace(partial, partial.with_name(durable_name))
        except OSError:
            log.exception("Could not publish MP3 chunk %s", partial.name)
            return False
        self._part_number = number + 1
        self.parts.append((durable_name, frames))
        return True

    def _fail_open_encoder(self, exc: BaseException) -> None:
        """Stop writing to a dead ffmpeg and keep the timeline moving."""
        if not isinstance(self._encoder, _NullEncoder):
            detail = str(exc).strip()
            log.warning(
                "MP3 encoder stopped for %s: %s",
                self.path.name,
                detail or "pipe closed",
            )
        self._drop_open_encoder(reset_frames=False)
        self._encoder = _NullEncoder()
        self._partial = None

    def _drop_open_encoder(self, *, reset_frames: bool = True) -> None:
        encoder = self._encoder
        partial = self._partial
        self._encoder = None
        self._partial = None
        if reset_frames:
            self._part_frames = 0
        if encoder is not None:
            try:
                encoder.close()
            except Exception:
                log.exception("Could not close the MP3 encoder for %s", self.path.name)
        if partial is not None:
            partial.unlink(missing_ok=True)

    def _assemble(self) -> None:
        if not self.parts:
            return
        paths = [self.path.parent / name for name, _frames in self.parts]
        if any(not path.is_file() for path in paths):
            log.error("Missing a sealed MP3 chunk for %s", self.path.name)
            return
        try:
            self._joiner(paths, self.path)
        except Exception:
            log.exception("Could not join MP3 chunks for %s", self.path.name)
            return
        self.parts = []


@dataclass(frozen=True, slots=True)
class TrackEntry:
    """One file inside the finished zip."""

    kind: str
    filename: str
    user_id: int | None
    display_name: str


@dataclass(frozen=True, slots=True)
class FinishedTake:
    """A closed take whose zip is ready to attach or upload."""

    guild_id: int
    session_id: str
    zip_path: Path
    duration_seconds: float
    tracks: tuple[TrackEntry, ...]
    voice_channel_name: str
    text_channel_id: int | None = None
    recovered: bool = False
    closed_chunks: int = 0


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of the optional R2 upload."""

    synced: bool
    error: bool
    url: str | None


class ObjectUploader(Protocol):
    def put_file(self, key: str, path: Path, *, content_type: str) -> None: ...

    def presigned_get(self, key: str, *, expires: int) -> str: ...

    def list_entries(self) -> list[tuple[str, datetime]]: ...

    def delete_key(self, key: str) -> None: ...


class TakeTap:
    """Copy bot playback stems into an open take without mixing them."""

    def __init__(self, take: Take) -> None:
        self.take = take

    def on_bot_frame(
        self,
        *,
        music: bytes | None,
        overlay: bytes | None,
        overlay_kind: str | None,
    ) -> None:
        if music:
            self.take.submit_bot("music", music)
        if overlay and overlay_kind:
            self.take.submit_bot(overlay_kind, overlay)


class TappedAudioSource(discord.AudioSource):
    """Play *inner* unchanged and copy its frames onto one recording stem."""

    def __init__(self, inner: discord.AudioSource, tap: FrameTap, kind: str) -> None:
        self.inner = inner
        self.tap = tap
        self.kind = kind

    def read(self) -> bytes:
        data = self.inner.read()
        if data:
            try:
                self.tap.on_bot_frame(music=b"", overlay=data, overlay_kind=self.kind)
            except Exception:
                log.exception("Recording tap failed for %s", self.kind)
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        cleanup = getattr(self.inner, "cleanup", None)
        if callable(cleanup):
            cleanup()


class Take:
    """Open MP3 stems for one guild, written from a single background thread."""

    def __init__(
        self,
        directory: Path,
        *,
        guild_id: int,
        session_id: str,
        voice_channel_id: int,
        text_channel_id: int | None,
        starter_id: int | None,
        voice_channel_name: str,
        encoder_factory: EncoderFactory,
        max_bytes: int,
        chunk_frames: int = CHUNK_FRAMES,
        chunk_seconds: float = CHUNK_SECONDS,
        part_joiner: PartJoiner = join_mp3_parts,
    ) -> None:
        self.directory = directory
        self.guild_id = guild_id
        self.session_id = session_id
        self.voice_channel_id = voice_channel_id
        self.text_channel_id = text_channel_id
        self.starter_id = starter_id
        self.voice_channel_name = voice_channel_name
        self.max_bytes = max_bytes
        self.clock_t0 = time.monotonic()
        self.started_at = datetime.now(timezone.utc)
        self.limit_hit = threading.Event()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._factory = encoder_factory
        self._chunk_frames = max(1, chunk_frames)
        self._chunk_seconds = chunk_seconds if chunk_seconds > 0 else CHUNK_SECONDS
        self._joiner = part_joiner
        self.tracks = {
            kind: self._make_writer(
                directory / f"{kind}.mp3",
                MUSIC_BITRATE if kind == "music" else SPEECH_BITRATE,
            )
            for kind in BOT_KINDS
        }
        self.speakers: dict[int, TrackWriter] = {}
        self.speaker_names: dict[int, str] = {}
        self._queue: queue.Queue[tuple[object, ...] | None] = queue.Queue(
            maxsize=_QUEUE_MAX
        )
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._finalized = False
        self._dropped = 0
        self._drop_logged = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"record-{guild_id}",
            daemon=True,
        )
        _atomic_json(self.directory / "session.json", self._session_payload())
        self._thread.start()

    def _make_writer(self, path: Path, bitrate: str) -> TrackWriter:
        return TrackWriter(
            path,
            bitrate,
            self._factory,
            chunk_frames=self._chunk_frames,
            part_joiner=self._joiner,
        )

    def frame_index(self, now: float | None = None) -> int:
        moment = time.monotonic() if now is None else now
        return max(0, round((moment - self.clock_t0) / FRAME_SECONDS))

    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.clock_t0)

    @property
    def speaker_count(self) -> int:
        with self._state_lock:
            return len(self.speakers)

    def submit_bot(
        self,
        kind: str,
        pcm: bytes,
        *,
        index: int | None = None,
    ) -> None:
        if kind not in self.tracks or self._finalized:
            return
        self._put(("bot", kind, self._index(index), pcm))

    def submit_speaker(
        self,
        user_id: int,
        name: str,
        pcm: bytes,
        *,
        index: int | None = None,
    ) -> None:
        if self._finalized:
            return
        self._put(("speaker", user_id, name, self._index(index), pcm))

    def finalize(self) -> FinishedTake:
        """Stop the writer, pad every stem to the same length, and zip them."""
        self._finalized = True
        self._stop.set()
        try:
            self._queue.put(None, timeout=5)
        except queue.Full:
            log.warning("Recording queue stayed full while stopping guild %s", self.guild_id)
        self._thread.join(timeout=15)
        acquired = self._write_lock.acquire(timeout=5)
        try:
            writers = (*self.tracks.values(), *self.speakers.values())
            end_index = max(
                self.frame_index(),
                1,
                *(writer.next_index for writer in writers),
            )
            for writer in writers:
                try:
                    writer.pad_to(end_index)
                    writer.close()
                except Exception:
                    log.exception("Could not close a recording stem in %s", self.directory)
            entries = self._entries()
            duration = end_index * FRAME_SECONDS
            self._write_manifest(entries, duration_seconds=duration)
            zip_path = self.directory / "take.zip"
            self._write_zip(zip_path, entries)
        finally:
            if acquired:
                self._write_lock.release()
        return FinishedTake(
            guild_id=self.guild_id,
            session_id=self.session_id,
            zip_path=zip_path,
            duration_seconds=duration,
            tracks=entries,
            voice_channel_name=self.voice_channel_name,
            text_channel_id=self.text_channel_id,
        )

    def discard(self) -> None:
        """Drop an unfinished take, including its directory."""
        self._finalized = True
        self._stop.set()
        try:
            self._queue.put(None, timeout=2)
        except queue.Full:
            pass
        self._thread.join(timeout=5)
        with self._write_lock:
            for writer in (*self.tracks.values(), *self.speakers.values()):
                try:
                    writer.abandon()
                except Exception:
                    log.exception("Could not close a discarded stem")
        shutil.rmtree(self.directory, ignore_errors=True)

    def _index(self, index: int | None) -> int:
        if index is None:
            return self.frame_index()
        return max(0, index)

    def _put(self, item: tuple[object, ...]) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._dropped += 1
            if not self._drop_logged:
                log.warning(
                    "Recording queue is full in guild %s; later frames may be dropped",
                    self.guild_id,
                )
                self._drop_logged = True

    def _run(self) -> None:
        next_seal = time.monotonic() + self._chunk_seconds
        while not self._stop.is_set() or not self._queue.empty():
            timeout = max(0.01, min(0.2, next_seal - time.monotonic()))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = _NO_ITEM
            if time.monotonic() >= next_seal:
                try:
                    with self._write_lock:
                        if self._seal_open_writers():
                            self._write_checkpoint()
                except Exception:
                    log.exception(
                        "Could not seal recording chunks in guild %s",
                        self.guild_id,
                    )
                next_seal = time.monotonic() + self._chunk_seconds
            if item is _NO_ITEM:
                continue
            if item is None:
                break
            try:
                with self._write_lock:
                    self._apply(item)
                    if self._directory_bytes() >= self.max_bytes:
                        self.limit_hit.set()
            except Exception:
                log.exception("Recording writer failed in guild %s", self.guild_id)

    def _seal_open_writers(self) -> bool:
        sealed = False
        for writer in (*self.tracks.values(), *self.speakers.values()):
            if writer.seal_open():
                sealed = True
        return sealed

    def _apply(self, item: tuple[object, ...]) -> None:
        before = self._chunk_token()
        self._apply_item(item)
        if self._chunk_token() != before:
            self._write_checkpoint()

    def _apply_item(self, item: tuple[object, ...]) -> None:
        kind = item[0]
        if kind == "bot":
            _, track_kind, index, pcm = item
            if not isinstance(track_kind, str) or not isinstance(index, int):
                return
            if not isinstance(pcm, (bytes, bytearray)):
                return
            self.tracks[track_kind].write_at(index, bytes(pcm))
            return
        _, user_id, name, index, pcm = item
        if not isinstance(user_id, int) or not isinstance(index, int):
            return
        if not isinstance(pcm, (bytes, bytearray)):
            return
        writer = self.speakers.get(user_id)
        if writer is None:
            writer = self._make_writer(
                self.directory / f"user-{user_id}.mp3",
                SPEECH_BITRATE,
            )
            with self._state_lock:
                self.speakers[user_id] = writer
                self.speaker_names[user_id] = str(name or user_id)
        writer.write_at(index, bytes(pcm))

    def _chunk_token(self) -> tuple[tuple[str, ...], ...]:
        writers = (*self.tracks.values(), *self.speakers.values())
        return tuple(
            tuple(name for name, _frames in writer.parts)
            for writer in writers
        )

    def _directory_bytes(self) -> int:
        total = 0
        for path in self.directory.glob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def _entries(self) -> tuple[TrackEntry, ...]:
        entries = [
            TrackEntry(kind, f"{kind}.mp3", None, _TRACK_LABELS[kind])
            for kind in BOT_KINDS
        ]
        for user_id, writer in sorted(self.speakers.items()):
            if not writer.received_audio:
                continue
            entries.append(
                TrackEntry(
                    "speaker",
                    writer.path.name,
                    user_id,
                    self.speaker_names.get(user_id, str(user_id)),
                )
            )
        return tuple(entries)

    def _session_payload(self) -> dict[str, object]:
        return {
            "guild_id": self.guild_id,
            "session_id": self.session_id,
            "voice_channel_id": self.voice_channel_id,
            "text_channel_id": self.text_channel_id,
            "voice_channel_name": self.voice_channel_name,
            "starter_user_id": self.starter_id,
            "started_at": self.started_at.isoformat(),
            "sample_rate": SAMPLE_RATE,
        }

    def _checkpoint_tracks(self) -> list[dict[str, object]]:
        tracks: list[dict[str, object]] = []
        for kind, writer in self.tracks.items():
            if not writer.parts:
                continue
            tracks.append(
                {
                    "kind": kind,
                    "filename": f"{kind}.mp3",
                    "user_id": None,
                    "display_name": _TRACK_LABELS[kind],
                    "parts": [
                        {"file": name, "frames": frames}
                        for name, frames in writer.parts
                    ],
                }
            )
        for user_id, writer in sorted(self.speakers.items()):
            if not writer.parts:
                continue
            tracks.append(
                {
                    "kind": "speaker",
                    "filename": writer.path.name,
                    "user_id": user_id,
                    "display_name": self.speaker_names.get(user_id, str(user_id)),
                    "parts": [
                        {"file": name, "frames": frames}
                        for name, frames in writer.parts
                    ],
                }
            )
        return tracks

    def _write_checkpoint(self) -> None:
        payload = self._session_payload()
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        payload["tracks"] = self._checkpoint_tracks()
        _atomic_json(self.directory / "checkpoint.json", payload)

    def _write_manifest(
        self,
        entries: Sequence[TrackEntry],
        *,
        duration_seconds: float,
        recovered: bool = False,
        closed_chunks: int = 0,
    ) -> None:
        _atomic_json(
            self.directory / "manifest.json",
            _manifest_payload(
                guild_id=self.guild_id,
                session_id=self.session_id,
                voice_channel_id=self.voice_channel_id,
                text_channel_id=self.text_channel_id,
                voice_channel_name=self.voice_channel_name,
                started_at=self.started_at.isoformat(),
                starter_id=self.starter_id,
                duration_seconds=duration_seconds,
                recovered=recovered,
                closed_chunks=closed_chunks,
                entries=entries,
            ),
        )

    def _write_zip(self, zip_path: Path, entries: Sequence[TrackEntry]) -> None:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            manifest = self.directory / "manifest.json"
            archive.write(manifest, "manifest.json")
            for entry in entries:
                path = self.directory / entry.filename
                if path.is_file():
                    archive.write(path, entry.filename)


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _manifest_payload(
    *,
    guild_id: int,
    session_id: str,
    voice_channel_id: int,
    text_channel_id: int | None,
    voice_channel_name: str,
    started_at: str,
    starter_id: int | None,
    duration_seconds: float,
    recovered: bool,
    closed_chunks: int,
    entries: Sequence[TrackEntry],
) -> dict[str, object]:
    return {
        "guild_id": guild_id,
        "session_id": session_id,
        "voice_channel_id": voice_channel_id,
        "text_channel_id": text_channel_id,
        "voice_channel_name": voice_channel_name,
        "started_at": started_at,
        "starter_user_id": starter_id,
        "sample_rate": SAMPLE_RATE,
        "bitrates": {"music": MUSIC_BITRATE, "speech": SPEECH_BITRATE},
        "duration_seconds": duration_seconds,
        "recovered": recovered,
        "closed_chunks": closed_chunks,
        "tracks": [
            {
                "kind": entry.kind,
                "filename": entry.filename,
                "user_id": entry.user_id,
                "display_name": entry.display_name,
            }
            for entry in entries
        ],
    }


def _closed_parts(directory: Path) -> dict[str, list[tuple[int, int, str]]]:
    grouped: dict[str, list[tuple[int, int, str]]] = {}
    try:
        paths = list(directory.iterdir())
    except OSError:
        return grouped
    for path in paths:
        match = _PART_NAME.match(path.name)
        if match is None or not path.is_file():
            continue
        grouped.setdefault(match.group("stem"), []).append(
            (int(match.group("index")), int(match.group("frames")), path.name)
        )
    for items in grouped.values():
        items.sort()
    return grouped


def _final_stem_files(directory: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    try:
        paths = list(directory.iterdir())
    except OSError:
        return found
    for path in paths:
        if not path.is_file() or path.suffix != ".mp3":
            continue
        if ".part-" in path.name or _PART_NAME.match(path.name):
            continue
        found[path.stem] = path
    return found


def _stem_sort_key(stem: str) -> tuple[int, int, str]:
    if stem in BOT_KINDS:
        return (0, BOT_KINDS.index(stem), stem)
    if stem.startswith("user-") and stem[5:].isdigit():
        return (1, int(stem[5:]), stem)
    return (2, 0, stem)


def _checkpoint_names(
    payload: dict[str, object] | None,
) -> dict[str, tuple[str, int | None, str]]:
    names: dict[str, tuple[str, int | None, str]] = {}
    if payload is None:
        return names
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return names
    for track in tracks:
        if not isinstance(track, dict):
            continue
        filename = track.get("filename")
        if not isinstance(filename, str) or not filename.endswith(".mp3"):
            continue
        stem = filename[:-4]
        kind = track.get("kind")
        if not isinstance(kind, str) or not kind:
            kind = "speaker"
        user_id = track.get("user_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            user_id = None
        display = track.get("display_name")
        if not isinstance(display, str) or not display:
            display = _TRACK_LABELS.get(stem, stem)
        names[stem] = (kind, user_id, display)
    return names


def _entry_for_stem(
    stem: str,
    names: dict[str, tuple[str, int | None, str]],
) -> TrackEntry:
    known = names.get(stem)
    if known is not None:
        kind, user_id, display = known
        return TrackEntry(kind, f"{stem}.mp3", user_id, display)
    label = _TRACK_LABELS.get(stem)
    if label is not None:
        return TrackEntry(stem, f"{stem}.mp3", None, label)
    if stem.startswith("user-") and stem[5:].isdigit():
        user_id = int(stem[5:])
        return TrackEntry("speaker", f"{stem}.mp3", user_id, str(user_id))
    return TrackEntry("speaker", f"{stem}.mp3", None, stem)


def _take_from_payload(
    directory: Path,
    payload: dict[str, object],
    zip_path: Path,
) -> FinishedTake | None:
    guild_id = _positive_int(payload.get("guild_id"))
    if guild_id is None:
        guild_id = _positive_int(directory.parent.name)
    if guild_id is None:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = directory.name
    duration = payload.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        duration = 0
    voice_name = payload.get("voice_channel_name")
    if not isinstance(voice_name, str):
        voice_name = ""
    text_id = payload.get("text_channel_id")
    if isinstance(text_id, bool) or not isinstance(text_id, int):
        text_id = None
    chunks = payload.get("closed_chunks")
    if isinstance(chunks, bool) or not isinstance(chunks, int):
        chunks = 0
    entries: list[TrackEntry] = []
    tracks = payload.get("tracks")
    if isinstance(tracks, list):
        for track in tracks:
            if not isinstance(track, dict):
                continue
            filename = track.get("filename")
            kind = track.get("kind")
            display = track.get("display_name")
            if not isinstance(filename, str) or not isinstance(kind, str):
                continue
            if not isinstance(display, str) or not display:
                display = filename
            user_id = track.get("user_id")
            if isinstance(user_id, bool) or not isinstance(user_id, int):
                user_id = None
            entries.append(TrackEntry(kind, filename, user_id, display))
    return FinishedTake(
        guild_id=guild_id,
        session_id=session_id,
        zip_path=zip_path,
        duration_seconds=float(duration),
        tracks=tuple(entries),
        voice_channel_name=voice_name,
        text_channel_id=text_id,
        recovered=payload.get("recovered") is True,
        closed_chunks=chunks,
    )


def assemble_crashed_session(
    directory: Path,
    joiner: PartJoiner,
) -> FinishedTake | None:
    """Build a zip from sealed chunks left behind when a take did not finish.

    A directory that already has ``take.zip`` is left alone unless sealed
    chunk files are still present. Those leftovers mean the final join did
    not finish, so the zip is rebuilt from the chunks.
    """
    grouped = _closed_parts(directory)
    finals = _final_stem_files(directory)
    if (directory / "take.zip").is_file() and not grouped:
        return None
    if not grouped and not finals:
        return None
    session = _read_json(directory / "session.json") or {}
    checkpoint = _read_json(directory / "checkpoint.json") or {}
    meta = {**session, **checkpoint}
    names = _checkpoint_names(checkpoint)
    stems = sorted(set(grouped) | set(finals), key=_stem_sort_key)
    entries: list[TrackEntry] = []
    max_frames = 0
    closed = 0
    failed = False
    for stem in stems:
        dest = directory / f"{stem}.mp3"
        items = grouped.get(stem, [])
        if items:
            try:
                joiner(
                    [directory / name for _index, _frames, name in items],
                    dest,
                )
            except Exception:
                log.exception("Could not join recovered chunks for %s", dest.name)
                failed = True
                continue
            frames = sum(frame_count for _index, frame_count, _name in items)
            max_frames = max(max_frames, frames)
            closed += len(items)
        elif not dest.is_file():
            failed = True
            continue
        if dest.is_file():
            entries.append(_entry_for_stem(stem, names))
    if failed or not entries:
        return None
    for partial in directory.glob("*.partial"):
        partial.unlink(missing_ok=True)
    guild_id = _positive_int(meta.get("guild_id"))
    if guild_id is None:
        guild_id = _positive_int(directory.parent.name)
    if guild_id is None:
        log.warning("Recovered recording %s has no guild id", directory)
        return None
    session_id = meta.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = directory.name
    text_id = meta.get("text_channel_id")
    if isinstance(text_id, bool) or not isinstance(text_id, int):
        text_id = None
    voice_id = _positive_int(meta.get("voice_channel_id")) or 0
    voice_name = meta.get("voice_channel_name")
    if not isinstance(voice_name, str):
        voice_name = ""
    started_at = meta.get("started_at")
    if not isinstance(started_at, str):
        started_at = datetime.now(timezone.utc).isoformat()
    starter_id = meta.get("starter_user_id")
    if isinstance(starter_id, bool) or not isinstance(starter_id, int):
        starter_id = None
    duration = max_frames * FRAME_SECONDS
    zip_path = directory / "take.zip"
    _atomic_json(
        directory / "manifest.json",
        _manifest_payload(
            guild_id=guild_id,
            session_id=session_id,
            voice_channel_id=voice_id,
            text_channel_id=text_id,
            voice_channel_name=voice_name,
            started_at=started_at,
            starter_id=starter_id,
            duration_seconds=duration,
            recovered=True,
            closed_chunks=closed,
            entries=entries,
        ),
    )
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(directory / "manifest.json", "manifest.json")
        for entry in entries:
            path = directory / entry.filename
            if path.is_file():
                archive.write(path, entry.filename)
    return FinishedTake(
        guild_id=guild_id,
        session_id=session_id,
        zip_path=zip_path,
        duration_seconds=duration,
        tracks=tuple(entries),
        voice_channel_name=voice_name,
        text_channel_id=text_id,
        recovered=True,
        closed_chunks=closed,
    )


def _pending_report(directory: Path) -> FinishedTake | None:
    """Return a recovered zip that has not been posted to Discord yet."""
    if (directory / "notified").is_file():
        return None
    zip_path = directory / "take.zip"
    payload = _read_json(directory / "manifest.json")
    if payload is None or payload.get("recovered") is not True or not zip_path.is_file():
        return None
    return _take_from_payload(directory, payload, zip_path)


def format_duration(seconds: float) -> str:
    """Render a take length as ``M:SS`` or ``H:MM:SS``."""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def completion_message(
    tracks: Sequence[TrackEntry],
    duration_seconds: float,
    *,
    r2_synced: bool,
    r2_error: bool,
    presigned_url: str | None,
    attached: bool,
    too_large: bool,
    recovered: bool = False,
    closed_chunks: int = 0,
) -> str:
    """Vietnamese channel message for a finished take."""
    names = [track.display_name for track in tracks]
    if recovered:
        lines = [
            f"Đã khôi phục bản ghi dở ({format_duration(duration_seconds)}).",
            f"Đã giữ {closed_chunks} đoạn MP3 đã đóng.",
            "Track: " + ", ".join(names) + ".",
            "Đoạn cuối chưa đóng có thể bị mất vì bot dừng giữa chừng.",
        ]
    else:
        lines = [
            f"Đã lưu bản ghi ({format_duration(duration_seconds)}).",
            "Track: " + ", ".join(names) + ".",
        ]
    if r2_synced:
        lines.append("Đã đồng bộ lên R2.")
    elif r2_error:
        lines.append("Đã lưu trên máy. Chưa đồng bộ lên R2.")
    if not attached and too_large and presigned_url:
        lines.append(presigned_url)
    elif not attached and too_large and not r2_synced:
        lines.append(
            "File lớn hơn giới hạn Discord. Bản ghi nằm trên ổ ghi âm của bot."
        )
    return "\n".join(lines)


class RecordingService:
    """One open take per guild, plus local retention and the R2 mirror."""

    def __init__(
        self,
        settings: object,
        remote: ObjectUploader | None = None,
        encoder_factory: EncoderFactory = start_ffmpeg_encoder,
        chunk_frames: int = CHUNK_FRAMES,
        chunk_seconds: float = CHUNK_SECONDS,
        part_joiner: PartJoiner = join_mp3_parts,
    ) -> None:
        raw_dir = getattr(settings, "recording_data_dir", None)
        if isinstance(raw_dir, str) and raw_dir.strip():
            self.root = Path(raw_dir)
        else:
            self.root = Path("data/recordings")
        self.max_seconds = _setting_int(settings, "recording_max_minutes", 120) * 60
        self.retention_days = _setting_int(settings, "recording_retention_days", 7)
        self.max_bytes = _setting_int(settings, "recording_max_bytes", 2_147_483_648)
        self.remote = remote
        self._factory = encoder_factory
        self._chunk_frames = max(1, chunk_frames)
        self._chunk_seconds = chunk_seconds if chunk_seconds > 0 else CHUNK_SECONDS
        self._joiner = part_joiner
        self._takes: dict[int, Take] = {}
        self._lock = threading.Lock()

    def active(self, guild_id: int) -> Take | None:
        with self._lock:
            return self._takes.get(guild_id)

    def active_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._takes)

    def start(
        self,
        *,
        guild_id: int,
        voice_channel_id: int,
        text_channel_id: int | None,
        starter_id: int | None,
        voice_channel_name: str,
    ) -> Take:
        session_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        directory = self.root / str(guild_id) / session_id
        take = Take(
            directory,
            guild_id=guild_id,
            session_id=session_id,
            voice_channel_id=voice_channel_id,
            text_channel_id=text_channel_id,
            starter_id=starter_id,
            voice_channel_name=voice_channel_name,
            encoder_factory=self._factory,
            max_bytes=self.max_bytes,
            chunk_frames=self._chunk_frames,
            chunk_seconds=self._chunk_seconds,
            part_joiner=self._joiner,
        )
        with self._lock:
            if guild_id in self._takes:
                take.discard()
                raise RecordingBusy("Kênh này đang được ghi.")
            self._takes[guild_id] = take
        return take

    def stop(self, guild_id: int) -> FinishedTake | None:
        with self._lock:
            take = self._takes.pop(guild_id, None)
        if take is None:
            return None
        return take.finalize()

    def abort(self, guild_id: int) -> None:
        with self._lock:
            take = self._takes.pop(guild_id, None)
        if take is not None:
            take.discard()

    def recover_local(self) -> list[FinishedTake]:
        """Assemble crashed takes and return recovered zips that are not posted yet."""
        if not self.root.is_dir():
            return []
        active = {take.directory.resolve() for take in self._open_takes()}
        found: list[FinishedTake] = []
        for guild_dir in list(self.root.iterdir()):
            if not guild_dir.is_dir():
                continue
            for session_dir in list(guild_dir.iterdir()):
                if not session_dir.is_dir():
                    continue
                try:
                    if session_dir.resolve() in active:
                        continue
                except OSError:
                    continue
                try:
                    finished = assemble_crashed_session(session_dir, self._joiner)
                    if finished is None:
                        finished = _pending_report(session_dir)
                except Exception:
                    log.exception("Could not recover recording %s", session_dir)
                    continue
                if finished is not None:
                    found.append(finished)
        return found

    def mark_reported(self, finished: FinishedTake) -> None:
        """Remember that the recovered-take message was posted."""
        target = finished.zip_path.parent / "notified"
        target.write_text("ok", encoding="utf-8")

    def sync(self, finished: FinishedTake) -> SyncResult:
        """Upload the zip when R2 is configured. Local files stay either way."""
        remote = self.remote
        if remote is None:
            return SyncResult(synced=False, error=False, url=None)
        key = f"{finished.guild_id}/{finished.session_id}/take.zip"
        try:
            remote.put_file(
                key,
                finished.zip_path,
                content_type="application/zip",
            )
        except Exception:
            log.exception(
                "Could not upload recording %s for guild %s",
                finished.session_id,
                finished.guild_id,
            )
            return SyncResult(synced=False, error=True, url=None)
        url: str | None = None
        try:
            url = remote.presigned_get(key, expires=86400)
        except Exception:
            log.exception(
                "Could not sign recording %s for guild %s",
                finished.session_id,
                finished.guild_id,
            )
        return SyncResult(synced=True, error=False, url=url)

    def purge_local(self, *, now: datetime | None = None) -> list[Path]:
        """Delete expired zips and sessions that never sealed a chunk."""
        if not self.root.is_dir():
            return []
        moment = now or datetime.now(timezone.utc)
        now_ts = moment.timestamp()
        removed: list[Path] = []
        active = {take.directory.resolve() for take in self._open_takes()}
        for guild_dir in list(self.root.iterdir()):
            if not guild_dir.is_dir():
                continue
            for session_dir in list(guild_dir.iterdir()):
                if not session_dir.is_dir():
                    continue
                try:
                    if session_dir.resolve() in active:
                        continue
                    age = now_ts - session_dir.stat().st_mtime
                except OSError:
                    continue
                finished = (session_dir / "take.zip").is_file()
                expired = (
                    finished and age >= self.retention_days * 86400
                ) or (not finished and age >= _INCOMPLETE_MAX_AGE_SECONDS)
                if not expired:
                    continue
                shutil.rmtree(session_dir, ignore_errors=True)
                removed.append(session_dir)
            try:
                guild_dir.rmdir()
            except OSError:
                pass
        return removed

    def purge_remote(self, *, now: datetime | None = None) -> None:
        remote = self.remote
        if remote is None:
            return
        moment = now or datetime.now(timezone.utc)
        cutoff = moment - timedelta(days=self.retention_days)
        try:
            entries = remote.list_entries()
        except Exception:
            log.exception("Could not list recordings in R2")
            return
        for key, modified in entries:
            try:
                if not isinstance(modified, datetime):
                    continue
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                if modified >= cutoff:
                    continue
                remote.delete_key(key)
            except Exception:
                log.exception("Could not delete expired recording %s", key)

    def _open_takes(self) -> tuple[Take, ...]:
        with self._lock:
            return tuple(self._takes.values())


def _setting_int(settings: object, name: str, default: int) -> int:
    value = getattr(settings, name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


# Imported by type checkers that look for an explicit factory alias.
EncoderStarter: Callable[[Path, str], PcmEncoder] = start_ffmpeg_encoder
