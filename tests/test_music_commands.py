"""Command behavior: stop vs leave with an active TTS session."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, Mock

from src.cogs.music import MusicCog


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
        self.assertIn("Stopped music", sent)
        self.assertIn("leave", sent)

    async def test_stop_without_session_disconnects(self) -> None:
        self.sessions.is_active.return_value = False
        self.players.remove = AsyncMock(return_value=True)

        await self.cog.stop.callback(self.cog, self.ctx)

        self.players.remove.assert_awaited_once_with(1, disconnect=True)
        self.ctx.voice_client.disconnect.assert_awaited()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("left", sent.lower())

    async def test_leave_ends_session_and_disconnects(self) -> None:
        self.players.remove = AsyncMock(return_value=True)
        self.sessions.stop = AsyncMock(return_value=True)

        await self.cog.leave.callback(self.cog, self.ctx)

        self.players.remove.assert_awaited_once_with(1, disconnect=False)
        self.sessions.stop.assert_awaited_once_with(1)
        self.ctx.voice_client.disconnect.assert_awaited()
        sent = self.ctx.send.await_args.args[0]
        self.assertIn("stopped monitoring", sent.lower())


if __name__ == "__main__":
    unittest.main()
