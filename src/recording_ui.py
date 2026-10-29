"""Shared Discord panel for multi-track recording.

``!tfd record`` posts one embed per guild. Bắt đầu and Dừng live on that
message. The view stays alive for the whole process, like the music panel,
so a two-hour take does not disable the buttons.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import discord

from .recording import format_duration

log = logging.getLogger(__name__)

IDLE_DESCRIPTION = (
    "Chưa ghi. Bấm **Bắt đầu** để ghi mỗi người một track, "
    "cùng nhạc, TTS và soundboard."
)
RECORDING_REFRESH_SECONDS = 10.0


class RecordingActions(Protocol):
    """Cog operations used by the recording panel."""

    async def ui_ensure_panel_access(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
        *,
        connect_if_missing: bool = False,
    ) -> bool: ...

    def ui_recording_state(self, guild_id: int) -> RecordingPanelState: ...

    async def ui_start_recording(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str: ...

    async def ui_stop_recording(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        voice_channel_id: int,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class RecordingPanelState:
    """What the embed and buttons show for one guild."""

    recording: bool
    elapsed_seconds: float
    speaker_count: int
    voice_channel_name: str


@dataclass
class RecordingPanelRecord:
    guild_id: int
    voice_channel_id: int
    voice_channel_name: str
    destination: discord.abc.Messageable
    message: discord.Message
    view: RecordingPanelView


def due_for_refresh(
    *,
    last_edit: float,
    last_speakers: int,
    now: float,
    speakers: int,
    interval: float = RECORDING_REFRESH_SECONDS,
) -> bool:
    """True when a new speaker appeared or the elapsed line is 10 seconds old."""
    if speakers != last_speakers:
        return True
    return now - last_edit >= interval


def build_recording_embed(state: RecordingPanelState) -> discord.Embed:
    """Public card for the recording panel."""
    if state.recording:
        name = discord.utils.escape_markdown(state.voice_channel_name or "kênh thoại")
        description = (
            f"Đang ghi **{name}**.\n"
            f"Thời gian: {format_duration(state.elapsed_seconds)}.\n"
            f"Người nói: {state.speaker_count}."
        )
        color = discord.Color.red()
    else:
        description = IDLE_DESCRIPTION
        color = discord.Color.blurple()
    return discord.Embed(title="Ghi âm", description=description, color=color)


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
        return
    await interaction.response.send_message(content, ephemeral=True)


class RecordingPanelView(discord.ui.View):
    """Room-shared start and stop buttons for one voice channel."""

    def __init__(
        self,
        actions: RecordingActions,
        manager: RecordingPanelManager,
        guild_id: int,
        voice_channel_id: int,
        state: RecordingPanelState,
    ) -> None:
        super().__init__(timeout=None)
        self.actions = actions
        self.manager = manager
        self.guild_id = guild_id
        self.voice_channel_id = voice_channel_id
        self._registered = False
        self.sync(state)

    def sync(self, state: RecordingPanelState) -> None:
        self.start_recording.disabled = state.recording
        self.stop_recording.disabled = not state.recording

    async def _current(self, interaction: discord.Interaction) -> bool:
        if self._registered:
            record = self.manager.get(self.guild_id)
            if record is None or record.view is not self:
                await _send_ephemeral(
                    interaction,
                    "Bảng ghi âm này đã được thay thế hoặc xóa.",
                )
                return False
        return await self.actions.ui_ensure_panel_access(
            interaction,
            self.guild_id,
            self.voice_channel_id,
        )

    async def _run(
        self,
        interaction: discord.Interaction,
        action: Callable[
            [discord.Interaction, int, int],
            Awaitable[str],
        ],
    ) -> None:
        if not await self._current(interaction):
            return
        await interaction.response.defer()
        message = await action(interaction, self.guild_id, self.voice_channel_id)
        if message:
            await interaction.followup.send(message, ephemeral=True)
        await self.manager.refresh(self.guild_id)

    @discord.ui.button(label="Bắt đầu", style=discord.ButtonStyle.success, row=0)
    async def start_recording(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._run(interaction, self.actions.ui_start_recording)

    @discord.ui.button(label="Dừng", style=discord.ButtonStyle.danger, row=0)
    async def stop_recording(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._run(interaction, self.actions.ui_stop_recording)


class RecordingPanelManager:
    """One recording panel per guild. A repeat command edits that message."""

    def __init__(self, actions: RecordingActions) -> None:
        self.actions = actions
        self._records: dict[int, RecordingPanelRecord] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, guild_id: int) -> RecordingPanelRecord | None:
        return self._records.get(guild_id)

    def destination(self, guild_id: int) -> discord.abc.Messageable | None:
        record = self._records.get(guild_id)
        if record is None:
            return None
        return record.destination

    async def post_panel(
        self,
        destination: discord.abc.Messageable,
        guild_id: int,
        voice_channel_id: int,
        *,
        voice_channel_name: str,
    ) -> discord.Message:
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            state = self.actions.ui_recording_state(guild_id)
            state = RecordingPanelState(
                state.recording,
                state.elapsed_seconds,
                state.speaker_count,
                voice_channel_name,
            )
            current = self._records.get(guild_id)
            if current is not None and current.voice_channel_id == voice_channel_id:
                current.voice_channel_name = voice_channel_name
                current.destination = destination
                current.view.sync(state)
                try:
                    await current.message.edit(
                        embed=build_recording_embed(state),
                        view=current.view,
                    )
                    return current.message
                except (discord.HTTPException, discord.NotFound):
                    log.warning("Could not edit the recording panel in guild %s", guild_id)
                    await self._retire(current)
            elif current is not None:
                await self._retire(current)
            view = RecordingPanelView(
                self.actions,
                self,
                guild_id,
                voice_channel_id,
                state,
            )
            message = await destination.send(
                embed=build_recording_embed(state),
                view=view,
            )
            view._registered = True
            self._records[guild_id] = RecordingPanelRecord(
                guild_id,
                voice_channel_id,
                voice_channel_name,
                destination,
                message,
                view,
            )
            return message

    async def refresh(self, guild_id: int) -> None:
        record = self._records.get(guild_id)
        if record is None:
            return
        state = self.actions.ui_recording_state(guild_id)
        if not state.voice_channel_name:
            state = RecordingPanelState(
                state.recording,
                state.elapsed_seconds,
                state.speaker_count,
                record.voice_channel_name,
            )
        record.view.sync(state)
        try:
            await record.message.edit(
                embed=build_recording_embed(state),
                view=record.view,
            )
        except (discord.HTTPException, discord.NotFound):
            log.warning("Could not refresh the recording panel in guild %s", guild_id)

    async def close(self) -> None:
        for record in list(self._records.values()):
            await self._retire(record)
        self._records.clear()

    async def _retire(self, record: RecordingPanelRecord) -> None:
        self._records.pop(record.guild_id, None)
        for child in record.view.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        with contextlib.suppress(discord.HTTPException, discord.NotFound):
            await record.message.edit(view=record.view)
        record.view.stop()
