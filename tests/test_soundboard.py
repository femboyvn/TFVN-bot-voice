from __future__ import annotations

import errno
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.soundboard import (
    INDEX_FILENAME,
    INDEX_VERSION,
    MIN_DURATION_MS,
    SECONDS_PER_DAY,
    DictObjectStore,
    S3ObjectStore,
    SoundboardCorruptError,
    SoundboardEntry,
    SoundboardError,
    SoundboardIngest,
    SoundboardService,
    SoundboardStore,
    can_delete_sound,
    classify_soundboard_url,
    ffmpeg_encode_command,
    ffprobe_duration_command,
    format_clip_duration,
    looks_like_audio,
    normalize_sound_name,
    parse_myinstants_audio_url,
    remote_object_key,
)


def _entry(
    sound_id: str = "abcd1234",
    name: str = "bruh",
    *,
    added_by: int = 10,
) -> SoundboardEntry:
    return SoundboardEntry(
        id=sound_id,
        name=name,
        mp3=f"{sound_id}.mp3",
        source_url="https://www.myinstants.com/en/instant/bruh/",
        duration_ms=1100,
        added_by=added_by,
        added_at="2026-09-08T12:00:00+00:00",
    )


class SoundboardHelperTests(unittest.TestCase):
    def test_normalize_name_accepts_vietnamese_and_rejects_length(self) -> None:
        self.assertEqual(normalize_sound_name("  bruh  "), "bruh")
        with self.assertRaises(SoundboardError):
            normalize_sound_name("x")
        with self.assertRaises(SoundboardError):
            normalize_sound_name("x" * 33)

    def test_format_clip_duration(self) -> None:
        self.assertEqual(format_clip_duration(1100), "1.1s")
        self.assertEqual(format_clip_duration(12_000), "0:12")

    def test_can_delete_owner_or_manage_guild(self) -> None:
        entry = _entry(added_by=10)
        self.assertTrue(can_delete_sound(entry, user_id=10, manage_guild=False))
        self.assertTrue(can_delete_sound(entry, user_id=99, manage_guild=True))
        self.assertFalse(can_delete_sound(entry, user_id=99, manage_guild=False))

    def test_classify_urls(self) -> None:
        with patch("src.soundboard._validate_url"):
            self.assertEqual(
                classify_soundboard_url(
                    "https://www.myinstants.com/en/instant/bruh/"
                ),
                "myinstants",
            )
            self.assertEqual(
                classify_soundboard_url(
                    "https://www.myinstants.com/media/sounds/bruh.mp3"
                ),
                "myinstants",
            )
            self.assertEqual(
                classify_soundboard_url(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                ),
                "youtube",
            )
            self.assertEqual(
                classify_soundboard_url("https://youtu.be/dQw4w9WgXcQ"),
                "youtube",
            )
            self.assertEqual(
                classify_soundboard_url(
                    "https://cdn.discordapp.com/attachments/1/2/clip.mp3"
                ),
                "direct",
            )
            with self.assertRaisesRegex(SoundboardError, "playlist"):
                classify_soundboard_url(
                    "https://www.youtube.com/playlist?list=PLxxxx"
                )
            with self.assertRaisesRegex(SoundboardError, "Spotify"):
                classify_soundboard_url(
                    "https://open.spotify.com/track/abc"
                )

    def test_classify_blocks_private_urls(self) -> None:
        with self.assertRaises(SoundboardError):
            classify_soundboard_url("http://127.0.0.1/clip.mp3")

    def test_parse_myinstants_og_audio_both_attribute_orders(self) -> None:
        html = """
        <html><head>
        <meta property="og:audio" content="https://www.myinstants.com/media/sounds/vine-boom.mp3">
        </head></html>
        """
        self.assertEqual(
            parse_myinstants_audio_url(html),
            "https://www.myinstants.com/media/sounds/vine-boom.mp3",
        )
        reversed_html = """
        <meta content="https://www.myinstants.com/media/sounds/bruh.mp3" property="og:audio">
        """
        self.assertEqual(
            parse_myinstants_audio_url(reversed_html),
            "https://www.myinstants.com/media/sounds/bruh.mp3",
        )
        fallback = '<a href="https://www.myinstants.com/media/sounds/only.mp3">x</a>'
        self.assertEqual(
            parse_myinstants_audio_url(fallback),
            "https://www.myinstants.com/media/sounds/only.mp3",
        )
        self.assertIsNone(parse_myinstants_audio_url("<html></html>"))

    def test_ffmpeg_argv_trims_and_encodes_mp3(self) -> None:
        command = ffmpeg_encode_command(Path("in.wav"), Path("out.mp3"), 12)
        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], "12")
        self.assertIn("libmp3lame", command)
        probe = ffprobe_duration_command(Path("out.mp3"))
        self.assertEqual(probe[0], "ffprobe")

    def test_looks_like_audio(self) -> None:
        self.assertTrue(looks_like_audio(b"ID3rest", "application/octet-stream"))
        self.assertTrue(looks_like_audio(b"xxxx", "audio/mpeg"))
        self.assertFalse(looks_like_audio(b"<html>", "text/html"))


class SoundboardStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = SoundboardStore(self.root, max_sounds=2)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _write_mp3(self, name: str = "clip.mp3") -> Path:
        path = self.root / name
        path.write_bytes(b"ID3" + b"\x00" * 32)
        return path

    async def test_add_list_remove_roundtrip(self) -> None:
        source = self._write_mp3()
        entry = _entry()
        stored = await self.store.commit_new(7, entry, source)
        self.assertEqual(stored.id, "abcd1234")
        listed = await self.store.list(7)
        self.assertEqual(listed, (entry,))
        self.assertTrue((self.root / "7" / "abcd1234.mp3").is_file())
        index = json.loads((self.root / "7" / "index.json").read_text())
        self.assertEqual(index["version"], INDEX_VERSION)
        self.assertEqual(index["sounds"][0]["mp3"], "abcd1234.mp3")

        removed = await self.store.remove(7, "abcd1234")
        self.assertEqual(removed, entry)
        self.assertEqual(await self.store.list(7), ())
        self.assertFalse((self.root / "7" / "abcd1234.mp3").exists())

    async def test_duplicate_name_and_max_sounds(self) -> None:
        await self.store.commit_new(1, _entry("aaaa1111", "One"), self._write_mp3("a.mp3"))
        with self.assertRaisesRegex(SoundboardError, "trùng tên"):
            await self.store.commit_new(
                1,
                _entry("bbbb2222", "one"),
                self._write_mp3("b.mp3"),
            )
        await self.store.commit_new(1, _entry("bbbb2222", "Two"), self._write_mp3("c.mp3"))
        with self.assertRaisesRegex(SoundboardError, "đã đủ"):
            await self.store.commit_new(
                1,
                _entry("cccc3333", "Three"),
                self._write_mp3("d.mp3"),
            )

    async def test_path_jail_rejects_bad_ids(self) -> None:
        evil = SoundboardEntry(
            id="abcd1234",
            name="x",
            mp3="../escape.mp3",
            source_url="https://example.test/a.mp3",
            duration_ms=1000,
            added_by=1,
            added_at="t",
        )
        with self.assertRaises(SoundboardError):
            await self.store.commit_new(1, evil, self._write_mp3())

    async def test_missing_mp3_lists_but_is_not_playable(self) -> None:
        await self.store.commit_new(3, _entry(), self._write_mp3())
        (self.root / "3" / "abcd1234.mp3").unlink()
        listed = await self.store.list(3)
        self.assertEqual(len(listed), 1)
        self.assertIsNone(self.store.mp3_path(3, listed[0]))

    async def test_corrupt_json_refuses_write(self) -> None:
        guild_dir = self.root / "9"
        guild_dir.mkdir()
        (guild_dir / "index.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(SoundboardCorruptError):
            await self.store.list(9)
        with self.assertRaises(SoundboardCorruptError):
            await self.store.commit_new(9, _entry(), self._write_mp3())

    async def test_empty_guild_lists_nothing(self) -> None:
        self.assertEqual(await self.store.list(404), ())


class SoundboardIngestTests(unittest.TestCase):
    def test_direct_audio_encodes_with_injected_ffmpeg(self) -> None:
        recorded: dict[str, object] = {}

        def fetch(url: str, max_bytes: int) -> tuple[bytes, str]:
            recorded["url"] = url
            recorded["max_bytes"] = max_bytes
            return b"ID3payload", "audio/mpeg"

        def encode(src: Path, dest: Path, max_seconds: int) -> int:
            recorded["src"] = src.read_bytes()
            recorded["max_seconds"] = max_seconds
            self.assertTrue(src.is_relative_to(output.parent))
            self.assertTrue(dest.is_relative_to(output.parent))
            dest.write_bytes(b"ID3encoded")
            return 1500

        ingest = SoundboardIngest(fetch=fetch, encode=encode)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "nested" / "out.mp3"
            with patch("src.soundboard._validate_url"):
                duration = ingest.materialize(
                    "https://cdn.discordapp.com/attachments/1/2/a.mp3",
                    output,
                    max_seconds=12,
                    max_bytes=1500,
                )
            self.assertEqual(duration, 1500)
            self.assertEqual(output.read_bytes(), b"ID3encoded")
            self.assertEqual(recorded["max_seconds"], 12)
            self.assertEqual(recorded["src"], b"ID3payload")
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_myinstants_page_uses_og_audio(self) -> None:
        html = (
            '<meta property="og:audio" '
            'content="https://www.myinstants.com/media/sounds/vine-boom.mp3">'
        )
        fetched: list[str] = []

        def fetch(url: str, max_bytes: int) -> tuple[bytes, str]:
            fetched.append(url)
            if url.endswith("/instant/vine/"):
                return html.encode(), "text/html"
            return b"ID3clip", "audio/mpeg"

        def encode(src: Path, dest: Path, max_seconds: int) -> int:
            dest.write_bytes(src.read_bytes())
            return 900

        ingest = SoundboardIngest(fetch=fetch, encode=encode)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.mp3"
            with patch("src.soundboard._validate_url"):
                ingest.materialize(
                    "https://www.myinstants.com/en/instant/vine/",
                    dest,
                    max_seconds=12,
                    max_bytes=1500,
                )
        self.assertEqual(
            fetched[0],
            "https://www.myinstants.com/en/instant/vine/",
        )
        self.assertEqual(
            fetched[1],
            "https://www.myinstants.com/media/sounds/vine-boom.mp3",
        )

    def test_rejects_short_and_large_encoded_files(self) -> None:
        def fetch(url: str, max_bytes: int) -> tuple[bytes, str]:
            return b"ID3x", "audio/mpeg"

        def encode_short(src: Path, dest: Path, max_seconds: int) -> int:
            dest.write_bytes(b"ID3")
            return MIN_DURATION_MS - 1

        ingest = SoundboardIngest(fetch=fetch, encode=encode_short)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.mp3"
            with patch("src.soundboard._validate_url"):
                with self.assertRaisesRegex(SoundboardError, "ngắn"):
                    ingest.materialize(
                        "https://files.example/a.mp3",
                        dest,
                        max_seconds=12,
                        max_bytes=1500,
                    )

        def encode_large(src: Path, dest: Path, max_seconds: int) -> int:
            dest.write_bytes(b"ID3" + b"\x00" * 2000)
            return 800

        ingest = SoundboardIngest(fetch=fetch, encode=encode_large)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.mp3"
            with patch("src.soundboard._validate_url"):
                with self.assertRaisesRegex(SoundboardError, "quá lớn"):
                    ingest.materialize(
                        "https://files.example/a.mp3",
                        dest,
                        max_seconds=12,
                        max_bytes=1500,
                    )

    def test_youtube_uses_injected_ytdlp(self) -> None:
        def ytdlp(url: str, dest_dir: Path, max_seconds: int) -> Path:
            path = dest_dir / "source.webm"
            path.write_bytes(b"webm")
            self.assertEqual(max_seconds, 12)
            self.assertIn("youtube.com", url)
            return path

        def encode(src: Path, dest: Path, max_seconds: int) -> int:
            self.assertEqual(src.read_bytes(), b"webm")
            dest.write_bytes(b"ID3yt")
            return 2000

        ingest = SoundboardIngest(ytdlp=ytdlp, encode=encode)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out.mp3"
            with patch("src.soundboard._validate_url"):
                duration = ingest.materialize(
                    "https://www.youtube.com/watch?v=abc",
                    dest,
                    max_seconds=12,
                    max_bytes=1500,
                )
        self.assertEqual(duration, 2000)


class SoundboardServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        store = SoundboardStore(root, max_sounds=40)

        def fetch(url: str, max_bytes: int) -> tuple[bytes, str]:
            return b"ID3src", "audio/mpeg"

        def encode(src: Path, dest: Path, max_seconds: int) -> int:
            dest.write_bytes(b"ID3out")
            return 1100

        ingest = SoundboardIngest(fetch=fetch, encode=encode)
        self.service = SoundboardService(
            store,
            ingest,
            max_seconds=12,
            max_bytes=1500,
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_add_sound_persists_json_and_mp3(self) -> None:
        with patch("src.soundboard._validate_url"):
            entry = await self.service.add_sound(
                5,
                name=" Bruh ",
                url="https://cdn.discordapp.com/attachments/1/2/a.mp3",
                added_by=42,
            )
        self.assertEqual(entry.name, "Bruh")
        listed = await self.service.list(5)
        self.assertEqual(listed[0].name, "Bruh")
        path = self.service.store.mp3_path(5, listed[0])
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path.read_bytes(), b"ID3out")

    async def test_add_sound_with_data_on_separate_filesystem(self) -> None:
        root = self.service.store.root.resolve()
        real_replace = os.replace

        def replace(source: str | Path, destination: str | Path) -> None:
            source_on_volume = Path(source).resolve().is_relative_to(root)
            destination_on_volume = Path(destination).resolve().is_relative_to(root)
            if source_on_volume != destination_on_volume:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            real_replace(source, destination)

        for guild_id, remote in ((5, None), (6, DictObjectStore())):
            with self.subTest(remote=remote is not None):
                self.service.store.remote = remote
                with (
                    patch("src.soundboard._validate_url"),
                    patch("src.soundboard.os.replace", side_effect=replace),
                ):
                    entry = await self.service.add_sound(
                        guild_id,
                        name="Bruh",
                        url="https://files.example/a.mp3",
                        added_by=42,
                    )
                self.assertEqual(await self.service.list(guild_id), (entry,))
                path = await self.service.ensure_playable(guild_id, entry)
                self.assertIsNotNone(path)
                assert path is not None
                self.assertEqual(path.read_bytes(), b"ID3out")
                self.assertEqual(
                    set(path.parent.iterdir()),
                    {path, path.parent / INDEX_FILENAME},
                )
                if remote is not None:
                    self.assertEqual(
                        remote.objects[f"{guild_id}/{entry.mp3}"],
                        b"ID3out",
                    )
                    index = json.loads(remote.objects[f"{guild_id}/{INDEX_FILENAME}"])
                    self.assertEqual(index["sounds"][0]["id"], entry.id)

    async def test_add_sound_cleans_staging_after_encode_failure(self) -> None:
        def encode(src: Path, dest: Path, max_seconds: int) -> int:
            dest.write_bytes(b"ID3partial")
            raise SoundboardError("Không mã hóa được âm thanh.")

        self.service.ingest = SoundboardIngest(
            fetch=lambda url, max_bytes: (b"ID3src", "audio/mpeg"),
            encode=encode,
        )
        with patch("src.soundboard._validate_url"):
            with self.assertRaisesRegex(SoundboardError, "Không mã hóa"):
                await self.service.add_sound(
                    5,
                    name="Bruh",
                    url="https://files.example/a.mp3",
                    added_by=42,
                )
        self.assertEqual(await self.service.list(5), ())
        self.assertFalse(
            any(path.is_file() for path in self.service.store.root.rglob("*"))
        )
        self.assertFalse(list(self.service.store.root.rglob("tfd-soundboard-*")))

    async def test_remove_requires_owner(self) -> None:
        with patch("src.soundboard._validate_url"):
            entry = await self.service.add_sound(
                5,
                name="Bruh",
                url="https://cdn.discordapp.com/attachments/1/2/a.mp3",
                added_by=42,
            )
        with self.assertRaisesRegex(SoundboardError, "do bạn thêm"):
            await self.service.remove_sound(
                5,
                entry.id,
                user_id=99,
                manage_guild=False,
            )
        removed = await self.service.remove_sound(
            5,
            entry.id,
            user_id=99,
            manage_guild=True,
        )
        self.assertEqual(removed.id, entry.id)
        self.assertEqual(await self.service.list(5), ())


class RemoteObjectKeyTests(unittest.TestCase):
    def test_joins_prefix(self) -> None:
        self.assertEqual(
            remote_object_key("soundboard", "7/abcd1234.mp3"),
            "soundboard/7/abcd1234.mp3",
        )
        self.assertEqual(remote_object_key("", "7/index.json"), "7/index.json")
        self.assertEqual(
            remote_object_key("/soundboard/", "/7/a.mp3"),
            "soundboard/7/a.mp3",
        )


class S3ObjectStoreTests(unittest.TestCase):
    def test_get_put_delete_use_prefixed_keys(self) -> None:
        client = Mock()
        body = Mock()
        body.read.return_value = b"ID3data"
        client.get_object.return_value = {"Body": body}
        store = S3ObjectStore(client, "bucket", prefix="soundboard")

        store.put_bytes("7/a.mp3", b"ID3data", content_type="audio/mpeg")
        client.put_object.assert_called_once_with(
            Bucket="bucket",
            Key="soundboard/7/a.mp3",
            Body=b"ID3data",
            ContentType="audio/mpeg",
        )
        self.assertEqual(store.get_bytes("7/a.mp3"), b"ID3data")
        client.get_object.assert_called_once_with(
            Bucket="bucket",
            Key="soundboard/7/a.mp3",
        )
        store.delete_key("7/a.mp3")
        client.delete_object.assert_called_once_with(
            Bucket="bucket",
            Key="soundboard/7/a.mp3",
        )

    def test_get_missing_object_returns_none(self) -> None:
        client = Mock()
        error = Exception("missing")
        error.response = {"Error": {"Code": "NoSuchKey"}}  # type: ignore[attr-defined]
        client.get_object.side_effect = error
        store = S3ObjectStore(client, "bucket", prefix="soundboard")
        self.assertIsNone(store.get_bytes("7/missing.mp3"))


class RemoteCacheStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.remote = DictObjectStore()
        self.store = SoundboardStore(
            self.root,
            max_sounds=40,
            remote=self.remote,
            cache_ttl_seconds=7 * SECONDS_PER_DAY,
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _write_mp3(self, name: str = "clip.mp3") -> Path:
        path = self.root / name
        path.write_bytes(b"ID3" + b"\x00" * 32)
        return path

    async def test_commit_uploads_index_and_mp3_to_remote(self) -> None:
        entry = _entry()
        await self.store.commit_new(7, entry, self._write_mp3())
        self.assertIn("7/abcd1234.mp3", self.remote.objects)
        self.assertIn(f"7/{INDEX_FILENAME}", self.remote.objects)
        self.assertTrue((self.root / "7" / "abcd1234.mp3").is_file())

    async def test_ensure_playable_downloads_when_cache_missing(self) -> None:
        entry = _entry()
        await self.store.commit_new(7, entry, self._write_mp3())
        cached = self.root / "7" / "abcd1234.mp3"
        cached.unlink()
        self.assertFalse(cached.exists())

        path = await self.store.ensure_playable(7, entry)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), self.remote.objects["7/abcd1234.mp3"])

    async def test_unused_cache_expires_after_ttl_but_remote_keeps_file(self) -> None:
        entry = _entry()
        await self.store.commit_new(7, entry, self._write_mp3())
        cached = self.root / "7" / "abcd1234.mp3"
        old = time.time() - (8 * SECONDS_PER_DAY)
        os.utime(cached, (old, old))

        self.assertIsNone(self.store.mp3_path(7, entry))
        removed = self.store._evict_expired(7)
        self.assertEqual(removed, 1)
        self.assertFalse(cached.exists())
        self.assertIn("7/abcd1234.mp3", self.remote.objects)

        path = await self.store.ensure_playable(7, entry)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.is_file())

    async def test_play_touch_keeps_cache_from_expiring(self) -> None:
        entry = _entry()
        await self.store.commit_new(7, entry, self._write_mp3())
        cached = self.root / "7" / "abcd1234.mp3"
        almost = time.time() - (6 * SECONDS_PER_DAY)
        os.utime(cached, (almost, almost))
        path = await self.store.ensure_playable(7, entry)
        self.assertEqual(path, cached)
        age = time.time() - cached.stat().st_mtime
        self.assertLess(age, 5)

    async def test_remove_deletes_remote_and_cache(self) -> None:
        entry = _entry()
        await self.store.commit_new(7, entry, self._write_mp3())
        removed = await self.store.remove(7, entry.id)
        self.assertEqual(removed, entry)
        self.assertNotIn("7/abcd1234.mp3", self.remote.objects)
        self.assertFalse((self.root / "7" / "abcd1234.mp3").exists())
        listed = await self.store.list(7)
        self.assertEqual(listed, ())

    async def test_local_only_mode_does_not_expire_files(self) -> None:
        local = SoundboardStore(self.root, max_sounds=40, cache_ttl_seconds=1)
        entry = _entry()
        await local.commit_new(3, entry, self._write_mp3("local.mp3"))
        cached = self.root / "3" / "abcd1234.mp3"
        os.utime(cached, (0, 0))
        self.assertEqual(local._evict_expired(3), 0)
        self.assertTrue(cached.is_file())


if __name__ == "__main__":
    unittest.main()
