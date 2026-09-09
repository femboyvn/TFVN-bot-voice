from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.media import QueuedTrack
from src.playlists import PlaylistBackups, PlaylistError, PlaylistStore
from src.soundboard import DictObjectStore


def track(number: int) -> QueuedTrack:
    return QueuedTrack(f"Bài {number}", f"https://youtu.be/song{number}", number + 60)


class PlaylistStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nested" / "playlists.db"
        self.store = PlaylistStore(self.path, max_per_user=2, max_tracks=4)

    async def test_roundtrip_survives_new_store_and_preserves_order(self) -> None:
        saved = await self.store.create(1, 10, " Nhạc tối ", (track(2), track(1)))
        restored = await PlaylistStore(self.path).get(1, 10, "NHẠC TỐI")
        self.assertEqual(restored, saved)
        self.assertEqual(restored.name, "Nhạc tối")
        self.assertEqual(restored.tracks, (track(2), track(1)))
        self.assertEqual(await self.store.list(1, 10), (saved,))

    async def test_owner_and_guild_isolation_for_all_operations(self) -> None:
        saved = await self.store.create(1, 10, "Private", (track(1),))
        for guild, owner in ((2, 10), (1, 20)):
            with self.subTest(guild=guild, owner=owner):
                self.assertEqual(await self.store.list(guild, owner), ())
                operations = (
                    lambda: self.store.get(guild, owner, saved.id),
                    lambda: self.store.append(guild, owner, saved.id, (track(2),)),
                    lambda: self.store.rename(guild, owner, saved.id, "Other"),
                    lambda: self.store.edit_track(guild, owner, saved.id, 1),
                    lambda: self.store.delete(guild, owner, saved.id),
                )
                for operation in operations:
                    with self.assertRaisesRegex(PlaylistError, "Không tìm thấy"):
                        await operation()
        self.assertEqual(await self.store.get(1, 10, saved.id), saved)
        await self.store.create(2, 10, "Private")
        await self.store.create(1, 20, "Private")

    async def test_unicode_names_are_normalized_and_duplicate_rename_rolls_back(self) -> None:
        saved = await self.store.create(1, 10, "Café")
        with self.assertRaisesRegex(PlaylistError, "trùng tên"):
            await self.store.create(1, 10, "CAFE\u0301")
        other = await self.store.create(1, 10, "Other", (track(1),))
        with self.assertRaisesRegex(PlaylistError, "trùng tên"):
            await self.store.rename(1, 10, other.id, "café")
        self.assertEqual(await self.store.get(1, 10, other.id), other)
        renamed = await self.store.rename(1, 10, saved.id, "New name")
        self.assertEqual(renamed.id, saved.id)
        self.assertEqual(renamed.name, "New name")

    async def test_invalid_names_and_metadata_leave_no_playlist(self) -> None:
        for name in ("", " ", "x" * 65, "a\nb", "x\x00y"):
            with self.subTest(name=name), self.assertRaises(PlaylistError):
                await self.store.create(1, 10, name)
        invalid = (QueuedTrack("", "https://youtu.be/a"),
                   QueuedTrack("Bad", "file:///etc/passwd"),
                   QueuedTrack("Bad", "https://[bad"),
                   QueuedTrack("Bad", "https://youtu.be/a", -1))
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(PlaylistError):
                await self.store.create(1, 10, "List", (item,))
        self.assertEqual(await self.store.list(1, 10), ())

    async def test_move_remove_and_delete_preserve_integrity(self) -> None:
        saved = await self.store.create(1, 10, "List", (track(1), track(2), track(3)))
        moved = await self.store.edit_track(1, 10, saved.id, 3, destination=1)
        self.assertEqual(moved.tracks, (track(3), track(1), track(2)))
        moved = await self.store.edit_track(1, 10, saved.id, 1, destination=3)
        self.assertEqual(moved.tracks, saved.tracks)
        removed = await self.store.edit_track(1, 10, saved.id, 2)
        self.assertEqual(removed.tracks, (track(1), track(3)))
        for position, destination in ((0, None), (3, None), (1, 0), (1, 3)):
            with self.assertRaises(PlaylistError):
                await self.store.edit_track(1, 10, saved.id, position, destination=destination)
            self.assertEqual(await self.store.get(1, 10, saved.id), removed)
        await self.store.delete(1, 10, saved.id)
        self.assertEqual(await self.store.list(1, 10), ())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0], 0)

    async def test_track_limit_rejects_whole_append(self) -> None:
        saved = await self.store.create(1, 10, "List", (track(1), track(2)))
        with self.assertRaisesRegex(PlaylistError, "tối đa 4"):
            await self.store.append(1, 10, saved.id, (track(3), track(4), track(5)))
        self.assertEqual(await self.store.get(1, 10, saved.id), saved)
        with self.assertRaisesRegex(PlaylistError, "tối đa 4"):
            await self.store.create(1, 10, "Too long", tuple(track(i) for i in range(5)))
        self.assertEqual(await self.store.list(1, 10), (saved,))

    async def test_stale_position_edit_is_rejected_in_transaction(self) -> None:
        saved = await self.store.create(1, 10, "List", (track(1), track(2), track(3)))
        updated = await self.store.edit_track(1, 10, saved.id, 1)
        for destination in (None, 2):
            with self.assertRaisesRegex(PlaylistError, "đã thay đổi"):
                await self.store.edit_track(
                    1, 10, saved.id, 1, destination=destination,
                    expected_revision=saved.revision,
                )
        self.assertEqual(await self.store.get(1, 10, saved.id), updated)

    async def test_sql_failure_rolls_back_deleted_and_inserted_tracks(self) -> None:
        saved = await self.store.create(1, 10, "List", (track(1),))
        with sqlite3.connect(self.path) as db:
            db.execute("""
                CREATE TRIGGER reject_track BEFORE INSERT ON playlist_tracks
                WHEN NEW.title = 'Bài 3'
                BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END
            """)
        with self.assertLogs("src.playlists", level="ERROR"):
            with self.assertRaises(PlaylistError):
                await self.store.append(1, 10, saved.id, (track(2), track(3)))
        self.assertEqual(await self.store.get(1, 10, saved.id), saved)

    async def test_cancel_waits_for_worker_to_finish_and_close_connection(self) -> None:
        saved = await self.store.create(1, 10, "List")
        blocker = sqlite3.connect(self.path)
        blocker.execute("BEGIN IMMEDIATE")
        entered = threading.Event()
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            entered.set()
            return connection

        try:
            with patch("src.playlists.sqlite3.connect", side_effect=connect):
                operation = asyncio.create_task(self.store.append(1, 10, saved.id, (track(1),)))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                operation.cancel()
                await asyncio.sleep(0)
                self.assertFalse(operation.done())
                blocker.rollback()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(operation, 2)
        finally:
            blocker.close()
        # Cancellation is reported only after the accepted write has finished.
        self.assertEqual((await self.store.get(1, 10, saved.id)).tracks, (track(1),))

    async def test_concurrent_creates_enforce_quota_across_connections(self) -> None:
        other = PlaylistStore(self.path, max_per_user=2)
        results = await asyncio.gather(
            self.store.create(1, 10, "A"), other.create(1, 10, "B"),
            self.store.create(1, 10, "C"), return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, PlaylistError) for result in results), 1)
        self.assertEqual(len(await self.store.list(1, 10)), 2)

    async def test_concurrent_appends_keep_both_atomic_batches(self) -> None:
        saved = await self.store.create(1, 10, "List")
        other = PlaylistStore(self.path)
        await asyncio.gather(
            self.store.append(1, 10, saved.id, (track(1), track(2))),
            other.append(1, 10, saved.id, (track(3), track(4))),
        )
        result = await self.store.get(1, 10, saved.id)
        self.assertIn(result.tracks, (
            (track(1), track(2), track(3), track(4)),
            (track(3), track(4), track(1), track(2)),
        ))

    async def test_corrupt_database_is_not_overwritten(self) -> None:
        self.path.parent.mkdir()
        original = b"not a SQLite database"
        self.path.write_bytes(original)
        with self.assertLogs("src.playlists", level="ERROR"):
            with self.assertRaisesRegex(PlaylistError, "Không truy cập"):
                await self.store.create(1, 10, "List")
        self.assertEqual(self.path.read_bytes(), original)

    async def test_future_schema_version_refuses_mutation(self) -> None:
        await self.store.create(1, 10, "Existing")
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version = 999")
        with self.assertRaisesRegex(PlaylistError, "Phiên bản"):
            await self.store.create(1, 10, "New")
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM playlists").fetchone()[0], 1)

    async def test_backup_restores_database_and_cleans_staging(self) -> None:
        saved = await self.store.create(1, 10, "List", (track(1), track(2)))
        remote = DictObjectStore()
        await self.store.backup(remote)
        restore_path = self.path.parent / "restored.db"
        restore_path.write_bytes(remote.objects["playlists/latest.sqlite3"])
        self.assertEqual(await PlaylistStore(restore_path).get(1, 10, saved.id), saved)
        self.assertFalse(list(self.path.parent.glob(".playlist-backup-*")))
        with sqlite3.connect(restore_path) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    async def test_failed_backup_does_not_change_library_or_leave_files(self) -> None:
        class FailedRemote(DictObjectStore):
            def put_bytes(self, key, data, *, content_type):
                raise OSError("offline")

        saved = await self.store.create(1, 10, "List", (track(1),))
        with self.assertRaisesRegex(OSError, "offline"):
            await self.store.backup(FailedRemote())
        self.assertEqual(await self.store.get(1, 10, saved.id), saved)
        self.assertFalse(list(self.path.parent.glob(".playlist-backup-*")))

    async def test_absent_local_library_preserves_remote_snapshot(self) -> None:
        remote = DictObjectStore()
        remote.objects["playlists/latest.sqlite3"] = b"existing backup"
        await self.store.backup(remote)
        self.assertEqual(remote.objects["playlists/latest.sqlite3"], b"existing backup")
        self.assertFalse(self.path.exists())


class PlaylistBackupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_upload_is_retried_at_next_interval(self) -> None:
        completed = asyncio.Event()
        store = AsyncMock()

        async def backup(remote) -> None:
            if store.backup.await_count == 1:
                raise OSError("offline")
            completed.set()

        store.backup.side_effect = backup
        runner = PlaylistBackups(store, DictObjectStore(), 0.001)
        with self.assertLogs("src.playlists", level="ERROR"):
            runner.start()
            try:
                await asyncio.wait_for(completed.wait(), 1)
            finally:
                await runner.close()
        self.assertGreaterEqual(store.backup.await_count, 2)

    async def test_close_waits_for_active_snapshot_and_stops_timer(self) -> None:
        started = asyncio.Event()
        finish = asyncio.Event()

        async def backup(remote):
            started.set()
            await finish.wait()

        store = AsyncMock()
        store.backup.side_effect = backup
        runner = PlaylistBackups(store, DictObjectStore(), 0.001)
        runner.start()
        try:
            await asyncio.wait_for(started.wait(), 1)
            closing = asyncio.create_task(runner.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            finish.set()
            await asyncio.wait_for(closing, 1)
            store.backup.assert_awaited_once()
        finally:
            finish.set()
            await runner.close()
