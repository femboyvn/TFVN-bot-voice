"""Personal, per-guild playlists backed by transactional SQLite storage."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

from .media import QueuedTrack
from .soundboard import ObjectStore

log = logging.getLogger(__name__)
T = TypeVar("T")
SCHEMA_VERSION = 2
MAX_PLAYLIST_NAME = 64
SERVER_OWNER_ID = 0


class PlaylistError(RuntimeError):
    """A Vietnamese error safe to show to the requester."""


@dataclass(frozen=True, slots=True)
class SavedPlaylist:
    id: str
    guild_id: int
    owner_id: int
    name: str
    tracks: tuple[QueuedTrack, ...]
    created_at: str
    revision: int = 0

    @property
    def is_server(self) -> bool:
        return self.owner_id == SERVER_OWNER_ID


@dataclass(frozen=True, slots=True)
class PlaybackSession:
    """Last bound room and canonical queue for restart restore."""

    guild_id: int
    voice_channel_id: int
    text_channel_id: int
    loop_current: bool = False
    loop_queue: bool = False
    current: QueuedTrack | None = None
    queued: tuple[QueuedTrack, ...] = ()


async def _in_thread(operation: Callable[[], T]) -> T:
    """Finish and close database work before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


def normalize_playlist_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name).strip()
    if not 1 <= len(name) <= MAX_PLAYLIST_NAME or any(
        unicodedata.category(char).startswith("C") for char in name
    ):
        raise PlaylistError("Tên danh sách phải có 1–64 ký tự, không xuống dòng.")
    return name


