from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from src.soundboard import InstantHit, SoundboardEntry
from src.soundboard_ui import (
    SOUNDBOARD_PAGE_SIZE,
    AddSoundModal,
    SoundSearchView,
    SoundboardSelect,
    SoundboardView,
    build_soundboard_embed,
)


def _entry(index: int, *, added_by: int = 10) -> SoundboardEntry:
    sound_id = f"{index:08x}"
    return SoundboardEntry(
        id=sound_id,
        name=f"Clip {index}",
        mp3=f"{sound_id}.mp3",
        source_url="https://www.myinstants.com/en/instant/x/",
        duration_ms=1000 + index,
        added_by=added_by,
        added_at="2026-09-08T12:00:00+00:00",
    )


class _Actions:
    def __init__(self, entries: tuple[SoundboardEntry, ...] = ()) -> None:
        self.entries = entries
        self.ui_ensure_panel_access = AsyncMock(return_value=True)
        self.ui_play_soundboard = AsyncMock(return_value="Đã phát.")
        self.ui_add_soundboard = AsyncMock(return_value="Đã lưu.")
        self.ui_remove_soundboard = AsyncMock(return_value="Đã xóa.")
        self.ui_search_soundboard = AsyncMock(return_value=())

    async def ui_list_soundboard(self, guild_id: int) -> tuple[SoundboardEntry, ...]:
        return self.entries


class SoundboardUiTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_embed_tells_user_to_add(self) -> None:
        embed = build_soundboard_embed(())
        self.assertEqual(embed.title, "Bảng âm thanh")
        self.assertIn("Chưa có âm thanh", embed.description or "")
        self.assertIn("MyInstants", embed.description or "")

    def test_twenty_six_entries_paginate(self) -> None:
        entries = tuple(_entry(index) for index in range(26))
        actions = _Actions(entries)
        view = SoundboardView(actions, 1, 2, entries)
        self.assertEqual(SOUNDBOARD_PAGE_SIZE, 25)
        self.assertEqual(view.page_count, 2)
        select = next(
            child for child in view.children if isinstance(child, SoundboardSelect)
        )
        self.assertEqual(len(select.options), 25)
        self.assertTrue(view.next_page.disabled is False)
        self.assertTrue(view.previous_page.disabled)

        view.page = 1
        view._rebuild()
        select = next(
            child for child in view.children if isinstance(child, SoundboardSelect)
        )
        self.assertEqual(len(select.options), 1)
        self.assertEqual(select.options[0].value, f"{25:08x}")

    def test_add_modal_fields(self) -> None:
        view = SoundboardView(_Actions(), 1, 2, ())
        modal = AddSoundModal(view)
        self.assertEqual(modal.title, "Thêm âm thanh")
        self.assertIs(modal.name, modal.children[0])
        self.assertIs(modal.url, modal.children[1])
        self.assertEqual(modal.name.max_length, 32)
        self.assertEqual(modal.url.max_length, 500)

    def test_empty_library_hides_select_and_disables_delete(self) -> None:
        view = SoundboardView(_Actions(), 1, 2, ())
        self.assertFalse(
            any(isinstance(child, SoundboardSelect) for child in view.children)
        )
        self.assertTrue(view.delete_sound.disabled)

    async def test_play_selected_defers_and_calls_action(self) -> None:
        entries = (_entry(1),)
        actions = _Actions(entries)
        view = SoundboardView(actions, 1, 2, entries)
        interaction = MagicMock(spec=discord.Interaction)
        interaction.response.is_done.return_value = False
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await view.play_selected(interaction, entries[0].id)

        actions.ui_ensure_panel_access.assert_awaited()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        actions.ui_play_soundboard.assert_awaited_once_with(
            interaction, 1, 2, entries[0].id
        )
        self.assertEqual(view.selected_id, entries[0].id)

    async def test_timeout_disables_children(self) -> None:
        view = SoundboardView(_Actions((_entry(1),)), 1, 2, (_entry(1),))
        view.message = None
        await view.on_timeout()
        for child in view.children:
            if hasattr(child, "disabled"):
                self.assertTrue(child.disabled)

    def _modal_interaction(self) -> MagicMock:
        value = MagicMock(spec=discord.Interaction)
        value.response.is_done.return_value = False
        value.response.defer = AsyncMock()
        value.response.send_message = AsyncMock()
        value.followup.send = AsyncMock(return_value=MagicMock())
        value.edit_original_response = AsyncMock()
        return value

    async def test_keyword_add_opens_myinstants_search(self) -> None:
        hits = (
            InstantHit(
                "Vine Boom",
                "https://www.myinstants.com/en/instant/vine/",
                "https://www.myinstants.com/media/sounds/vine.mp3",
            ),
        )
        actions = _Actions()
        actions.ui_search_soundboard.return_value = hits
        modal = AddSoundModal(SoundboardView(actions, 1, 2, ()))
        modal.name._value = ""
        modal.url._value = "vine boom"
        interaction = self._modal_interaction()
        await modal.on_submit(interaction)
        actions.ui_search_soundboard.assert_awaited_once_with("vine boom")
        actions.ui_add_soundboard.assert_not_awaited()
        view = interaction.followup.send.await_args.kwargs["view"]
        self.assertIsInstance(view, SoundSearchView)

    async def test_url_add_requires_a_name(self) -> None:
        actions = _Actions()
        modal = AddSoundModal(SoundboardView(actions, 1, 2, ()))
        modal.name._value = ""
        modal.url._value = "https://www.myinstants.com/en/instant/vine/"
        interaction = self._modal_interaction()
        await modal.on_submit(interaction)
        self.assertIn("tên", interaction.followup.send.await_args.args[0].lower())
        actions.ui_add_soundboard.assert_not_awaited()

    async def test_search_hit_saves_selected_instant(self) -> None:
        hits = (InstantHit("Vine", "https://page.test/vine", "https://mp3.test/vine.mp3"),)
        actions = _Actions()
        picker = SoundboardView(actions, 1, 2, ())
        picker.message = None
        view = SoundSearchView(picker, hits)
        interaction = self._modal_interaction()
        await view.select_hit(interaction, 0)
        actions.ui_add_soundboard.assert_awaited_once_with(
            interaction, 1, 2, "Vine", "https://mp3.test/vine.mp3",
        )
        await view.select_hit(self._modal_interaction(), 0)
        actions.ui_add_soundboard.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
