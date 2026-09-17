"""User-facing music and voice-session commands.

Discord replies are Vietnamese (customer UI). Developer comments stay English.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from collections.abc import Callable
from pathlib import Path
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
    AudioSettingsValidationError,
    MusicPanelManager,
    format_audio_settings,
    format_panel_bump_interval,
    parse_name_announce,
    parse_panel_bump_interval,
)
from ..player import (
    ControlResult,
    GuildAudioSettings,
    JumpResult,
    LoopMode,
    PlayerManager,
    PlayerSnapshot,
)
from ..playlists import (
    SERVER_OWNER_ID,
    PlaybackSession,
    PlaylistError,
    PlaylistStore,
    SavedPlaylist,
)
from ..session import SessionManager
from ..soundboard import (
    InstantHit,
    SoundboardEntry,
    SoundboardError,
    SoundboardService,
    format_clip_duration,
)
from ..soundboard_ui import SoundboardView
from ..spotify import is_spotify_input
from ..voice import (
    VoiceAccessError,
    connect_member_voice_client,
    connect_voice_channel,
    disconnect_guild_voice_client,
    get_or_connect_voice_client,
    guild_channel_exists,
    live_voice_channel_id,
    member_voice_channel,
    same_voice_channel_error,
    voice_client_is_live,
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
        soundboard: SoundboardService | None = None,
        playlists: PlaylistStore | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.media = media
        self.players = players
        self.sessions = sessions
        self.soundboard_service = soundboard or SoundboardService.from_settings(
            settings
        )
        self.playlists = playlists or PlaylistStore(
            Path(str(settings.playlist_db_path)),
            max_per_user=settings.playlist_max_per_user,
            max_tracks=settings.playlist_max_tracks,
            max_per_server=settings.playlist_max_server,
        )
        self.music_ui = MusicPanelManager(
            self,
            command_prefix=settings.command_prefix,
        )
        self._operation_locks: dict[int, asyncio.Lock] = {}
        self._restored = False
        self._restoring = False
        self.players.add_state_listener(self.music_ui.on_player_state_change)
        self.players.add_state_listener(self._persist_playback_state)

    @commands.hybrid_command()
    @commands.guild_only()
    async def music(self, ctx: commands.Context[Any]) -> None:
        """Join the caller's room and post its shared music control panel."""
        async with self._operation_lock(ctx.guild.id):
            await self._join_and_post_panel(ctx)

    @commands.hybrid_command()
    @commands.guild_only()
    async def soundboard(self, ctx: commands.Context[Any]) -> None:
        """Join the caller's room, post the music panel, and open the soundboard."""
        async with self._operation_lock(ctx.guild.id):
            channel_id = await self._join_and_post_panel(ctx)
            if channel_id is None:
                return
            try:
                entries = await self.soundboard_service.list(ctx.guild.id)
            except SoundboardError as exc:
                await ctx.send(str(exc))
                return
        view = SoundboardView(self, ctx.guild.id, channel_id, entries)
        message = await ctx.send(embed=view.render_embed(), view=view)
        view.message = message

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
            await self.music_ui.refresh(ctx.guild.id)
        await ctx.send(
            f"{action}: đang theo dõi chat của **{discord.utils.escape_markdown(session.voice_channel_name)}**. "
            "Tin nhắn tại đó sẽ được đọc bằng TTS. Dùng "
            f"`{ctx.prefix}leave` để thoát."
        )

    @commands.hybrid_command(name="playlist")
    @commands.guild_only()
    async def playlist(
        self, ctx: commands.Context[Any], *, arguments: str = "",
    ) -> None:
        """Manage personal or server playlists; quote names containing spaces."""
        try:
            args = shlex.split(arguments)
        except ValueError:
            await ctx.send("Dấu ngoặc kép chưa đóng. Hãy kiểm tra tên danh sách.")
            return
        server = bool(args and args[0].lower() == "server")
        if server:
            args = args[1:]
        owner_id = SERVER_OWNER_ID if server else ctx.author.id
        action = args.pop(0).lower() if args else "list"
        try:
            if action in {"list", "show"}:
                if action == "show" and not args:
                    raise PlaylistError("Hãy nhập tên danh sách cần xem.")
                from ..playlist_ui import PlaylistLauncher

                view = PlaylistLauncher(
                    self, ctx.guild.id, ctx.author.id,
                    ref=" ".join(args) if action == "show" else "",
                    server=server,
                )
                view.message = await ctx.send(
                    "Bấm để mở danh sách chung của máy chủ." if server
                    else "Bấm để mở danh sách phát riêng của bạn.",
                    view=view,
                )
                return
            if action == "play":
                if not await self._preflight_voice_connection(ctx):
                    return
                saved = await self.playlists.get(
                    ctx.guild.id, owner_id, " ".join(args),
                )
                self._require_playlist_tracks(saved)
                if not await self._enqueue_prepared(
                    ctx, MediaBatch(items=saved.tracks, is_playlist=True),
                ):
                    return
                message = f"Đã thêm {len(saved.tracks)} bài vào hàng đợi."
            elif action == "save":
                async with self._operation_lock(ctx.guild.id):
                    if not await self._require_voice_control(ctx):
                        return
                    tracks = self._playlist_queue_snapshot(ctx.guild.id)
                    if server:
                        self._require_server_playlist_manage(ctx.author)
                    saved = await self.playlists.create(
                        ctx.guild.id, owner_id, " ".join(args), tracks,
                    )
                message = self._playlist_saved_message(saved)
            else:
                # The final argument may contain spaces without quoting;
                # references preceding it should be quoted when necessary.
                if action == "create" and args:
                    args = [" ".join(args)]
                elif action in {"add", "rename"} and len(args) >= 2:
                    args = [args[0], " ".join(args[1:])]
                async with ctx.typing():
                    if server and action in {"create", "rename", "delete"}:
                        self._require_server_playlist_manage(ctx.author)
                    message = await self._edit_playlist(
                        ctx.guild.id, owner_id, action, args,
                    )
        except (PlaylistError, MediaExtractionError) as exc:
            message = str(exc)
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _require_server_playlist_manage(user: object) -> None:
        permissions = getattr(user, "guild_permissions", None)
        if not getattr(permissions, "manage_guild", False):
            raise PlaylistError(
                "Chỉ người có quyền Quản lý máy chủ mới sửa danh sách chung."
            )

    @staticmethod
    def _loop_message(mode: LoopMode) -> str:
        if mode is LoopMode.TRACK:
            return "Đã bật lặp bài hiện tại."
        if mode is LoopMode.QUEUE:
            return "Đã bật lặp cả hàng đợi."
        return "Đã tắt lặp."

    @staticmethod
    def _human_voice_members(channel: object) -> list:
        members = getattr(channel, "members", ()) or ()
        if not isinstance(members, (list, tuple)):
            return []
        return [
            member for member in members if not getattr(member, "bot", False)
        ]

    def _voice_voter_count(self, guild: discord.Guild, voice_channel_id: int) -> int:
        channel = guild.get_channel(voice_channel_id)
        return max(1, len(self._human_voice_members(channel)))

    def _apply_vote_skip(self, player: object, user_id: int, voter_count: int) -> str:
        status, votes, needed = player.vote_skip(user_id, voter_count=voter_count)
        if status == "skipped":
            return "Đã bỏ qua."
        if status == "already":
            return f"Bạn đã bỏ phiếu rồi ({votes}/{needed})."
        if status == "voted":
            return f"Đã ghi phiếu bỏ qua ({votes}/{needed})."
        return "Không có gì đang phát."

    @staticmethod
    def _playlist_saved_message(saved: SavedPlaylist) -> str:
        name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(saved.name)
        )
        return f"Đã lưu **{name}** ({len(saved.tracks)} bài)."

    @staticmethod
    def _require_playlist_tracks(saved: SavedPlaylist) -> None:
        if not saved.tracks:
            raise PlaylistError("Danh sách trống. Hãy thêm bài trước khi phát.")

    def _playlist_queue_snapshot(self, guild_id: int) -> tuple[QueuedTrack, ...]:
        snapshot = self.ui_snapshot(guild_id)
        tracks = (
            ((snapshot.current,) if snapshot.current is not None else ())
            + snapshot.queued
            if snapshot is not None else ()
        )
        if not tracks:
            raise PlaylistError("Không có bài nào để lưu.")
        return tracks

    async def _edit_playlist(
        self, guild_id: int, owner_id: int, action: str, args: list[str],
        *, guard: Callable[[], None] | None = None,
        expected_revision: int | None = None,
    ) -> str:
        arity = {"create": 1, "add": 2, "rename": 2, "remove": 2,
                 "move": 3, "delete": 1}
        if action not in arity or len(args) != arity[action]:
            raise PlaylistError(
                "Cú pháp chưa đúng. Xem lệnh playlist trong menu Trợ giúp. "
                'Đặt tên có khoảng trắng trong dấu ngoặc kép: "Nhạc tối".'
            )
        ref = args[0]
        if action == "create":
            saved = await self.playlists.create(guild_id, owner_id, ref)
        elif action == "add":
            # Resolve ownership before doing HTTP/search work.
            await self.playlists.get(guild_id, owner_id, ref)
            batch = await self.media.prepare(args[1])
            if guard is not None:
                guard()
            saved = await self.playlists.append(guild_id, owner_id, ref, batch.items)
            message = self._playlist_saved_message(saved)
            if batch.skipped:
                message += f" Đã bỏ qua {batch.skipped} bài không khả dụng."
            if batch.truncated:
                message += " Nguồn có thêm bài; chỉ nhập tối đa 25 bài mỗi lần."
            return message
        elif action == "rename":
            saved = await self.playlists.rename(guild_id, owner_id, ref, args[1])
        elif action == "delete":
            await self.playlists.delete(guild_id, owner_id, ref)
            return "Đã xóa danh sách phát."
        else:
            try:
                position = int(args[1])
                destination = int(args[2]) if action == "move" else None
            except ValueError as exc:
                raise PlaylistError("Số thứ tự bài hát phải là số nguyên.") from exc
            saved = await self.playlists.edit_track(
                guild_id, owner_id, ref, position, destination=destination,
                expected_revision=expected_revision,
            )
        return self._playlist_saved_message(saved)

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
            bound_missing = (
                expected_channel_id is not None
                and not guild_channel_exists(ctx.guild, expected_channel_id)
            )
            if bound_missing:
                await self._leave_voice(ctx)
                return
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
        """Turn speaker-name TTS prefix on or off for this guild.

        Usage: ``!tfd nameannounce on`` / ``!tfd nameannounce off``
        Default is off (message body only). The value is also stored
        in the panel settings form and reused by later chat-reading sessions.
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

            try:
                enabled = parse_name_announce(mode)
            except AudioSettingsValidationError:
                await ctx.send(
                    f"Dùng `{ctx.prefix}nameannounce on` hoặc "
                    f"`{ctx.prefix}nameannounce off`."
                )
                return

            session.set_name_announce(enabled)
            current = self.players.audio_settings(ctx.guild.id)
            if current.name_announce != enabled:
                self.players.set_audio_settings(
                    ctx.guild.id,
                    GuildAudioSettings(
                        music_volume=current.music_volume,
                        duck_level=current.duck_level,
                        tts_language=current.tts_language,
                        name_announce=enabled,
                    ),
                )
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
            if player is None:
                message = "Không có gì đang phát."
            else:
                channel_id = getattr(
                    getattr(ctx.voice_client, "channel", None), "id", None
                ) or getattr(
                    member_voice_channel(ctx.author), "id", 0
                )
                message = self._apply_vote_skip(
                    player,
                    ctx.author.id,
                    self._voice_voter_count(ctx.guild, int(channel_id or 0)),
                )
        await ctx.send(message)

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
                mode = None
            else:
                mode = player.toggle_loop()
        if mode is None:
            await ctx.send("Không có gì đang phát.")
            return
        await ctx.send(self._loop_message(mode))

    @commands.command()
    @commands.guild_only()
    async def stop(self, ctx: commands.Context[Any]) -> None:
        """Stop music and clear the queue without ending the voice session."""
        async with self._operation_lock(ctx.guild.id):
            if not await self._require_voice_control(ctx):
                return
            player = self.players.get(ctx.guild.id)
            stopped = await player.stop_music() if player is not None else False
            session_active = self.sessions.is_active(ctx.guild.id)

        if session_active:
            if stopped:
                await ctx.send(
                    "Đã dừng nhạc và xóa hàng đợi. "
                    "Phiên chat TTS vẫn đang chạy — "
                    f"dùng `{ctx.prefix}leave` để thoát."
                )
            else:
                await ctx.send(
                    "Không có gì đang phát. Phiên chat TTS vẫn đang chạy — "
                    f"dùng `{ctx.prefix}leave` để thoát."
                )
            return

        if stopped:
            await ctx.send(
                "Đã dừng nhạc và xóa hàng đợi. "
                f"Dùng `{ctx.prefix}leave` để bot rời kênh thoại."
            )
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
    async def on_ready(self) -> None:
        if self._restored:
            return
        self._restored = True
        await self._restore_playback_sessions()

    async def _persist_playback_state(
        self, guild_id: int, snapshot: PlayerSnapshot,
    ) -> None:
        if self._restoring:
            return
        record = self.music_ui.get(guild_id)
        if record is None:
            return
        text_id = getattr(record.destination, "id", None)
        if text_id is None:
            return
        try:
            await self.playlists.save_playback(
                guild_id,
                record.voice_channel_id,
                int(text_id),
                current=snapshot.current,
                queued=snapshot.queued,
                loop_current=snapshot.loop_current,
                loop_queue=snapshot.loop_queue,
            )
        except PlaylistError:
            log.exception("Could not persist playback for guild %s", guild_id)

    async def _persist_session(
        self,
        guild_id: int,
        voice_channel_id: int,
        text_channel_id: int | None,
    ) -> None:
        if text_channel_id is None:
            return
        snapshot = self.ui_snapshot(guild_id)
        try:
            await self.playlists.save_playback(
                guild_id,
                voice_channel_id,
                text_channel_id,
                current=snapshot.current if snapshot else None,
                queued=snapshot.queued if snapshot else (),
                loop_current=bool(snapshot and snapshot.loop_current),
                loop_queue=bool(snapshot and snapshot.loop_queue),
            )
        except PlaylistError:
            log.exception("Could not persist session for guild %s", guild_id)

    async def _restore_playback_sessions(self) -> None:
        try:
            sessions = await self.playlists.list_playback()
        except PlaylistError:
            log.exception("Could not load playback sessions")
            return
        self._restoring = True
        try:
            for session in sessions:
                try:
                    await self._restore_one_session(session)
                except Exception:
                    log.exception(
                        "Could not restore playback for guild %s", session.guild_id,
                    )
        finally:
            self._restoring = False

    async def _restore_one_session(self, session: PlaybackSession) -> None:
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            return
        voice = guild.get_channel(session.voice_channel_id)
        text = guild.get_channel(session.text_channel_id)
        if voice is None or text is None or not hasattr(text, "send"):
            return
        if not self._human_voice_members(voice):
            return
        player = await self.players.get_or_create(guild)
        player.reserve_activity()
        try:
            await connect_voice_channel(guild, voice, self.settings)
            player.loop_current = session.loop_current
            player.loop_queue = session.loop_queue
            tracks = (
                ((session.current,) if session.current is not None else ())
                + session.queued
            )
            if tracks:
                await player.enqueue_many(tracks, text)
            await self.music_ui.post_panel(text, guild.id, session.voice_channel_id)
        except VoiceAccessError as exc:
            log.warning("Restore skipped for guild %s: %s", guild.id, exc)
        finally:
            player.release_activity()

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self,
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        """Forget a deleted controller without touching guild playback."""
        self.music_ui.drop_message(payload.message_id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Release a vanished voice room so the bot can join a new channel."""
        bot_user = self.bot.user
        if bot_user is None or getattr(member, "id", None) != bot_user.id:
            return
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        before_id = getattr(getattr(before, "channel", None), "id", None)
        after_id = getattr(getattr(after, "channel", None), "id", None)
        if before_id is None or before_id == after_id:
            return
        async with self._operation_lock(guild.id):
            if after_id is not None:
                session = self.sessions.get(guild.id)
                if (
                    session is not None
                    and session.active
                    and session.voice_channel_id == before_id
                ):
                    await self.sessions.stop(guild.id)
                await self.music_ui.invalidate_if_channel_changed(guild.id, after_id)
                await self.music_ui.refresh(guild.id)
                return
            await self._release_disconnected_voice(guild, before_id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self,
        channel: discord.abc.GuildChannel,
    ) -> None:
        """Drop bindings when a temporary voice room or panel channel is deleted."""
        guild = getattr(channel, "guild", None)
        channel_id = getattr(channel, "id", None)
        if guild is None or channel_id is None:
            return
        async with self._operation_lock(guild.id):
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                await self._release_disconnected_voice(
                    guild,
                    channel_id,
                    require_binding=True,
                )
                return
            await self.music_ui.drop_if_channel(guild.id, channel_id)

    async def _release_disconnected_voice(
        self,
        guild: discord.Guild,
        previous_channel_id: int,
        *,
        require_binding: bool = False,
    ) -> None:
        """Clear session/panel pins after Discord already dropped the bot."""
        if voice_client_is_live(guild, guild.voice_client):
            return

        session = self.sessions.get(guild.id)
        record = self.music_ui.get(guild.id)
        session_bound = (
            session is not None
            and session.active
            and session.voice_channel_id == previous_channel_id
        )
        panel_bound = record is not None and (
            record.voice_channel_id == previous_channel_id
            or getattr(record.destination, "id", None) == previous_channel_id
        )
        if require_binding and not session_bound and not panel_bound:
            return

        await self._stop_all_and_disconnect(guild)
        if not guild_channel_exists(guild, previous_channel_id):
            await self.music_ui.drop_if_channel(guild.id, previous_channel_id)
            return
        await self.music_ui.refresh(guild.id)

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

        if await self._enqueue_prepared(ctx, batch):
            await ctx.send(self._format_enqueue_confirmation(batch, confirmation))

    async def _enqueue_prepared(
        self, ctx: commands.Context[Any], batch: MediaBatch,
    ) -> bool:
        """Append prepared metadata after rechecking room access under the lock."""
        async with self._operation_lock(ctx.guild.id):
            if not await self._preflight_voice_connection(ctx):
                return False
            # Touch an existing player before connecting so its idle deadline
            # cannot tear down the voice client during this operation.
            player = await self.players.get_or_create(ctx.guild)
            player.reserve_activity()
            try:
                voice_client = await self._connect_for_context(ctx)
                if voice_client is None:
                    return False
                await player.enqueue_many(batch.items, ctx.channel)
            finally:
                player.release_activity()
        return True

    async def ui_list_playlists(
        self, guild_id: int, owner_id: int,
    ) -> tuple[SavedPlaylist, ...]:
        return await self.playlists.list(guild_id, owner_id)

    def _playlist_interaction_guard(
        self, interaction: discord.Interaction, guild_id: int,
        voice_channel_id: int | None,
    ) -> None:
        if interaction.guild is None or interaction.guild.id != guild_id:
            raise PlaylistError("Danh sách này chỉ dùng được trong máy chủ.")
        if voice_channel_id is not None:
            error = self._interaction_access_error(
                interaction, guild_id, voice_channel_id, allow_disconnected=True,
            )
            if error:
                raise PlaylistError(error)

    async def ui_playlist_search(
        self, interaction: discord.Interaction, guild_id: int, ref: str,
        query: str, voice_channel_id: int | None = None,
        *, owner_id: int | None = None,
    ) -> tuple[SearchResult, ...]:
        library_owner = interaction.user.id if owner_id is None else owner_id
        self._playlist_interaction_guard(interaction, guild_id, voice_channel_id)
        await self.playlists.get(guild_id, library_owner, ref)
        results = await self.media.search(query, limit=5)
        self._playlist_interaction_guard(interaction, guild_id, voice_channel_id)
        return tuple(results)

    async def ui_playlist_action(
        self, interaction: discord.Interaction, guild_id: int, action: str,
        args: list[str], voice_channel_id: int | None = None,
        *, expected_revision: int | None = None,
        owner_id: int | None = None,
    ) -> str:
        library_owner = interaction.user.id if owner_id is None else owner_id

        def guard() -> None:
            self._playlist_interaction_guard(
                interaction, guild_id, voice_channel_id,
            )

        try:
            guard()
            if library_owner == SERVER_OWNER_ID and action in {
                "create", "rename", "delete",
            }:
                self._require_server_playlist_manage(interaction.user)
            if action not in {"save", "play"}:
                return await self._edit_playlist(
                    guild_id, library_owner, action, args, guard=guard,
                    expected_revision=expected_revision,
                )
            if len(args) != 1:
                raise PlaylistError("Hãy nhập tên danh sách phát.")
            channel_id = voice_channel_id
            if channel_id is None:
                member_channel = member_voice_channel(interaction.user)
                if member_channel is None:
                    raise PlaylistError("Hãy vào một kênh thoại trước.")
                live_id = live_voice_channel_id(interaction.guild)
                channel_id = live_id if live_id is not None else member_channel.id
            if action == "play":
                error = self._interaction_access_error(
                    interaction, guild_id, channel_id, allow_disconnected=True,
                )
                if error:
                    return error
                saved = await self.playlists.get(guild_id, library_owner, args[0])
                self._require_playlist_tracks(saved)
                error = await self._enqueue_interaction_batch(
                    interaction, guild_id, channel_id,
                    MediaBatch(items=saved.tracks, is_playlist=True),
                )
                return error or f"Đã thêm {len(saved.tracks)} bài vào hàng đợi."
            async with self._operation_lock(guild_id):
                error = self._interaction_access_error(
                    interaction, guild_id, channel_id,
                )
                if error:
                    return error
                tracks = self._playlist_queue_snapshot(guild_id)
                if library_owner == SERVER_OWNER_ID:
                    self._require_server_playlist_manage(interaction.user)
                saved = await self.playlists.create(
                    guild_id, library_owner, args[0], tracks,
                )
                return self._playlist_saved_message(saved)
        except (PlaylistError, MediaExtractionError) as exc:
            return str(exc)
        except Exception:
            log.exception("Playlist interaction failed in guild %s", guild_id)
            return "Không cập nhật được danh sách phát. Hãy thử lại."

    async def _connect_for_context(
        self,
        ctx: commands.Context[Any],
    ) -> discord.VoiceClient | None:
        """Connect a command caller without moving an occupied voice client."""
        voice_client = await get_or_connect_voice_client(ctx, self.settings)
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

    async def _join_and_post_panel(
        self,
        ctx: commands.Context[Any],
    ) -> int | None:
        """Connect and post the shared panel. Returns the bound voice channel id."""
        if not await self._preflight_voice_connection(ctx):
            return None
        player = await self.players.get_or_create(ctx.guild)
        player.reserve_activity()
        try:
            voice_client = await self._connect_for_context(ctx)
            if voice_client is None:
                return None
            channel = voice_client.channel
            if channel is None:
                await ctx.send("Đã kết nối nhưng không gắn được kênh thoại.")
                return None
            session = self.sessions.get(ctx.guild.id)
            if (
                session is not None
                and session.active
                and session.voice_channel_id != channel.id
            ):
                await self.sessions.stop(ctx.guild.id)
            await self.music_ui.post_panel(
                ctx.channel,
                ctx.guild.id,
                channel.id,
            )
            await self._persist_session(
                ctx.guild.id, channel.id, getattr(ctx.channel, "id", None),
            )
            return channel.id
        finally:
            player.release_activity()

    async def _preflight_voice_connection(
        self,
        ctx: commands.Context[Any],
    ) -> bool:
        """Cheap caller/room validation that also permits a new connection."""
        member_channel = member_voice_channel(ctx.author)
        if member_channel is None:
            await ctx.send("Hãy vào một kênh thoại trước.")
            return False
        # A disconnected TTS session must not pin later joins to a vanished
        # temporary room. Only a live voice client still owns a room.
        if live_voice_channel_id(ctx.guild) is None:
            error = same_voice_channel_error(
                ctx.guild,
                ctx.author,
                expected_channel_id=getattr(member_channel, "id", None),
                allow_disconnected=True,
            )
        else:
            error = same_voice_channel_error(
                ctx.guild,
                ctx.author,
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

    def ui_tts_available(self) -> bool:
        """Return whether voice-reading controls are available globally."""
        return self.settings.tts_enabled

    def ui_title_reading_enabled(self, guild_id: int) -> bool:
        return (
            self.settings.tts_enabled
            and self.players.title_announcements_enabled(guild_id)
        )

    def ui_chat_reading_enabled(self, guild_id: int) -> bool:
        return self.settings.tts_enabled and self.sessions.is_active(guild_id)

    def ui_voice_connected(self, guild_id: int) -> bool:
        return self.ui_voice_channel_id(guild_id) is not None

    def ui_voice_channel_id(self, guild_id: int) -> int | None:
        """Return the active voice room used to validate automatic bumps."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        return live_voice_channel_id(guild)

    def ui_audio_settings(self, guild_id: int) -> GuildAudioSettings:
        """Return this guild's process-lifetime shared audio preferences."""
        return self.players.audio_settings(guild_id)

    async def ui_list_soundboard(
        self, guild_id: int
    ) -> tuple[SoundboardEntry, ...]:
        return await self.soundboard_service.list(guild_id)

    async def ui_play_soundboard(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        sound_id: str,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
                allow_disconnected=True,
            )
            if error:
                return error
            entry = await self.soundboard_service.store.get(guild_id, sound_id)
            if entry is None:
                return "Âm thanh không còn tồn tại."
            path = await self.soundboard_service.ensure_playable(guild_id, entry)
            if path is None:
                return "Không tìm thấy tệp âm thanh này."
            guild = interaction.guild
            if guild is None:
                return "Bảng điều khiển này không còn hợp lệ."
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
            finally:
                player.release_activity()

        timeout = float(getattr(self.settings, "soundboard_max_seconds", 12)) + 5.0
        ok = await player.play_overlay(path, timeout=timeout)
        player.touch()
        if ok:
            return (
                f"Đã phát **{discord.utils.escape_markdown(entry.name)}**."
            )
        return "Không phát được âm thanh. Hãy thử lại."

    async def ui_add_soundboard(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        name: str,
        url: str,
    ) -> str:
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
            finally:
                player.release_activity()

        try:
            entry = await self.soundboard_service.add_sound(
                guild_id,
                name=name,
                url=url,
                added_by=interaction.user.id,
            )
        except SoundboardError as exc:
            return str(exc)
        except Exception:
            log.exception("Could not add soundboard clip in guild %s", guild_id)
            return "Không lưu được âm thanh. Hãy thử lại."
        return (
            f"Đã lưu **{discord.utils.escape_markdown(entry.name)}** "
            f"({format_clip_duration(entry.duration_ms)})."
        )

    async def ui_remove_soundboard(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        sound_id: str,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
            )
            if error:
                return error
            permissions = getattr(interaction.user, "guild_permissions", None)
            manage_guild = bool(getattr(permissions, "manage_guild", False))
            try:
                entry = await self.soundboard_service.remove_sound(
                    guild_id,
                    sound_id,
                    user_id=interaction.user.id,
                    manage_guild=manage_guild,
                )
            except SoundboardError as exc:
                return str(exc)
        return f"Đã xóa **{discord.utils.escape_markdown(entry.name)}**."

    async def ui_search_soundboard(self, query: str) -> tuple[InstantHit, ...]:
        try:
            return await self.soundboard_service.search_instants(query, limit=5)
        except SoundboardError:
            raise
        except Exception as exc:
            log.exception("MyInstants search failed")
            raise SoundboardError("Không tìm được âm thanh MyInstants.") from exc

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
            if not is_url and not is_spotify_input(normalized):
                results = await self.media.search(normalized, limit=5)
                if not results:
                    return AddInputResult(message="Không tìm thấy kết quả.")
                return AddInputResult(
                    message="Chọn nút số tương ứng để thêm vào hàng đợi:",
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
            if player is None:
                return "Không có gì đang phát."
            guild = interaction.guild
            voters = (
                self._voice_voter_count(guild, voice_channel_id)
                if guild is not None else 1
            )
            return self._apply_vote_skip(player, interaction.user.id, voters)

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
            mode = player.toggle_loop()
        return self._loop_message(mode)

    async def ui_previous(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction, guild_id, voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            moved = bool(player and player.previous())
        return "Đã phát bài trước." if moved else "Không có bài trước đó."

    async def ui_shuffle_queue(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction, guild_id, voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            if player is None:
                return "Hàng đợi trống."
            count = player.shuffle_queue()
        return f"Đã xáo trộn {count} bài đang chờ." if count else "Hàng đợi trống."

    async def ui_remove_queued(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        position: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction, guild_id, voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            track = player.remove_queued(position) if player else None
        if track is None:
            return "Số thứ tự bài chờ không hợp lệ."
        title = discord.utils.escape_markdown(track.title)
        return f"Đã xóa **{title}** khỏi hàng đợi."

    async def ui_move_queued(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        position: int,
        destination: int,
    ) -> str:
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction, guild_id, voice_channel_id,
            )
            if error:
                return error
            player = self.players.get(guild_id)
            moved = bool(player and player.move_queued(position, destination))
        return "Đã đổi chỗ bài trong hàng đợi." if moved else "Số thứ tự không hợp lệ."

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
            player = self.players.get(guild_id)
            stopped = await player.stop_music() if player is not None else False
            session_active = self.sessions.is_active(guild_id)
        if session_active:
            return (
                "Đã dừng nhạc và xóa hàng đợi. Đọc tin nhắn vẫn đang bật."
                if stopped
                else "Không có gì đang phát. Phiên chat TTS vẫn đang chạy."
            )

        if stopped:
            return "Đã dừng nhạc và xóa hàng đợi. Bot vẫn ở kênh thoại."
        return "Không có gì đang phát."

    async def ui_toggle_title_reading(
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
                allow_disconnected=True,
            )
            if error:
                return error
            if not self.settings.tts_enabled:
                return "TTS đã bị tắt trong cấu hình bot."
            enabled = self.players.toggle_title_announcements(guild_id)

        if enabled:
            return "Đã bật đọc tên bài hát."
        return "Đã tắt đọc tên bài hát. Thông báo chữ vẫn được gửi."

    async def ui_toggle_chat_reading(
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
                allow_disconnected=True,
            )
            if error:
                return error
            if not self.settings.tts_enabled:
                return "TTS đã bị tắt trong cấu hình bot."

            guild = interaction.guild
            if guild is None:
                return "Bảng điều khiển này không còn hợp lệ."

            session = self.sessions.get(guild_id)
            if session is not None and session.active:
                if session.voice_channel_id != voice_channel_id:
                    return "Bảng điều khiển này không còn gắn với phiên chat TTS."

                player = self.players.get(guild_id)
                music_pending = False
                if player is not None:
                    # Keep idle retirement from racing the awaited session close.
                    # Releasing the reservation starts a fresh idle deadline.
                    player.reserve_activity()
                    snapshot = player.snapshot()
                    music_pending = (
                        snapshot.current is not None or bool(snapshot.queued)
                    )
                try:
                    await self.sessions.stop(guild_id)
                finally:
                    if player is not None:
                        player.release_activity()
                voice_client = guild.voice_client
                if voice_client is not None and voice_client.is_connected():
                    if music_pending:
                        return (
                            "Đã tắt đọc tin nhắn. Nhạc vẫn tiếp tục phát "
                            "và bot vẫn ở kênh thoại."
                        )
                    return "Đã tắt đọc tin nhắn. Bot vẫn ở kênh thoại."
                return "Đã tắt đọc tin nhắn."

            player = self.players.get(guild_id)
            if player is not None:
                player.reserve_activity()
            try:
                try:
                    voice_client = await connect_member_voice_client(
                        guild,
                        interaction.user,
                        self.settings,
                        expected_channel_id=voice_channel_id,
                    )
                except VoiceAccessError as exc:
                    return str(exc)

                channel = voice_client.channel
                if not isinstance(
                    channel,
                    (discord.VoiceChannel, discord.StageChannel),
                ):
                    return "Không thể bắt đầu đọc chat trên loại kênh này."
                self.sessions.start(guild, channel)
            finally:
                if player is not None:
                    player.release_activity()

        return "Đã bật đọc tin nhắn trong kênh thoại."

    async def ui_leave(
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
            had_player, had_session, disconnected = (
                await self._stop_all_and_disconnect(guild)
            )

        if had_player or had_session or disconnected:
            return (
                "Đã dừng nhạc, xóa hàng đợi, tắt đọc tin nhắn "
                "và rời kênh thoại."
            )
        return "Bot chưa kết nối kênh thoại."

    async def ui_update_audio_settings(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        settings: GuildAudioSettings,
        panel_bump_minutes: int | None = None,
    ) -> str:
        """Atomically apply settings after rechecking the bound-room user."""
        async with self._operation_lock(guild_id):
            error = self._interaction_access_error(
                interaction,
                guild_id,
                voice_channel_id,
                allow_disconnected=True,
            )
            if error:
                return error
            if panel_bump_minutes is not None:
                try:
                    panel_bump_minutes = parse_panel_bump_interval(
                        str(panel_bump_minutes)
                    )
                except ValueError:
                    return (
                        "Thời gian đưa bảng lên phải là 0 hoặc số phút "
                        "từ 1 đến 1440."
                    )
            if not self.settings.tts_enabled:
                current = self.players.audio_settings(guild_id)
                settings = GuildAudioSettings(
                    music_volume=settings.music_volume,
                    duck_level=current.duck_level,
                    tts_language=current.tts_language,
                    name_announce=current.name_announce,
                )
            try:
                applied = self.players.set_audio_settings(guild_id, settings)
            except (TypeError, ValueError):
                return "Cài đặt âm thanh không hợp lệ."
            if self.settings.tts_enabled:
                self.sessions.refresh_tts_language(guild_id)
                session = self.sessions.get(guild_id)
                if session is not None and session.active:
                    session.set_name_announce(applied.name_announce)
            if panel_bump_minutes is not None:
                self.music_ui.set_bump_interval_minutes(
                    guild_id,
                    panel_bump_minutes,
                )

        message = f"Đã cập nhật cài đặt: {format_audio_settings(applied)}."
        if panel_bump_minutes is not None:
            message += (
                " Tự đưa bảng lên: "
                f"{format_panel_bump_interval(panel_bump_minutes).lower()}."
            )
        if not self.settings.tts_enabled:
            message += (
                " TTS đang bị tắt; các cài đặt TTS được giữ nguyên."
                if panel_bump_minutes is not None
                else " TTS đang bị tắt; chỉ âm lượng nhạc được thay đổi."
            )
        return message

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
        self.players.remove_state_listener(self._persist_playback_state)
        await self.music_ui.close()

    async def _leave_voice(self, ctx: commands.Context[Any]) -> None:
        """Stop music, end the chat session, and disconnect from voice."""
        had_player, had_session, disconnected = (
            await self._stop_all_and_disconnect(ctx.guild)
        )

        await self.music_ui.refresh(ctx.guild.id)

        if had_player or had_session or disconnected:
            if had_session:
                await ctx.send("Đã rời kênh thoại và dừng theo dõi chat.")
            else:
                await ctx.send("Đã rời kênh thoại.")
            return
        await ctx.send("Bot chưa kết nối kênh thoại.")

    async def _stop_all_and_disconnect(
        self,
        guild: discord.Guild,
    ) -> tuple[bool, bool, bool]:
        """Stop music and chat reading, then leave the captured voice client."""
        voice_client = guild.voice_client
        had_player = await self.players.remove(guild.id, disconnect=False)
        had_session = await self.sessions.stop(guild.id)
        with contextlib.suppress(PlaylistError):
            await self.playlists.clear_playback(guild.id)

        disconnected = False
        if voice_client and voice_client.is_connected():
            disconnected = await disconnect_guild_voice_client(
                guild,
                expected_client=voice_client,
            )
        return had_player, had_session, disconnected

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