class PlaylistStore:
    """Each operation owns a connection and transaction in a worker thread.

    BEGIN IMMEDIATE serializes quota checks and writes even across store
    instances. Connections are always closed; no event-loop thread does SQL.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_per_user: int = 20,
        max_tracks: int = 100,
        max_per_server: int = 20,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.max_per_user = max_per_user
        self.max_tracks = max_tracks
        self.max_per_server = max_per_server

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            with db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, SCHEMA_VERSION):
                    raise PlaylistError(
                        "Phiên bản thư viện chưa được hỗ trợ. Hãy cập nhật bot."
                    )
                if version == 0:
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS playlists (
                            id TEXT PRIMARY KEY,
                            guild_id TEXT NOT NULL,
                            owner_id TEXT NOT NULL,
                            name TEXT NOT NULL,
                            name_key TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            revision INTEGER NOT NULL DEFAULT 0,
                            UNIQUE (guild_id, owner_id, name_key)
                        )
                    """)
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS playlist_tracks (
                            playlist_id TEXT NOT NULL REFERENCES playlists(id)
                                ON DELETE CASCADE,
                            position INTEGER NOT NULL CHECK (position >= 1),
                            title TEXT NOT NULL,
                            url TEXT NOT NULL,
                            duration INTEGER CHECK (duration >= 0),
                            PRIMARY KEY (playlist_id, position)
                        )
                    """)
                    version = 1
                if version == 1:
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS guild_sessions (
                            guild_id TEXT PRIMARY KEY,
                            voice_channel_id TEXT NOT NULL,
                            text_channel_id TEXT NOT NULL,
                            loop_current INTEGER NOT NULL DEFAULT 0,
                            loop_queue INTEGER NOT NULL DEFAULT 0,
                            updated_at TEXT NOT NULL
                        )
                    """)
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS guild_queue (
                            guild_id TEXT NOT NULL,
                            position INTEGER NOT NULL,
                            is_current INTEGER NOT NULL DEFAULT 0,
                            title TEXT NOT NULL,
                            url TEXT NOT NULL,
                            duration INTEGER CHECK (duration >= 0),
                            PRIMARY KEY (guild_id, position)
                        )
                    """)
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                yield db

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def execute() -> T:
            try:
                with self._connection() as db:
                    return operation(db)
            except (sqlite3.Error, OSError) as exc:
                log.exception("Playlist database operation failed")
                raise PlaylistError(
                    "Không truy cập được danh sách phát. Hãy thử lại."
                ) from exc

        return await _in_thread(execute)

    @staticmethod
    def _decode(db: sqlite3.Connection, row: sqlite3.Row) -> SavedPlaylist:
        tracks = tuple(
            QueuedTrack(track["title"], track["url"], track["duration"])
            for track in db.execute(
                "SELECT title, url, duration FROM playlist_tracks "
                "WHERE playlist_id = ? ORDER BY position",
                (row["id"],),
            )
        )
        return SavedPlaylist(
            row["id"], int(row["guild_id"]), int(row["owner_id"]),
            row["name"], tracks, row["created_at"], row["revision"],
        )

    def _get(
        self, db: sqlite3.Connection, guild_id: int, owner_id: int, ref: str,
    ) -> SavedPlaylist:
        name_key = unicodedata.normalize("NFC", ref).strip().casefold()
        row = db.execute(
            "SELECT * FROM playlists WHERE guild_id = ? AND owner_id = ? "
            "AND (id = ? OR name_key = ?) ORDER BY (id = ?) DESC LIMIT 1",
            (str(guild_id), str(owner_id), ref, name_key, ref),
        ).fetchone()
        if row is None:
            raise PlaylistError(
                "Không tìm thấy danh sách phát của máy chủ."
                if owner_id == SERVER_OWNER_ID else
                "Không tìm thấy danh sách phát của bạn."
            )
        return self._decode(db, row)

    def _validate_tracks(
        self, tracks: Sequence[QueuedTrack],
    ) -> tuple[QueuedTrack, ...]:
        if len(tracks) > self.max_tracks:
            raise PlaylistError(
                f"Mỗi danh sách chứa tối đa {self.max_tracks} bài."
            )
        for track in tracks:
            try:
                parsed = urlparse(track.webpage_url)
                valid = (
                    parsed.scheme in {"http", "https"} and parsed.hostname
                    and len(track.webpage_url) <= 2048
                    and 0 < len(track.title.strip()) <= 500
                    and (track.duration is None or (
                        isinstance(track.duration, int) and track.duration >= 0
                    ))
                )
            except (TypeError, ValueError, AttributeError):
                valid = False
            if not valid:
                raise PlaylistError(
                    "Bài hát không có liên kết hoặc thông tin hợp lệ để lưu."
                )
        return tuple(tracks)

    @staticmethod
    def _write_tracks(
        db: sqlite3.Connection, playlist_id: str, tracks: Sequence[QueuedTrack],
    ) -> None:
        db.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,),
        )
        db.executemany(
            "INSERT INTO playlist_tracks VALUES (?, ?, ?, ?, ?)",
            ((playlist_id, index, track.title, track.webpage_url, track.duration)
             for index, track in enumerate(tracks, 1)),
        )
        db.execute(
            "UPDATE playlists SET revision = revision + 1 WHERE id = ?",
            (playlist_id,),
        )

    async def list(self, guild_id: int, owner_id: int) -> tuple[SavedPlaylist, ...]:
        def read(db: sqlite3.Connection) -> tuple[SavedPlaylist, ...]:
            return tuple(self._decode(db, row) for row in db.execute(
                "SELECT * FROM playlists WHERE guild_id = ? AND owner_id = ? "
                "ORDER BY name_key, id", (str(guild_id), str(owner_id)),
            ).fetchall())
        return await self._run(read)

    async def get(self, guild_id: int, owner_id: int, ref: str) -> SavedPlaylist:
        return await self._run(lambda db: self._get(db, guild_id, owner_id, ref))

    async def create(
        self, guild_id: int, owner_id: int, name: str,
        tracks: Sequence[QueuedTrack] = (),
    ) -> SavedPlaylist:
        name = normalize_playlist_name(name)
        tracks = self._validate_tracks(tracks)

        def write(db: sqlite3.Connection) -> SavedPlaylist:
            count = db.execute(
                "SELECT COUNT(*) FROM playlists WHERE guild_id = ? "
                "AND owner_id = ?", (str(guild_id), str(owner_id)),
            ).fetchone()[0]
            limit = (
                self.max_per_server if owner_id == SERVER_OWNER_ID
                else self.max_per_user
            )
            if count >= limit:
                raise PlaylistError(
                    f"Máy chủ có thể lưu tối đa {limit} danh sách chung."
                    if owner_id == SERVER_OWNER_ID else
                    f"Bạn có thể lưu tối đa {limit} danh sách "
                    "trong máy chủ này."
                )
            playlist_id = uuid.uuid4().hex
            try:
                db.execute(
                    "INSERT INTO playlists "
                    "(id, guild_id, owner_id, name, name_key, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (playlist_id, str(guild_id), str(owner_id), name,
                     name.casefold(), datetime.now(timezone.utc).isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise PlaylistError(
                    "Máy chủ đã có danh sách phát trùng tên."
                    if owner_id == SERVER_OWNER_ID else
                    "Bạn đã có danh sách phát trùng tên."
                ) from exc
            self._write_tracks(db, playlist_id, tracks)
            return self._get(db, guild_id, owner_id, playlist_id)
        return await self._run(write)

    async def rename(
        self, guild_id: int, owner_id: int, ref: str, name: str,
    ) -> SavedPlaylist:
        name = normalize_playlist_name(name)

        def write(db: sqlite3.Connection) -> SavedPlaylist:
            playlist = self._get(db, guild_id, owner_id, ref)
            try:
                db.execute(
                    "UPDATE playlists SET name = ?, name_key = ?, "
                    "revision = revision + 1 WHERE id = ?",
                    (name, name.casefold(), playlist.id),
                )
            except sqlite3.IntegrityError as exc:
                raise PlaylistError(
                    "Máy chủ đã có danh sách phát trùng tên."
                    if owner_id == SERVER_OWNER_ID else
                    "Bạn đã có danh sách phát trùng tên."
                ) from exc
            return self._get(db, guild_id, owner_id, playlist.id)
        return await self._run(write)

    async def delete(self, guild_id: int, owner_id: int, ref: str) -> None:
        def write(db: sqlite3.Connection) -> None:
            playlist = self._get(db, guild_id, owner_id, ref)
            db.execute("DELETE FROM playlists WHERE id = ?", (playlist.id,))
        await self._run(write)

    async def append(
        self, guild_id: int, owner_id: int, ref: str,
        tracks: Sequence[QueuedTrack],
    ) -> SavedPlaylist:
        tracks = self._validate_tracks(tracks)

        def write(db: sqlite3.Connection) -> SavedPlaylist:
            playlist = self._get(db, guild_id, owner_id, ref)
            updated = self._validate_tracks(playlist.tracks + tracks)
            self._write_tracks(db, playlist.id, updated)
            return self._get(db, guild_id, owner_id, playlist.id)
        return await self._run(write)

    async def edit_track(
        self, guild_id: int, owner_id: int, ref: str, position: int,
        *, destination: int | None = None,
        expected_revision: int | None = None,
    ) -> SavedPlaylist:
        """Remove a track, or move it to another one-based position."""
        def write(db: sqlite3.Connection) -> SavedPlaylist:
            playlist = self._get(db, guild_id, owner_id, ref)
            if expected_revision is not None and playlist.revision != expected_revision:
                raise PlaylistError(
                    "Danh sách đã thay đổi. Hãy chọn lại bài từ danh sách mới."
                )
            tracks = list(playlist.tracks)
            positions = (position,) if destination is None else (position, destination)
            if any(not 1 <= index <= len(tracks) for index in positions):
                raise PlaylistError("Số thứ tự bài hát không hợp lệ.")
            track = tracks.pop(position - 1)
            if destination is not None:
                tracks.insert(destination - 1, track)
            self._write_tracks(db, playlist.id, tracks)
            return self._get(db, guild_id, owner_id, playlist.id)
        return await self._run(write)

    async def save_playback(
        self,
        guild_id: int,
        voice_channel_id: int,
        text_channel_id: int,
        *,
        current: QueuedTrack | None,
        queued: Sequence[QueuedTrack],
        loop_current: bool = False,
        loop_queue: bool = False,
    ) -> None:
        """Replace the persisted room binding and canonical queue."""
        tracks = []
        if current is not None:
            tracks.append((current, 1))
        tracks.extend((track, 0) for track in queued)

        def write(db: sqlite3.Connection) -> None:
            db.execute(
                "INSERT INTO guild_sessions "
                "(guild_id, voice_channel_id, text_channel_id, loop_current, "
                "loop_queue, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "voice_channel_id = excluded.voice_channel_id, "
                "text_channel_id = excluded.text_channel_id, "
                "loop_current = excluded.loop_current, "
                "loop_queue = excluded.loop_queue, "
                "updated_at = excluded.updated_at",
                (
                    str(guild_id),
                    str(voice_channel_id),
                    str(text_channel_id),
                    int(loop_current),
                    int(loop_queue),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            db.execute("DELETE FROM guild_queue WHERE guild_id = ?", (str(guild_id),))
            db.executemany(
                "INSERT INTO guild_queue "
                "(guild_id, position, is_current, title, url, duration) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (str(guild_id), index, is_current, track.title, track.webpage_url,
                     track.duration)
                    for index, (track, is_current) in enumerate(tracks, 1)
                ),
            )

        await self._run(write)

    async def load_playback(self, guild_id: int) -> PlaybackSession | None:
        def read(db: sqlite3.Connection) -> PlaybackSession | None:
            return self._decode_session(db, str(guild_id))
        return await self._run(read)

    async def list_playback(self) -> tuple[PlaybackSession, ...]:
        def read(db: sqlite3.Connection) -> tuple[PlaybackSession, ...]:
            rows = db.execute("SELECT guild_id FROM guild_sessions").fetchall()
            sessions = []
            for row in rows:
                session = self._decode_session(db, row["guild_id"])
                if session is not None:
                    sessions.append(session)
            return tuple(sessions)
        return await self._run(read)

    async def clear_playback(self, guild_id: int) -> None:
        def write(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM guild_queue WHERE guild_id = ?", (str(guild_id),))
            db.execute("DELETE FROM guild_sessions WHERE guild_id = ?", (str(guild_id),))
        await self._run(write)

    @staticmethod
    def _decode_session(
        db: sqlite3.Connection, guild_id: str,
    ) -> PlaybackSession | None:
        row = db.execute(
            "SELECT * FROM guild_sessions WHERE guild_id = ?", (guild_id,),
        ).fetchone()
        if row is None:
            return None
        current = None
        queued: list[QueuedTrack] = []
        for track in db.execute(
            "SELECT is_current, title, url, duration FROM guild_queue "
            "WHERE guild_id = ? ORDER BY position",
            (guild_id,),
        ):
            item = QueuedTrack(track["title"], track["url"], track["duration"])
            if track["is_current"] and current is None:
                current = item
            else:
                queued.append(item)
        return PlaybackSession(
            int(row["guild_id"]),
            int(row["voice_channel_id"]),
            int(row["text_channel_id"]),
            bool(row["loop_current"]),
            bool(row["loop_queue"]),
            current,
            tuple(queued),
        )

    async def backup(self, remote: ObjectStore) -> None:
        """Upload a consistent snapshot; staging stays on the database volume."""
        def upload() -> None:
            if not self.path.is_file():
                return
            with tempfile.TemporaryDirectory(
                prefix=".playlist-backup-", dir=self.path.parent,
            ) as tmp:
                snapshot = Path(tmp) / "playlists.db"
                with (
                    contextlib.closing(sqlite3.connect(self.path)) as source,
                    contextlib.closing(sqlite3.connect(snapshot)) as target,
                ):
                    source.backup(target)
                remote.put_bytes(
                    "playlists/latest.sqlite3", snapshot.read_bytes(),
                    content_type="application/vnd.sqlite3",
                )
        await _in_thread(upload)


class PlaylistBackups:
    """Opt-in periodic R2 snapshots, finishing an upload before shutdown."""

    def __init__(
        self, store: PlaylistStore, remote: ObjectStore, interval_seconds: float,
    ) -> None:
        self.store = store
        self.remote = remote
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="playlist-backups")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval_seconds)
            except TimeoutError:
                try:
                    await self.store.backup(self.remote)
                except Exception:
                    log.exception("Could not back up playlists to R2")

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
