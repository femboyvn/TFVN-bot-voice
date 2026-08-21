from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from src.config import Settings
from src.voice import (
    VoiceAccessError,
    connect_member_voice_client,
    same_voice_channel_error,
)


def _member_in(channel: Mock) -> Mock:
    member = Mock()
    member.voice.channel = channel
    return member


class VoiceRoomAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(discord_token="test-token")
        self.guild = Mock()
        self.guild.id = 1
        self.channel = Mock()
        self.channel.id = 10
        self.member = _member_in(self.channel)

    async def test_reuses_connection_for_member_in_same_room(self) -> None:
        voice_client = Mock()
        voice_client.channel = self.channel
        voice_client.is_connected.return_value = True
        self.guild.voice_client = voice_client

        result = await connect_member_voice_client(
            self.guild,
            self.member,
            self.settings,
        )

        self.assertIs(result, voice_client)
        voice_client.move_to.assert_not_called()

    async def test_never_moves_connection_for_member_in_another_room(self) -> None:
        other_channel = Mock()
        other_channel.id = 20
        voice_client = Mock()
        voice_client.channel = other_channel
        voice_client.is_connected.return_value = True
        self.guild.voice_client = voice_client

        with self.assertRaises(VoiceAccessError):
            await connect_member_voice_client(
                self.guild,
                self.member,
                self.settings,
            )

        voice_client.move_to.assert_not_called()

    async def test_connects_when_bot_is_disconnected(self) -> None:
        self.guild.voice_client = None
        connected = Mock()
        self.channel.connect = AsyncMock(return_value=connected)

        result = await connect_member_voice_client(
            self.guild,
            self.member,
            self.settings,
            expected_channel_id=10,
        )

        self.assertIs(result, connected)
        self.channel.connect.assert_awaited_once_with(
            timeout=self.settings.voice_connect_timeout,
            reconnect=False,
        )

    async def test_concurrent_same_room_connects_share_one_handshake(self) -> None:
        self.guild.voice_client = None
        connected = Mock()
        connected.channel = self.channel
        connected.is_connected.return_value = True
        started = asyncio.Event()
        release = asyncio.Event()

        async def connect(*, timeout: float, reconnect: bool) -> Mock:
            self.guild.voice_client = connected
            started.set()
            await release.wait()
            return connected

        self.channel.connect = AsyncMock(side_effect=connect)
        first = asyncio.create_task(
            connect_member_voice_client(self.guild, self.member, self.settings)
        )
        await started.wait()
        second = asyncio.create_task(
            connect_member_voice_client(self.guild, self.member, self.settings)
        )
        await asyncio.sleep(0)
        release.set()

        results = await asyncio.gather(first, second)

        self.assertEqual(results, [connected, connected])
        self.channel.connect.assert_awaited_once()

    def test_same_room_check_denies_outsider_and_allows_room_member(self) -> None:
        voice_client = Mock()
        voice_client.channel = self.channel
        voice_client.is_connected.return_value = True
        self.guild.voice_client = voice_client

        self.assertIsNone(same_voice_channel_error(self.guild, self.member))

        other_channel = Mock()
        other_channel.id = 20
        outsider = _member_in(other_channel)
        self.assertIsNotNone(same_voice_channel_error(self.guild, outsider))

    def test_bound_panel_rejects_member_in_a_different_room(self) -> None:
        self.guild.voice_client = None

        error = same_voice_channel_error(
            self.guild,
            self.member,
            expected_channel_id=999,
        )

        self.assertIsNotNone(error)
        self.assertIn("bảng điều khiển", error)

    def test_bound_panel_can_validate_member_while_bot_is_disconnected(self) -> None:
        self.guild.voice_client = None

        error = same_voice_channel_error(
            self.guild,
            self.member,
            expected_channel_id=10,
            allow_disconnected=True,
        )

        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
