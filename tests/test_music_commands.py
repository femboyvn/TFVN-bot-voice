"""Command behavior: stop vs leave with an active TTS session."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, Mock

from src.cogs.music import MusicCog
from src.player import JumpResult


class StopVsLeaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Mock()
        self.settings = Mock()
        self.settings.command_prefix = "!tfd "
        self.media = Mock()
        self.players = AsyncMock()
        self.sessions = Mock()
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

    async def test_stop_with_session_keeps_connection_and_session(self) -> None:
        self.sessions.is_active.return_value = True
        self.players.remove = AsyncMock(return_value=True)
        self.sessions.stop = AsyncMock()

        await self.cog.stop.callback(self.cog, self.ctx)

        self.players.remove.assert_awaited_once_with(1, disconnect=False)
        self.sessions.stop.assert_not_called()
        self.ctx.voice_client.disconnect.assert_not_called()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("Đã dừng nhạc", sent)
        self.assertIn("leave", sent)

    async def test_stop_without_session_disconnects(self) -> None:
        self.sessions.is_active.return_value = False
        self.players.remove = AsyncMock(return_value=True)

        await self.cog.stop.callback(self.cog, self.ctx)

        self.players.remove.assert_awaited_once_with(1, disconnect=True)
        self.ctx.voice_client.disconnect.assert_awaited()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("rời", sent.lower())

    async def test_leave_ends_session_and_disconnects(self) -> None:
        self.players.remove = AsyncMock(return_value=True)
        self.sessions.stop = AsyncMock(return_value=True)

        await self.cog.leave.callback(self.cog, self.ctx)

        self.players.remove.assert_awaited_once_with(1, disconnect=False)
        self.sessions.stop.assert_awaited_once_with(1)
        self.ctx.voice_client.disconnect.assert_awaited()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("theo dõi", sent.lower())

    async def test_nameannounce_requires_active_session(self) -> None:
        self.sessions.get.return_value = None
        await self.cog.name_announce.callback(self.cog, self.ctx, "on")
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("join", sent)

    async def test_nameannounce_toggles_session_flag(self) -> None:
        session = Mock()
        session.active = True
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


if __name__ == "__main__":
    unittest.main()
