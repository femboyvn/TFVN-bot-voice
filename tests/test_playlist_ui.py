from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from src.media import QueuedTrack, SearchResult
from src.playlists import SERVER_OWNER_ID, PlaylistError, SavedPlaylist
from src.playlist_ui import (
    DeletePlaylistConfirmation, PlaylistLauncher, PlaylistModal,
    PlaylistSearchView, PlaylistSelect, PlaylistView,
)


def playlist(index: int = 1, count: int = 0) -> SavedPlaylist:
    return SavedPlaylist(
        f"{index:032x}", 1, 10, f"List {index}",
        tuple(QueuedTrack(f"Song {i}", f"https://youtu.be/{i}") for i in range(count)),
        "2026-09-09T00:00:00+00:00",
    )


def interaction(user_id: int = 10, guild_id: int = 1) -> MagicMock:
    value = MagicMock(spec=discord.Interaction)
    value.user.id = user_id
    value.guild.id = guild_id
    value.response.is_done.return_value = False
    async def defer(**kwargs) -> None:
        value.response.is_done.return_value = True

    value.response.defer = AsyncMock(side_effect=defer)
    value.response.send_message = AsyncMock()
    value.response.edit_message = AsyncMock()
    value.response.send_modal = AsyncMock()
    value.followup.send = AsyncMock()
    value.original_response = AsyncMock()
    return value


class PlaylistUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.actions = MagicMock()
        self.actions.ui_list_playlists = AsyncMock(return_value=())
        self.actions.ui_playlist_action = AsyncMock(return_value="Đã lưu.")
        self.actions.ui_playlist_search = AsyncMock(return_value=())

    async def test_launcher_only_opens_private_library_for_requester(self) -> None:
        launcher = PlaylistLauncher(self.actions, 1, 10)
        wrong = interaction(99)
        await launcher.open_library.callback(wrong)
        self.actions.ui_list_playlists.assert_not_awaited()
        right = interaction()
        await launcher.open_library.callback(right)
        self.actions.ui_list_playlists.assert_awaited_once_with(1, 10)
        self.assertTrue(right.followup.send.await_args.kwargs["ephemeral"])
        self.assertIsInstance(right.followup.send.await_args.kwargs["view"], PlaylistView)

    async def test_empty_library_and_discord_component_limits(self) -> None:
        view = PlaylistView(self.actions, 1, 10)
        self.assertIn("Chưa có", view.render_embed().description)
        self.assertFalse(view.create.disabled)
        self.assertFalse(view.save_queue.disabled)
        for button in (view.play, view.add_track, view.rename, view.delete,
                       view.remove_track, view.move_track):
            self.assertTrue(button.disabled)
        for row in range(5):
            self.assertLessEqual(sum(child.width for child in view.children if child.row == row), 5)

    async def test_playlist_and_track_pagination_preserve_numbering(self) -> None:
        entries = tuple(playlist(i, 21) for i in range(26))
        view = PlaylistView(self.actions, 1, 10, entries)
        select = next(child for child in view.children if isinstance(child, PlaylistSelect))
        self.assertEqual(len(select.options), 25)
        self.assertEqual(view.page_count, 2)
        await view.next_list.callback(interaction())
        select = next(child for child in view.children if isinstance(child, PlaylistSelect))
        self.assertEqual(len(select.options), 1)
        await view.next_tracks.callback(interaction())
        self.assertIn("11. Song 10", view.render_embed().description)
        self.assertNotIn("1. Song 0", view.render_embed().description)
        await view.next_tracks.callback(interaction())
        self.assertIn("21. Song 20", view.render_embed().description)
        self.assertTrue(view.next_tracks.disabled)

    async def test_wrong_user_guild_and_stale_panel_cannot_mutate(self) -> None:
        view = PlaylistView(self.actions, 1, 10, (playlist(1, 1),))
        for value in (interaction(99), interaction(10, 2)):
            await view.run_action(value, "delete", [playlist().id])
        self.actions.ui_playlist_action.assert_not_awaited()
        panel = MagicMock()
        panel.ensure_access = AsyncMock(return_value=False)
        view.panel_view = panel
        await view.play.callback(interaction())
        self.actions.ui_playlist_action.assert_not_awaited()

    async def test_modal_keeps_original_playlist_when_selection_changes(self) -> None:
        entries = (playlist(1, 2), playlist(2, 2))
        view = PlaylistView(self.actions, 1, 10, entries)
        modal = PlaylistModal(view, "move")
        view.selected_id = entries[1].id
        modal.value._value = "2"
        modal.destination._value = "1"
        value = interaction()
        await modal.on_submit(value)
        self.actions.ui_playlist_action.assert_awaited_once_with(
            value, 1, "move", [entries[0].id, "2", "1"], None,
            expected_revision=entries[0].revision,
            owner_id=10,
        )

    async def test_search_modal_offers_choices_and_selected_url_is_saved_once(self) -> None:
        view = PlaylistView(self.actions, 1, 10, (playlist(),))
        modal = PlaylistModal(view, "add")
        modal.value._value = "some song"
        results = tuple(SearchResult(f"Result {i}", f"https://youtu.be/{i}", 60) for i in range(5))
        self.actions.ui_playlist_search.return_value = results
        value = interaction()
        await modal.on_submit(value)
        self.actions.ui_playlist_action.assert_not_awaited()
        choices = value.followup.send.await_args.kwargs["view"]
        self.assertIsInstance(choices, PlaylistSearchView)
        self.assertEqual(len(choices.children), 5)
        selected = interaction()
        await choices.children[2].callback(selected)
        await choices.children[2].callback(interaction())
        self.actions.ui_playlist_action.assert_awaited_once_with(
            selected, 1, "add", [playlist().id, results[2].url], None,
            expected_revision=None, owner_id=10,
        )
        self.assertTrue(all(button.disabled for button in choices.children))

    async def test_search_with_no_results_does_not_save(self) -> None:
        view = PlaylistView(self.actions, 1, 10, (playlist(),))
        modal = PlaylistModal(view, "add")
        modal.value._value = "nothing"
        value = interaction()
        await modal.on_submit(value)
        self.assertIn("Không tìm thấy", value.followup.send.await_args.args[0])
        self.actions.ui_playlist_action.assert_not_awaited()

    async def test_direct_url_modal_uses_add_action_without_search(self) -> None:
        view = PlaylistView(self.actions, 1, 10, (playlist(),))
        modal = PlaylistModal(view, "add")
        modal.value._value = "https://youtu.be/track"
        value = interaction()
        await modal.on_submit(value)
        self.actions.ui_playlist_search.assert_not_awaited()
        self.actions.ui_playlist_action.assert_awaited_once_with(
            value, 1, "add", [playlist().id, "https://youtu.be/track"], None,
            expected_revision=None, owner_id=10,
        )

    async def test_delete_confirmation_requires_owner_and_is_used_once(self) -> None:
        picker = PlaylistView(self.actions, 1, 10, (playlist(),))
        view = DeletePlaylistConfirmation(picker, playlist().id)
        await view.confirm.callback(interaction(99))
        self.actions.ui_playlist_action.assert_not_awaited()
        value = interaction()
        await view.confirm.callback(value)
        await view.confirm.callback(interaction())
        self.actions.ui_playlist_action.assert_awaited_once_with(
            value, 1, "delete", [playlist().id], None,
            expected_revision=None, owner_id=10,
        )

    async def test_cancel_delete_and_timeout_leave_store_untouched(self) -> None:
        picker = PlaylistView(self.actions, 1, 10, (playlist(),))
        view = DeletePlaylistConfirmation(picker, playlist().id)
        await view.cancel.callback(interaction())
        await view.confirm.callback(interaction())
        await picker.on_timeout()
        self.assertTrue(all(child.disabled for child in picker.children))
        self.actions.ui_playlist_action.assert_not_awaited()

    async def test_missing_library_shows_friendly_error(self) -> None:
        self.actions.ui_list_playlists.side_effect = PlaylistError("Không truy cập được.")
        value = interaction()
        await PlaylistLauncher(self.actions, 1, 10).open_library.callback(value)
        self.assertIn("Không truy cập", value.followup.send.await_args.args[0])

    async def test_create_and_save_select_the_new_playlist(self) -> None:
        old = playlist(1, 2)
        new = playlist(2, 1)
        for action in ("create", "save"):
            with self.subTest(action=action):
                view = PlaylistView(self.actions, 1, 10, (old,))
                self.actions.ui_list_playlists.return_value = (old, new)
                await view.run_action(interaction(), action, [new.name])
                self.assertEqual(view.selected_id, new.id)
                self.assertIn(new.name, view.render_embed().description)

    async def test_server_launcher_opens_shared_library(self) -> None:
        launcher = PlaylistLauncher(self.actions, 1, 10, server=True)
        self.assertEqual(launcher.open_library.label, "Danh sách máy chủ")
        value = interaction()
        await launcher.open_library.callback(value)
        self.actions.ui_list_playlists.assert_awaited_once_with(1, SERVER_OWNER_ID)
        self.assertTrue(value.followup.send.await_args.kwargs["view"].is_server_library)

    async def test_toggle_scope_reloads_server_then_personal_library(self) -> None:
        view = PlaylistView(self.actions, 1, 10, (playlist(),))
        self.assertFalse(view.is_server_library)
        self.assertEqual(view.toggle_scope.label, "Máy chủ")
        self.actions.ui_list_playlists.return_value = ()
        await view.toggle_scope.callback(interaction())
        self.actions.ui_list_playlists.assert_awaited_with(1, SERVER_OWNER_ID)
        self.assertTrue(view.is_server_library)
        self.assertEqual(view.toggle_scope.label, "Của tôi")
        self.assertEqual(view.render_embed().title, "Danh sách phát của máy chủ")
        await view.toggle_scope.callback(interaction())
        self.actions.ui_list_playlists.assert_awaited_with(1, 10)
        self.assertFalse(view.is_server_library)
