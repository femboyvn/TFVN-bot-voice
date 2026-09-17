"""Command behavior: stop vs leave with an active TTS session."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import discord
from discord.ext import commands

from src.media import MediaBatch, MediaExtractionError, QueuedTrack, SearchResult
from src.cogs.music import MusicCog
from src.music_ui import PANEL_INTERACTION_TOKEN
from src.player import GuildAudioSettings, JumpResult
from src.playlists import PlaybackSession
from src.soundboard import SoundboardEntry


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
        self.soundboard = Mock()
        self.soundboard.list = AsyncMock(return_value=())
        self.soundboard.add_sound = AsyncMock()
        self.soundboard.remove_sound = AsyncMock()
        self.soundboard.store.get = AsyncMock(return_value=None)
        self.soundboard.store.mp3_path = Mock(return_value=None)
        self.settings.soundboard_max_seconds = 12
        self.playlists = Mock()
        self.playlists.save_playback = AsyncMock()
        self.playlists.clear_playback = AsyncMock()
        self.playlists.list_playback = AsyncMock(return_value=())
        self.cog = MusicCog(
            self.bot,
            self.settings,
            self.media,
            self.players,
            self.sessions,
            self.soundboard,
            self.playlists,
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
        self.playlists.clear_playback.assert_awaited_once_with(1)
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

    async def test_join_after_disconnect_ignores_stale_session_room(self) -> None:
        stale_session = Mock()
        stale_session.active = True
        stale_session.voice_channel_id = 7
        stale_session.voice_channel_name = "Cũ"
        self.sessions.get.return_value = stale_session
        self.sessions.is_active.return_value = True
        self.sessions.start.return_value = stale_session
        self.ctx.guild.voice_client = None
        new_channel = MagicMock(spec=discord.VoiceChannel)
        new_channel.id = 99
        new_channel.name = "Mới"
        connected = Mock()
        connected.channel = new_channel
        self.ctx.author.voice.channel = new_channel
        self.cog.music_ui.invalidate_if_channel_changed = AsyncMock()
        self.cog.music_ui.refresh = AsyncMock()

        with patch(
            "src.cogs.music.get_or_connect_voice_client",
            new=AsyncMock(return_value=connected),
        ) as connect:
            await self.cog.join.callback(self.cog, self.ctx)

        connect.assert_awaited_once_with(self.ctx, self.settings)
        self.sessions.start.assert_called_once_with(self.ctx.guild, new_channel)

    async def test_music_after_disconnect_does_not_require_deleted_session_room(
        self,
    ) -> None:
        stale_session = Mock()
        stale_session.active = True
        stale_session.voice_channel_id = 7
        self.sessions.get.return_value = stale_session
        self.sessions.stop = AsyncMock()
        self.ctx.guild.voice_client = None
        new_channel = Mock()
        new_channel.id = 99
        self.ctx.author.voice.channel = new_channel
        connected = Mock()
        connected.channel = new_channel
        player = Mock()
        self.players.get_or_create = AsyncMock(return_value=player)
        self.cog.music_ui.post_panel = AsyncMock()
        self.cog.music_ui.invalidate_if_channel_changed = AsyncMock()

        with patch(
            "src.cogs.music.get_or_connect_voice_client",
            new=AsyncMock(return_value=connected),
        ):
            await self.cog.music.callback(self.cog, self.ctx)

        self.sessions.stop.assert_awaited_once_with(1)
        self.cog.music_ui.post_panel.assert_awaited_once_with(
            self.ctx.channel,
            1,
            99,
        )

    async def test_music_treats_deleted_voice_channel_client_as_disconnected(
        self,
    ) -> None:
        stale_channel = Mock()
        stale_channel.id = 7
        self.ctx.voice_client.channel = stale_channel
        self.ctx.voice_client.is_connected.return_value = True
        self.ctx.guild.get_channel = Mock(return_value=None)
        new_channel = Mock()
        new_channel.id = 99
        self.ctx.author.voice.channel = new_channel
        connected = Mock()
        connected.channel = new_channel
        player = Mock()
        self.players.get_or_create = AsyncMock(return_value=player)
        self.cog.music_ui.post_panel = AsyncMock()
        self.cog.music_ui.invalidate_if_channel_changed = AsyncMock()
        self.sessions.stop = AsyncMock()

        with patch(
            "src.cogs.music.get_or_connect_voice_client",
            new=AsyncMock(return_value=connected),
        ):
            await self.cog.music.callback(self.cog, self.ctx)

        self.cog.music_ui.post_panel.assert_awaited_once_with(
            self.ctx.channel,
            1,
            99,
        )

    async def test_leave_cleans_up_when_bound_voice_channel_was_deleted(self) -> None:
        session = Mock()
        session.active = True
        session.voice_channel_id = 7
        self.sessions.get.return_value = session
        self.sessions.stop = AsyncMock(return_value=True)
        self.players.remove = AsyncMock(return_value=True)
        self.cog.music_ui.refresh = AsyncMock()
        self.ctx.guild.get_channel = Mock(return_value=None)
        other = Mock()
        other.id = 99
        self.ctx.author.voice.channel = other
        self.ctx.voice_client.is_connected.return_value = False

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(return_value=False),
        ):
            await self.cog.leave.callback(self.cog, self.ctx)

        self.sessions.stop.assert_awaited_once_with(1)
        self.players.remove.assert_awaited_once_with(1, disconnect=False)

    async def test_bot_voice_disconnect_stops_session_and_refreshes_panel(
        self,
    ) -> None:
        self.bot.user = Mock(id=55)
        member = Mock()
        member.id = 55
        member.guild = self.ctx.guild
        before = Mock()
        before.channel = Mock(id=7)
        after = Mock()
        after.channel = None
        self.sessions.get.return_value = Mock(active=True, voice_channel_id=7)
        self.sessions.stop = AsyncMock(return_value=True)
        self.players.remove = AsyncMock(return_value=True)
        self.ctx.voice_client.is_connected.return_value = False
        self.cog.music_ui.refresh = AsyncMock()
        self.cog.music_ui.drop_if_channel = AsyncMock()
        self.ctx.guild.get_channel = Mock(return_value=Mock())

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(return_value=False),
        ):
            await self.cog.on_voice_state_update(member, before, after)

        self.players.remove.assert_awaited_once_with(1, disconnect=False)
        self.sessions.stop.assert_awaited_once_with(1)
        self.cog.music_ui.refresh.assert_awaited_once_with(1)
        self.cog.music_ui.drop_if_channel.assert_not_awaited()

    async def test_deleted_bound_voice_channel_drops_panel_and_session(self) -> None:
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 7
        channel.guild = self.ctx.guild
        self.sessions.get.return_value = Mock(active=True, voice_channel_id=7)
        self.sessions.stop = AsyncMock(return_value=True)
        self.players.remove = AsyncMock(return_value=True)
        self.cog.music_ui.drop_if_channel = AsyncMock(return_value=True)
        self.cog.music_ui.refresh = AsyncMock()
        self.ctx.guild.get_channel = Mock(return_value=None)
        self.ctx.voice_client.is_connected.return_value = False

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(return_value=False),
        ):
            await self.cog.on_guild_channel_delete(channel)

        self.sessions.stop.assert_awaited_once_with(1)
        self.cog.music_ui.drop_if_channel.assert_awaited_once_with(1, 7)
        self.cog.music_ui.refresh.assert_not_awaited()

    async def test_unrelated_voice_channel_delete_does_not_stop_session(self) -> None:
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 99
        channel.guild = self.ctx.guild
        self.sessions.get.return_value = Mock(active=True, voice_channel_id=7)
        self.sessions.stop = AsyncMock()
        self.players.remove = AsyncMock()
        self.cog.music_ui.drop_if_channel = AsyncMock()
        self.ctx.voice_client.is_connected.return_value = False

        await self.cog.on_guild_channel_delete(channel)

        self.sessions.stop.assert_not_awaited()
        self.players.remove.assert_not_awaited()
        self.cog.music_ui.drop_if_channel.assert_not_awaited()

    def _make_panel_interaction(self) -> MagicMock:
        interaction = MagicMock()
        interaction.guild = self.ctx.guild
        interaction.user = self.ctx.author
        interaction.channel = self.ctx.channel
        interaction.extras = {}
        return interaction

    def test_panel_voice_connection_state_uses_cached_guild(self) -> None:
        self.bot.get_guild = Mock(return_value=self.ctx.guild)

        self.assertTrue(self.cog.ui_voice_connected(1))
        self.ctx.voice_client.is_connected.return_value = False
        self.assertFalse(self.cog.ui_voice_connected(1))
        self.bot.get_guild.return_value = None
        self.assertFalse(self.cog.ui_voice_connected(1))

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

    async def test_panel_leave_ends_session_music_and_voice(self) -> None:
        interaction = self._make_panel_interaction()
        self.players.remove = AsyncMock(return_value=True)
        self.sessions.stop = AsyncMock(return_value=True)

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(return_value=True),
        ) as disconnect:
            result = await self.cog.ui_leave(interaction, 1, 7)

        self.players.remove.assert_awaited_once_with(1, disconnect=False)
        self.sessions.stop.assert_awaited_once_with(1)
        disconnect.assert_awaited_once_with(
            interaction.guild,
            expected_client=self.ctx.voice_client,
        )
        self.assertIn("rời kênh thoại", result.lower())

    async def test_panel_leave_rejects_outside_room_without_mutation(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        outside_channel = Mock()
        outside_channel.id = 99
        interaction.user.voice.channel = outside_channel
        self.players.remove = AsyncMock()
        self.sessions.stop = AsyncMock()

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(),
        ) as disconnect:
            result = await self.cog.ui_leave(interaction, 1, 7)

        self.players.remove.assert_not_awaited()
        self.sessions.stop.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.assertIn("đúng kênh thoại", result.lower())

    async def test_panel_leave_rejects_stale_panel_without_mutation(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        original_view = object()
        interaction.extras = {PANEL_INTERACTION_TOKEN: original_view}
        replacement = Mock()
        replacement.view = object()
        self.cog.music_ui.get = Mock(return_value=replacement)
        self.players.remove = AsyncMock()
        self.sessions.stop = AsyncMock()

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(),
        ) as disconnect:
            result = await self.cog.ui_leave(interaction, 1, 7)

        self.players.remove.assert_not_awaited()
        self.sessions.stop.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.assertIn("thay thế", result.lower())

    async def test_panel_leave_reports_when_nothing_is_connected(self) -> None:
        interaction = self._make_panel_interaction()
        interaction.guild.voice_client = None
        self.players.remove = AsyncMock(return_value=False)
        self.sessions.stop = AsyncMock(return_value=False)

        with patch(
            "src.cogs.music.disconnect_guild_voice_client",
            new=AsyncMock(),
        ) as disconnect:
            result = await self.cog.ui_leave(interaction, 1, 7)

        self.players.remove.assert_not_awaited()
        self.sessions.stop.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.assertIn("chưa kết nối", result.lower())

    async def test_panel_audio_settings_update_shared_guild_state(self) -> None:
        interaction = self._make_panel_interaction()
        requested = GuildAudioSettings(0.55, 0.15, "en", False)
        applied = GuildAudioSettings(0.55, 0.15, "en", False)
        session = Mock()
        session.active = True
        session.set_name_announce = Mock(return_value=False)
        self.sessions.get.return_value = session
        self.players.set_audio_settings = Mock(return_value=applied)
        self.sessions.refresh_tts_language = Mock(return_value=True)

        result = await self.cog.ui_update_audio_settings(
            interaction,
            1,
            7,
            requested,
        )

        self.players.set_audio_settings.assert_called_once_with(1, requested)
        self.sessions.refresh_tts_language.assert_called_once_with(1)
        session.set_name_announce.assert_called_once_with(False)
        self.assertIn("55%", result)
        self.assertIn("15%", result)
        self.assertIn("en", result)
        self.assertIn("Tắt", result)

    async def test_panel_audio_settings_store_bump_interval_and_report_it(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        requested = GuildAudioSettings(0.55, 0.15, "en")
        self.players.set_audio_settings = Mock(return_value=requested)
        self.sessions.refresh_tts_language = Mock(return_value=True)
        self.cog.music_ui.set_bump_interval_minutes = Mock()

        result = await self.cog.ui_update_audio_settings(
            interaction,
            1,
            7,
            requested,
            15,
        )

        self.players.set_audio_settings.assert_called_once_with(1, requested)
        self.sessions.refresh_tts_language.assert_called_once_with(1)
        self.cog.music_ui.set_bump_interval_minutes.assert_called_once_with(
            1,
            15,
        )
        self.assertIn("tự đưa bảng lên", result.lower())
        self.assertIn("mỗi 15 phút", result.lower())

    async def test_panel_audio_settings_reject_invalid_bump_interval_atomically(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        requested = GuildAudioSettings(0.55, 0.15, "en")
        self.players.set_audio_settings = Mock()
        self.sessions.refresh_tts_language = Mock()
        self.cog.music_ui.set_bump_interval_minutes = Mock()

        result = await self.cog.ui_update_audio_settings(
            interaction,
            1,
            7,
            requested,
            1441,
        )

        self.players.set_audio_settings.assert_not_called()
        self.sessions.refresh_tts_language.assert_not_called()
        self.cog.music_ui.set_bump_interval_minutes.assert_not_called()
        self.assertIn("0 hoặc số phút từ 1 đến 1440", result)

    async def test_panel_audio_settings_persist_without_player_or_session(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        requested = GuildAudioSettings(0.8, 0.3, "ja")
        self.players.get.return_value = None
        self.sessions.get.return_value = None
        self.players.set_audio_settings = Mock(return_value=requested)
        self.sessions.refresh_tts_language = Mock(return_value=False)

        result = await self.cog.ui_update_audio_settings(
            interaction,
            1,
            7,
            requested,
        )

        self.players.set_audio_settings.assert_called_once_with(1, requested)
        self.sessions.refresh_tts_language.assert_called_once_with(1)
        self.assertIn("Đã cập nhật", result)

    async def test_panel_audio_settings_with_tts_disabled_changes_only_music(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        self.settings.tts_enabled = False
        current = GuildAudioSettings(0.7, 0.2, "vi", False)
        requested = GuildAudioSettings(1.25, 0.8, "en", True)
        music_only = GuildAudioSettings(1.25, 0.2, "vi", False)
        self.players.audio_settings = Mock(return_value=current)
        self.players.set_audio_settings = Mock(return_value=music_only)
        self.sessions.refresh_tts_language = Mock(return_value=False)
        self.sessions.get.return_value = Mock()

        result = await self.cog.ui_update_audio_settings(
            interaction,
            1,
            7,
            requested,
        )

        self.players.audio_settings.assert_called_once_with(1)
        self.players.set_audio_settings.assert_called_once_with(1, music_only)
        self.sessions.refresh_tts_language.assert_not_called()
        self.sessions.get.assert_not_called()
        self.assertEqual(requested, GuildAudioSettings(1.25, 0.8, "en", True))
        self.assertIn("125%", result)
        self.assertIn("20%", result)
        self.assertIn("vi", result)
        self.assertIn("chỉ âm lượng nhạc", result.lower())

    async def test_panel_audio_settings_reject_outside_room_without_mutation(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        outside_channel = Mock()
        outside_channel.id = 99
        interaction.user.voice.channel = outside_channel
        self.players.set_audio_settings = Mock()
        self.sessions.refresh_tts_language = Mock()
        self.cog.music_ui.set_bump_interval_minutes = Mock()

        result = await self.cog.ui_update_audio_settings(
            interaction,
            1,
            7,
            GuildAudioSettings(0.5, 0.2, "vi"),
            15,
        )

        self.players.set_audio_settings.assert_not_called()
        self.sessions.refresh_tts_language.assert_not_called()
        self.cog.music_ui.set_bump_interval_minutes.assert_not_called()
        self.assertIn("đúng kênh thoại", result.lower())

    async def test_panel_audio_settings_reject_stale_panel_without_mutation(
        self,
    ) -> None:
        interaction = self._make_panel_interaction()
        original_view = object()
        interaction.extras = {PANEL_INTERACTION_TOKEN: original_view}
        replacement = Mock()
        replacement.view = object()
        self.cog.music_ui.get = Mock(return_value=replacement)
        self.players.set_audio_settings = Mock()
        self.sessions.refresh_tts_language = Mock()
        self.cog.music_ui.set_bump_interval_minutes = Mock()

        result = await self.cog.ui_update_audio_settings(
            interaction,
            1,
            7,
            GuildAudioSettings(0.5, 0.2, "vi"),
            15,
        )

        self.players.set_audio_settings.assert_not_called()
        self.sessions.refresh_tts_language.assert_not_called()
        self.cog.music_ui.set_bump_interval_minutes.assert_not_called()
        self.assertIn("thay thế", result.lower())

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
        player.reserve_activity.assert_called_once_with()
        player.release_activity.assert_called_once_with()
        self.players.remove.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.ctx.voice_client.disconnect.assert_not_awaited()
        self.assertIn("bot vẫn ở kênh thoại", result.lower())

    async def test_chat_toggle_off_keeps_connection_when_no_music_exists(
        self,
    ) -> None:
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
        self.players.remove.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.ctx.voice_client.disconnect.assert_not_awaited()

    async def test_nameannounce_requires_active_session(self) -> None:
        self.sessions.get.return_value = None
        self.players.set_audio_settings = Mock()
        await self.cog.name_announce.callback(self.cog, self.ctx, "on")
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("join", sent)
        self.players.set_audio_settings.assert_not_called()

    async def test_nameannounce_toggles_session_flag(self) -> None:
        session = Mock()
        session.active = True
        session.voice_channel_id = 7
        session.set_name_announce = Mock(return_value=False)
        self.sessions.get.return_value = session
        current = GuildAudioSettings(0.7, 0.2, "vi", True)
        self.players.audio_settings = Mock(return_value=current)
        self.players.set_audio_settings = Mock(
            return_value=GuildAudioSettings(0.7, 0.2, "vi", False)
        )

        await self.cog.name_announce.callback(self.cog, self.ctx, "off")
        session.set_name_announce.assert_called_once_with(False)
        self.players.set_audio_settings.assert_called_once_with(
            1,
            GuildAudioSettings(0.7, 0.2, "vi", False),
        )
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
        self.playlists.save_playback.assert_awaited()

    def test_music_soundboard_and_playlist_are_hybrid_commands(self) -> None:
        for command in (self.cog.music, self.cog.soundboard, self.cog.playlist):
            self.assertIsInstance(command, commands.HybridCommand)

    async def test_skip_records_vote_until_majority(self) -> None:
        player = Mock()
        player.vote_skip.return_value = ("voted", 1, 2)
        self.players.get.return_value = player
        self.ctx.author.id = 10
        room = Mock()
        room.members = [Mock(bot=False), Mock(bot=False), Mock(bot=False)]
        self.ctx.guild.get_channel = Mock(return_value=room)

        await self.cog.skip.callback(self.cog, self.ctx)

        player.vote_skip.assert_called_once_with(10, voter_count=3)
        self.assertIn("1/2", self.ctx.send.await_args.args[0])

    async def test_ui_skip_uses_vote_skip(self) -> None:
        player = Mock()
        player.vote_skip.return_value = ("skipped", 2, 2)
        self.players.get.return_value = player
        interaction = self._make_panel_interaction()
        interaction.user.id = 10
        room = Mock()
        room.members = [Mock(bot=False), Mock(bot=False)]
        self.ctx.guild.get_channel = Mock(return_value=room)

        result = await self.cog.ui_skip(interaction, 1, 7)

        player.vote_skip.assert_called_once_with(10, voter_count=2)
        self.assertEqual(result, "Đã bỏ qua.")

    async def test_restore_rejoins_when_humans_remain(self) -> None:
        session = PlaybackSession(
            guild_id=1, voice_channel_id=7, text_channel_id=8,
            loop_current=False, loop_queue=True,
            current=QueuedTrack("Now", "https://youtu.be/now", 10),
            queued=(QueuedTrack("Next", "https://youtu.be/next", 10),),
        )
        self.playlists.list_playback = AsyncMock(return_value=(session,))
        voice = Mock()
        voice.members = [Mock(bot=False)]
        text = Mock()
        text.send = AsyncMock()
        guild = Mock()
        guild.id = 1
        guild.get_channel.side_effect = lambda channel_id: {7: voice, 8: text}[channel_id]
        self.bot.get_guild.return_value = guild
        player = Mock()
        player.enqueue_many = AsyncMock()
        self.players.get_or_create = AsyncMock(return_value=player)
        self.cog.music_ui.post_panel = AsyncMock()

        with patch("src.cogs.music.connect_voice_channel", new=AsyncMock()) as connect:
            await self.cog._restore_playback_sessions()

        connect.assert_awaited_once()
        self.assertTrue(player.loop_queue)
        player.enqueue_many.assert_awaited_once()
        self.cog.music_ui.post_panel.assert_awaited_once_with(text, 1, 7)

    async def test_restore_skips_empty_room_and_non_list_members(self) -> None:
        session = PlaybackSession(1, 7, 8)
        voice = Mock()
        voice.members = []
        text = Mock()
        text.send = AsyncMock()
        guild = Mock()
        guild.get_channel.side_effect = lambda channel_id: voice if channel_id == 7 else text
        self.bot.get_guild.return_value = guild
        self.players.get_or_create = AsyncMock()

        await self.cog._restore_one_session(session)
        self.players.get_or_create.assert_not_awaited()

        voice.members = MagicMock()
        await self.cog._restore_one_session(session)
        self.players.get_or_create.assert_not_awaited()

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

    async def test_soundboard_posts_panel_and_picker(self) -> None:
        player = Mock()
        self.players.get_or_create = AsyncMock(return_value=player)
        self.cog._connect_for_context = AsyncMock(
            return_value=self.ctx.voice_client
        )
        self.cog.music_ui.post_panel = AsyncMock()
        sent = MagicMock()
        self.ctx.send = AsyncMock(return_value=sent)

        await self.cog.soundboard.callback(self.cog, self.ctx)

        self.cog.music_ui.post_panel.assert_awaited_once_with(
            self.ctx.channel,
            1,
            7,
        )
        self.soundboard.list.assert_awaited_once_with(1)
        self.ctx.send.assert_awaited()
        kwargs = self.ctx.send.await_args.kwargs
        self.assertEqual(kwargs["embed"].title, "Bảng âm thanh")
        self.assertIsNotNone(kwargs["view"])

    async def test_soundboard_outsider_does_not_post_panel_or_picker(self) -> None:
        other_channel = Mock()
        other_channel.id = 99
        self.ctx.author.voice.channel = other_channel
        self.cog.music_ui.post_panel = AsyncMock()

        await self.cog.soundboard.callback(self.cog, self.ctx)

        self.cog.music_ui.post_panel.assert_not_awaited()
        self.soundboard.list.assert_not_awaited()
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

    async def test_ui_add_input_plain_query_returns_ordered_numbered_results(
        self,
    ) -> None:
        expected = [
            SearchResult("Bài một", "https://www.youtube.com/watch?v=one", 60),
            SearchResult("Bài hai", "https://www.youtube.com/watch?v=two", 120),
            SearchResult("Bài ba", "https://www.youtube.com/watch?v=three", 180),
        ]
        self.media.search = AsyncMock(return_value=expected)
        self.media.prepare = AsyncMock()
        self.players.get_or_create = AsyncMock()
        self.cog._enqueue_interaction_batch = AsyncMock()
        interaction = self._make_panel_interaction()

        result = await self.cog.ui_add_input(
            interaction,
            1,
            7,
            "  bài thử  ",
        )

        self.media.search.assert_awaited_once_with("bài thử", limit=5)
        self.assertEqual(
            result.message,
            "Chọn nút số tương ứng để thêm vào hàng đợi:",
        )
        self.assertEqual(result.results, tuple(expected))
        self.media.prepare.assert_not_awaited()
        self.cog._enqueue_interaction_batch.assert_not_awaited()
        self.players.get_or_create.assert_not_awaited()

    async def test_ui_add_input_plain_query_handles_empty_results_without_enqueue(
        self,
    ) -> None:
        self.media.search = AsyncMock(return_value=[])
        self.media.prepare = AsyncMock()
        self.players.get_or_create = AsyncMock()
        self.cog._enqueue_interaction_batch = AsyncMock()
        interaction = self._make_panel_interaction()

        result = await self.cog.ui_add_input(interaction, 1, 7, "không có bài")

        self.media.search.assert_awaited_once_with("không có bài", limit=5)
        self.assertEqual(result.message, "Không tìm thấy kết quả.")
        self.assertEqual(result.results, ())
        self.media.prepare.assert_not_awaited()
        self.cog._enqueue_interaction_batch.assert_not_awaited()
        self.players.get_or_create.assert_not_awaited()

    async def test_ui_add_input_plain_query_handles_extraction_failure_without_enqueue(
        self,
    ) -> None:
        self.media.search = AsyncMock(
            side_effect=MediaExtractionError("Tìm kiếm YouTube thất bại")
        )
        self.media.prepare = AsyncMock()
        self.players.get_or_create = AsyncMock()
        self.cog._enqueue_interaction_batch = AsyncMock()
        interaction = self._make_panel_interaction()

        result = await self.cog.ui_add_input(interaction, 1, 7, "bài bị lỗi")

        self.media.search.assert_awaited_once_with("bài bị lỗi", limit=5)
        self.assertEqual(result.message, "Tìm kiếm YouTube thất bại")
        self.assertEqual(result.results, ())
        self.media.prepare.assert_not_awaited()
        self.cog._enqueue_interaction_batch.assert_not_awaited()
        self.players.get_or_create.assert_not_awaited()

    async def test_ui_add_input_spotify_uri_enqueues_instead_of_searching(
        self,
    ) -> None:
        item = QueuedTrack(
            "Artist - Song",
            "https://www.youtube.com/watch?v=matched",
            200,
        )
        batch = MediaBatch(items=(item,))
        self.media.search = AsyncMock()
        self.media.prepare = AsyncMock(return_value=batch)
        self.cog._enqueue_interaction_batch = AsyncMock(return_value=None)
        interaction = self._make_panel_interaction()

        result = await self.cog.ui_add_input(
            interaction,
            1,
            7,
            "  spotify:track:abc123  ",
        )

        self.media.prepare.assert_awaited_once_with("spotify:track:abc123")
        self.media.search.assert_not_awaited()
        self.cog._enqueue_interaction_batch.assert_awaited_once()
        self.assertIn("Artist - Song", result.message)
        self.assertEqual(result.results, ())

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

    async def test_ui_play_soundboard_plays_overlay_and_touches_player(self) -> None:
        entry = SoundboardEntry(
            id="abcd1234",
            name="bruh",
            mp3="abcd1234.mp3",
            source_url="https://www.myinstants.com/en/instant/bruh/",
            duration_ms=1100,
            added_by=10,
            added_at="2026-09-08T12:00:00+00:00",
        )
        clip = Path("/tmp/abcd1234.mp3")
        self.soundboard.store.get = AsyncMock(return_value=entry)
        self.soundboard.ensure_playable = AsyncMock(return_value=clip)
        player = Mock()
        player.play_overlay = AsyncMock(return_value=True)
        self.players.get_or_create = AsyncMock(return_value=player)
        interaction = self._make_panel_interaction()

        with patch(
            "src.cogs.music.connect_member_voice_client",
            new=AsyncMock(return_value=self.ctx.voice_client),
        ):
            result = await self.cog.ui_play_soundboard(
                interaction, 1, 7, "abcd1234"
            )

        player.play_overlay.assert_awaited_once_with(clip, timeout=17.0)
        player.touch.assert_called()
        self.assertIn("Đã phát", result)
        self.assertIn("bruh", result)
        player.enqueue_many = AsyncMock()
        player.enqueue_many.assert_not_awaited()

    async def test_ui_play_soundboard_rejects_wrong_room(self) -> None:
        other_channel = Mock()
        other_channel.id = 99
        self.ctx.author.voice.channel = other_channel
        self.soundboard.store.get = AsyncMock()
        interaction = self._make_panel_interaction()

        result = await self.cog.ui_play_soundboard(interaction, 1, 7, "abcd1234")

        self.assertIn("kênh thoại", result)
        self.soundboard.store.get.assert_not_awaited()

    async def test_ui_add_soundboard_saves_clip(self) -> None:
        entry = SoundboardEntry(
            id="abcd1234",
            name="bruh",
            mp3="abcd1234.mp3",
            source_url="https://www.myinstants.com/en/instant/bruh/",
            duration_ms=1100,
            added_by=10,
            added_at="2026-09-08T12:00:00+00:00",
        )
        self.soundboard.add_sound = AsyncMock(return_value=entry)
        player = Mock()
        self.players.get_or_create = AsyncMock(return_value=player)
        interaction = self._make_panel_interaction()

        with patch(
            "src.cogs.music.connect_member_voice_client",
            new=AsyncMock(return_value=self.ctx.voice_client),
        ):
            result = await self.cog.ui_add_soundboard(
                interaction,
                1,
                7,
                "bruh",
                "https://www.myinstants.com/en/instant/bruh/",
            )

        self.soundboard.add_sound.assert_awaited_once()
        self.assertIn("Đã lưu", result)
        self.assertIn("bruh", result)

    async def test_ui_remove_soundboard_rejects_stale_panel(self) -> None:
        old_view = object()
        record = Mock()
        record.view = object()
        self.cog.music_ui.get = Mock(return_value=record)
        interaction = self._make_panel_interaction()
        interaction.extras = {PANEL_INTERACTION_TOKEN: old_view}

        result = await self.cog.ui_remove_soundboard(interaction, 1, 7, "abcd1234")

        self.assertIn("thay thế", result)
        self.soundboard.remove_sound.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
