from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.media import QueuedTrack, SearchResult
from src.music_ui import (
    AddInputResult,
    AddMusicModal,
    AudioSettingsModal,
    AudioSettingsValidationError,
    ClearQueueConfirmation,
    MusicPanelManager,
    MusicPanelView,
    QueuePaginatorView,
    SearchResultSelect,
    SearchResultView,
    build_music_embed,
    format_panel_bump_interval,
    parse_audio_settings,
    parse_panel_bump_interval,
)
from src.player import GuildAudioSettings, PlaybackState, PlayerSnapshot


def _snapshot(
    *,
    state: PlaybackState = PlaybackState.IDLE,
    current: QueuedTrack | None = None,
    queued: tuple[QueuedTrack, ...] = (),
    loop: bool = False,
) -> PlayerSnapshot:
    return PlayerSnapshot(current=current, queued=queued, state=state, loop_current=loop)


class _Actions:
    def __init__(self, snapshot: PlayerSnapshot | None = None) -> None:
        self.current_snapshot = snapshot
        self.tts_available = True
        self.title_reading = True
        self.chat_reading = False
        self.voice_connected = True
        self.bound_voice_channel_id: int | None = 2
        self.current_audio_settings = GuildAudioSettings(0.7, 0.2, "vi")
        self.ui_ensure_panel_access = AsyncMock(return_value=True)
        self.ui_add_input = AsyncMock(return_value=AddInputResult(message="Đã thêm."))
        self.ui_enqueue_search_result = AsyncMock(return_value="Đã thêm.")
        self.ui_pause = AsyncMock(return_value="Đã tạm dừng.")
        self.ui_resume = AsyncMock(return_value="Đã tiếp tục.")
        self.ui_skip = AsyncMock(return_value="Đã bỏ qua.")
        self.ui_toggle_loop = AsyncMock(return_value="Đã bật lặp.")
        self.ui_jump = AsyncMock(return_value="Đã tua.")
        self.ui_clear_queue = AsyncMock(return_value="Đã xóa.")
        self.ui_stop = AsyncMock(return_value="Đã dừng.")
        self.ui_leave = AsyncMock(return_value="Đã rời kênh thoại.")
        self.ui_toggle_title_reading = AsyncMock(
            return_value="Đã tắt đọc tên bài."
        )
        self.ui_toggle_chat_reading = AsyncMock(
            return_value="Đã bật đọc tin nhắn."
        )
        self.ui_update_audio_settings = AsyncMock(
            return_value=(
                "Đã cập nhật cài đặt: Nhạc: 125.5% · "
                "Nhạc còn lại khi TTS: 20.25% · TTS: zh-TW."
            )
        )

    def ui_snapshot(self, guild_id: int) -> PlayerSnapshot | None:
        return self.current_snapshot

    def ui_tts_available(self) -> bool:
        return self.tts_available

    def ui_title_reading_enabled(self, guild_id: int) -> bool:
        return self.title_reading

    def ui_chat_reading_enabled(self, guild_id: int) -> bool:
        return self.chat_reading

    def ui_voice_connected(self, guild_id: int) -> bool:
        return self.voice_connected

    def ui_voice_channel_id(self, guild_id: int) -> int | None:
        return self.bound_voice_channel_id

    def ui_audio_settings(self, guild_id: int) -> GuildAudioSettings:
        return self.current_audio_settings


def _interaction(user_id: int = 10) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = user_id
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.original_response = AsyncMock(return_value=None)
    interaction.edit_original_response = AsyncMock()
    return interaction


class EmbedAndViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.track = QueuedTrack("Bài thử", "https://youtube.test/1", 65)

    def test_embed_shows_room_current_loop_count_and_first_five(self) -> None:
        queued = tuple(
            QueuedTrack(f"Bài {index}", f"https://youtube.test/{index}")
            for index in range(1, 8)
        )
        embed = build_music_embed(
            123,
            _snapshot(
                state=PlaybackState.PLAYING,
                current=self.track,
                queued=queued,
                loop=True,
            ),
        )
        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(values["Kênh thoại"], "<#123>")
        self.assertIn("Bài thử", values["Bài hiện tại"])
        self.assertEqual(values["Lặp bài"], "Bật")
        self.assertEqual(values["Đang chờ"], "7")
        self.assertIn("Bài 5", values["Tiếp theo"])
        self.assertNotIn("Bài 6", values["Tiếp theo"])

    def test_embed_shows_public_audio_settings(self) -> None:
        embed = build_music_embed(
            123,
            _snapshot(),
            GuildAudioSettings(1.255, 0.2025, "zh-TW"),
            15,
        )

        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(
            values["Cài đặt âm thanh"],
            "Nhạc: 125.5% · Nhạc còn lại khi TTS: 20.25% · TTS: zh-TW",
        )
        self.assertEqual(values["Tự đưa bảng lên"], "Mỗi 15 phút")

    def test_audio_settings_parser_accepts_locale_percentages_and_language(self) -> None:
        self.assertEqual(
            parse_audio_settings(" 125,5% ", "20.25%", " ZH_tw "),
            GuildAudioSettings(1.255, 0.2025, "zh-TW"),
        )

    def test_audio_settings_parser_rejects_each_invalid_field(self) -> None:
        cases = (
            (("NaN", "20", "vi"), "Âm lượng nhạc"),
            (("70", "101", "vi"), "khi TTS"),
            (("70", "20", "không-có"), "Mã ngôn ngữ TTS"),
        )
        for values, expected_message in cases:
            with self.subTest(values=values):
                with self.assertRaisesRegex(
                    AudioSettingsValidationError,
                    expected_message,
                ):
                    parse_audio_settings(*values)

    def test_panel_bump_interval_accepts_disabled_and_supported_bounds(self) -> None:
        self.assertEqual(parse_panel_bump_interval(" 0 "), 0)
        self.assertEqual(parse_panel_bump_interval("1"), 1)
        self.assertEqual(parse_panel_bump_interval("1440"), 1440)
        self.assertEqual(format_panel_bump_interval(0), "Tắt")
        self.assertEqual(format_panel_bump_interval(45), "Mỗi 45 phút")

    def test_panel_bump_interval_rejects_non_integer_and_out_of_range(self) -> None:
        for value in ("", "1.5", "-1", "1441", "không"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    AudioSettingsValidationError,
                    "0 hoặc số phút từ 1 đến 1440",
                ):
                    parse_panel_bump_interval(value)

    async def test_controls_are_state_sensitive(self) -> None:
        actions = _Actions(_snapshot())
        manager = MagicMock()
        idle = MusicPanelView(actions, manager, 1, 2, actions.current_snapshot)
        self.assertFalse(idle.add_music.disabled)
        self.assertTrue(idle.pause_resume.disabled)
        self.assertTrue(idle.show_queue.disabled)
        self.assertTrue(idle.stop_music.disabled)

        playing = _snapshot(
            state=PlaybackState.PAUSED,
            current=self.track,
            queued=(self.track,),
            loop=True,
        )
        idle.sync(playing)
        self.assertEqual(idle.pause_resume.label, "Tiếp tục")
        self.assertFalse(idle.pause_resume.disabled)
        self.assertFalse(idle.show_queue.disabled)
        self.assertEqual(idle.loop_track.style, discord.ButtonStyle.primary)

        idle.sync(
            _snapshot(
                state=PlaybackState.LOADING,
                current=self.track,
            )
        )
        self.assertFalse(idle.next_track.disabled)
        self.assertTrue(idle.pause_resume.disabled)

    async def test_panel_rows_and_enabled_settings_modal_fit_discord_limits(self) -> None:
        actions = _Actions(_snapshot())
        view = MusicPanelView(actions, MagicMock(), 1, 2, actions.current_snapshot)

        controls_per_row = {
            row: sum(child.row == row for child in view.children)
            for row in range(3)
        }
        self.assertEqual(controls_per_row, {0: 4, 1: 4, 2: 4})
        self.assertEqual(len(view.children), 12)

        modal = AudioSettingsModal(
            view,
            actions.current_audio_settings,
            tts_available=True,
        )
        self.assertEqual(
            modal.children,
            [
                modal.music_volume,
                modal.duck_level,
                modal.tts_language,
                modal.panel_bump_minutes,
            ],
        )

    async def test_tts_controls_reflect_state_and_availability(self) -> None:
        actions = _Actions(_snapshot())
        view = MusicPanelView(actions, MagicMock(), 1, 2, actions.current_snapshot)

        self.assertEqual(view.toggle_title_reading.label, "Đọc tên bài: Bật")
        self.assertEqual(view.toggle_title_reading.style, discord.ButtonStyle.success)
        self.assertFalse(view.toggle_title_reading.disabled)
        self.assertEqual(view.toggle_chat_reading.label, "Đọc tin nhắn: Tắt")
        self.assertEqual(view.toggle_chat_reading.style, discord.ButtonStyle.secondary)
        self.assertFalse(view.toggle_chat_reading.disabled)

        actions.title_reading = False
        actions.chat_reading = True
        view.sync(actions.current_snapshot)

        self.assertEqual(view.toggle_title_reading.label, "Đọc tên bài: Tắt")
        self.assertEqual(view.toggle_title_reading.style, discord.ButtonStyle.secondary)
        self.assertEqual(view.toggle_chat_reading.label, "Đọc tin nhắn: Bật")
        self.assertEqual(view.toggle_chat_reading.style, discord.ButtonStyle.success)

        actions.tts_available = False
        view.sync(actions.current_snapshot)

        self.assertTrue(view.toggle_title_reading.disabled)
        self.assertTrue(view.toggle_chat_reading.disabled)

    async def test_leave_control_reflects_voice_connection(self) -> None:
        actions = _Actions(_snapshot())
        view = MusicPanelView(actions, MagicMock(), 1, 2, actions.current_snapshot)

        self.assertEqual(view.leave_voice.label, "Rời")
        self.assertEqual(view.leave_voice.style, discord.ButtonStyle.danger)
        self.assertFalse(view.leave_voice.disabled)

        actions.voice_connected = False
        actions.bound_voice_channel_id = None
        view.sync(actions.current_snapshot)

        self.assertTrue(view.leave_voice.disabled)

    async def test_queue_paginator_uses_ten_items_per_page(self) -> None:
        queued = tuple(
            QueuedTrack(f"Bài {index}", f"https://youtube.test/{index}")
            for index in range(21)
        )
        view = QueuePaginatorView(10, queued)
        self.assertEqual(view.page_count, 3)
        self.assertTrue(view.previous_page.disabled)
        self.assertFalse(view.next_page.disabled)
        self.assertIn("Bài 9", view.render_embed().description)
        self.assertNotIn("Bài 10]", view.render_embed().description)


class InteractionViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.track = QueuedTrack("Bài thử", "https://youtube.test/1", 65)
        self.actions = _Actions(
            _snapshot(state=PlaybackState.PLAYING, current=self.track)
        )
        self.manager = MagicMock()
        self.manager.refresh = AsyncMock()
        self.manager.bump_interval_minutes.return_value = 15
        self.view = MusicPanelView(self.actions, self.manager, 1, 2, self.actions.current_snapshot)

    async def test_add_opens_modal_after_allow_disconnected_access_check(self) -> None:
        interaction = _interaction()
        await self.view.add_music.callback(interaction)
        self.actions.ui_ensure_panel_access.assert_awaited_once_with(
            interaction, 1, 2, connect_if_missing=True
        )
        interaction.response.send_modal.assert_awaited_once()
        self.assertIsInstance(interaction.response.send_modal.await_args.args[0], AddMusicModal)

    async def test_settings_button_opens_prefilled_modal_for_disconnected_access(self) -> None:
        interaction = _interaction()

        await self.view.audio_settings.callback(interaction)

        self.actions.ui_ensure_panel_access.assert_awaited_once_with(
            interaction,
            1,
            2,
            connect_if_missing=True,
        )
        interaction.response.send_modal.assert_awaited_once()
        modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, AudioSettingsModal)
        self.assertEqual(modal.timeout, 120.0)
        self.assertEqual(modal.music_volume.default, "70")
        self.assertEqual(modal.duck_level.default, "20")
        self.assertEqual(modal.tts_language.default, "vi")
        self.assertEqual(modal.panel_bump_minutes.default, "15")
        self.assertEqual(self.view.audio_settings.label, "Cài đặt")
        self.assertEqual(
            self.view.audio_settings.style,
            discord.ButtonStyle.secondary,
        )

    async def test_settings_button_denial_does_not_open_modal(self) -> None:
        self.actions.ui_ensure_panel_access.return_value = False
        interaction = _interaction()

        await self.view.audio_settings.callback(interaction)

        self.actions.ui_ensure_panel_access.assert_awaited_once_with(
            interaction,
            1,
            2,
            connect_if_missing=True,
        )
        interaction.response.send_modal.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()
        self.actions.ui_update_audio_settings.assert_not_awaited()

    async def test_settings_modal_rejects_replaced_registered_panel_before_defer(
        self,
    ) -> None:
        modal = AudioSettingsModal(
            self.view,
            self.actions.current_audio_settings,
        )
        self.view._registered = True
        replacement = MagicMock()
        replacement.view = MagicMock()
        self.manager.get.return_value = replacement
        interaction = _interaction()

        await modal.on_submit(interaction)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "thay thế",
            interaction.response.send_message.await_args.args[0],
        )
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs["ephemeral"]
        )
        interaction.response.defer.assert_not_awaited()
        self.actions.ui_ensure_panel_access.assert_not_awaited()
        self.actions.ui_update_audio_settings.assert_not_awaited()
        self.manager.refresh.assert_not_awaited()

    async def test_settings_modal_submits_normalized_values_ephemerally(self) -> None:
        modal = AudioSettingsModal(
            self.view,
            self.actions.current_audio_settings,
        )
        modal.music_volume._value = " 125,5% "
        modal.duck_level._value = "20.25%"
        modal.tts_language._value = " ZH_tw "
        modal.panel_bump_minutes._value = " 45 "
        interaction = _interaction()

        await modal.on_submit(interaction)

        self.actions.ui_ensure_panel_access.assert_awaited_once_with(
            interaction,
            1,
            2,
            connect_if_missing=True,
        )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.actions.ui_update_audio_settings.assert_awaited_once_with(
            interaction,
            1,
            2,
            GuildAudioSettings(1.255, 0.2025, "zh-TW"),
            45,
        )
        interaction.followup.send.assert_awaited_once_with(
            "Đã cập nhật cài đặt: Nhạc: 125.5% · "
            "Nhạc còn lại khi TTS: 20.25% · TTS: zh-TW.",
            ephemeral=True,
        )
        self.manager.refresh.assert_awaited_once_with(1)

    async def test_tts_unavailable_modal_updates_only_music_and_preserves_tts_settings(
        self,
    ) -> None:
        original = GuildAudioSettings(0.7, 0.2, "vi")
        self.actions.current_audio_settings = original
        self.actions.tts_available = False
        open_interaction = _interaction()

        await self.view.audio_settings.callback(open_interaction)

        open_interaction.response.send_modal.assert_awaited_once()
        modal = open_interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, AudioSettingsModal)
        self.assertEqual(
            modal.children,
            [modal.music_volume, modal.panel_bump_minutes],
        )
        self.actions.ui_ensure_panel_access.reset_mock()
        modal.music_volume._value = "85,5%"
        modal.panel_bump_minutes._value = "30"
        interaction = _interaction()

        await modal.on_submit(interaction)

        self.actions.ui_ensure_panel_access.assert_awaited_once_with(
            interaction,
            1,
            2,
            connect_if_missing=True,
        )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.actions.ui_update_audio_settings.assert_awaited_once_with(
            interaction,
            1,
            2,
            GuildAudioSettings(0.855, original.duck_level, original.tts_language),
            30,
        )
        interaction.followup.send.assert_awaited_once_with(
            self.actions.ui_update_audio_settings.return_value,
            ephemeral=True,
        )
        self.manager.refresh.assert_awaited_once_with(1)

    async def test_invalid_settings_modal_is_atomic_and_ephemeral(self) -> None:
        cases = (
            ("NaN", "20", "vi", "0", "Âm lượng nhạc"),
            ("70", "101%", "vi", "0", "khi TTS"),
            ("70", "20", "không-có", "0", "Mã ngôn ngữ TTS"),
            ("70", "20", "vi", "1.5", "0 hoặc số phút"),
        )
        for music, duck, language, bump_minutes, expected_message in cases:
            with self.subTest(
                music=music,
                duck=duck,
                language=language,
            ):
                modal = AudioSettingsModal(
                    self.view,
                    self.actions.current_audio_settings,
                )
                modal.music_volume._value = music
                modal.duck_level._value = duck
                modal.tts_language._value = language
                modal.panel_bump_minutes._value = bump_minutes
                interaction = _interaction()

                await modal.on_submit(interaction)

                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                self.actions.ui_update_audio_settings.assert_not_awaited()
                self.manager.refresh.assert_not_awaited()
                interaction.followup.send.assert_awaited_once()
                error_call = interaction.followup.send.await_args
                self.assertIn(expected_message, error_call.args[0])
                self.assertTrue(error_call.kwargs["ephemeral"])

                self.actions.ui_ensure_panel_access.reset_mock()
                self.manager.refresh.reset_mock()

    async def test_pause_defers_and_delegates_to_action(self) -> None:
        interaction = _interaction()
        await self.view.pause_resume.callback(interaction)
        self.actions.ui_pause.assert_awaited_once_with(interaction, 1, 2)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            "Đã tạm dừng.", ephemeral=True
        )
        self.manager.refresh.assert_awaited_once_with(1)

    async def test_tts_buttons_allow_reconnect_and_delegate_ephemerally(self) -> None:
        cases = (
            (
                self.view.toggle_title_reading,
                self.actions.ui_toggle_title_reading,
                "Đã tắt đọc tên bài.",
            ),
            (
                self.view.toggle_chat_reading,
                self.actions.ui_toggle_chat_reading,
                "Đã bật đọc tin nhắn.",
            ),
        )
        for button, action, message in cases:
            with self.subTest(button=button.label):
                interaction = _interaction()

                await button.callback(interaction)

                self.actions.ui_ensure_panel_access.assert_awaited_once_with(
                    interaction,
                    1,
                    2,
                    connect_if_missing=True,
                )
                action.assert_awaited_once_with(interaction, 1, 2)
                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                interaction.followup.send.assert_awaited_once_with(
                    message,
                    ephemeral=True,
                )
                self.manager.refresh.assert_awaited_once_with(1)

                self.actions.ui_ensure_panel_access.reset_mock()
                action.reset_mock()
                self.manager.refresh.reset_mock()

    async def test_tts_buttons_deny_before_action_or_defer(self) -> None:
        self.actions.ui_ensure_panel_access.return_value = False
        cases = (
            (self.view.toggle_title_reading, self.actions.ui_toggle_title_reading),
            (self.view.toggle_chat_reading, self.actions.ui_toggle_chat_reading),
        )
        for button, action in cases:
            with self.subTest(button=button.label):
                interaction = _interaction()

                await button.callback(interaction)

                self.actions.ui_ensure_panel_access.assert_awaited_once_with(
                    interaction,
                    1,
                    2,
                    connect_if_missing=True,
                )
                action.assert_not_awaited()
                interaction.response.defer.assert_not_awaited()
                interaction.followup.send.assert_not_awaited()
                self.manager.refresh.assert_not_awaited()

                self.actions.ui_ensure_panel_access.reset_mock()

    async def test_leave_uses_connected_access_and_delegates_ephemerally(self) -> None:
        interaction = _interaction()

        await self.view.leave_voice.callback(interaction)

        self.actions.ui_ensure_panel_access.assert_awaited_once_with(
            interaction,
            1,
            2,
            connect_if_missing=False,
        )
        self.actions.ui_leave.assert_awaited_once_with(interaction, 1, 2)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            "Đã rời kênh thoại.",
            ephemeral=True,
        )
        self.manager.refresh.assert_awaited_once_with(1)

    async def test_leave_denial_stops_before_action_or_defer(self) -> None:
        self.actions.ui_ensure_panel_access.return_value = False
        interaction = _interaction()

        await self.view.leave_voice.callback(interaction)

        self.actions.ui_ensure_panel_access.assert_awaited_once_with(
            interaction,
            1,
            2,
            connect_if_missing=False,
        )
        self.actions.ui_leave.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        self.manager.refresh.assert_not_awaited()

    async def test_outside_room_stops_before_control(self) -> None:
        self.actions.ui_ensure_panel_access.return_value = False
        interaction = _interaction()
        await self.view.next_track.callback(interaction)
        self.actions.ui_skip.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()

    async def test_next_loop_and_stop_delegate_to_matching_actions(self) -> None:
        for button, action in (
            (self.view.next_track, self.actions.ui_skip),
            (self.view.loop_track, self.actions.ui_toggle_loop),
            (self.view.stop_music, self.actions.ui_stop),
        ):
            with self.subTest(button=button.label):
                interaction = _interaction()
                await button.callback(interaction)
                action.assert_awaited_once_with(interaction, 1, 2)

    async def test_jump_and_clear_buttons_open_their_dialogs(self) -> None:
        jump_interaction = _interaction()
        await self.view.jump_track.callback(jump_interaction)
        jump_interaction.response.send_modal.assert_awaited_once()

        clear_interaction = _interaction()
        await self.view.clear_queue.callback(clear_interaction)
        sent_view = clear_interaction.response.send_message.await_args.kwargs["view"]
        self.assertIsInstance(sent_view, ClearQueueConfirmation)

    async def test_queue_button_sends_requester_only_paginator(self) -> None:
        self.actions.current_snapshot = _snapshot(
            state=PlaybackState.PLAYING,
            current=self.track,
            queued=(self.track,),
        )
        interaction = _interaction()
        await self.view.show_queue.callback(interaction)
        sent_view = interaction.response.send_message.await_args.kwargs["view"]
        self.assertIsInstance(sent_view, QueuePaginatorView)
        self.assertEqual(sent_view.requester_id, 10)

    async def test_search_result_view_is_requester_bound(self) -> None:
        result = SearchResult("Kết quả", "https://youtube.test/result", 10)
        view = SearchResultView(self.actions, self.manager, 1, 2, 10, (result,))
        outsider = _interaction(11)
        self.assertFalse(await view.interaction_check(outsider))
        outsider.response.send_message.assert_awaited()
        self.assertTrue(await view.interaction_check(_interaction(10)))

    async def test_search_selection_enqueues_only_selected_result(self) -> None:
        results = (
            SearchResult("Một", "https://youtube.test/one", 10),
            SearchResult("Hai", "https://youtube.test/two", 20),
        )
        view = SearchResultView(self.actions, self.manager, 1, 2, 10, results)
        select = next(child for child in view.children if isinstance(child, SearchResultSelect))
        select._values = ["1"]
        interaction = _interaction(10)

        await select.callback(interaction)

        self.actions.ui_enqueue_search_result.assert_awaited_once_with(
            interaction, 1, 2, results[1]
        )
        interaction.response.defer.assert_awaited_once()

    async def test_search_result_view_accepts_only_one_rapid_selection(self) -> None:
        result = SearchResult("Kết quả", "https://youtube.test/result", 10)
        view = SearchResultView(self.actions, self.manager, 1, 2, 10, (result,))
        select = next(
            child for child in view.children if isinstance(child, SearchResultSelect)
        )
        select._values = ["0"]
        first = _interaction(10)
        second = _interaction(10)

        await asyncio.gather(select.callback(first), select.callback(second))

        self.actions.ui_enqueue_search_result.assert_awaited_once()
        accepted = [
            interaction
            for interaction in (first, second)
            if interaction.response.defer.await_count == 1
        ]
        rejected = [
            interaction
            for interaction in (first, second)
            if interaction.response.send_message.await_count == 1
        ]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        rejected[0].response.send_message.assert_awaited_once_with(
            "Kết quả này đã được sử dụng.",
            view=None,
            embed=None,
            ephemeral=True,
        )
        self.assertTrue(all(child.disabled for child in view.children))

    async def test_search_selection_from_replaced_panel_is_stale(self) -> None:
        self.view._registered = True
        replacement = MagicMock()
        replacement.view = MagicMock()
        self.manager.get.return_value = replacement
        result = SearchResult("Kết quả", "https://youtube.test/result", 10)
        view = SearchResultView(
            self.actions,
            self.manager,
            1,
            2,
            10,
            (result,),
            panel_view=self.view,
        )
        select = next(
            child for child in view.children if isinstance(child, SearchResultSelect)
        )
        select._values = ["0"]
        interaction = _interaction(10)

        await select.callback(interaction)

        self.actions.ui_enqueue_search_result.assert_not_awaited()
        self.actions.ui_ensure_panel_access.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "thay thế",
            interaction.response.send_message.await_args.args[0],
        )

    async def test_add_modal_returns_requester_bound_search_results(self) -> None:
        result = SearchResult("Kết quả", "https://youtube.test/result", 10)
        self.actions.ui_add_input.return_value = AddInputResult(results=(result,))
        modal = AddMusicModal(self.actions, self.manager, 1, 2)
        modal.query._value = "kết quả"
        interaction = _interaction(10)

        await modal.on_submit(interaction)

        self.actions.ui_add_input.assert_awaited_once_with(
            interaction, 1, 2, "kết quả"
        )
        sent_view = interaction.followup.send.await_args_list[0].kwargs["view"]
        self.assertIsInstance(sent_view, SearchResultView)
        self.assertEqual(sent_view.requester_id, 10)

    async def test_clear_confirmation_is_requester_bound_and_thirty_seconds(self) -> None:
        view = ClearQueueConfirmation(self.view, 10)
        self.assertEqual(view.timeout, 30.0)
        self.assertFalse(await view.interaction_check(_interaction(11)))

    async def test_clear_confirmation_accepts_only_one_decision(self) -> None:
        view = ClearQueueConfirmation(self.view, 10)
        first = _interaction(10)
        second = _interaction(10)

        await asyncio.gather(
            view.confirm.callback(first),
            view.confirm.callback(second),
        )

        self.actions.ui_clear_queue.assert_awaited_once()
        rejected = [
            interaction
            for interaction in (first, second)
            if interaction.response.send_message.await_count == 1
        ]
        self.assertEqual(len(rejected), 1)
        self.assertIn(
            "đã được sử dụng",
            rejected[0].response.send_message.await_args.args[0],
        )


class PanelManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_posting_fresh_panel_disables_previous_panel(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        first_message = MagicMock()
        first_message.id = 100
        first_message.edit = AsyncMock()
        second_message = MagicMock()
        second_message.id = 101
        second_message.edit = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(side_effect=[first_message, second_message])

        await manager.post_panel(destination, 1, 2)
        old = manager.get(1)
        await manager.post_panel(destination, 1, 2)

        first_message.edit.assert_awaited_once()
        self.assertTrue(all(child.disabled for child in old.view.children))
        self.assertIs(manager.get(1).message, second_message)

    async def test_refresh_updates_embed_and_buttons(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)

        playing = _snapshot(
            state=PlaybackState.PLAYING,
            current=QueuedTrack("Mới", "https://youtube.test/new"),
        )
        await manager.refresh(1, playing)

        message.edit.assert_awaited_once()
        record = manager.get(1)
        self.assertFalse(record.view.pause_resume.disabled)
        edited_embed = message.edit.await_args.kwargs["embed"]
        current_value = next(
            field.value for field in edited_embed.fields if field.name == "Bài hiện tại"
        )
        self.assertIn("Mới", current_value)

    async def test_player_notification_does_not_wait_for_blocked_edit(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)

        edit_started = asyncio.Event()
        allow_edit = asyncio.Event()
        edit_finished = asyncio.Event()

        async def blocked_edit(**kwargs: object) -> None:
            edit_started.set()
            await allow_edit.wait()
            edit_finished.set()

        message.edit = AsyncMock(side_effect=blocked_edit)
        playing = _snapshot(
            state=PlaybackState.PLAYING,
            current=QueuedTrack("Mới", "https://youtube.test/new"),
        )
        actions.current_snapshot = playing

        await asyncio.wait_for(
            manager.on_player_state_change(1, playing),
            timeout=0.1,
        )
        await asyncio.wait_for(edit_started.wait(), timeout=0.5)
        self.assertFalse(edit_finished.is_set())

        allow_edit.set()
        await asyncio.wait_for(edit_finished.wait(), timeout=0.5)
        edited_embed = message.edit.await_args.kwargs["embed"]
        current_value = next(
            field.value
            for field in edited_embed.fields
            if field.name == "Bài hiện tại"
        )
        self.assertIn("Mới", current_value)
        await manager.close()

    async def test_post_panel_refreshes_state_changed_while_send_waits(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        send_started = asyncio.Event()
        allow_send = asyncio.Event()

        async def blocked_send(**kwargs: object) -> object:
            send_started.set()
            await allow_send.wait()
            return message

        destination = MagicMock()
        destination.send = AsyncMock(side_effect=blocked_send)
        post_task = asyncio.create_task(manager.post_panel(destination, 1, 2))
        await asyncio.wait_for(send_started.wait(), timeout=0.5)
        actions.current_snapshot = _snapshot(
            state=PlaybackState.PLAYING,
            current=QueuedTrack("Đến sau", "https://youtube.test/late"),
        )
        allow_send.set()

        self.assertIs(await asyncio.wait_for(post_task, timeout=0.5), message)
        message.edit.assert_awaited_once()
        edited_embed = message.edit.await_args.kwargs["embed"]
        current_value = next(
            field.value
            for field in edited_embed.fields
            if field.name == "Bài hiện tại"
        )
        self.assertIn("Đến sau", current_value)
        self.assertFalse(manager.get(1).view.pause_resume.disabled)
        await manager.close()

    async def test_channel_change_invalidates_and_disables_panel(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)

        self.assertTrue(await manager.invalidate_if_channel_changed(1, 3))
        self.assertIsNone(manager.get(1))
        message.edit.assert_awaited_once()

    async def test_refresh_drops_deleted_panel_without_touching_playback(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        response = MagicMock()
        response.status = 404
        response.reason = "Not Found"
        message.edit = AsyncMock(side_effect=discord.NotFound(response, "gone"))
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)

        await manager.refresh(1, _snapshot())

        self.assertIsNone(manager.get(1))

    async def test_refresh_drops_panel_after_generic_edit_failure(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        response = MagicMock()
        response.status = 500
        response.reason = "Server Error"
        message.edit = AsyncMock(
            side_effect=discord.HTTPException(response, "failed")
        )
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)

        await manager.refresh(1, _snapshot())

        self.assertIsNone(manager.get(1))

    async def test_bump_sends_fresh_panel_before_deleting_old_message(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        events: list[str] = []
        first_message = MagicMock()
        first_message.id = 100
        first_message.edit = AsyncMock()

        async def delete_first() -> None:
            events.append("delete-old")

        first_message.delete = AsyncMock(side_effect=delete_first)
        second_message = MagicMock()
        second_message.id = 101
        second_message.edit = AsyncMock()
        second_message.delete = AsyncMock()
        messages = iter((first_message, second_message))

        async def send_panel(**kwargs: object) -> object:
            events.append("send")
            return next(messages)

        destination = MagicMock()
        destination.send = AsyncMock(side_effect=send_panel)
        await manager.post_panel(destination, 1, 2)
        old = manager.get(1)
        events.clear()

        self.assertTrue(await manager.bump_panel(1))

        self.assertEqual(events, ["send", "delete-old"])
        first_message.delete.assert_awaited_once_with()
        first_message.edit.assert_not_awaited()
        self.assertTrue(all(child.disabled for child in old.view.children))
        replacement = manager.get(1)
        self.assertIsNot(replacement, old)
        self.assertIs(replacement.message, second_message)
        self.assertTrue(replacement.view._registered)
        second_message.edit.assert_awaited_once()
        await manager.close()

    async def test_bump_skips_disconnected_panel_without_posting(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)
        original = manager.get(1)
        destination.send.reset_mock()
        actions.voice_connected = False
        actions.bound_voice_channel_id = None

        self.assertFalse(await manager.bump_panel(1))

        destination.send.assert_not_awaited()
        message.delete.assert_not_awaited()
        self.assertIs(manager.get(1), original)
        await manager.close()

    async def test_bump_skips_panel_connected_to_a_different_voice_room(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)
        original = manager.get(1)
        destination.send.reset_mock()
        actions.bound_voice_channel_id = 3

        self.assertFalse(await manager.bump_panel(1))

        destination.send.assert_not_awaited()
        message.delete.assert_not_awaited()
        self.assertIs(manager.get(1), original)
        await manager.close()

    async def test_bump_send_failure_preserves_working_old_panel(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        message.delete = AsyncMock()
        response = MagicMock()
        response.status = 500
        response.reason = "Server Error"
        destination = MagicMock()
        destination.send = AsyncMock(
            side_effect=(
                message,
                discord.HTTPException(response, "failed"),
            )
        )
        await manager.post_panel(destination, 1, 2)
        original = manager.get(1)

        self.assertFalse(await manager.bump_panel(1))

        self.assertIs(manager.get(1), original)
        message.delete.assert_not_awaited()
        self.assertFalse(original.view.add_music.disabled)
        await manager.close()

    async def test_bump_disables_old_controls_when_old_delete_fails(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        response = MagicMock()
        response.status = 500
        response.reason = "Server Error"
        first_message = MagicMock()
        first_message.id = 100
        first_message.edit = AsyncMock()
        first_message.delete = AsyncMock(
            side_effect=discord.HTTPException(response, "failed")
        )
        second_message = MagicMock()
        second_message.id = 101
        second_message.edit = AsyncMock()
        second_message.delete = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(side_effect=(first_message, second_message))
        await manager.post_panel(destination, 1, 2)
        old = manager.get(1)

        self.assertTrue(await manager.bump_panel(1))

        first_message.delete.assert_awaited_once_with()
        first_message.edit.assert_awaited_once_with(view=old.view)
        self.assertTrue(all(child.disabled for child in old.view.children))
        self.assertIs(manager.get(1).message, second_message)
        await manager.close()

    async def test_raw_delete_during_bump_send_deletes_orphan_replacement(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        first_message = MagicMock()
        first_message.id = 100
        first_message.edit = AsyncMock()
        first_message.delete = AsyncMock()
        second_message = MagicMock()
        second_message.id = 101
        second_message.edit = AsyncMock()
        second_message.delete = AsyncMock()
        send_started = asyncio.Event()
        allow_send = asyncio.Event()

        async def blocked_send(**kwargs: object) -> object:
            send_started.set()
            await allow_send.wait()
            return second_message

        destination = MagicMock()
        destination.send = AsyncMock(return_value=first_message)
        await manager.post_panel(destination, 1, 2)
        destination.send = AsyncMock(side_effect=blocked_send)

        bumping = asyncio.create_task(manager.bump_panel(1))
        await asyncio.wait_for(send_started.wait(), timeout=0.5)
        self.assertTrue(manager.drop_message(100))
        allow_send.set()

        self.assertFalse(await asyncio.wait_for(bumping, timeout=0.5))
        self.assertIsNone(manager.get(1))
        second_message.delete.assert_awaited_once_with()
        first_message.delete.assert_not_awaited()
        await manager.close()

    async def test_bump_refreshes_settings_changed_during_send(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        first_message = MagicMock()
        first_message.id = 100
        first_message.edit = AsyncMock()
        first_message.delete = AsyncMock()
        second_message = MagicMock()
        second_message.id = 101
        second_message.edit = AsyncMock()
        second_message.delete = AsyncMock()
        send_started = asyncio.Event()
        allow_send = asyncio.Event()

        async def blocked_send(**kwargs: object) -> object:
            send_started.set()
            await allow_send.wait()
            return second_message

        destination = MagicMock()
        destination.send = AsyncMock(return_value=first_message)
        await manager.post_panel(destination, 1, 2)
        destination.send = AsyncMock(side_effect=blocked_send)

        bumping = asyncio.create_task(manager.bump_panel(1))
        await asyncio.wait_for(send_started.wait(), timeout=0.5)
        actions.current_audio_settings = GuildAudioSettings(1.1, 0.4, "ja")
        actions.title_reading = False
        actions.chat_reading = True
        allow_send.set()

        self.assertTrue(await asyncio.wait_for(bumping, timeout=0.5))
        edited_embed = second_message.edit.await_args.kwargs["embed"]
        values = {field.name: field.value for field in edited_embed.fields}
        self.assertEqual(
            values["Cài đặt âm thanh"],
            "Nhạc: 110% · Nhạc còn lại khi TTS: 40% · TTS: ja",
        )
        replacement = manager.get(1)
        self.assertEqual(replacement.view.toggle_title_reading.label, "Đọc tên bài: Tắt")
        self.assertEqual(replacement.view.toggle_chat_reading.label, "Đọc tin nhắn: Bật")
        await manager.close()

    async def test_bump_timer_uses_minutes_and_reposts_periodically(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        first_message = MagicMock()
        first_message.id = 100
        first_message.edit = AsyncMock()
        first_message.delete = AsyncMock()
        second_message = MagicMock()
        second_message.id = 101
        second_message.delete = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(side_effect=(first_message, second_message))
        await manager.post_panel(destination, 1, 2)

        real_wait_for = asyncio.wait_for
        timeouts: list[float] = []
        bump_completed = asyncio.Event()

        async def expire_countdown(awaitable: object, *, timeout: float) -> None:
            timeouts.append(timeout)
            awaitable.close()
            raise TimeoutError

        async def finish_bump(**kwargs: object) -> None:
            manager.set_bump_interval_minutes(1, 0)
            bump_completed.set()

        second_message.edit = AsyncMock(side_effect=finish_bump)
        with patch(
            "src.music_ui.asyncio.wait_for",
            side_effect=expire_countdown,
        ):
            manager.set_bump_interval_minutes(1, 1)
            await real_wait_for(bump_completed.wait(), timeout=0.5)

        self.assertEqual(timeouts, [60])
        self.assertIs(manager.get(1).message, second_message)
        first_message.delete.assert_awaited_once_with()
        second_message.edit.assert_awaited_once()
        self.assertEqual(manager.bump_interval_minutes(1), 0)
        await manager.close()

    async def test_reconfiguring_bump_interval_restarts_countdown(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)

        real_wait_for = asyncio.wait_for
        countdowns: list[float] = []
        first_countdown = asyncio.Event()
        second_countdown = asyncio.Event()

        async def wait_for_wakeup(awaitable: object, *, timeout: float) -> None:
            countdowns.append(timeout)
            if len(countdowns) == 1:
                first_countdown.set()
            else:
                second_countdown.set()
            await awaitable

        with patch(
            "src.music_ui.asyncio.wait_for",
            side_effect=wait_for_wakeup,
        ):
            manager.set_bump_interval_minutes(1, 5)
            await real_wait_for(first_countdown.wait(), timeout=0.5)
            manager.set_bump_interval_minutes(1, 10)
            await real_wait_for(second_countdown.wait(), timeout=0.5)
            manager.set_bump_interval_minutes(1, 0)
            await manager.close()

        self.assertEqual(countdowns, [300, 600])
        self.assertEqual(destination.send.await_count, 1)
        self.assertEqual(manager.bump_interval_minutes(1), 0)

    async def test_interval_change_at_timeout_boundary_prevents_old_countdown_bump(
        self,
    ) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        first_message = MagicMock()
        first_message.id = 100
        first_message.edit = AsyncMock()
        first_message.delete = AsyncMock()
        second_message = MagicMock()
        second_message.id = 101
        second_message.edit = AsyncMock()
        second_message.delete = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(side_effect=(first_message, second_message))
        await manager.post_panel(destination, 1, 2)

        real_wait_for = asyncio.wait_for
        first_countdown = asyncio.Event()
        release_expired_countdown = asyncio.Event()
        restarted_countdown = asyncio.Event()
        countdowns: list[float] = []

        async def boundary_wait(awaitable: object, *, timeout: float) -> None:
            countdowns.append(timeout)
            if len(countdowns) == 1:
                first_countdown.set()
                await release_expired_countdown.wait()
                awaitable.close()
                raise TimeoutError
            restarted_countdown.set()
            await awaitable

        with patch("src.music_ui.asyncio.wait_for", side_effect=boundary_wait):
            manager.set_bump_interval_minutes(1, 5)
            await real_wait_for(first_countdown.wait(), timeout=0.5)
            manager.set_bump_interval_minutes(1, 10)
            release_expired_countdown.set()
            await real_wait_for(restarted_countdown.wait(), timeout=0.5)
            manager.set_bump_interval_minutes(1, 0)
            await manager.close()

        self.assertEqual(countdowns, [300, 600])
        self.assertEqual(destination.send.await_count, 1)

    async def test_manual_replacement_at_timeout_boundary_restarts_countdown(
        self,
    ) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        first_message = MagicMock()
        first_message.id = 100
        first_message.edit = AsyncMock()
        first_message.delete = AsyncMock()
        second_message = MagicMock()
        second_message.id = 101
        second_message.edit = AsyncMock()
        second_message.delete = AsyncMock()
        automatic_message = MagicMock()
        automatic_message.id = 102
        automatic_message.edit = AsyncMock()
        automatic_message.delete = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(
            side_effect=(first_message, second_message, automatic_message)
        )
        await manager.post_panel(destination, 1, 2)

        real_wait_for = asyncio.wait_for
        first_countdown = asyncio.Event()
        release_expired_countdown = asyncio.Event()
        restarted_countdown = asyncio.Event()
        countdowns: list[float] = []

        async def boundary_wait(awaitable: object, *, timeout: float) -> None:
            countdowns.append(timeout)
            if len(countdowns) == 1:
                first_countdown.set()
                await release_expired_countdown.wait()
                awaitable.close()
                raise TimeoutError
            restarted_countdown.set()
            await awaitable

        with patch("src.music_ui.asyncio.wait_for", side_effect=boundary_wait):
            manager.set_bump_interval_minutes(1, 5)
            await real_wait_for(first_countdown.wait(), timeout=0.5)
            await manager.post_panel(destination, 1, 2)
            release_expired_countdown.set()
            await real_wait_for(restarted_countdown.wait(), timeout=0.5)
            self.assertIs(manager.get(1).message, second_message)
            manager.set_bump_interval_minutes(1, 0)
            await manager.close()

        self.assertEqual(countdowns, [300, 300])
        self.assertEqual(destination.send.await_count, 2)

    async def test_bump_interval_setter_rejects_invalid_values(self) -> None:
        manager = MusicPanelManager(_Actions(_snapshot()))

        for value in (True, 1.5, "5"):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    manager.set_bump_interval_minutes(1, value)
        for value in (-1, 1441):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    manager.set_bump_interval_minutes(1, value)

        self.assertEqual(manager.bump_interval_minutes(1), 0)
        await manager.close()

    async def test_drop_message_stops_future_automatic_bumps(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)
        real_wait_for = asyncio.wait_for
        countdown_started = asyncio.Event()

        async def wait_for_wakeup(awaitable: object, *, timeout: float) -> None:
            countdown_started.set()
            await awaitable

        with patch(
            "src.music_ui.asyncio.wait_for",
            side_effect=wait_for_wakeup,
        ):
            manager.set_bump_interval_minutes(1, 1)
            await real_wait_for(countdown_started.wait(), timeout=0.5)
            self.assertTrue(manager.drop_message(100))
            await manager.close()

        self.assertIsNone(manager.get(1))
        self.assertEqual(destination.send.await_count, 1)

    async def test_close_stops_timer_and_clears_runtime_interval(self) -> None:
        actions = _Actions(_snapshot())
        manager = MusicPanelManager(actions)
        message = MagicMock()
        message.id = 100
        message.edit = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)
        await manager.post_panel(destination, 1, 2)
        real_wait_for = asyncio.wait_for
        countdown_started = asyncio.Event()

        async def wait_for_wakeup(awaitable: object, *, timeout: float) -> None:
            countdown_started.set()
            await awaitable

        with patch(
            "src.music_ui.asyncio.wait_for",
            side_effect=wait_for_wakeup,
        ):
            manager.set_bump_interval_minutes(1, 1)
            await real_wait_for(countdown_started.wait(), timeout=0.5)
            await manager.close()

        self.assertIsNone(manager.get(1))
        self.assertEqual(manager.bump_interval_minutes(1), 0)
        self.assertEqual(destination.send.await_count, 1)


if __name__ == "__main__":
    unittest.main()
