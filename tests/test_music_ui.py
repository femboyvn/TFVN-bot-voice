from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from src.media import QueuedTrack, SearchResult
from src.music_ui import (
    AddInputResult,
    AddMusicModal,
    ClearQueueConfirmation,
    MusicPanelManager,
    MusicPanelView,
    QueuePaginatorView,
    SearchResultSelect,
    SearchResultView,
    build_music_embed,
)
from src.player import PlaybackState, PlayerSnapshot


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

    def ui_snapshot(self, guild_id: int) -> PlayerSnapshot | None:
        return self.current_snapshot


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
        self.view = MusicPanelView(self.actions, self.manager, 1, 2, self.actions.current_snapshot)

    async def test_add_opens_modal_after_allow_disconnected_access_check(self) -> None:
        interaction = _interaction()
        await self.view.add_music.callback(interaction)
        self.actions.ui_ensure_panel_access.assert_awaited_once_with(
            interaction, 1, 2, connect_if_missing=True
        )
        interaction.response.send_modal.assert_awaited_once()
        self.assertIsInstance(interaction.response.send_modal.await_args.args[0], AddMusicModal)

    async def test_pause_defers_and_delegates_to_action(self) -> None:
        interaction = _interaction()
        await self.view.pause_resume.callback(interaction)
        self.actions.ui_pause.assert_awaited_once_with(interaction, 1, 2)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once_with(
            "Đã tạm dừng.", ephemeral=True
        )
        self.manager.refresh.assert_awaited_once_with(1)

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


if __name__ == "__main__":
    unittest.main()
