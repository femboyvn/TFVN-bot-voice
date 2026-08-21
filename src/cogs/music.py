"""User-facing music and voice-session commands.

Discord replies are Vietnamese (customer UI). Developer comments stay English.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import discord
from discord.ext import commands

from ..config import Settings
from ..media import (
    MediaBatch,
    MediaExtractionError,
    MediaService,
    QueuedTrack,
    SearchResult,
    format_duration,
    parse_jump_timestamp,
)
from ..music_ui import (
    PANEL_INTERACTION_TOKEN,
    AddInputResult,
    MusicPanelManager,
)
from ..player import (
    ControlResult,
    JumpResult,
    PlayerManager,
    PlayerSnapshot,
)
from ..session import SessionManager
from ..voice import (
    VoiceAccessError,
    connect_member_voice_client,
    disconnect_guild_voice_client,
    get_or_connect_voice_client,
    member_voice_channel,
    same_voice_channel_error,
)

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
        self.music_ui = MusicPanelManager(self)
        self._operation_locks: dict[int, asyncio.Lock] = {}
        self.players.add_state_listener(self.music_ui.on_player_state_change)

    @commands.command()
    @commands.guild_only()
    async def music(self, ctx: commands.Context[Any]) -> None:
        """Join the caller's room and post its shared music control panel."""
        async with self._operation_lock(ctx.guild.id):
            if not await self._preflight_voice_connection(ctx):
                return
            player = await self.players.get_or_create(ctx.guild)
            player.reserve_activity()
            try:
                voice_client = await self._connect_for_context(ctx)
                if voice_client is None:
                    return

                channel = voice_client.channel
                if channel is None:
                    await ctx.send("Đã kết nối nhưng không gắn được kênh thoại.")
                    return

                await self.music_ui.post_panel(
                    ctx.channel,
                    ctx.guild.id,
                    channel.id,
                )
            finally:
                player.release_activity()

    @commands.command()
    @commands.guild_only()
    async def join(self, ctx: commands.Context[Any]) -> None:
        """Join the caller's voice channel and monitor that channel's text chat via TTS."""
        async with self._operation_lock(ctx.guild.id):
            voice_client = await self._connect_for_context(ctx)
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
        async with self._operation_lock(ctx.guild.id):
            session = self.sessions.get(ctx.guild.id)
            expected_channel_id = (
                session.voice_channel_id
                if session is not None and session.active
                else None
            )
            if not await self._require_voice_control(
                ctx,
                expected_channel_id=expected_channel_id,
                allow_disconnected=expected_channel_id is not None,
            ):
                return
            await self._leave_voice(ctx)

    @commands.command(name="nameannounce")
    @commands.guild_only()
    async def name_announce(self, ctx: commands.Context[Any], mode: str) -> None:
        """Turn speaker-name TTS prefix on or off for the current session.

        Usage: ``!tfd nameannounce on`` / ``!tfd nameannounce off``
        Default for a new session is on (``"{name} nói {message}"``).
        """
        async with self._operation_lock(ctx.guild.id):
            session = self.sessions.get(ctx.guild.id)
            if session is None or not session.active:
                await ctx.send(
                    "Chưa có phiên chat TTS. Dùng "
                    f"`{ctx.prefix}join` trước."
                )
                return
            if not await self._require_voice_control(
                ctx,
                expected_channel_id=session.voice_channel_id,
                allow_disconnected=True,
            ):
                return

            normalized = mode.strip().lower()
            if normalized in {"on", "true", "1", "yes", "enable", "enabled"}:
                enabled = True
            elif normalized in {"off", "false", "0", "no", "disable", "disabled"}:
                enabled = False
            else:
                await ctx.send(
                    f"Dùng `{ctx.prefix}nameannounce on` hoặc "
                    f"`{ctx.prefix}nameannounce off`."
                )
                return

            session.set_name_announce(enabled)
        if enabled:
            await ctx.send(
                "Đã bật đọc tên người gửi "
                f"(`Tên nói …`). Dùng `{ctx.prefix}nameannounce off` để tắt."
            )
        else:
            await ctx.send(
                "Đã tắt đọc tên người gửi (chỉ đọc nội dung tin nhắn). "
                f"Dùng `{ctx.prefix}nameannounce on` để bật lại."
            )

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
        async with self._operation_lock(ctx.guild.id):
            if not await self._require_voice_control(ctx):
                return
            player = self.players.get(ctx.guild.id)
            result = player.pause() if player else ControlResult.NOT_PLAYING
        if result is ControlResult.SUCCESS:
            await ctx.send("Đã tạm dừng.")
            return
        if result is ControlResult.ALREADY_PAUSED:
            await ctx.send("Nhạc đang được tạm dừng.")
            return
        await ctx.send("Không có gì đang phát.")

    @commands.command()
    @commands.guild_only()
    async def resume(self, ctx: commands.Context[Any]) -> None:
        async with self._operation_lock(ctx.guild.id):
            if not await self._require_voice_control(ctx):
                return
            player = self.players.get(ctx.guild.id)
            result = player.resume() if player else ControlResult.NOT_PLAYING
        if result is ControlResult.SUCCESS:
            await ctx.send("Đã tiếp tục.")
            return
        await ctx.send("Phát nhạc hiện không bị tạm dừng.")

    @commands.command()
    @commands.guild_only()
    async def skip(self, ctx: commands.Context[Any]) -> None:
        async with self._operation_lock(ctx.guild.id):
            if not await self._require_voice_control(ctx):
                return
            player = self.players.get(ctx.guild.id)
            skipped = bool(player and player.skip())
        if skipped:
            await ctx.send("Đã bỏ qua.")
            return
        await ctx.send("Không có gì đang phát.")

    @commands.command()
    @commands.guild_only()
    async def jump(self, ctx: commands.Context[Any], timestamp: str) -> None:
        """Jump to an ``HH:MM:SS`` position in the current track."""
        offset = parse_jump_timestamp(timestamp)
        if offset is None:
            await ctx.send(f"Thời gian không hợp lệ. Dùng `{ctx.prefix}jump HH:MM:SS`.")
            return

        async with self._operation_lock(ctx.guild.id):
            if not await self._require_voice_control(ctx):
                return
            player = self.players.get(ctx.guild.id)
            result = player.jump(offset) if player else JumpResult.NOT_PLAYING
        if result is JumpResult.SUCCESS:
            await ctx.send(f"Đã chuyển đến {format_duration(offset)}.")
            return
        if result in {JumpResult.OUT_OF_RANGE, JumpResult.UNKNOWN_DURATION}:
            await ctx.send("Thời điểm đó không tồn tại trong bài hiện tại.")
            return
        await ctx.send("Không có gì đang phát.")

    @commands.command(name="loop")
    @commands.guild_only()
    async def loop_track(self, ctx: commands.Context[Any]) -> None:
        async with self._operation_lock(ctx.guild.id):
            if not await self._require_voice_control(ctx):
                return
            player = self.players.get(ctx.guild.id)
            if not player or player.current is None:
                enabled = None
            else:
                enabled = player.toggle_loop()
        if enabled is None:
            await ctx.send("Không có gì đang phát.")
            return
        await ctx.send(f"Chế độ lặp {'đã bật' if enabled else 'đã tắt'}.")

    @commands.command()
    @commands.guild_only()
    async def stop(self, ctx: commands.Context[Any]) -> None:
        """Stop music and clear the queue. TTS session stays until ``leave``."""
        async with self._operation_lock(ctx.guild.id):
            if not await self._require_voice_control(ctx):
                return
            session_active = self.sessions.is_active(ctx.guild.id)
            voice_client = ctx.guild.voice_client
            # Keep the voice connection when a chat session is still running.
            had_player = await self.players.remove(
                ctx.guild.id,
                disconnect=not session_active,
            )

            disconnected = False
            if (
                not session_active
                and not had_player
                and voice_client
                and voice_client.is_connected()
            ):
                disconnected = await disconnect_guild_voice_client(
                    ctx.guild,
                    expected_client=voice_client,
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

        if disconnected or (had_player and voice_client is not None):
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
    async def on_raw_message_delete(
        self,
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        """Forget a deleted controller without touching guild playback."""
        self.music_ui.drop_message(payload.message_id)

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
        # Reject outsiders before starting potentially expensive yt-dlp work,
        # then repeat the check under the guild operation lock before mutation.
        if not await self._preflight_voice_connection(ctx):
            return
        async with ctx.typing():
            try:
                batch = await self.media.prepare(query)
            except MediaExtractionError as exc:
                await ctx.send(str(exc))
                return

        async with self._operation_lock(ctx.guild.id):
            if not await self._preflight_voice_connection(ctx):
                return
            # Touch an existing player before connecting so its idle deadline
            # cannot tear down the voice client during this operation.
            player = await self.players.get_or_create(ctx.guild)
            player.reserve_activity()
            try:
                voice_client = await self._connect_for_context(ctx)
                if voice_client is None:
                    return
                await player.enqueue_many(batch.items, ctx.channel)
            finally:
                player.release_activity()
        await ctx.send(self._format_enqueue_confirmation(batch, confirmation))

    async def _connect_for_context(
        self,
        ctx: commands.Context[Any],
    ) -> discord.VoiceClient | None:
        """Connect a command caller without moving an occupied voice client."""
        session = self.sessions.get(ctx.guild.id)
        expected_channel_id = (
            session.voice_channel_id if session is not None and session.active else None
        )
        voice_client = await get_or_connect_voice_client(
            ctx,
            self.settings,
            expected_channel_id=expected_channel_id,
        )
        if voice_client is not None and voice_client.channel is not None:
            await self.music_ui.invalidate_if_channel_changed(
                ctx.guild.id,
                voice_client.channel.id,
            )
        return voice_client

    async def _require_voice_control(
        self,
        ctx: commands.Context[Any],
        *,
        expected_channel_id: int | None = None,
        allow_disconnected: bool = False,
    ) -> bool:
        error = same_voice_channel_error(
            ctx.guild,
            ctx.author,
            expected_channel_id=expected_channel_id,
            allow_disconnected=allow_disconnected,
        )
        if error is None:
            return True
        await ctx.send(error)
        return False

    async def _preflight_voice_connection(
        self,
        ctx: commands.Context[Any],
    ) -> bool:
        """Cheap caller/room validation that also permits a new connection."""
        member_channel = member_voice_channel(ctx.author)
        if member_channel is None:
            await ctx.send("Hãy vào một kênh thoại trước.")
            return False
        session = self.sessions.get(ctx.guild.id)
        expected_channel_id = (
            session.voice_channel_id
            if session is not None and session.active
            else getattr(member_channel, "id", None)
        )
        error = same_voice_channel_error(
            ctx.guild,
            ctx.author,
            expected_channel_id=expected_channel_id,
            allow_disconnected=True,
        )
        if error is None:
            return True
        await ctx.send(error)
        return False

    def _operation_lock(self, guild_id: int) -> asyncio.Lock:
        """Serialize user-visible voice/player mutations for one guild."""
        return self._operation_locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def _format_enqueue_confirmation(
        batch: MediaBatch,
        confirmation: str = "Đã thêm vào hàng đợi",
    ) -> str:
        count = len(batch.items)
        if not batch.is_playlist:
            title = discord.utils.escape_markdown(batch.items[0].title)
            return f"{confirmation}: **{title}**"

        details: list[str] = [f"{confirmation} **{count} bài** từ playlist."]
        if batch.skipped:
            details.append(f"Đã bỏ qua {batch.skipped} mục không khả dụng.")
        if batch.truncated:
            details.append("Chỉ kiểm tra 25 mục đầu tiên.")
        return " ".join(details)

    def ui_snapshot(self, guild_id: int) -> PlayerSnapshot | None:
        player = self.players.get(guild_id)
        return player.snapshot() if player is not None else None

    async def ui_ensure_panel_access(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        *,
        connect_if_missing: bool = False,
    ) -> bool:
        guild = interaction.guild
        if guild is None or guild.id != guild_id:
            await self._send_interaction_error(
                interaction,
                "Bảng điều khiển này chỉ dùng được trong máy chủ.",
            )
            return False
        error = same_voice_channel_error(
            guild,
            interaction.user,
            expected_channel_id=voice_channel_id,
            allow_disconnected=connect_if_missing,
        )
        if error is None:
            return True
        await self._send_interaction_error(interaction, error)
        return False

    async def ui_add_input(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        value: str,
    ) -> AddInputResult:
        normalized = value.strip()
        parsed = urlparse(normalized)
        is_url = parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
        try:
            if not is_url:
                results = await self.media.search(normalized, limit=5)
                if not results:
                    return AddInputResult(message="Không tìm thấy kết quả.")
                return AddInputResult(
                    message="Chọn một kết quả để thêm vào hàng đợi:",
                    results=tuple(results),
                )

            batch = await self.media.prepare(normalized)
            error = await self._enqueue_interaction_batch(
                interaction,
                guild_id,
                voice_channel_id,
                batch,
            )
            if error:
                return AddInputResult(message=error)
            return AddInputResult(message=self._format_enqueue_confirmation(batch))
        except MediaExtractionError as exc:
            return AddInputResult(message=str(exc))
        except Exception:
            log.exception("Could not add music from panel in guild %s", guild_id)
            return AddInputResult(message="Không thể thêm nhạc. Hãy thử lại.")

    async def ui_enqueue_search_result(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        result: SearchResult,
    ) -> str:
        item = QueuedTrack(result.title, result.url, result.duration)
        batch = MediaBatch(items=(item,))
        try:
            error = await self._enqueue_interaction_batch(
                interaction,
                guild_id,
                voice_channel_id,
                batch,
            )
        except Exception:
            log.exception("Could not enqueue selected result in guild %s", guild_id)
            return "Không thể thêm kết quả đã chọn."
        if error:
            return error
        return self._format_enqueue_confirmation(batch)

    async def ui_pause(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            result = player.pause() if player else ControlResult.NOT_PLAYING
        if result is ControlResult.SUCCESS:
            return "Đã tạm dừng."
        if result is ControlResult.ALREADY_PAUSED:
            return "Nhạc đang được tạm dừng."
        return "Không có gì đang phát."

    async def ui_resume(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            result = player.resume() if player else ControlResult.NOT_PLAYING
        if result is ControlResult.SUCCESS:
            return "Đã tiếp tục."
        return "Phát nhạc hiện không bị tạm dừng."

    async def ui_skip(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            skipped = bool(player and player.skip())
        return "Đã bỏ qua." if skipped else "Không có gì đang phát."

    async def ui_toggle_loop(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            if player is None or player.current is None:
                return "Không có gì đang phát."
            enabled = player.toggle_loop()
        return f"Chế độ lặp {'đã bật' if enabled else 'đã tắt'}."

    async def ui_jump(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        offset: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            result = player.jump(offset) if player else JumpResult.NOT_PLAYING
        if result is JumpResult.SUCCESS:
            return f"Đã chuyển đến {format_duration(offset)}."
        if result in {JumpResult.OUT_OF_RANGE, JumpResult.UNKNOWN_DURATION}:
            return "Thời điểm đó không tồn tại trong bài hiện tại."
        return "Không có gì đang phát."

    async def ui_clear_queue(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            removed = await player.clear_queue() if player else 0
        if not removed:
            return "Hàng đợi đã trống."
        return f"Đã xóa {removed} bài đang chờ."

    async def ui_stop(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
            )
            if error:
                return error
            guild = interaction.guild
            if guild is None:
                return "Bảng điều khiển này không còn hợp lệ."
            session_active = self.sessions.is_active(guild_id)
            voice_client = guild.voice_client
            had_player = await self.players.remove(
                guild_id,
                disconnect=not session_active,
            )
            disconnected = False
            if (
                not session_active
                and not had_player
                and voice_client
                and voice_client.is_connected()
            ):
                disconnected = await disconnect_guild_voice_client(
                    guild,
                    expected_client=voice_client,
                )
        if session_active:
            return (
                "Đã dừng nhạc. Phiên chat TTS vẫn đang chạy."
                if had_player
                else "Không có gì đang phát. Phiên chat TTS vẫn đang chạy."
            )

        if disconnected or (had_player and voice_client is not None):
            return "Đã dừng và rời kênh thoại."
        return "Đã dừng." if had_player else "Không có gì đang phát."

    async def _enqueue_interaction_batch(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        batch: MediaBatch,
    ) -> str | None:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
                allow_disconnected=True,
            )
            if error:
                return error
            guild = interaction.guild
            if guild is None:
                return "Bảng điều khiển này không còn hợp lệ."
            # Reserve the player so even a deliberately short idle timeout
            # cannot retire it during a slow voice handshake.
            player = await self.players.get_or_create(guild)
            player.reserve_activity()
            try:
                try:
                    await connect_member_voice_client(
                        guild,
                        interaction.user,
                        self.settings,
                        expected_channel_id=voice_channel_id,
                    )
                except VoiceAccessError as exc:
                    return str(exc)
                channel = interaction.channel
                if channel is None:
                    return "Không thể xác định kênh để thông báo bài hát."
                await player.enqueue_many(batch.items, channel)
                return None
            finally:
                player.release_activity()

    def _interaction_access_error(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        *,
        allow_disconnected: bool = False,
    ) -> str | None:
        guild = interaction.guild
        if guild is None or guild.id != guild_id:
            return "Bảng điều khiển này không còn hợp lệ."
        extras = getattr(interaction, "extras", None)
        expected_panel = (
            extras.get(PANEL_INTERACTION_TOKEN)
            if isinstance(extras, dict)
            else None
        )
        if expected_panel is not None:
            record = self.music_ui.get(guild_id)
            if record is None or record.view is not expected_panel:
                return "Bảng điều khiển này đã được thay thế hoặc xóa."
        return same_voice_channel_error(
            guild,
            interaction.user,
            expected_channel_id=voice_channel_id,
            allow_disconnected=allow_disconnected,
        )

    @staticmethod
    async def _send_interaction_error(
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def close(self) -> None:
        self.players.remove_state_listener(self.music_ui.on_player_state_change)
        await self.music_ui.close()

    async def _leave_voice(self, ctx: commands.Context[Any]) -> None:
        """Stop music, end the chat session, and disconnect from voice."""
        voice_client = ctx.guild.voice_client
        had_player = await self.players.remove(ctx.guild.id, disconnect=False)
        had_session = await self.sessions.stop(ctx.guild.id)

        disconnected = False
        if voice_client and voice_client.is_connected():
            disconnected = await disconnect_guild_voice_client(
                ctx.guild,
                expected_client=voice_client,
            )

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
