"""Shared, room-bound Discord controls for music playback.

The UI deliberately delegates voice access and mutations to ``MusicUIActions``.
This keeps interaction rendering independent from the command cog and makes the
same authorization policy usable by prefix commands and component callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import discord

from .media import SearchResult, format_duration, parse_jump_timestamp
from .player import PlaybackState, PlayerSnapshot

log = logging.getLogger(__name__)

SEARCH_VIEW_TIMEOUT = 120.0
CLEAR_CONFIRM_TIMEOUT = 30.0
QUEUE_PAGE_SIZE = 10
PANEL_INTERACTION_TOKEN = "tfd_music_panel_view"


@dataclass(frozen=True, slots=True)
class AddInputResult:
    """Outcome of submitting the add-music modal.

    URLs normally return a confirmation in ``message``. Plain searches return
    up to five choices in ``results`` for the requester to select.
    """

    message: str | None = None
    results: tuple[SearchResult, ...] = ()


class MusicUIActions(Protocol):
    """Cog-owned operations required by the interaction UI.

    ``ui_ensure_panel_access`` sends its own ephemeral denial when returning
    ``False``. ``connect_if_missing=True`` means a disconnected bound-room user
    may continue; actual reconnection happens inside ``add_input`` or
    ``enqueue_search_result`` after the interaction has been deferred.
    """

    def ui_snapshot(self, guild_id: int) -> PlayerSnapshot | None: ...

    def ui_tts_available(self) -> bool: ...

    def ui_title_reading_enabled(self, guild_id: int) -> bool: ...

    def ui_chat_reading_enabled(self, guild_id: int) -> bool: ...

    async def ui_ensure_panel_access(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        *,
        connect_if_missing: bool = False,
    ) -> bool: ...

    async def ui_add_input(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        value: str,
    ) -> AddInputResult: ...

    async def ui_enqueue_search_result(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        result: SearchResult,
    ) -> str: ...

    async def ui_pause(
        self, interaction: discord.Interaction, guild_id: int, voice_channel_id: int
    ) -> str: ...

    async def ui_resume(
        self, interaction: discord.Interaction, guild_id: int, voice_channel_id: int
    ) -> str: ...

    async def ui_skip(
        self, interaction: discord.Interaction, guild_id: int, voice_channel_id: int
    ) -> str: ...

    async def ui_toggle_loop(
        self, interaction: discord.Interaction, guild_id: int, voice_channel_id: int
    ) -> str: ...

    async def ui_jump(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        offset: int,
    ) -> str: ...

    async def ui_clear_queue(
        self, interaction: discord.Interaction, guild_id: int, voice_channel_id: int
    ) -> str: ...

    async def ui_stop(
        self, interaction: discord.Interaction, guild_id: int, voice_channel_id: int
    ) -> str: ...

    async def ui_toggle_title_reading(
        self, interaction: discord.Interaction, guild_id: int, voice_channel_id: int
    ) -> str: ...

    async def ui_toggle_chat_reading(
        self, interaction: discord.Interaction, guild_id: int, voice_channel_id: int
    ) -> str: ...


def _track_title(track: object, *, maximum: int = 180) -> str:
    title = discord.utils.escape_markdown(str(getattr(track, "title", "Không có tiêu đề")))
    return title if len(title) <= maximum else f"{title[: maximum - 1]}…"


def _track_line(
    track: object,
    index: int,
    *,
    include_link: bool = False,
) -> str:
    title = _track_title(track)
    url = getattr(track, "webpage_url", None)
    duration = format_duration(getattr(track, "duration", None))
    # Queue fields have strict Discord size limits. Link only the single
    # current-track row and omit unusually long URLs rather than failing edits.
    rendered = (
        f"[{title}](<{url}>)"
        if include_link and isinstance(url, str) and len(url) <= 700
        else title
    )
    return f"{index}. {rendered}{f' · {duration}' if duration else ''}"


def _state_text(state: PlaybackState) -> str:
    return {
        PlaybackState.IDLE: "Đang rảnh",
        PlaybackState.LOADING: "Đang tải",
        PlaybackState.PLAYING: "Đang phát",
        PlaybackState.PAUSED: "Đã tạm dừng",
    }.get(state, "Đang rảnh")


def build_music_embed(
    voice_channel_id: int,
    snapshot: PlayerSnapshot | None,
) -> discord.Embed:
    """Render the public panel state from an immutable player snapshot."""
    state = snapshot.state if snapshot is not None else PlaybackState.IDLE
    color = {
        PlaybackState.PLAYING: discord.Color.green(),
        PlaybackState.PAUSED: discord.Color.gold(),
        PlaybackState.LOADING: discord.Color.blurple(),
    }.get(state, discord.Color.light_grey())
    embed = discord.Embed(title="Bảng điều khiển nhạc", color=color)
    embed.add_field(name="Kênh thoại", value=f"<#{voice_channel_id}>", inline=True)
    embed.add_field(name="Trạng thái", value=_state_text(state), inline=True)

    current = snapshot.current if snapshot is not None else None
    if current is None:
        current_text = "Không có bài nào."
    else:
        current_text = _track_line(current, 1, include_link=True).partition(". ")[2]
    embed.add_field(name="Bài hiện tại", value=current_text, inline=False)

    queued = snapshot.queued if snapshot is not None else ()
    embed.add_field(
        name="Lặp bài",
        value="Bật" if snapshot is not None and snapshot.loop_current else "Tắt",
        inline=True,
    )
    embed.add_field(name="Đang chờ", value=str(len(queued)), inline=True)
    next_tracks = "\n".join(_track_line(track, index) for index, track in enumerate(queued[:5], 1))
    embed.add_field(name="Tiếp theo", value=next_tracks or "Hàng đợi trống.", inline=False)
    embed.set_footer(text="Mọi thành viên trong kênh thoại đều có thể điều khiển.")
    return embed


async def _send_ephemeral(
    interaction: discord.Interaction,
    content: str,
    *,
    view: discord.ui.View | None = None,
    embed: discord.Embed | None = None,
) -> object | None:
    if interaction.response.is_done():
        return await interaction.followup.send(
            content,
            view=view,
            embed=embed,
            ephemeral=True,
            wait=True,
        )
    await interaction.response.send_message(
        content,
        view=view,
        embed=embed,
        ephemeral=True,
    )
    with contextlib.suppress(discord.HTTPException, AttributeError):
        return await interaction.original_response()
    return None


def _disable(view: discord.ui.View) -> None:
    for child in view.children:
        if hasattr(child, "disabled"):
            child.disabled = True


class _RequesterView(discord.ui.View):
    def __init__(self, requester_id: int, *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.message: object | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await _send_ephemeral(interaction, "Chỉ người mở bảng này mới có thể sử dụng.")
        return False

    async def on_timeout(self) -> None:
        _disable(self)
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await self.message.edit(view=self)


class SearchResultSelect(discord.ui.Select):
    """Single-choice YouTube result selector."""

    def __init__(self, results: Sequence[SearchResult]) -> None:
        self.results = tuple(results[:5])
        options = []
        for index, result in enumerate(self.results):
            duration = format_duration(result.duration)
            options.append(
                discord.SelectOption(
                    label=result.title[:100] or "Không có tiêu đề",
                    value=str(index),
                    description=(duration or "YouTube")[:100],
                )
            )
        super().__init__(
            placeholder="Chọn một bài để thêm",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="music:search-result",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, SearchResultView):
            await _send_ephemeral(interaction, "Kết quả tìm kiếm đã hết hạn.")
            return
        try:
            result = self.results[int(self.values[0])]
        except (IndexError, TypeError, ValueError):
            await _send_ephemeral(interaction, "Kết quả tìm kiếm không còn hợp lệ.")
            return
        allowed = await view.ensure_access(interaction)
        if not allowed:
            return
        if not await view.consume():
            await _send_ephemeral(interaction, "Kết quả này đã được sử dụng.")
            return

        # Claim the requester-only result before the first network await after
        # access validation. Discord can deliver two rapid select interactions
        # before the disabled component edit reaches the client.
        view.stop()
        _disable(view)
        # A component update defer keeps the ephemeral result message as the
        # original response, so edit_original_response below disables this
        # selector rather than editing a separate "thinking" response.
        await interaction.response.defer(ephemeral=True)
        message = await view.actions.ui_enqueue_search_result(
            interaction,
            view.guild_id,
            view.voice_channel_id,
            result,
        )
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(view=view)
        await interaction.followup.send(message, ephemeral=True)
        await view.manager.refresh(view.guild_id)


class SearchResultView(_RequesterView):
    """Requester-bound, expiring list of five YouTube search results."""

    def __init__(
        self,
        actions: MusicUIActions,
        manager: MusicPanelManager,
        guild_id: int,
        voice_channel_id: int,
        requester_id: int,
        results: Sequence[SearchResult],
        *,
        panel_view: MusicPanelView | None = None,
    ) -> None:
        super().__init__(requester_id, timeout=SEARCH_VIEW_TIMEOUT)
        self.actions = actions
        self.manager = manager
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.panel_view = panel_view
        self._consume_lock = asyncio.Lock()
        self._consumed = False
        self.add_item(SearchResultSelect(results))

    async def consume(self) -> bool:
        """Atomically reserve this one-shot result view."""
        async with self._consume_lock:
            if self._consumed:
                return False
            self._consumed = True
            return True

    async def ensure_access(self, interaction: discord.Interaction) -> bool:
        if self.panel_view is not None:
            return await self.panel_view.ensure_access(
                interaction,
                connect_if_missing=True,
            )
        return await self.actions.ui_ensure_panel_access(
            interaction,
            self.guild_id,
            self.voice_channel_id,
            connect_if_missing=True,
        )


class AddMusicModal(discord.ui.Modal, title="Thêm nhạc"):
    query = discord.ui.TextInput(
        label="Tên bài, URL video hoặc playlist YouTube",
        placeholder="Nhập nội dung tìm kiếm hoặc dán liên kết…",
        required=True,
        max_length=500,
    )

    def __init__(
        self,
        actions: MusicUIActions,
        manager: MusicPanelManager,
        guild_id: int,
        voice_channel_id: int,
        *,
        panel_view: MusicPanelView | None = None,
    ) -> None:
        super().__init__(timeout=SEARCH_VIEW_TIMEOUT)
        self.actions = actions
        self.manager = manager
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.panel_view = panel_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.query).strip()
        if not value:
            await _send_ephemeral(interaction, "Vui lòng nhập tên bài hát hoặc URL.")
            return
        if self.panel_view is not None:
            allowed = await self.panel_view.ensure_access(
                interaction,
                connect_if_missing=True,
            )
        else:
            allowed = await self.actions.ui_ensure_panel_access(
                interaction,
                self.guild_id,
                self.voice_channel_id,
                connect_if_missing=True,
            )
        if not allowed:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.actions.ui_add_input(
            interaction,
            self.guild_id,
            self.voice_channel_id,
            value,
        )
        if result.results:
            view = SearchResultView(
                self.actions,
                self.manager,
                self.guild_id,
                self.voice_channel_id,
                interaction.user.id,
                result.results,
                panel_view=self.panel_view,
            )
            message = await interaction.followup.send(
                result.message or "Chọn một kết quả để thêm vào hàng đợi:",
                view=view,
                ephemeral=True,
                wait=True,
            )
            view.message = message
        else:
            await interaction.followup.send(
                result.message or "Không tìm thấy kết quả.",
                ephemeral=True,
            )
        await self.manager.refresh(self.guild_id)


class JumpModal(discord.ui.Modal, title="Tua đến"):
    timestamp = discord.ui.TextInput(
        label="Thời điểm (HH:MM:SS)",
        placeholder="00:01:30",
        required=True,
        min_length=8,
        max_length=8,
    )

    def __init__(self, view: MusicPanelView) -> None:
        super().__init__(timeout=SEARCH_VIEW_TIMEOUT)
        self.panel_view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        offset = parse_jump_timestamp(str(self.timestamp).strip())
        if offset is None:
            await _send_ephemeral(interaction, "Thời gian không hợp lệ. Dùng định dạng HH:MM:SS.")
            return
        view = self.panel_view
        if not await view.ensure_access(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        message = await view.actions.ui_jump(
            interaction,
            view.guild_id,
            view.voice_channel_id,
            offset,
        )
        await interaction.followup.send(message, ephemeral=True)
        await view.manager.refresh(view.guild_id)


class QueuePaginatorView(_RequesterView):
    """Static requester-only pages of ten queued tracks."""

    def __init__(self, requester_id: int, queued: Sequence[object]) -> None:
        super().__init__(requester_id, timeout=SEARCH_VIEW_TIMEOUT)
        self.queued = tuple(queued)
        self.page = 0
        self._sync_buttons()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.queued) + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)

    def render_embed(self) -> discord.Embed:
        start = self.page * QUEUE_PAGE_SIZE
        entries = self.queued[start : start + QUEUE_PAGE_SIZE]
        description = "\n".join(
            _track_line(track, start + index)
            for index, track in enumerate(entries, 1)
        ) or "Hàng đợi trống."
        embed = discord.Embed(
            title="Hàng đợi nhạc",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Trang {self.page + 1}/{self.page_count} · {len(self.queued)} bài")
        return embed

    def _sync_buttons(self) -> None:
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1

    @discord.ui.button(label="Trước", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="Sau", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)


class ClearQueueConfirmation(_RequesterView):
    """Short-lived confirmation before clearing waiting tracks."""

    def __init__(self, panel_view: MusicPanelView, requester_id: int) -> None:
        super().__init__(requester_id, timeout=CLEAR_CONFIRM_TIMEOUT)
        self.panel_view = panel_view
        self._decision_lock = asyncio.Lock()
        self._decided = False

    async def _claim(self) -> bool:
        async with self._decision_lock:
            if self._decided:
                return False
            self._decided = True
            return True

    @discord.ui.button(label="Xóa hàng đợi", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = self.panel_view
        if not await view.ensure_access(interaction):
            return
        if not await self._claim():
            await _send_ephemeral(interaction, "Xác nhận này đã được sử dụng.")
            return
        self.stop()
        _disable(self)
        await interaction.response.defer(ephemeral=True)
        message = await view.actions.ui_clear_queue(
            interaction, view.guild_id, view.voice_channel_id
        )
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(view=self)
        await interaction.followup.send(message, ephemeral=True)
        await view.manager.refresh(view.guild_id)

    @discord.ui.button(label="Hủy", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._claim():
            await _send_ephemeral(interaction, "Xác nhận này đã được sử dụng.")
            return
        self.stop()
        _disable(self)
        await interaction.response.edit_message(content="Đã hủy.", view=self)


class MusicPanelView(discord.ui.View):
    """Public, process-lifetime controls shared by one bound voice room."""

    def __init__(
        self,
        actions: MusicUIActions,
        manager: MusicPanelManager,
        guild_id: int,
        voice_channel_id: int,
        snapshot: PlayerSnapshot | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.actions = actions
        self.manager = manager
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self._registered = False
        self.sync(snapshot)

    def sync(self, snapshot: PlayerSnapshot | None) -> None:
        state = snapshot.state if snapshot is not None else PlaybackState.IDLE
        has_current = snapshot is not None and snapshot.current is not None
        active = has_current and state in {PlaybackState.PLAYING, PlaybackState.PAUSED}
        waiting = len(snapshot.queued) if snapshot is not None else 0
        self.pause_resume.label = "Tiếp tục" if state is PlaybackState.PAUSED else "Tạm dừng"
        self.pause_resume.emoji = "▶️" if state is PlaybackState.PAUSED else "⏸️"
        self.pause_resume.disabled = not active
        # A resolving item is still the current track; let users skip a slow or
        # unavailable extraction without clearing the rest of the queue.
        self.next_track.disabled = not has_current
        self.loop_track.disabled = not active
        self.loop_track.style = (
            discord.ButtonStyle.primary
            if snapshot is not None and snapshot.loop_current
            else discord.ButtonStyle.secondary
        )
        self.jump_track.disabled = not active
        self.show_queue.disabled = waiting == 0
        self.clear_queue.disabled = waiting == 0
        self.stop_music.disabled = not has_current and waiting == 0 and state is PlaybackState.IDLE
        title_reading = self.actions.ui_title_reading_enabled(self.guild_id)
        self.toggle_title_reading.label = (
            f"Đọc tên bài: {'Bật' if title_reading else 'Tắt'}"
        )
        self.toggle_title_reading.style = (
            discord.ButtonStyle.success
            if title_reading
            else discord.ButtonStyle.secondary
        )
        self.toggle_title_reading.disabled = not self.actions.ui_tts_available()
        chat_reading = self.actions.ui_chat_reading_enabled(self.guild_id)
        self.toggle_chat_reading.label = (
            f"Đọc tin nhắn: {'Bật' if chat_reading else 'Tắt'}"
        )
        self.toggle_chat_reading.style = (
            discord.ButtonStyle.success
            if chat_reading
            else discord.ButtonStyle.secondary
        )
        self.toggle_chat_reading.disabled = not self.actions.ui_tts_available()

    async def ensure_access(
        self,
        interaction: discord.Interaction,
        *,
        connect_if_missing: bool = False,
    ) -> bool:
        if self._registered:
            record = self.manager.get(self.guild_id)
            if record is None or record.view is not self:
                await _send_ephemeral(
                    interaction,
                    "Bảng điều khiển này đã được thay thế hoặc xóa.",
                )
                return False
        # Interaction.extras is the supported per-interaction scratch mapping.
        # The Cog revalidates this exact panel identity under its guild mutation
        # lock after any modal/search/network delay.
        interaction.extras[PANEL_INTERACTION_TOKEN] = self
        return await self.actions.ui_ensure_panel_access(
            interaction,
            self.guild_id,
            self.voice_channel_id,
            connect_if_missing=connect_if_missing,
        )

    async def _run(
        self,
        interaction: discord.Interaction,
        action: str,
        *,
        connect_if_missing: bool = False,
    ) -> None:
        if not await self.ensure_access(
            interaction,
            connect_if_missing=connect_if_missing,
        ):
            return
        await interaction.response.defer(ephemeral=True)
        message = await getattr(self.actions, f"ui_{action}")(
            interaction, self.guild_id, self.voice_channel_id
        )
        await interaction.followup.send(message, ephemeral=True)
        await self.manager.refresh(self.guild_id)

    @discord.ui.button(label="Thêm nhạc", emoji="➕", style=discord.ButtonStyle.success, row=0)
    async def add_music(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.ensure_access(interaction, connect_if_missing=True):
            return
        await interaction.response.send_modal(
            AddMusicModal(
                self.actions,
                self.manager,
                self.guild_id,
                self.voice_channel_id,
                panel_view=self,
            )
        )

    @discord.ui.button(label="Tạm dừng", emoji="⏸️", style=discord.ButtonStyle.secondary, row=0)
    async def pause_resume(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.actions.ui_snapshot(self.guild_id)
        action = "resume" if state is not None and state.state is PlaybackState.PAUSED else "pause"
        await self._run(interaction, action)

    @discord.ui.button(label="Bài tiếp", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def next_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, "skip")

    @discord.ui.button(label="Lặp", emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def loop_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, "toggle_loop")

    @discord.ui.button(label="Tua đến", emoji="⏩", style=discord.ButtonStyle.secondary, row=1)
    async def jump_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.ensure_access(interaction):
            return
        await interaction.response.send_modal(JumpModal(self))

    @discord.ui.button(label="Hàng đợi", emoji="📋", style=discord.ButtonStyle.secondary, row=1)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.ensure_access(interaction):
            return
        snapshot = self.actions.ui_snapshot(self.guild_id)
        queued = snapshot.queued if snapshot is not None else ()
        view = QueuePaginatorView(interaction.user.id, queued)
        message = await _send_ephemeral(
            interaction,
            "Hàng đợi hiện tại:",
            view=view,
            embed=view.render_embed(),
        )
        view.message = message

    @discord.ui.button(label="Xóa hàng đợi", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def clear_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.ensure_access(interaction):
            return
        view = ClearQueueConfirmation(self, interaction.user.id)
        await interaction.response.send_message(
            "Xóa toàn bộ bài đang chờ? Bài hiện tại vẫn tiếp tục phát.",
            view=view,
            ephemeral=True,
        )
        with contextlib.suppress(discord.HTTPException, AttributeError):
            view.message = await interaction.original_response()

    @discord.ui.button(label="Dừng", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop_music(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run(interaction, "stop")

    @discord.ui.button(
        label="Đọc tên bài: Bật",
        emoji="🔈",
        style=discord.ButtonStyle.success,
        row=2,
    )
    async def toggle_title_reading(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._run(
            interaction,
            "toggle_title_reading",
            connect_if_missing=True,
        )

    @discord.ui.button(
        label="Đọc tin nhắn: Tắt",
        emoji="💬",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def toggle_chat_reading(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._run(
            interaction,
            "toggle_chat_reading",
            connect_if_missing=True,
        )


@dataclass(slots=True)
class MusicPanelRecord:
    guild_id: int
    voice_channel_id: int
    message: discord.Message
    view: MusicPanelView
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    version: int = 0
    applied_version: int = 0
    pending_snapshot: PlayerSnapshot | None = None


class MusicPanelRegistry:
    """In-memory registry containing at most one panel per guild."""

    def __init__(self) -> None:
        self._records: dict[int, MusicPanelRecord] = {}

    def get(self, guild_id: int) -> MusicPanelRecord | None:
        return self._records.get(guild_id)

    def put(self, record: MusicPanelRecord) -> MusicPanelRecord | None:
        previous = self._records.get(record.guild_id)
        self._records[record.guild_id] = record
        return previous

    def pop(self, guild_id: int) -> MusicPanelRecord | None:
        return self._records.pop(guild_id, None)

    def pop_if(self, guild_id: int, record: MusicPanelRecord) -> bool:
        if self._records.get(guild_id) is not record:
            return False
        self._records.pop(guild_id, None)
        return True

    def values(self) -> tuple[MusicPanelRecord, ...]:
        return tuple(self._records.values())


class MusicPanelManager:
    """Posts, refreshes, coalesces, and retires guild music panels."""

    def __init__(
        self,
        actions: MusicUIActions,
        *,
        registry: MusicPanelRegistry | None = None,
    ) -> None:
        self.actions = actions
        self.registry = registry or MusicPanelRegistry()
        self._post_locks: dict[int, asyncio.Lock] = {}
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    def get(self, guild_id: int) -> MusicPanelRecord | None:
        return self.registry.get(guild_id)

    async def post_panel(
        self,
        destination: discord.abc.Messageable,
        guild_id: int,
        voice_channel_id: int,
    ) -> discord.Message:
        lock = self._post_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            old = self.registry.pop(guild_id)
            if old is not None:
                await self._disable_record(old)
            snapshot = self.actions.ui_snapshot(guild_id)
            view = MusicPanelView(self.actions, self, guild_id, voice_channel_id, snapshot)
            message = await destination.send(
                embed=build_music_embed(voice_channel_id, snapshot),
                view=view,
            )
            record = MusicPanelRecord(
                guild_id,
                voice_channel_id,
                message,
                view,
                pending_snapshot=snapshot,
            )
            view._registered = True
            self.registry.put(record)
            # A player event may have occurred while destination.send awaited
            # Discord with no registry record present. Re-render from a fresh
            # snapshot after registration so that event cannot be lost.
            fresh_snapshot = self.actions.ui_snapshot(guild_id)
            if fresh_snapshot != snapshot:
                await self.refresh(guild_id, fresh_snapshot)
            return message

    async def on_player_state_change(
        self,
        guild_id: int,
        _snapshot: PlayerSnapshot,
    ) -> None:
        """Queue a coalesced panel edit without delaying audio playback."""
        if not self._closing:
            # Fetch at task execution time. A scheduled callback from a closed
            # player must never overwrite the state of its replacement.
            self.request_refresh(guild_id)

    async def refresh(
        self,
        guild_id: int,
        snapshot: PlayerSnapshot | None = None,
    ) -> None:
        """Player-listener-compatible refresh with serialized/coalesced edits."""
        record = self.registry.get(guild_id)
        if record is None:
            return
        record.pending_snapshot = (
            snapshot if snapshot is not None else self.actions.ui_snapshot(guild_id)
        )
        record.version += 1
        async with record.lock:
            while record.applied_version < record.version:
                if self.registry.get(guild_id) is not record:
                    return
                target_version = record.version
                current_snapshot = record.pending_snapshot
                record.view.sync(current_snapshot)
                try:
                    await record.message.edit(
                        embed=build_music_embed(record.voice_channel_id, current_snapshot),
                        view=record.view,
                    )
                except (discord.NotFound, discord.Forbidden):
                    self.registry.pop_if(guild_id, record)
                    record.view.stop()
                    return
                except discord.HTTPException:
                    log.warning("Could not refresh music panel in guild %s", guild_id)
                    self.registry.pop_if(guild_id, record)
                    record.view.stop()
                    return
                record.applied_version = target_version

    def request_refresh(
        self,
        guild_id: int,
        snapshot: PlayerSnapshot | None = None,
    ) -> asyncio.Task[None]:
        """Schedule a refresh from synchronous lifecycle hooks."""
        task = asyncio.create_task(self.refresh(guild_id, snapshot))
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)
        return task

    async def invalidate_if_channel_changed(self, guild_id: int, voice_channel_id: int) -> bool:
        lock = self._post_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            record = self.registry.get(guild_id)
            if record is None or record.voice_channel_id == voice_channel_id:
                return False
            self.registry.pop_if(guild_id, record)
            await self._disable_record(record)
            return True

    def drop_message(self, message_id: int) -> bool:
        for record in self.registry.values():
            if record.message.id == message_id:
                dropped = self.registry.pop_if(record.guild_id, record)
                if dropped:
                    record.view.stop()
                return dropped
        return False

    async def close(self) -> None:
        self._closing = True
        records = self.registry.values()
        for record in records:
            self.registry.pop_if(record.guild_id, record)
        tasks = tuple(self._refresh_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(self._disable_record(record) for record in records),
            return_exceptions=True,
        )
        self._post_locks.clear()

    @staticmethod
    async def _disable_record(record: MusicPanelRecord) -> None:
        async with record.lock:
            _disable(record.view)
            try:
                with contextlib.suppress(
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    await record.message.edit(view=record.view)
            finally:
                record.view.stop()


__all__ = [
    "AddInputResult",
    "AddMusicModal",
    "ClearQueueConfirmation",
    "JumpModal",
    "MusicPanelManager",
    "MusicPanelRecord",
    "MusicPanelRegistry",
    "MusicPanelView",
    "MusicUIActions",
    "QueuePaginatorView",
    "SearchResultSelect",
    "SearchResultView",
    "build_music_embed",
]
