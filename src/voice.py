"""Discord voice connection helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

from .config import Settings

log = logging.getLogger(__name__)
_VOICE_CONNECT_LOCKS: dict[int, asyncio.Lock] = {}


class VoiceAccessError(RuntimeError):
    """A user-facing reason a member cannot join or control this voice client."""


def member_voice_channel(member: object) -> object | None:
    """Return a member's current voice channel without assuming a concrete Member."""
    voice_state = getattr(member, "voice", None)
    return getattr(voice_state, "channel", None)


def same_voice_channel_error(
    guild: discord.Guild,
    member: object,
    *,
    expected_channel_id: int | None = None,
    allow_disconnected: bool = False,
) -> str | None:
    """Validate that *member* can control the guild's connected voice client."""
    member_channel = member_voice_channel(member)
    if member_channel is None:
        return "Hãy vào kênh thoại của bot trước."

    member_channel_id = getattr(member_channel, "id", None)
    if expected_channel_id is not None and member_channel_id != expected_channel_id:
        return "Bạn phải ở đúng kênh thoại của bảng điều khiển này."

    voice_client = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        if allow_disconnected and expected_channel_id is not None:
            return None
        return "Bot chưa kết nối kênh thoại."

    bot_channel = voice_client.channel
    if bot_channel is None or getattr(bot_channel, "id", None) != member_channel_id:
        return "Chỉ thành viên trong kênh thoại của bot mới có thể điều khiển nhạc."
    return None


async def connect_member_voice_client(
    guild: discord.Guild,
    member: object,
    settings: Settings,
    *,
    expected_channel_id: int | None = None,
) -> discord.VoiceClient:
    """Return a same-room voice client or connect to the member's channel.

    An existing connection is never moved. This prevents a member in another
    channel from taking over a room's music or TTS session.
    """
    lock = _VOICE_CONNECT_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        return await _connect_member_voice_client_locked(
            guild,
            member,
            settings,
            expected_channel_id=expected_channel_id,
        )


async def disconnect_guild_voice_client(
    guild: discord.Guild,
    *,
    expected_client: discord.VoiceClient | None = None,
) -> bool:
    """Serialize disconnects with connects and avoid closing a replaced client."""
    lock = _VOICE_CONNECT_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        voice_client = guild.voice_client
        if voice_client is None:
            return False
        if expected_client is not None and voice_client is not expected_client:
            return False
        if not voice_client.is_connected():
            return False
        await voice_client.disconnect(force=True)
        return True


async def _connect_member_voice_client_locked(
    guild: discord.Guild,
    member: object,
    settings: Settings,
    *,
    expected_channel_id: int | None,
) -> discord.VoiceClient:
    target_channel = member_voice_channel(member)
    if target_channel is None:
        raise VoiceAccessError("Hãy vào một kênh thoại trước.")

    target_channel_id = getattr(target_channel, "id", None)
    if expected_channel_id is not None and target_channel_id != expected_channel_id:
        raise VoiceAccessError(
            "Bạn phải ở đúng kênh thoại của bảng điều khiển này."
        )

    voice_client = guild.voice_client
    if voice_client and voice_client.is_connected():
        connected_channel = voice_client.channel
        if (
            connected_channel is None
            or getattr(connected_channel, "id", None) != target_channel_id
        ):
            raise VoiceAccessError(
                "Bot đang ở kênh thoại khác. Hãy vào cùng kênh để điều khiển."
            )
        return voice_client

    if voice_client:
        await voice_client.disconnect(force=True)

    last_error: Exception | None = None
    for attempt in range(1, settings.voice_connect_retries + 1):
        try:
            return await target_channel.connect(
                timeout=settings.voice_connect_timeout,
                reconnect=False,
            )
        except discord.ConnectionClosed as exc:
            last_error = exc
            if exc.code == 4017:
                raise VoiceAccessError(
                    "Kết nối thoại cần hỗ trợ Discord DAVE/E2EE. "
                    "Hãy nâng cấp discord.py[voice] 2.7 trở lên rồi khởi động lại bot."
                ) from exc
        except (discord.ClientException, asyncio.TimeoutError, OSError) as exc:
            last_error = exc

        log.warning(
            "Voice connection attempt %s/%s failed for guild %s: %s",
            attempt,
            settings.voice_connect_retries,
            guild.id,
            last_error,
        )
        if attempt < settings.voice_connect_retries:
            await asyncio.sleep(attempt * 2)

    raise VoiceAccessError(
        f"Không thể kết nối thoại sau {settings.voice_connect_retries} lần thử."
    )


async def get_or_connect_voice_client(
    ctx: commands.Context[Any],
    settings: Settings,
    *,
    expected_channel_id: int | None = None,
) -> discord.VoiceClient | None:
    try:
        return await connect_member_voice_client(
            ctx.guild,
            ctx.author,
            settings,
            expected_channel_id=expected_channel_id,
        )
    except VoiceAccessError as exc:
        await ctx.send(str(exc))
        return None
