"""Room-bound Discord picker for the custom soundboard.

Anyone in the bound voice room can play clips. Add uses a modal; delete
asks for confirmation. The view is not requester-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Protocol
from urllib.parse import urlparse

import discord

from .soundboard import InstantHit, SoundboardEntry, SoundboardError, format_clip_duration

log = logging.getLogger(__name__)

SOUNDBOARD_VIEW_TIMEOUT = 180.0
SOUNDBOARD_PAGE_SIZE = 25
ADD_MODAL_TIMEOUT = 120.0
DELETE_CONFIRM_TIMEOUT = 30.0


class SoundboardActions(Protocol):
    """Cog operations required by the soundboard picker."""

    async def ui_ensure_panel_access(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        *,
        connect_if_missing: bool = False,
    ) -> bool: ...

    async def ui_list_soundboard(
        self, guild_id: int
    ) -> tuple[SoundboardEntry, ...]: ...

    async def ui_play_soundboard(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        sound_id: str,
    ) -> str: ...

    async def ui_add_soundboard(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        name: str,
        url: str,
    ) -> str: ...

    async def ui_search_soundboard(
        self,
        query: str,
    ) -> tuple[InstantHit, ...]: ...

    async def ui_remove_soundboard(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        sound_id: str,
    ) -> str: ...


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
        return
    await interaction.response.send_message(content, ephemeral=True)


def _disable(view: discord.ui.View) -> None:
    for child in view.children:
        if hasattr(child, "disabled"):
            child.disabled = True


def build_soundboard_embed(
    entries: Sequence[SoundboardEntry],
    *,
    page: int = 0,
    page_count: int = 1,
) -> discord.Embed:
    """Public/ephemeral catalog card for the soundboard picker."""
    if not entries:
        description = (
            "Chưa có âm thanh. Bấm **Thêm** rồi dán URL hoặc từ khóa MyInstants."
        )
    else:
        start = page * SOUNDBOARD_PAGE_SIZE
        window = entries[start : start + SOUNDBOARD_PAGE_SIZE]
        lines = [
            f"**{discord.utils.escape_markdown(entry.name)}** · "
            f"{format_clip_duration(entry.duration_ms)}"
            for entry in window
        ]
        description = "\n".join(lines)
    embed = discord.Embed(
        title="Bảng âm thanh",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text=f"Trang {page + 1}/{page_count} · {len(entries)} âm thanh"
    )
    return embed


class SoundboardSelect(discord.ui.Select["SoundboardView"]):
    """One page of saved clips. Choosing an option plays it immediately."""

    def __init__(self, entries: Sequence[SoundboardEntry]) -> None:
        options = [
            discord.SelectOption(
                label=entry.name[:100],
                value=entry.id,
                description=format_clip_duration(entry.duration_ms)[:100],
            )
            for entry in entries
        ]
        super().__init__(
            placeholder="Chọn âm thanh để phát",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, SoundboardView):
            await _send_ephemeral(interaction, "Bảng âm thanh đã hết hạn.")
            return
        await view.play_selected(interaction, self.values[0])


class AddSoundModal(discord.ui.Modal, title="Thêm âm thanh"):
    name = discord.ui.TextInput(
        label="Tên (bắt buộc nếu dán URL)",
        placeholder="Ví dụ: bruh — để trống khi tìm MyInstants",
        required=False,
        max_length=32,
    )
    url = discord.ui.TextInput(
        label="URL hoặc từ khóa MyInstants",
        placeholder="https://… hoặc vine boom",
        required=True,
        max_length=500,
    )

    def __init__(self, picker: SoundboardView) -> None:
        super().__init__(timeout=ADD_MODAL_TIMEOUT)
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction) -> None:
        picker = self.picker
        if not await picker.ensure_access(interaction):
            return
        query = str(self.url).strip()
        name = str(self.name).strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        parsed = urlparse(query)
        is_url = parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
        if not is_url:
            try:
                hits = await picker.actions.ui_search_soundboard(query)
            except Exception as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            if not hits:
                await interaction.followup.send(
                    "Không tìm thấy âm thanh MyInstants.", ephemeral=True,
                )
                return
            view = SoundSearchView(picker, hits)
            message = await interaction.followup.send(
                "Chọn nút số tương ứng để lưu vào bảng âm thanh:",
                view=view,
                embed=view.render_embed(),
                ephemeral=True,
                wait=True,
            )
            view.message = message
            return
        if len(name) < 2:
            await interaction.followup.send(
                "Hãy nhập tên (2–32 ký tự) khi thêm bằng URL.",
                ephemeral=True,
            )
            return
        message = await picker.actions.ui_add_soundboard(
            interaction,
            picker.guild_id,
            picker.voice_channel_id,
            name,
            query,
        )
        await picker.reload()
        if picker.message is not None:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await picker.message.edit(
                    embed=picker.render_embed(),
                    view=picker,
                )
        await interaction.followup.send(message, ephemeral=True)


class SoundSearchButton(discord.ui.Button["SoundSearchView"]):
    def __init__(self, index: int) -> None:
        super().__init__(
            label=str(index + 1),
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, SoundSearchView):
            await _send_ephemeral(interaction, "Kết quả đã hết hạn.")
            return
        await view.select_hit(interaction, self.index)


class SoundSearchView(discord.ui.View):
    """Numbered MyInstants hits mapped onto the parent soundboard picker."""

    def __init__(self, picker: SoundboardView, hits: Sequence[InstantHit]) -> None:
        super().__init__(timeout=SOUNDBOARD_VIEW_TIMEOUT)
        self.picker = picker
        self.hits = tuple(hits[:5])
        self.message: object | None = None
        self._lock = asyncio.Lock()
        self._consumed = False
        for index in range(len(self.hits)):
            self.add_item(SoundSearchButton(index))

    def render_embed(self) -> discord.Embed:
        lines = []
        for index, hit in enumerate(self.hits, 1):
            title = discord.utils.escape_markdown(hit.name)
            lines.append(f"{index}. {title}")
        return discord.Embed(
            title="Kết quả MyInstants",
            description="\n".join(lines) or "Không có kết quả.",
            color=discord.Color.blurple(),
        )

    async def select_hit(self, interaction: discord.Interaction, index: int) -> None:
        if not await self.picker.ensure_access(interaction):
            return
        async with self._lock:
            if self._consumed:
                await _send_ephemeral(interaction, "Kết quả này đã được sử dụng.")
                return
            self._consumed = True
        try:
            hit = self.hits[index]
        except IndexError:
            await _send_ephemeral(interaction, "Kết quả không còn hợp lệ.")
            return
        self.stop()
        _disable(self)
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.picker.actions.ui_add_soundboard(
                interaction,
                self.picker.guild_id,
                self.picker.voice_channel_id,
                hit.name[:32],
                hit.mp3_url or hit.page_url,
            )
        except SoundboardError as exc:
            message = str(exc)
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(view=self)
        await self.picker.reload()
        if self.picker.message is not None:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await self.picker.message.edit(
                    embed=self.picker.render_embed(),
                    view=self.picker,
                )
        await interaction.followup.send(message, ephemeral=True)


class DeleteSoundConfirmation(discord.ui.View):
    """Short-lived confirm before deleting one saved clip."""

    def __init__(self, picker: SoundboardView, sound_id: str, requester_id: int) -> None:
        super().__init__(timeout=DELETE_CONFIRM_TIMEOUT)
        self.picker = picker
        self.sound_id = sound_id
        self.requester_id = requester_id
        self.message: object | None = None
        self._decision_lock = asyncio.Lock()
        self._decided = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await _send_ephemeral(interaction, "Chỉ người mở xác nhận này mới dùng được.")
        return False

    async def _claim(self) -> bool:
        async with self._decision_lock:
            if self._decided:
                return False
            self._decided = True
            return True

    async def on_timeout(self) -> None:
        _disable(self)
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await self.message.edit(view=self)

    @discord.ui.button(label="Xóa âm thanh", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.picker.ensure_access(interaction):
            return
        if not await self._claim():
            await _send_ephemeral(interaction, "Xác nhận này đã được sử dụng.")
            return
        self.stop()
        _disable(self)
        await interaction.response.defer(ephemeral=True)
        message = await self.picker.actions.ui_remove_soundboard(
            interaction,
            self.picker.guild_id,
            self.picker.voice_channel_id,
            self.sound_id,
        )
        await self.picker.reload()
        if self.picker.message is not None:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await self.picker.message.edit(
                    embed=self.picker.render_embed(),
                    view=self.picker,
                )
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(view=self)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Hủy", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self._claim():
            await _send_ephemeral(interaction, "Xác nhận này đã được sử dụng.")
            return
        self.stop()
        _disable(self)
        await interaction.response.edit_message(content="Đã hủy.", view=self)


class SoundboardView(discord.ui.View):
    """Paginated catalog. Room members play, add, and delete clips."""

    def __init__(
        self,
        actions: SoundboardActions,
        guild_id: int,
        voice_channel_id: int,
        entries: Sequence[SoundboardEntry] = (),
        *,
        panel_view: object | None = None,
    ) -> None:
        super().__init__(timeout=SOUNDBOARD_VIEW_TIMEOUT)
        self.actions = actions
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self.panel_view = panel_view
        self.entries: tuple[SoundboardEntry, ...] = tuple(entries)
        self.page = 0
        self.selected_id: str | None = None
        self.message: object | None = None
        self._rebuild()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.entries) + SOUNDBOARD_PAGE_SIZE - 1) // SOUNDBOARD_PAGE_SIZE)

    def page_entries(self) -> tuple[SoundboardEntry, ...]:
        start = self.page * SOUNDBOARD_PAGE_SIZE
        return self.entries[start : start + SOUNDBOARD_PAGE_SIZE]

    def render_embed(self) -> discord.Embed:
        return build_soundboard_embed(
            self.entries,
            page=self.page,
            page_count=self.page_count,
        )

    def _rebuild(self) -> None:
        self.clear_items()
        if self.page >= self.page_count:
            self.page = self.page_count - 1
        window = self.page_entries()
        if window:
            self.add_item(SoundboardSelect(window))
        self.add_item(self.previous_page)
        self.add_item(self.next_page)
        self.add_item(self.add_sound)
        self.add_item(self.delete_sound)
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.page_count - 1
        self.delete_sound.disabled = not self.entries

    async def reload(self) -> None:
        self.entries = await self.actions.ui_list_soundboard(self.guild_id)
        if self.selected_id and all(
            entry.id != self.selected_id for entry in self.entries
        ):
            self.selected_id = None
        self._rebuild()

    async def ensure_access(
        self,
        interaction: discord.Interaction,
        *,
        connect_if_missing: bool = True,
    ) -> bool:
        panel = self.panel_view
        ensure = getattr(panel, "ensure_access", None)
        if callable(ensure):
            return await ensure(
                interaction,
                connect_if_missing=connect_if_missing,
            )
        return await self.actions.ui_ensure_panel_access(
            interaction,
            self.guild_id,
            self.voice_channel_id,
            connect_if_missing=connect_if_missing,
        )

    async def play_selected(
        self,
        interaction: discord.Interaction,
        sound_id: str,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        self.selected_id = sound_id
        await interaction.response.defer(ephemeral=True)
        message = await self.actions.ui_play_soundboard(
            interaction,
            self.guild_id,
            self.voice_channel_id,
            sound_id,
        )
        await interaction.followup.send(message, ephemeral=True)

    async def on_timeout(self) -> None:
        _disable(self)
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await self.message.edit(view=self)

    @discord.ui.button(label="Trước", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.ensure_access(interaction, connect_if_missing=False):
            return
        self.page = max(0, self.page - 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="Sau", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.ensure_access(interaction, connect_if_missing=False):
            return
        self.page = min(self.page_count - 1, self.page + 1)
        self._rebuild()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="Thêm", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add_sound(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await interaction.response.send_modal(AddSoundModal(self))

    @discord.ui.button(label="Xóa", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete_sound(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await self.ensure_access(interaction, connect_if_missing=False):
            return
        sound_id = self.selected_id
        if sound_id is None:
            await _send_ephemeral(
                interaction,
                "Chọn một âm thanh trong danh sách trước khi xóa.",
            )
            return
        view = DeleteSoundConfirmation(self, sound_id, interaction.user.id)
        await interaction.response.send_message(
            "Xóa âm thanh đã chọn khỏi thư viện máy chủ?",
            view=view,
            ephemeral=True,
        )
        with contextlib.suppress(discord.HTTPException, AttributeError):
            view.message = await interaction.original_response()
