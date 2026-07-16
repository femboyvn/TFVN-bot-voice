"""Discord voice connection helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

from .config import Settings

log = logging.getLogger(__name__)


async def get_or_connect_voice_client(
    ctx: commands.Context[Any],
    settings: Settings,
) -> discord.VoiceClient | None:
    voice_state = getattr(ctx.author, "voice", None)
    if not voice_state or not voice_state.channel:
        await ctx.send("Hãy vào một kênh thoại trước.")
        return None

    target_channel = voice_state.channel
    voice_client = ctx.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel and voice_client.channel.id != target_channel.id:
            await voice_client.move_to(target_channel)
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
                await ctx.send(
                    "Kết nối thoại cần hỗ trợ Discord DAVE/E2EE. "
                    "Hãy nâng cấp discord.py[voice] 2.7 trở lên rồi khởi động lại bot."
                )
                return None
        except (discord.ClientException, asyncio.TimeoutError, OSError) as exc:
            last_error = exc

        log.warning(
            "Voice connection attempt %s/%s failed for guild %s: %s",
            attempt,
            settings.voice_connect_retries,
            ctx.guild.id,
            last_error,
        )
        if attempt < settings.voice_connect_retries:
            await asyncio.sleep(attempt * 2)

    await ctx.send(
        f"Không thể kết nối thoại sau {settings.voice_connect_retries} lần thử."
    )
    return None
