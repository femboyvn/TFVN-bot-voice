from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from src.config import Settings
from src.voice import (
    VoiceAccessError,
    _abandon_voice_client,
    channel_was_deleted,
    connect_member_voice_client,
    connect_voice_channel,
    disconnect_guild_voice_client,
    live_voice_channel_id,
    receiving_voice_client_class,
    same_voice_channel_error,
    voice_client_is_live,
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

    async def test_connect_voice_channel_reuses_same_room(self) -> None:
        voice_client = Mock()
        voice_client.channel = self.channel
        voice_client.is_connected.return_value = True
        self.guild.voice_client = voice_client

        result = await connect_voice_channel(self.guild, self.channel, self.settings)

        self.assertIs(result, voice_client)
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
        self.assertTrue(issubclass(receiving_voice_client_class(), discord.VoiceClient))
        self.channel.connect.assert_awaited_once_with(
            timeout=self.settings.voice_connect_timeout,
            reconnect=False,
            cls=receiving_voice_client_class(),
        )

    async def test_concurrent_same_room_connects_share_one_handshake(self) -> None:
        self.guild.voice_client = None
        connected = Mock()
        connected.channel = self.channel
        connected.is_connected.return_value = True
        started = asyncio.Event()
        release = asyncio.Event()

        async def connect(*, timeout: float, reconnect: bool, cls: object) -> Mock:
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

    def test_deleted_channel_client_is_not_live(self) -> None:
        stale = Mock()
        stale.id = 10
        voice_client = Mock()
        voice_client.channel = stale
        voice_client.is_connected.return_value = True
        self.guild.voice_client = voice_client
        self.guild.get_channel = Mock(return_value=None)

        self.assertFalse(voice_client_is_live(self.guild, voice_client))
        self.assertIsNone(live_voice_channel_id(self.guild))

        other = Mock()
        other.id = 20
        member = _member_in(other)
        error = same_voice_channel_error(self.guild, member)
        self.assertIsNotNone(error)
        self.assertIn("chưa kết nối", error.lower())

    async def test_connects_when_stale_client_points_at_deleted_channel(self) -> None:
        stale = Mock()
        stale.id = 10
        voice_client = Mock()
        voice_client.channel = stale
        voice_client.is_connected.return_value = True
        voice_client.disconnect = AsyncMock()
        self.guild.voice_client = voice_client
        self.guild.get_channel = Mock(return_value=None)

        new_channel = Mock()
        new_channel.id = 20
        connected = Mock()
        new_channel.connect = AsyncMock(return_value=connected)
        member = _member_in(new_channel)

        result = await connect_member_voice_client(
            self.guild,
            member,
            self.settings,
        )

        self.assertIs(result, connected)
        voice_client.disconnect.assert_awaited_once_with(force=True)
        new_channel.connect.assert_awaited_once()

    async def test_disconnect_abandons_client_when_disconnect_hangs(self) -> None:
        voice_client = Mock()
        voice_client.channel = self.channel
        voice_client.is_connected.return_value = True
        voice_client.cleanup = Mock()
        self.guild.voice_client = voice_client
        self.guild.get_channel = Mock(return_value=self.channel)
        self.guild.change_voice_state = AsyncMock()

        async def hang(*, force: bool = False) -> None:
            await asyncio.Event().wait()

        voice_client.disconnect = hang
        with patch("src.voice._DISCONNECT_TIMEOUT_SECONDS", 0.05):
            result = await asyncio.wait_for(
                disconnect_guild_voice_client(self.guild),
                timeout=1,
            )

        self.assertTrue(result)
        voice_client.cleanup.assert_called_once_with()
        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        self.assertIsNone(self.guild.voice_client)

    async def test_force_disconnect_drops_client_when_gateway_leave_hangs(self) -> None:
        voice_client = Mock()
        voice_client.channel = self.channel
        voice_client.is_connected.return_value = True
        voice_client.disconnect = AsyncMock()
        voice_client.cleanup = Mock()
        runner = Mock()
        voice_client._connection._runner = runner
        self.guild.voice_client = voice_client
        self.guild.get_channel = Mock(return_value=None)

        async def hang(*, channel: object) -> None:
            await asyncio.Event().wait()

        self.guild.change_voice_state = hang
        with patch("src.voice._FORCE_DISCONNECT_TIMEOUT_SECONDS", 0.05):
            result = await asyncio.wait_for(
                disconnect_guild_voice_client(self.guild, force=True),
                timeout=1,
            )

        self.assertTrue(result)
        voice_client.disconnect.assert_not_awaited()
        voice_client.cleanup.assert_called_once_with()
        runner.cancel.assert_called_once_with()
        self.assertIsNone(self.guild.voice_client)

    async def test_force_disconnect_abandons_hung_disconnect(self) -> None:
        voice_client = Mock()
        voice_client.channel = self.channel
        voice_client.is_connected.return_value = True
        voice_client.cleanup = Mock()
        self.guild.voice_client = voice_client
        self.guild.get_channel = Mock(return_value=None)
        self.guild.change_voice_state = AsyncMock()

        async def hang(*, force: bool = False) -> None:
            await asyncio.Event().wait()

        voice_client.disconnect = hang
        with patch("src.voice._FORCE_DISCONNECT_TIMEOUT_SECONDS", 0.05):
            result = await asyncio.wait_for(
                disconnect_guild_voice_client(self.guild, force=True),
                timeout=1,
            )

        self.assertTrue(result)
        voice_client.cleanup.assert_called_once_with()
        self.guild.change_voice_state.assert_awaited_once_with(channel=None)
        self.assertIsNone(self.guild.voice_client)
        self.assertFalse(
            await disconnect_guild_voice_client(self.guild, force=True)
        )

    async def test_connect_abandons_hung_client_on_deleted_channel(self) -> None:
        voice_client = Mock()
        voice_client.channel = self.channel
        voice_client.is_connected.return_value = True
        voice_client.cleanup = Mock()
        self.guild.voice_client = voice_client
        self.guild.get_channel = Mock(return_value=None)

        async def hang(*, force: bool = False) -> None:
            await asyncio.Event().wait()

        voice_client.disconnect = hang
        new_channel = Mock()
        new_channel.id = 20
        connected = Mock()
        new_channel.connect = AsyncMock(return_value=connected)

        with patch("src.voice._FORCE_DISCONNECT_TIMEOUT_SECONDS", 0.05):
            result = await asyncio.wait_for(
                connect_member_voice_client(
                    self.guild,
                    _member_in(new_channel),
                    self.settings,
                ),
                timeout=1,
            )

        self.assertIs(result, connected)
        voice_client.cleanup.assert_called_once_with()
        new_channel.connect.assert_awaited_once()

    async def test_channel_was_deleted_uses_cache_before_fetch(self) -> None:
        self.guild.get_channel = Mock(return_value=None)
        self.guild.fetch_channel = AsyncMock()

        self.assertTrue(await channel_was_deleted(self.guild, 10))

        self.guild.fetch_channel.assert_not_awaited()

    async def test_channel_was_deleted_confirms_stale_cache_with_not_found(self) -> None:
        self.guild.get_channel = Mock(return_value=self.channel)
        response = Mock(status=404, reason="Not Found")
        self.guild.fetch_channel = AsyncMock(
            side_effect=discord.NotFound(response, "Unknown Channel")
        )

        self.assertTrue(await channel_was_deleted(self.guild, 10))

    async def test_channel_was_deleted_keeps_room_when_lookup_fails(self) -> None:
        self.guild.get_channel = Mock(return_value=self.channel)
        response = Mock(status=500, reason="Server Error")
        self.guild.fetch_channel = AsyncMock(
            side_effect=discord.HTTPException(response, "boom")
        )

        self.assertFalse(await channel_was_deleted(self.guild, 10))

    async def test_channel_was_deleted_is_false_when_fetch_returns_channel(self) -> None:
        self.guild.get_channel = Mock(return_value=self.channel)
        self.guild.fetch_channel = AsyncMock(return_value=self.channel)

        self.assertFalse(await channel_was_deleted(self.guild, 10))

    async def test_timed_out_rejoin_stops_and_declares_disconnect(self) -> None:
        self.settings = Settings(
            discord_token="test-token",
            voice_connect_timeout=0.05,
            voice_connect_retries=3,
        )
        voice_client = Mock()
        voice_client.channel = self.channel
        voice_client.is_connected.return_value = False
        voice_client.cleanup = Mock()

        async def disconnect_hang(*, force: bool = False) -> None:
            await asyncio.Event().wait()

        attempts = 0

        async def hang(*, timeout: float, reconnect: bool, cls: object) -> Mock:
            nonlocal attempts
            attempts += 1
            self.guild.voice_client = voice_client
            await asyncio.Event().wait()
            return voice_client

        voice_client.disconnect = disconnect_hang
        self.channel.connect = hang
        self.guild.voice_client = None
        self.guild.get_channel = Mock(return_value=self.channel)

        with (
            patch("src.voice._CONNECT_ATTEMPT_GRACE_SECONDS", 0.0),
            patch("src.voice._FORCE_DISCONNECT_TIMEOUT_SECONDS", 0.05),
        ):
            with self.assertRaises(VoiceAccessError) as caught:
                await asyncio.wait_for(
                    connect_member_voice_client(
                        self.guild,
                        self.member,
                        self.settings,
                    ),
                    timeout=1,
                )

        self.assertEqual(attempts, 1)
        self.assertIn("ngắt kết nối", str(caught.exception).lower())
        self.assertIsNone(self.guild.voice_client)

    async def test_timeout_does_not_start_another_rejoin_attempt(self) -> None:
        self.settings = Settings(
            discord_token="test-token",
            voice_connect_timeout=0.05,
            voice_connect_retries=3,
        )
        self.guild.voice_client = None
        self.channel.connect = AsyncMock(side_effect=asyncio.TimeoutError)

        with (
            patch("src.voice._CONNECT_ATTEMPT_GRACE_SECONDS", 0.0),
            patch("src.voice.asyncio.sleep", new=AsyncMock()),
        ):
            with self.assertRaises(VoiceAccessError) as caught:
                await connect_member_voice_client(
                    self.guild,
                    self.member,
                    self.settings,
                )

        self.assertEqual(self.channel.connect.await_count, 1)
        self.assertIn("hết thời gian", str(caught.exception).lower())

    async def test_failed_rejoin_attempts_stop_and_declare_disconnect(self) -> None:
        self.settings = Settings(
            discord_token="test-token",
            voice_connect_retries=2,
        )
        self.guild.voice_client = None
        self.channel.connect = AsyncMock(
            side_effect=discord.ClientException("voice unavailable")
        )

        with patch("src.voice.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(VoiceAccessError) as caught:
                await connect_member_voice_client(
                    self.guild,
                    self.member,
                    self.settings,
                )

        self.assertEqual(self.channel.connect.await_count, 2)
        message = str(caught.exception).lower()
        self.assertIn("2 lần thử", message)
        self.assertIn("ngắt kết nối", message)

    def test_abandon_stops_receive_and_playback(self) -> None:
        voice_client = Mock()
        voice_client.stop_listening = Mock()
        voice_client.stop = Mock()
        voice_client.cleanup = Mock()
        guild = Mock()
        guild.id = 4
        guild.voice_client = voice_client

        _abandon_voice_client(guild, voice_client)

        voice_client.stop_listening.assert_called_once_with()
        voice_client.stop.assert_called_once_with()
        voice_client.cleanup.assert_called_once_with()
        self.assertIsNone(guild.voice_client)


if __name__ == "__main__":
    unittest.main()
