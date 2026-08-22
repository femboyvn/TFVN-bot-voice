"""Command behavior: stop vs leave with an active TTS session."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord

from src.media import MediaBatch, QueuedTrack
from src.cogs.music import MusicCog
from src.music_ui import PANEL_INTERACTION_TOKEN
from src.player import JumpResult


class StopVsLeaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Mock()
        self.settings = Mock()
        self.settings.command_prefix = "!tfd "
        self.settings.tts_enabled = True
        self.media = Mock()
        self.players = Mock()
        self.players.add_state_listener = Mock()
        self.players.remove = AsyncMock()
        self.sessions = Mock()
        self.sessions.get.return_value = None
        self.cog = MusicCog(
            self.bot,
            self.settings,
            self.media,
            self.players,
            self.sessions,
        )
        self.ctx = AsyncMock()
        self.ctx.guild.id = 1
        self.ctx.prefix = "!tfd "
        self.ctx.voice_client = MagicMock()
        self.ctx.voice_client.is_connected.return_value = True
        self.ctx.voice_client.disconnect = AsyncMock()
        voice_channel = Mock()
        voice_channel.id = 7
        self.ctx.voice_client.channel = voice_channel
        self.ctx.guild.voice_client = self.ctx.voice_client
        self.ctx.author.voice.channel = voice_channel
        typing_context = MagicMock()
        typing_context.__aenter__ = AsyncMock(return_value=None)
        typing_context.__aexit__ = AsyncMock(return_value=None)
        self.ctx.typing = Mock(return_value=typing_context)

    async def test_stop_with_session_keeps_connection_and_session(self) -> None:
        self.sessions.is_active.return_value = True
        player = Mock()
        player.stop_music = AsyncMock(return_value=True)
        self.players.get.return_value = player
        self.players.remove = AsyncMock()
        self.sessions.stop = AsyncMock()

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(),
        ) as disconnect:
            await self.cog.stop.callback(self.cog, self.ctx)

        player.stop_music.assert_awaited_once_with()
        self.players.remove.assert_not_awaited()
        self.sessions.stop.assert_not_called()
        disconnect.assert_not_awaited()
        self.ctx.voice_client.disconnect.assert_not_called()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("Đã dừng nhạc", sent)
        self.assertNotIn("rời", sent.lower())

    async def test_stop_without_session_keeps_voice_connection(self) -> None:
        self.sessions.is_active.return_value = False
        player = Mock()
        player.stop_music = AsyncMock(return_value=True)
        self.players.get.return_value = player
        self.players.remove = AsyncMock()
        self.sessions.stop = AsyncMock()

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(),
        ) as disconnect:
            await self.cog.stop.callback(self.cog, self.ctx)

        player.stop_music.assert_awaited_once_with()
        self.players.remove.assert_not_awaited()
        self.sessions.stop.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.ctx.voice_client.disconnect.assert_not_awaited()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("Đã dừng nhạc", sent)
        self.assertIn("Dùng `!tfd leave`", sent)
        self.assertNotIn("Đã dừng và rời", sent)

    async def test_stop_without_player_keeps_voice_and_session(self) -> None:
        self.sessions.is_active.return_value = True
        self.sessions.stop = AsyncMock()
        self.players.get.return_value = None
        self.players.remove = AsyncMock()

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(),
        ) as disconnect:
            await self.cog.stop.callback(self.cog, self.ctx)

        self.players.remove.assert_not_awaited()
        self.sessions.stop.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.ctx.voice_client.disconnect.assert_not_awaited()
        self.assertIn("Không có gì đang phát", self.ctx.send.await_args.args[0])

    async def test_leave_ends_session_and_disconnects(self) -> None:
        self.players.remove = AsyncMock(return_value=True)
        self.sessions.stop = AsyncMock(return_value=True)
        self.cog.music_ui.refresh = AsyncMock()

        await self.cog.leave.callback(self.cog, self.ctx)

        self.players.remove.assert_awaited_once_with(1, disconnect=False)
        self.sessions.stop.assert_awaited_once_with(1)
        self.ctx.voice_client.disconnect.assert_awaited()
        self.cog.music_ui.refresh.assert_awaited_once_with(1)
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("theo dõi", sent.lower())

    async def test_join_starts_session_and_refreshes_panel(self) -> None:
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 7
        channel.name = "Phòng nhạc"
        self.ctx.voice_client.channel = channel
        self.ctx.author.voice.channel = channel
        self.cog._connect_for_context = AsyncMock(
            return_value=self.ctx.voice_client
        )
        self.sessions.is_active.return_value = False
        session = Mock()
        session.voice_channel_name = "Phòng nhạc"
        self.sessions.start.return_value = session
        self.cog.music_ui.refresh = AsyncMock()

        await self.cog.join.callback(self.cog, self.ctx)

        self.sessions.start.assert_called_once_with(self.ctx.guild, channel)
        self.cog.music_ui.refresh.assert_awaited_once_with(1)
        self.assertIn("theo dõi chat", self.ctx.send.await_args.args[0])

    def _make_panel_interaction(self) -> MagicMock:
        interaction = MagicMock()
        interaction.guild = self.ctx.guild
        interaction.user = self.ctx.author
        interaction.channel = self.ctx.channel
        interaction.extras = {}
        return interaction

    async def test_panel_stop_keeps_voice_and_chat_session_state(self) -> None:
        interaction = self._make_panel_interaction()

        for session_active in (False, True):
            with self.subTest(session_active=session_active):
                player = Mock()
                player.stop_music = AsyncMock(return_value=True)
                self.players.get.return_value = player
                self.players.remove = AsyncMock()
                self.sessions.is_active.return_value = session_active
                self.sessions.stop = AsyncMock()

                with patch(
                    "src.cogs.music.disconnect_guild_voice_client",
                    new=AsyncMock(),
                ) as disconnect:
                    result = await self.cog.ui_stop(interaction, 1, 7)

                player.stop_music.assert_awaited_once_with()
                self.players.remove.assert_not_awaited()
                self.sessions.stop.assert_not_awaited()
                disconnect.assert_not_awaited()
                self.ctx.voice_client.disconnect.assert_not_awaited()
                self.assertIn("Đã dừng nhạc", result)
                self.assertNotIn("rời", result.lower())

    async def test_panel_stop_without_player_does_not_disconnect(self) -> None:
        interaction = self._make_panel_interaction()
        self.players.get.return_value = None
        self.players.remove = AsyncMock()
        self.sessions.is_active.return_value = False
        self.sessions.stop = AsyncMock()

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(),
        ) as disconnect:
            result = await self.cog.ui_stop(interaction, 1, 7)

        self.players.remove.assert_not_awaited()
        self.sessions.stop.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.ctx.voice_client.disconnect.assert_not_awaited()
        self.assertIn("Không có gì đang phát", result)

    async def test_title_reading_toggle_updates_guild_preference(self) -> None:
        interaction = self._make_panel_interaction()
        self.players.toggle_title_announcements = Mock(return_value=False)

        result = await self.cog.ui_toggle_title_reading(interaction, 1, 7)

        self.players.toggle_title_announcements.assert_called_once_with(1)
        self.assertIn("tắt đọc tên bài", result.lower())
        self.assertIn("vẫn được gửi", result.lower())

    async def test_global_tts_gate_blocks_reading_toggles_and_state(self) -> None:
        interaction = self._make_panel_interaction()
        self.settings.tts_enabled = False
        self.players.title_announcements_enabled = Mock(return_value=True)
        self.players.toggle_title_announcements = Mock()
        self.sessions.is_active.return_value = True
        self.sessions.get = Mock()

        self.assertFalse(self.cog.ui_tts_available())
        self.assertFalse(self.cog.ui_title_reading_enabled(1))
        self.assertFalse(self.cog.ui_chat_reading_enabled(1))
        title_result = await self.cog.ui_toggle_title_reading(
            interaction,
            1,
            7,
        )
        chat_result = await self.cog.ui_toggle_chat_reading(
            interaction,
            1,
            7,
        )

        self.players.toggle_title_announcements.assert_not_called()
        self.sessions.get.assert_not_called()
        self.assertIn("cấu hình bot", title_result.lower())
        self.assertIn("cấu hình bot", chat_result.lower())

    async def test_title_toggle_rechecks_outsider_after_waiting_for_lock(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        self.players.toggle_title_announcements = Mock()
        lock = self.cog._operation_lock(1)
        await lock.acquire()
        task = asyncio.create_task(
            self.cog.ui_toggle_title_reading(interaction, 1, 7)
        )
        try:
            await asyncio.sleep(0)
            outside = Mock()
            outside.id = 99
            interaction.user.voice.channel = outside
        finally:
            lock.release()

        result = await asyncio.wait_for(task, timeout=1.0)
        self.players.toggle_title_announcements.assert_not_called()
        self.assertIn("đúng kênh thoại", result.lower())

    async def test_chat_toggle_rechecks_stale_panel_after_waiting_for_lock(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        original_view = object()
        interaction.extras = {PANEL_INTERACTION_TOKEN: original_view}
        current_record = Mock()
        current_record.view = original_view
        self.cog.music_ui.get = Mock(return_value=current_record)
        self.sessions.get = Mock()
        lock = self.cog._operation_lock(1)
        await lock.acquire()
        task = asyncio.create_task(
            self.cog.ui_toggle_chat_reading(interaction, 1, 7)
        )
        try:
            await asyncio.sleep(0)
            replacement = Mock()
            replacement.view = object()
            self.cog.music_ui.get.return_value = replacement
        finally:
            lock.release()

        result = await asyncio.wait_for(task, timeout=1.0)
        self.sessions.get.assert_not_called()
        self.assertIn("thay thế", result.lower())

    async def test_chat_toggle_on_reconnects_bound_room_and_starts_session(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 7
        channel.name = "Phòng nhạc"
        interaction.user.voice.channel = channel
        interaction.guild.voice_client = None
        voice_client = MagicMock()
        voice_client.channel = channel
        self.sessions.get.return_value = None
        self.sessions.start = Mock()
        self.players.get.return_value = None

        with patch(
            "src.cogs.music.connect_member_voice_client",
            new=AsyncMock(return_value=voice_client),
        ) as connect:
            result = await self.cog.ui_toggle_chat_reading(
                interaction,
                1,
                7,
            )

        connect.assert_awaited_once_with(
            interaction.guild,
            interaction.user,
            self.settings,
            expected_channel_id=7,
        )
        self.sessions.start.assert_called_once_with(interaction.guild, channel)
        self.assertIn("bật đọc tin nhắn", result.lower())

    async def test_chat_toggle_off_preserves_current_music_and_connection(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        session = Mock()
        session.active = True
        session.voice_channel_id = 7
        self.sessions.get.return_value = session
        self.sessions.stop = AsyncMock(return_value=True)
        player = Mock()
        snapshot = Mock()
        snapshot.current = QueuedTrack(
            "Đang phát",
            "https://www.youtube.com/watch?v=playing",
        )
        snapshot.queued = ()
        player.snapshot.return_value = snapshot
        self.players.get.return_value = player
        self.players.remove = AsyncMock()

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(),
        ) as disconnect:
            result = await self.cog.ui_toggle_chat_reading(
                interaction,
                1,
                7,
            )

        self.sessions.stop.assert_awaited_once_with(1)
        self.players.remove.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.ctx.voice_client.disconnect.assert_not_awaited()
        self.assertIn("nhạc vẫn tiếp tục", result.lower())

    async def test_chat_toggle_off_disconnects_when_no_music_exists(self) -> None:
        interaction = self._make_panel_interaction()
        session = Mock()
        session.active = True
        session.voice_channel_id = 7
        self.sessions.get.return_value = session
        self.sessions.stop = AsyncMock(return_value=True)
        self.players.get.return_value = None
        self.players.remove = AsyncMock(return_value=False)

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(return_value=True),
        ) as disconnect:
            await self.cog.ui_toggle_chat_reading(interaction, 1, 7)

        self.sessions.stop.assert_awaited_once_with(1)
        self.players.remove.assert_awaited_once_with(1, disconnect=True)
        disconnect.assert_awaited_once_with(
            interaction.guild,
            expected_client=self.ctx.voice_client,
        )

    async def test_nameannounce_requires_active_session(self) -> None:
        self.sessions.get.return_value = None
        await self.cog.name_announce.callback(self.cog, self.ctx, "on")
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("join", sent)

    async def test_nameannounce_toggles_session_flag(self) -> None:
        session = Mock()
        session.active = True
        session.voice_channel_id = 7
        session.set_name_announce = Mock(return_value=False)
        self.sessions.get.return_value = session

        await self.cog.name_announce.callback(self.cog, self.ctx, "off")
        session.set_name_announce.assert_called_once_with(False)
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("tắt", sent.lower())

    async def test_jump_converts_timestamp_and_restarts_current_track(self) -> None:
        player = Mock()
        player.jump.return_value = JumpResult.SUCCESS
        self.players.get = Mock(return_value=player)

        await self.cog.jump.callback(self.cog, self.ctx, "01:02:03")

        self.players.get.assert_called_once_with(1)
        player.jump.assert_called_once_with(3723)
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("1:02:03", sent)

    async def test_jump_rejects_invalid_timestamp(self) -> None:
        self.players.get = Mock()

        await self.cog.jump.callback(self.cog, self.ctx, "01:60:00")

        self.players.get.assert_not_called()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("HH:MM:SS", sent)

    async def test_jump_reports_timestamp_outside_current_track(self) -> None:
        player = Mock()
        self.players.get = Mock(return_value=player)

        for result in (JumpResult.OUT_OF_RANGE, JumpResult.UNKNOWN_DURATION):
            with self.subTest(result=result):
                self.ctx.send.reset_mock()
                player.jump.return_value = result

                await self.cog.jump.callback(self.cog, self.ctx, "01:02:03")

                sent = self.ctx.send.await_args.args[0]
                self.assertIn("không tồn tại", sent.lower())

    async def test_jump_requires_current_playback(self) -> None:
        self.players.get = Mock(return_value=None)

        await self.cog.jump.callback(self.cog, self.ctx, "00:00:00")

        sent = self.ctx.send.await_args.args[0]
        self.assertIn("Không có gì", sent)

    async def test_outside_room_cannot_control_playback(self) -> None:
        other_channel = Mock()
        other_channel.id = 99
        self.ctx.author.voice.channel = other_channel
        self.players.get = Mock()

        await self.cog.skip.callback(self.cog, self.ctx)

        self.players.get.assert_not_called()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("kênh thoại của bot", sent)

    async def test_music_posts_room_bound_panel_and_reserves_player(self) -> None:
        player = Mock()
        self.players.get_or_create = AsyncMock(return_value=player)
        self.cog._connect_for_context = AsyncMock(
            return_value=self.ctx.voice_client
        )
        self.cog.music_ui.post_panel = AsyncMock()

        await self.cog.music.callback(self.cog, self.ctx)

        self.players.get_or_create.assert_awaited_once_with(self.ctx.guild)
        player.reserve_activity.assert_called_once_with()
        player.release_activity.assert_called_once_with()
        self.cog.music_ui.post_panel.assert_awaited_once_with(
            self.ctx.channel,
            1,
            7,
        )

    async def test_music_outsider_cannot_touch_player_or_replace_panel(self) -> None:
        other_channel = Mock()
        other_channel.id = 99
        self.ctx.author.voice.channel = other_channel
        self.players.get_or_create = AsyncMock()
        self.cog._connect_for_context = AsyncMock()
        self.cog.music_ui.post_panel = AsyncMock()

        await self.cog.music.callback(self.cog, self.ctx)

        self.players.get_or_create.assert_not_awaited()
        self.cog._connect_for_context.assert_not_awaited()
        self.cog.music_ui.post_panel.assert_not_awaited()
        self.assertIn(
            "kênh thoại của bot",
            self.ctx.send.await_args.args[0],
        )

    async def test_outside_room_is_denied_before_media_extraction(self) -> None:
        other_channel = Mock()
        other_channel.id = 99
        self.ctx.author.voice.channel = other_channel
        self.media.prepare = AsyncMock()
        self.players.get_or_create = AsyncMock()

        await self.cog.play.callback(self.cog, self.ctx, query="slow playlist")

        self.media.prepare.assert_not_awaited()
        self.players.get_or_create.assert_not_awaited()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("kênh thoại của bot", sent)

    async def test_add_waits_until_concurrent_stop_finishes(self) -> None:
        item = QueuedTrack(
            "Bài mới",
            "https://www.youtube.com/watch?v=new",
            60,
        )
        self.media.prepare = AsyncMock(return_value=MediaBatch(items=(item,)))
        player = Mock()
        player.enqueue_many = AsyncMock(return_value=1)
        self.players.get_or_create = AsyncMock(return_value=player)
        self.cog._connect_for_context = AsyncMock(
            return_value=self.ctx.voice_client
        )
        self.sessions.is_active.return_value = False
        stop_started = asyncio.Event()
        allow_stop = asyncio.Event()

        async def blocked_stop() -> bool:
            stop_started.set()
            await allow_stop.wait()
            return True

        player.stop_music = AsyncMock(side_effect=blocked_stop)
        self.players.get.return_value = player
        self.players.remove = AsyncMock()
        stopping = asyncio.create_task(self.cog.stop.callback(self.cog, self.ctx))
        adding = None
        try:
            await asyncio.wait_for(stop_started.wait(), timeout=1.0)
            adding = asyncio.create_task(
                self.cog.play.callback(self.cog, self.ctx, query="Bài mới")
            )
            await asyncio.sleep(0)
            self.media.prepare.assert_awaited_once_with("Bài mới")
            self.players.get_or_create.assert_not_awaited()
            allow_stop.set()
            await asyncio.wait_for(stopping, timeout=1.0)
            await asyncio.wait_for(adding, timeout=1.0)
        finally:
            allow_stop.set()
            if not stopping.done():
                await stopping
            if adding is not None and not adding.done():
                await adding

        player.stop_music.assert_awaited_once_with()
        self.players.remove.assert_not_awaited()
        self.players.get_or_create.assert_awaited_once_with(self.ctx.guild)
        player.enqueue_many.assert_awaited_once_with((item,), self.ctx.channel)

    async def test_direct_modal_rejects_panel_replaced_during_extraction(self) -> None:
        old_view = object()
        old_record = Mock()
        old_record.view = old_view
        new_record = Mock()
        new_record.view = object()
        self.cog.music_ui.get = Mock(return_value=old_record)
        extraction_started = asyncio.Event()
        finish_extraction = asyncio.Event()
        item = QueuedTrack(
            "Bài chậm",
            "https://www.youtube.com/watch?v=slow",
            60,
        )

        async def prepare(_query: str) -> MediaBatch:
            extraction_started.set()
            await finish_extraction.wait()
            return MediaBatch(items=(item,))

        self.media.prepare = AsyncMock(side_effect=prepare)
        self.players.get_or_create = AsyncMock()
        interaction = MagicMock()
        interaction.guild = self.ctx.guild
        interaction.user = self.ctx.author
        interaction.channel = self.ctx.channel
        interaction.extras = {PANEL_INTERACTION_TOKEN: old_view}

        adding = asyncio.create_task(
            self.cog.ui_add_input(
                interaction,
                1,
                7,
                "https://www.youtube.com/watch?v=slow",
            )
        )
        try:
            await asyncio.wait_for(extraction_started.wait(), timeout=1.0)
            self.cog.music_ui.get.return_value = new_record
            finish_extraction.set()
            result = await asyncio.wait_for(adding, timeout=1.0)
        finally:
            finish_extraction.set()
            if not adding.done():
                await adding

        self.assertIn("thay thế", result.message)
        self.players.get_or_create.assert_not_awaited()

    async def test_enqueue_uses_batch_playlist_path(self) -> None:
        item = QueuedTrack(
            "Bài thử",
            "https://www.youtube.com/watch?v=test",
            60,
        )
        self.media.prepare = AsyncMock(return_value=MediaBatch(items=(item,)))
        player = Mock()
        player.enqueue_many = AsyncMock(return_value=1)
        self.players.get_or_create = AsyncMock(return_value=player)
        self.cog._connect_for_context = AsyncMock(
            return_value=self.ctx.voice_client
        )

        await self.cog.play.callback(self.cog, self.ctx, query="Bài thử")

        self.media.prepare.assert_awaited_once_with("Bài thử")
        player.reserve_activity.assert_called_once_with()
        player.release_activity.assert_called_once_with()
        player.enqueue_many.assert_awaited_once_with((item,), self.ctx.channel)
        self.assertIn("Bài thử", self.ctx.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
