"""User-facing music and voice-session commands.

Discord replies are Vietnamese (customer UI). Developer comments stay English.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from ..config import Settings
from ..media import MediaExtractionError, MediaService, format_duration
from ..player import PlayerManager
from ..session import SessionManager
from ..voice import get_or_connect_voice_client

log = logging.getLogger(__name__)


class MusicCog(commands.Cog, name="Music"):
    def __init__(
        self,
        bot: commands.Bot,
        settings: Settings,
        media: MediaService,
        players: PlayerManager,
        sessions: SessionManager,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.media = media
        self.players = players
        self.sessions = sessions

    @commands.command()
    @commands.guild_only()
    async def join(self, ctx: commands.Context[Any]) -> None:
        """Join the caller's voice channel and monitor that channel's text chat via TTS."""
        voice_client = await get_or_connect_voice_client(ctx, self.settings)
        if voice_client is None:
            return

        channel = voice_client.channel
        if channel is None:
            await ctx.send("Đã kết nối nhưng không gắn được kênh thoại.")
            return

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await ctx.send("Không thể bắt đầu phiên trên loại kênh này.")
            return

        already = self.sessions.is_active(ctx.guild.id)
        session = self.sessions.start(ctx.guild, channel)
        action = "Đang trong phiên" if already else "Đã vào"
        await ctx.send(
            f"{action}: đang theo dõi chat của **{discord.utils.escape_markdown(session.voice_channel_name)}**. "
            "Tin nhắn tại đó sẽ được đọc bằng TTS. Dùng "
            f"`{ctx.prefix}leave` để thoát."
        )

    @commands.command()
    @commands.guild_only()
    async def leave(self, ctx: commands.Context[Any]) -> None:
        """End the voice-chat session, stop music, and disconnect."""
        await self._leave_voice(ctx)

    @commands.command()
    @commands.guild_only()
    async def play(self, ctx: commands.Context[Any], *, query: str) -> None:
        """Queue a URL for playback and join the caller's voice channel."""
        await self._enqueue(ctx, query, "Đã xếp hàng")

    @commands.command(name="next")
    @commands.guild_only()
    async def add_next(self, ctx: commands.Context[Any], *, query: str) -> None:
        """Add a URL to the playback queue."""
        await self._enqueue(ctx, query, "Đã thêm vào hàng đợi")

    @commands.command()
    @commands.guild_only()
    async def pause(self, ctx: commands.Context[Any]) -> None:
        voice_client = ctx.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            await ctx.send("Đã tạm dừng.")
            return
        await ctx.send("Không có gì đang phát.")

    @commands.command()
    @commands.guild_only()
    async def resume(self, ctx: commands.Context[Any]) -> None:
        voice_client = ctx.voice_client
        if voice_client and voice_client.is_paused():
            voice_client.resume()
            await ctx.send("Đã tiếp tục.")
            return
        await ctx.send("Phát nhạc hiện không bị tạm dừng.")

    @commands.command()
    @commands.guild_only()
    async def skip(self, ctx: commands.Context[Any]) -> None:
        player = self.players.get(ctx.guild.id)
        if player and player.skip():
            await ctx.send("Đã bỏ qua.")
            return
        await ctx.send("Không có gì đang phát.")

    @commands.command(name="loop")
    @commands.guild_only()
    async def loop_track(self, ctx: commands.Context[Any]) -> None:
        player = self.players.get(ctx.guild.id)
        if not player or player.current is None:
            await ctx.send("Không có gì đang phát.")
            return
        enabled = player.toggle_loop()
        await ctx.send(
            f"Chế độ lặp {'đã bật' if enabled else 'đã tắt'}."
        )

    @commands.command()
    @commands.guild_only()
    async def stop(self, ctx: commands.Context[Any]) -> None:
        """Stop music and clear the queue. TTS session stays until ``leave``."""
        session_active = self.sessions.is_active(ctx.guild.id)
        # Keep the voice connection when a chat session is still running.
        had_player = await self.players.remove(
            ctx.guild.id,
            disconnect=not session_active,
        )

        if session_active:
            if had_player:
                await ctx.send(
                    "Đã dừng nhạc. Phiên chat TTS vẫn đang chạy — "
                    f"dùng `{ctx.prefix}leave` để thoát."
                )
            else:
                await ctx.send(
                    "Không có gì đang phát. Phiên chat TTS vẫn đang chạy — "
                    f"dùng `{ctx.prefix}leave` để thoát."
                )
            return

        voice_client = ctx.voice_client
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)
            await ctx.send("Đã dừng và rời kênh thoại.")
            return
        if had_player:
            await ctx.send("Đã dừng.")
            return
        await ctx.send("Không có gì đang phát.")

    @commands.command(name="search")
    async def youtube_search(self, ctx: commands.Context[Any], *, query: str) -> None:
        """Return the first five YouTube results for a query."""
        async with ctx.typing():
            try:
                entries = await self.media.search(query)
            except MediaExtractionError as exc:
                await ctx.send(str(exc))
                return

        if not entries:
            await ctx.send("Không tìm thấy kết quả.")
            return

        lines = ["**Kết quả tìm kiếm YouTube:**"]
        for index, entry in enumerate(entries, 1):
            title = discord.utils.escape_markdown(entry.title)
            duration = format_duration(entry.duration)
            suffix = f" ({duration})" if duration else ""
            lines.append(f"{index}. [{title}](<{entry.url}>){suffix}")
        await ctx.send("\n".join(lines))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Speak voice-channel text chat while a join session is active."""
        if message.guild is None or message.author.bot:
            return
        if not self.settings.tts_enabled:
            return

        session = self.sessions.get(message.guild.id)
        if session is None or not session.active:
            return

        # Prefer clean_content so mentions are readable speech.
        content = message.clean_content or message.content or ""
        author = getattr(message.author, "display_name", None) or str(message.author)
        session.offer_chat_message(
            author_is_bot=bool(message.author.bot),
            author_name=author,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            content=content,
            command_prefix=self.settings.command_prefix,
        )

    async def _enqueue(
        self,
        ctx: commands.Context[Any],
        query: str,
        confirmation: str,
    ) -> None:
        voice_client = await get_or_connect_voice_client(ctx, self.settings)
        if voice_client is None:
            return

        async with ctx.typing():
            try:
                track = await self.media.resolve(query)
            except MediaExtractionError as exc:
                await ctx.send(str(exc))
                return

        player = self.players.get_or_create(ctx.guild)
        position = await player.enqueue(track, ctx.channel)
        title = discord.utils.escape_markdown(track.title)
        await ctx.send(
            f"{confirmation}: **{title}** (vị trí hàng đợi {position})"
        )

    async def _leave_voice(self, ctx: commands.Context[Any]) -> None:
        """Stop music, end the chat session, and disconnect from voice."""
        had_player = await self.players.remove(ctx.guild.id, disconnect=False)
        had_session = await self.sessions.stop(ctx.guild.id)

        voice_client = ctx.voice_client
        disconnected = False
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)
            disconnected = True

        if had_player or had_session or disconnected:
            if had_session:
                await ctx.send("Đã rời kênh thoại và dừng theo dõi chat.")
            else:
                await ctx.send("Đã rời kênh thoại.")
            return
        await ctx.send("Bot chưa kết nối kênh thoại.")

    async def cog_command_error(
        self,
        ctx: commands.Context[Any],
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"Thiếu `{error.param.name}`. Dùng `{ctx.prefix}help {ctx.command}`."
            )
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("Lệnh này chỉ dùng được trong máy chủ.")
            return

        original = getattr(error, "original", error)
        log.error(
            "Command %s failed",
            ctx.command,
            exc_info=(type(original), original, original.__traceback__),
        )
        await ctx.send("Lệnh thất bại. Kiểm tra log của bot.")
