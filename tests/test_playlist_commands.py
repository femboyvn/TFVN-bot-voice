from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord

from src.cogs.music import MusicCog
from src.config import Settings
from src.media import MediaBatch, MediaExtractionError, QueuedTrack
from src.music_ui import PANEL_INTERACTION_TOKEN
from src.player import PlaybackState, PlayerSnapshot
from src.playlists import PlaylistError, PlaylistStore


class PlaylistCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PlaylistStore(Path(self.tmp.name) / "playlists.db")
        self.media = Mock()
        self.media.prepare = AsyncMock()
        self.media.search = AsyncMock()
        self.players = Mock()
        self.player = Mock()
        self.player.enqueue_many = AsyncMock()
        self.player.snapshot.return_value = PlayerSnapshot(
            QueuedTrack("Current", "https://youtu.be/current", 60),
            (QueuedTrack("Waiting", "https://youtu.be/waiting", 90),),
            PlaybackState.PLAYING, False,
        )
        self.players.get.return_value = self.player
        self.players.get_or_create = AsyncMock(return_value=self.player)
        self.sessions = Mock()
        self.sessions.get.return_value = None
        self.cog = MusicCog(
            Mock(), Settings(discord_token="test"), self.media,
            self.players, self.sessions, Mock(), self.store,
        )
        self.ctx = MagicMock()
        self.ctx.guild.id = 1
        self.ctx.author.id = 10
        self.ctx.author.voice.channel = Mock(spec=discord.VoiceChannel)
        self.ctx.author.voice.channel.id = 7
        self.ctx.guild.voice_client.is_connected.return_value = True
        self.ctx.guild.voice_client.channel = self.ctx.author.voice.channel
        self.ctx.voice_client = self.ctx.guild.voice_client
        self.ctx.channel = Mock()
        self.ctx.send = AsyncMock()
        self.ctx.typing.return_value.__aenter__ = AsyncMock()
        self.ctx.typing.return_value.__aexit__ = AsyncMock(return_value=False)
        self.ctx.prefix = "!tfd "

    async def run_command(self, text: str) -> str:
        self.ctx.send.reset_mock()
        await self.cog.playlist.callback(self.cog, self.ctx, arguments=text)
        return self.ctx.send.await_args.args[0]

    def interaction(self) -> MagicMock:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild = self.ctx.guild
        interaction.user = self.ctx.author
        interaction.channel = self.ctx.channel
        interaction.extras = {}
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_create_add_rename_move_remove_and_delete_commands(self) -> None:
        message = await self.run_command('create "Nhạc tối"')
        self.assertIn("Đã lưu", message)
        tracks = (QueuedTrack("A", "https://youtu.be/a"),
                  QueuedTrack("B", "https://youtu.be/b"))
        self.media.prepare.return_value = MediaBatch(tracks, is_playlist=True)
        await self.run_command('add "Nhạc tối" some search phrase')
        self.media.prepare.assert_awaited_once_with("some search phrase")
        await self.run_command('move "Nhạc tối" 2 1')
        saved = await self.store.get(1, 10, "Nhạc tối")
        self.assertEqual(saved.tracks, tuple(reversed(tracks)))
        await self.run_command('remove "Nhạc tối" 2')
        await self.run_command('rename "Nhạc tối" "Nhạc mới"')
        self.assertEqual((await self.store.get(1, 10, "Nhạc mới")).tracks, (tracks[1],))
        await self.run_command('delete "Nhạc mới"')
        self.assertEqual(await self.store.list(1, 10), ())

    async def test_save_captures_current_and_waiting_without_changing_player(self) -> None:
        message = await self.run_command('save "Session"')
        saved = await self.store.get(1, 10, "Session")
        snapshot = self.player.snapshot.return_value
        self.assertEqual(saved.tracks, (snapshot.current,) + snapshot.queued)
        self.assertIn("2 bài", message)
        self.player.enqueue_many.assert_not_awaited()
        self.player.stop_music.assert_not_called()
        self.player.clear_queue.assert_not_called()

    async def test_play_appends_metadata_without_eager_resolution(self) -> None:
        tracks = (QueuedTrack("A", "https://youtu.be/a"),
                  QueuedTrack("B", "https://youtu.be/b"))
        await self.store.create(1, 10, "Session", tracks)
        with patch("src.cogs.music.get_or_connect_voice_client", new=AsyncMock(
            return_value=self.ctx.voice_client,
        )):
            message = await self.run_command('play "Session"')
        self.player.enqueue_many.assert_awaited_once_with(tracks, self.ctx.channel)
        self.player.stop_music.assert_not_called()
        self.player.clear_queue.assert_not_called()
        self.media.prepare.assert_not_awaited()
        self.assertIn("2 bài", message)
        self.player.reserve_activity.assert_called_once()
        self.player.release_activity.assert_called_once()

    async def test_empty_playlist_and_empty_queue_are_reported(self) -> None:
        await self.store.create(1, 10, "Empty")
        self.assertIn("Danh sách trống", await self.run_command("play Empty"))
        self.players.get.return_value = None
        self.assertIn("Không có bài", await self.run_command("save New"))
        self.assertEqual(len(await self.store.list(1, 10)), 1)
        self.player.enqueue_many.assert_not_awaited()

    async def test_wrong_room_cannot_play_or_save_but_can_manage_own_list(self) -> None:
        self.ctx.author.voice.channel = Mock(spec=discord.VoiceChannel)
        self.ctx.author.voice.channel.id = 99
        self.assertIn("Đã lưu", await self.run_command("create Own"))
        for command in ("play Own", "save Forbidden"):
            self.assertNotIn("Đã", await self.run_command(command))
        self.player.enqueue_many.assert_not_awaited()
        self.assertEqual(len(await self.store.list(1, 10)), 1)

    async def test_ownership_is_checked_before_search(self) -> None:
        saved = await self.store.create(1, 20, "Other")
        message = await self.run_command(f"add {saved.id} query")
        self.assertIn("Không tìm thấy", message)
        self.media.prepare.assert_not_awaited()

    async def test_failed_import_keeps_playlist_unchanged_and_reports_skip_limit(self) -> None:
        saved = await self.store.create(1, 10, "List")
        self.media.prepare.side_effect = MediaExtractionError("Không tìm thấy bài.")
        self.assertIn("Không tìm thấy", await self.run_command("add List URL"))
        self.assertEqual(await self.store.get(1, 10, saved.id), saved)
        self.media.prepare.side_effect = None
        self.media.prepare.return_value = MediaBatch(
            (QueuedTrack("A", "https://youtu.be/a"),),
            is_playlist=True, skipped=2, truncated=True,
        )
        message = await self.run_command("add List URL")
        self.assertIn("2 bài không khả dụng", message)
        self.assertIn("25 bài", message)

    async def test_bad_command_arguments_are_vietnamese_errors(self) -> None:
        for command, fragment in (
            ('create "open', "ngoặc kép"), ("unknown", "Cú pháp"),
            ("add", "Cú pháp"), ("remove List x", "số nguyên"),
        ):
            with self.subTest(command=command):
                self.assertIn(fragment, await self.run_command(command))

    async def test_bare_command_opens_launcher_without_publishing_library(self) -> None:
        await self.store.create(1, 10, "Hidden name")
        message = await self.run_command("")
        self.assertNotIn("Hidden name", message)
        view = self.ctx.send.await_args.kwargs["view"]
        self.assertEqual(view.requester_id, 10)
        self.assertEqual(view.guild_id, 1)

    async def test_ui_play_rechecks_panel_after_waiting_for_guild_lock(self) -> None:
        saved = await self.store.create(1, 10, "List", (QueuedTrack("A", "https://youtu.be/a"),))
        old = object()
        record = Mock(view=old)
        self.cog.music_ui.get = Mock(return_value=record)
        interaction = self.interaction()
        interaction.extras[PANEL_INTERACTION_TOKEN] = old
        lock = self.cog._operation_lock(1)
        await lock.acquire()
        task = asyncio.create_task(self.cog.ui_playlist_action(
            interaction, 1, "play", [saved.id], 7,
        ))
        # Yield until the DB read finishes and the action waits for the lock.
        for _ in range(100):
            if lock._waiters:
                break
            await asyncio.sleep(0.001)
        record.view = object()
        lock.release()
        message = await asyncio.wait_for(task, 1)
        self.assertIn("thay thế", message)
        self.player.enqueue_many.assert_not_awaited()

    async def test_ui_add_rechecks_panel_after_media_extraction(self) -> None:
        saved = await self.store.create(1, 10, "List")
        old = object()
        record = Mock(view=old)
        self.cog.music_ui.get = Mock(return_value=record)
        interaction = self.interaction()
        interaction.extras[PANEL_INTERACTION_TOKEN] = old

        async def prepare(query):
            record.view = object()
            return MediaBatch((QueuedTrack("A", "https://youtu.be/a"),))

        self.media.prepare.side_effect = prepare
        message = await self.cog.ui_playlist_action(
            interaction, 1, "add", [saved.id, "query"], 7,
        )
        self.assertIn("thay thế", message)
        self.assertEqual(await self.store.get(1, 10, saved.id), saved)

    async def test_ui_play_and_save_use_same_queue_paths_as_commands(self) -> None:
        interaction = self.interaction()
        message = await self.cog.ui_playlist_action(interaction, 1, "save", ["UI"])
        self.assertIn("2 bài", message)
        saved = await self.store.get(1, 10, "UI")
        with patch("src.cogs.music.connect_member_voice_client", new=AsyncMock()):
            message = await self.cog.ui_playlist_action(interaction, 1, "play", [saved.id])
        self.assertIn("2 bài", message)
        self.player.enqueue_many.assert_awaited_once_with(saved.tracks, self.ctx.channel)

    async def test_ui_rejects_wrong_guild_and_other_owners(self) -> None:
        saved = await self.store.create(1, 20, "Other")
        interaction = self.interaction()
        self.assertIn("Không tìm thấy", await self.cog.ui_playlist_action(
            interaction, 1, "delete", [saved.id],
        ))
        self.assertIn("máy chủ", await self.cog.ui_playlist_action(
            interaction, 2, "create", ["Wrong guild"],
        ))
        self.assertEqual(await self.store.get(1, 20, saved.id), saved)
        self.assertEqual(await self.store.list(2, 10), ())

    async def test_ui_stale_track_edit_does_not_modify_new_order(self) -> None:
        tracks = (QueuedTrack("A", "https://youtu.be/a"),
                  QueuedTrack("B", "https://youtu.be/b"))
        saved = await self.store.create(1, 10, "List", tracks)
        updated = await self.store.edit_track(1, 10, saved.id, 1)
        result = await self.cog.ui_playlist_action(
            self.interaction(), 1, "remove", [saved.id, "1"],
            expected_revision=saved.revision,
        )
        self.assertIn("đã thay đổi", result)
        self.assertEqual(await self.store.get(1, 10, saved.id), updated)
