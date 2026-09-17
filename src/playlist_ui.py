"""Requester-only saved playlist picker, editing modals, and search results."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import unicodedata
from typing import Protocol

import discord

from .media import MediaExtractionError, SearchResult, format_duration
from .music_ui import _RequesterView, _disable, _send_ephemeral
from .playlists import (
    MAX_PLAYLIST_NAME,
    SERVER_OWNER_ID,
    PlaylistError,
    SavedPlaylist,
)
from .spotify import is_spotify_input

log = logging.getLogger(__name__)
PLAYLIST_PAGE_SIZE = 25
TRACK_PAGE_SIZE = 10
VIEW_TIMEOUT = 180.0


class PlaylistActions(Protocol):
    async def ui_list_playlists(
        self, guild_id: int, owner_id: int,
    ) -> tuple[SavedPlaylist, ...]: ...

    async def ui_playlist_action(
        self, interaction: discord.Interaction, guild_id: int, action: str,
        args: list[str], voice_channel_id: int | None = None,
        *, expected_revision: int | None = None,
        owner_id: int | None = None,
    ) -> str: ...

    async def ui_playlist_search(
        self, interaction: discord.Interaction, guild_id: int, ref: str,
        query: str, voice_channel_id: int | None = None,
        *, owner_id: int | None = None,
    ) -> tuple[SearchResult, ...]: ...


class PlaylistLauncher(_RequesterView):
    """Prefix commands expose a button; the library itself opens privately."""

    def __init__(
        self, actions: PlaylistActions, guild_id: int, requester_id: int,
        *, ref: str = "", server: bool = False,
    ) -> None:
        super().__init__(requester_id, timeout=VIEW_TIMEOUT)
        self.actions = actions
        self.guild_id = guild_id
        self.ref = ref
        self.server = server
        self.open_library.label = (
            "Danh sách máy chủ" if server else "Danh sách của tôi"
        )

    @discord.ui.button(label="Danh sách của tôi", style=discord.ButtonStyle.primary)
    async def open_library(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if not await self.interaction_check(interaction):
            return
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await _send_ephemeral(interaction, "Hãy mở danh sách trong máy chủ.")
            return
        await interaction.response.defer(ephemeral=True)
        await open_playlist_picker(
            self.actions, interaction, self.guild_id, ref=self.ref,
            owner_id=SERVER_OWNER_ID if self.server else interaction.user.id,
        )


async def open_playlist_picker(
    actions: PlaylistActions, interaction: discord.Interaction, guild_id: int,
    *, panel_view: object | None = None, ref: str = "",
    owner_id: int | None = None,
) -> None:
    """The caller defers before reading storage."""
    library_owner = interaction.user.id if owner_id is None else owner_id
    try:
        entries = await actions.ui_list_playlists(guild_id, library_owner)
        view = PlaylistView(
            actions, guild_id, interaction.user.id, entries, panel_view=panel_view,
            library_owner_id=library_owner,
        )
        if ref:
            ref = unicodedata.normalize("NFC", ref).strip()
            selected = next((entry for entry in entries if (
                entry.id == ref or entry.name.casefold() == ref.strip().casefold()
            )), None)
            if selected is None:
                raise PlaylistError("Không tìm thấy danh sách phát của bạn.")
            view.selected_id = selected.id
            view.page = entries.index(selected) // PLAYLIST_PAGE_SIZE
            view.rebuild()
        view.message = await interaction.followup.send(
            embed=view.render_embed(), view=view, ephemeral=True, wait=True,
        )
    except PlaylistError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
    except Exception:
        log.exception("Could not open playlists in guild %s", guild_id)
        await interaction.followup.send(
            "Không tải được danh sách phát. Hãy thử lại.", ephemeral=True,
        )


class PlaylistSelect(discord.ui.Select["PlaylistView"]):
    def __init__(
        self, entries: tuple[SavedPlaylist, ...], selected_id: str | None,
    ) -> None:
        super().__init__(
            placeholder="Chọn danh sách phát", row=0,
            options=[discord.SelectOption(
                label=entry.name[:100], value=entry.id,
                description=f"{len(entry.tracks)} bài",
                default=entry.id == selected_id,
            ) for entry in entries],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if view is None or not await view.ensure_access(interaction):
            return
        view.selected_id = self.values[0]
        view.track_page = 0
        await interaction.response.defer()
        await view.reload_and_edit(interaction)


class PlaylistView(_RequesterView):
    def __init__(
        self, actions: PlaylistActions, guild_id: int, requester_id: int,
        entries: tuple[SavedPlaylist, ...] = (), *, panel_view: object | None = None,
        library_owner_id: int | None = None,
    ) -> None:
        super().__init__(requester_id, timeout=VIEW_TIMEOUT)
        self.actions = actions
        self.guild_id = guild_id
        self.entries = entries
        self.panel_view = panel_view
        self.library_owner_id = (
            requester_id if library_owner_id is None else library_owner_id
        )
        self.voice_channel_id: int | None = getattr(panel_view, "voice_channel_id", None)
        self.page = 0
        self.track_page = 0
        self.selected_id: str | None = entries[0].id if entries else None
        self._action_lock = asyncio.Lock()
        self.rebuild()

    @property
    def is_server_library(self) -> bool:
        return self.library_owner_id == SERVER_OWNER_ID

    @property
    def selected(self) -> SavedPlaylist | None:
        return next((entry for entry in self.entries if entry.id == self.selected_id), None)

    @property
    def page_count(self) -> int:
        return max(1, (len(self.entries) + PLAYLIST_PAGE_SIZE - 1) // PLAYLIST_PAGE_SIZE)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.ensure_access(interaction)

    async def ensure_access(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if self.is_finished():
            await _send_ephemeral(interaction, "Danh sách đã hết hạn. Hãy mở lại.")
            return False
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await _send_ephemeral(interaction, "Danh sách này chỉ dùng trong máy chủ.")
            return False
        if self.panel_view is not None:
            return await self.panel_view.ensure_access(
                interaction, connect_if_missing=True,
            )
        return True

    def rebuild(self) -> None:
        self.page = min(self.page, self.page_count - 1)
        if self.selected is None:
            self.selected_id = self.entries[0].id if self.entries else None
        selected = self.selected
        count = len(selected.tracks) if selected else 0
        self.track_page = min(self.track_page, max(0, (count - 1) // TRACK_PAGE_SIZE))
        self.clear_items()
        start = self.page * PLAYLIST_PAGE_SIZE
        window = self.entries[start:start + PLAYLIST_PAGE_SIZE]
        if window:
            self.add_item(PlaylistSelect(window, self.selected_id))
        for button in (
            self.previous_list, self.next_list, self.create, self.save_queue,
            self.toggle_scope,
            self.add_track, self.rename, self.delete, self.play,
            self.previous_tracks, self.next_tracks, self.remove_track, self.move_track,
        ):
            self.add_item(button)
        self.toggle_scope.label = "Của tôi" if self.is_server_library else "Máy chủ"
        self.previous_list.disabled = self.page == 0
        self.next_list.disabled = self.page == self.page_count - 1
        self.previous_tracks.disabled = self.track_page == 0
        self.next_tracks.disabled = (self.track_page + 1) * TRACK_PAGE_SIZE >= count
        for button in (self.add_track, self.rename, self.delete):
            button.disabled = selected is None
        for button in (self.play, self.remove_track, self.move_track):
            button.disabled = count == 0

    def render_embed(self) -> discord.Embed:
        selected = self.selected
        title = (
            "Danh sách phát của máy chủ" if self.is_server_library
            else "Danh sách phát của bạn"
        )
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        if selected is None:
            embed.description = "Chưa có danh sách. Bấm **Tạo** hoặc **Lưu hàng đợi**."
        else:
            title = discord.utils.escape_markdown(selected.name)
            lines = [f"**{title}** · {len(selected.tracks)} bài"]
            start = self.track_page * TRACK_PAGE_SIZE
            window = selected.tracks[start:start + TRACK_PAGE_SIZE]
            for index, track in enumerate(window, start + 1):
                name = discord.utils.escape_markdown(track.title[:140])
                duration = format_duration(track.duration)
                lines.append(f"{index}. {name}" + (f" · {duration}" if duration else ""))
            if not selected.tracks:
                lines.append("Bấm **Thêm bài** để tìm kiếm hoặc dán liên kết.")
            embed.description = "\n".join(lines)
        track_pages = max(1, ((len(selected.tracks) if selected else 0) + 9) // 10)
        embed.set_footer(text=(
            f"Danh sách {self.page + 1}/{self.page_count} · "
            f"Trang bài {self.track_page + 1}/{track_pages} · "
            "Phát sẽ thêm vào hàng đợi; bài hiện tại vẫn tiếp tục."
        ))
        return embed

    async def reload_and_edit(
        self, interaction: discord.Interaction, *, select_name: str | None = None,
    ) -> None:
        try:
            self.entries = await self.actions.ui_list_playlists(
                self.guild_id, self.library_owner_id,
            )
            if select_name is not None:
                name_key = unicodedata.normalize("NFC", select_name).strip().casefold()
                for index, entry in enumerate(self.entries):
                    if entry.name.casefold() == name_key:
                        self.selected_id = entry.id
                        self.page = index // PLAYLIST_PAGE_SIZE
                        self.track_page = 0
                        break
            self.rebuild()
            if self.message is not None:
                with contextlib.suppress(discord.HTTPException):
                    await self.message.edit(embed=self.render_embed(), view=self)
        except PlaylistError as exc:
            await _send_ephemeral(interaction, str(exc))

    async def run_action(
        self, interaction: discord.Interaction, action: str, args: list[str],
        *, expected_revision: int | None = None,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        async with self._action_lock:
            if not await self.ensure_access(interaction):
                return
            message = await self.actions.ui_playlist_action(
                interaction, self.guild_id, action, args, self.voice_channel_id,
                expected_revision=expected_revision,
                owner_id=self.library_owner_id,
            )
            await self.reload_and_edit(
                interaction,
                select_name=args[0] if action in {"create", "save"} else None,
            )
        await interaction.followup.send(message, ephemeral=True)

    async def open_modal(self, interaction: discord.Interaction, action: str) -> None:
        if not await self.ensure_access(interaction):
            return
        if action not in {"create", "save"} and self.selected is None:
            await _send_ephemeral(interaction, "Hãy chọn một danh sách trước.")
            return
        await interaction.response.send_modal(PlaylistModal(self, action))

    async def turn_page(
        self, interaction: discord.Interaction, delta: int, *, tracks: bool,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        if tracks:
            self.track_page = max(0, self.track_page + delta)
        else:
            self.page = max(0, min(self.page_count - 1, self.page + delta))
            if self.entries:
                self.selected_id = self.entries[self.page * PLAYLIST_PAGE_SIZE].id
                self.track_page = 0
        self.rebuild()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="DS trước", row=1)
    async def previous_list(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await self.turn_page(interaction, -1, tracks=False)

    @discord.ui.button(label="DS sau", row=1)
    async def next_list(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.turn_page(interaction, 1, tracks=False)

    @discord.ui.button(label="Tạo", style=discord.ButtonStyle.success, row=1)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.open_modal(interaction, "create")

    @discord.ui.button(label="Lưu hàng đợi", row=1)
    async def save_queue(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await self.open_modal(interaction, "save")

    @discord.ui.button(label="Máy chủ", row=1)
    async def toggle_scope(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if not await self.ensure_access(interaction):
            return
        await interaction.response.defer()
        self.library_owner_id = (
            self.requester_id if self.is_server_library else SERVER_OWNER_ID
        )
        self.selected_id = None
        self.page = 0
        self.track_page = 0
        await self.reload_and_edit(interaction)

    @discord.ui.button(label="Thêm bài", style=discord.ButtonStyle.success, row=2)
    async def add_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.open_modal(interaction, "add")

    @discord.ui.button(label="Đổi tên", row=2)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.open_modal(interaction, "rename")

    @discord.ui.button(label="Xóa danh sách", style=discord.ButtonStyle.danger, row=2)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.ensure_access(interaction) or self.selected is None:
            return
        view = DeletePlaylistConfirmation(self, self.selected.id)
        name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(self.selected.name)
        )
        view.message = await _send_ephemeral(
            interaction, f"Xóa **{name}** ({len(self.selected.tracks)} bài)?", view=view,
        )

    @discord.ui.button(label="Phát", style=discord.ButtonStyle.primary, row=2)
    async def play(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.selected is not None:
            await self.run_action(interaction, "play", [self.selected.id])

    @discord.ui.button(label="Bài trước", row=3)
    async def previous_tracks(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await self.turn_page(interaction, -1, tracks=True)

    @discord.ui.button(label="Bài sau", row=3)
    async def next_tracks(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await self.turn_page(interaction, 1, tracks=True)

    @discord.ui.button(label="Xóa bài", row=3)
    async def remove_track(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await self.open_modal(interaction, "remove")

    @discord.ui.button(label="Đổi thứ tự", row=3)
    async def move_track(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await self.open_modal(interaction, "move")


class PlaylistModal(discord.ui.Modal):
    def __init__(self, picker: PlaylistView, action: str) -> None:
        titles = {"create": "Tạo danh sách", "save": "Lưu hàng đợi", "add": "Thêm bài",
                  "rename": "Đổi tên", "remove": "Xóa bài", "move": "Đổi thứ tự"}
        super().__init__(title=titles[action], timeout=VIEW_TIMEOUT)
        self.picker = picker
        self.action = action
        self.ref = picker.selected_id
        self.revision = picker.selected.revision if picker.selected else None
        is_name = action in {"create", "save", "rename"}
        self.value = discord.ui.TextInput(
            label=("Tên danh sách" if is_name else "URL hoặc từ khóa" if action == "add"
                   else "Số thứ tự bài hát"),
            max_length=MAX_PLAYLIST_NAME if is_name else 500 if action == "add" else 4,
            default=picker.selected.name if action == "rename" and picker.selected else None,
        )
        self.add_item(self.value)
        self.destination = discord.ui.TextInput(label="Chuyển đến vị trí", max_length=4)
        if action == "move":
            self.add_item(self.destination)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.picker.ensure_access(interaction):
            return
        value = str(self.value).strip()
        if self.action == "add" and not (
            value.lower().startswith(("http://", "https://")) or is_spotify_input(value)
        ):
            await interaction.response.defer(ephemeral=True)
            try:
                results = await self.picker.actions.ui_playlist_search(
                    interaction, self.picker.guild_id, self.ref or "", value,
                    self.picker.voice_channel_id,
                    owner_id=self.picker.library_owner_id,
                )
            except (PlaylistError, MediaExtractionError) as exc:
                await _send_ephemeral(interaction, str(exc))
                return
            if not results:
                await _send_ephemeral(interaction, "Không tìm thấy kết quả.")
                return
            view = PlaylistSearchView(self.picker, self.ref or "", results)
            view.message = await _send_ephemeral(
                interaction, "Chọn bài để lưu vào danh sách.",
                embed=view.render_embed(), view=view,
            )
            return
        args = [value] if self.action in {"create", "save"} else [self.ref or "", value]
        if self.action == "move":
            args.append(str(self.destination).strip())
        await self.picker.run_action(
            interaction, self.action, args,
            expected_revision=self.revision if self.action in {"remove", "move"} else None,
        )


class PlaylistSearchView(_RequesterView):
    def __init__(
        self, picker: PlaylistView, ref: str, results: tuple[SearchResult, ...],
    ) -> None:
        super().__init__(picker.requester_id, timeout=VIEW_TIMEOUT)
        self.picker = picker
        self.ref = ref
        self.results = results[:5]
        self._used = False
        for index, result in enumerate(self.results, 1):
            button = discord.ui.Button(label=str(index), style=discord.ButtonStyle.primary)

            async def choose(interaction: discord.Interaction, url: str = result.url) -> None:
                if not await self.interaction_check(interaction):
                    return
                if not await self.picker.ensure_access(interaction):
                    return
                if self._used:
                    await _send_ephemeral(interaction, "Kết quả này đã được sử dụng.")
                    return
                self._used = True
                _disable(self)
                self.stop()
                await self.picker.run_action(interaction, "add", [self.ref, url])
                if self.message is not None:
                    with contextlib.suppress(discord.HTTPException):
                        await self.message.edit(view=self)

            button.callback = choose
            self.add_item(button)

    def render_embed(self) -> discord.Embed:
        lines = [f"{index}. {discord.utils.escape_markdown(result.title[:140])}"
                 for index, result in enumerate(self.results, 1)]
        return discord.Embed(title="Chọn bài để lưu", description="\n".join(lines))


class DeletePlaylistConfirmation(_RequesterView):
    def __init__(self, picker: PlaylistView, ref: str) -> None:
        super().__init__(picker.requester_id, timeout=30)
        self.picker = picker
        self.ref = ref
        self._used = False

    @discord.ui.button(label="Xóa danh sách", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.interaction_check(interaction):
            return
        if not await self.picker.ensure_access(interaction) or self._used:
            return
        self._used = True
        self.stop()
        _disable(self)
        await self.picker.run_action(interaction, "delete", [self.ref])
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.message.edit(view=self)

    @discord.ui.button(label="Hủy")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.interaction_check(interaction) or self._used:
            return
        self._used = True
        self.stop()
        _disable(self)
        await interaction.response.edit_message(content="Đã hủy.", view=self)
