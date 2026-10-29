"""Recording panel copy, buttons, and a single message per guild."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from src.recording_ui import (
    IDLE_DESCRIPTION,
    RecordingPanelManager,
    RecordingPanelState,
    RecordingPanelView,
    build_recording_embed,
    due_for_refresh,
)


class _Actions:
    def __init__(self, state: RecordingPanelState | None = None) -> None:
        self.state = state or RecordingPanelState(False, 0.0, 0, "")
        self.ui_ensure_panel_access = AsyncMock(return_value=True)
        self.ui_start_recording = AsyncMock(return_value="")
        self.ui_stop_recording = AsyncMock(return_value="")

    def ui_recording_state(self, guild_id: int) -> RecordingPanelState:
        return self.state


def _interaction() -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class RecordingEmbedTests(unittest.TestCase):
    def test_idle_and_recording_copy(self) -> None:
        idle = build_recording_embed(RecordingPanelState(False, 0, 0, "Phòng"))
        self.assertEqual(idle.title, "Ghi âm")
        self.assertEqual(idle.description, IDLE_DESCRIPTION)
        self.assertIn("Bắt đầu", idle.description or "")

        live = build_recording_embed(
            RecordingPanelState(True, 65, 2, "Phòng hát")
        )
        self.assertIn("Đang ghi **Phòng hát**.", live.description or "")
        self.assertIn("Thời gian: 1:05.", live.description or "")
        self.assertIn("Người nói: 2.", live.description or "")

    def test_buttons_follow_the_take(self) -> None:
        actions = _Actions()
        manager = RecordingPanelManager(actions)
        view = RecordingPanelView(actions, manager, 1, 9, actions.state)
        self.assertFalse(view.start_recording.disabled)
        self.assertTrue(view.stop_recording.disabled)
        view.sync(RecordingPanelState(True, 10, 1, "Phòng"))
        self.assertTrue(view.start_recording.disabled)
        self.assertFalse(view.stop_recording.disabled)

    def test_refresh_on_a_new_speaker_or_after_ten_seconds(self) -> None:
        self.assertTrue(
            due_for_refresh(last_edit=0, last_speakers=0, now=1, speakers=1)
        )
        self.assertFalse(
            due_for_refresh(last_edit=0, last_speakers=1, now=9, speakers=1)
        )
        self.assertTrue(
            due_for_refresh(last_edit=0, last_speakers=1, now=10, speakers=1)
        )


class RecordingPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_post_edits_the_same_message(self) -> None:
        actions = _Actions()
        manager = RecordingPanelManager(actions)
        message = MagicMock()
        message.edit = AsyncMock()
        destination = MagicMock()
        destination.send = AsyncMock(return_value=message)

        first = await manager.post_panel(
            destination, 1, 9, voice_channel_name="Phòng"
        )
        second = await manager.post_panel(
            destination, 1, 9, voice_channel_name="Phòng"
        )

        self.assertIs(first, second)
        destination.send.assert_awaited_once()
        message.edit.assert_awaited()

    async def test_outsider_is_denied_and_insider_can_start_and_stop(self) -> None:
        actions = _Actions()
        manager = RecordingPanelManager(actions)
        from src.recording_ui import RecordingPanelView

        view = RecordingPanelView(actions, manager, 1, 9, actions.state)
        view._registered = False
        denied = _interaction()
        actions.ui_ensure_panel_access.return_value = False
        await view.start_recording.callback(denied)
        actions.ui_start_recording.assert_not_awaited()

        actions.ui_ensure_panel_access.return_value = True
        allowed = _interaction()
        await view.start_recording.callback(allowed)
        actions.ui_start_recording.assert_awaited()

        actions.state = RecordingPanelState(True, 3, 1, "Phòng")
        view.sync(actions.state)
        await view.stop_recording.callback(_interaction())
        actions.ui_stop_recording.assert_awaited()


if __name__ == "__main__":
    unittest.main()
