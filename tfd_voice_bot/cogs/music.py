"""User-facing music commands."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from ..config import Settings
from ..media import MediaExtractionError, MediaService, format_duration
from ..player import PlayerManager
from ..voice import get_or_connect_voice_client

log = logging.getLogger(__name__)


class MusicCog(commands.Cog, name="Music"):
    def __init__(
        self,
        bot: commands.Bot,
        settings: Settings,
        media: MediaService,
        players: PlayerManager,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.media = media
        self.players = players

    @commands.command()
    @commands.guild_only()
    async def play(self, ctx: commands.Context[Any], *, query: str) -> None:
        """Queue a URL for playback and join the caller's voice channel."""
        await self._enqueue(ctx, query, "Queued")

    @commands.command(name="next")
    @commands.guild_only()
    async def add_next(self, ctx: commands.Context[Any], *, query: str) -> None:
        """Add a URL to the playback queue."""
        await self._enqueue(ctx, query, "Added to queue")

    @commands.command()
    @commands.guild_only()
    async def pause(self, ctx: commands.Context[Any]) -> None:
        voice_client = ctx.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.pause()
            await ctx.send("Paused.")
            return
        await ctx.send("Nothing is playing.")

    @commands.command()
    @commands.guild_only()
    async def resume(self, ctx: commands.Context[Any]) -> None:
        voice_client = ctx.voice_client
        if voice_client and voice_client.is_paused():
            voice_client.resume()
            await ctx.send("Resumed.")
            return
        await ctx.send("Playback is not paused.")

    @commands.command()
    @commands.guild_only()
    async def skip(self, ctx: commands.Context[Any]) -> None:
        player = self.players.get(ctx.guild.id)
        if player and player.skip():
            await ctx.send("Skipped.")
            return
        await ctx.send("Nothing is playing.")

    @commands.command(name="loop")
    @commands.guild_only()
    async def loop_track(self, ctx: commands.Context[Any]) -> None:
        player = self.players.get(ctx.guild.id)
        if not player or player.current is None:
            await ctx.send("Nothing is playing.")
            return
        enabled = player.toggle_loop()
        await ctx.send(f"Loop mode {'enabled' if enabled else 'disabled'}.")

    @commands.command()
    @commands.guild_only()
    async def stop(self, ctx: commands.Context[Any]) -> None:
        if await self.players.remove(ctx.guild.id):
            await ctx.send("Stopped and left the voice channel.")
            return

        voice_client = ctx.voice_client
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)
            await ctx.send("Left the voice channel.")
            return
        await ctx.send("The bot is not connected to voice.")

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
            await ctx.send("No results found.")
            return

        lines = ["**YouTube search results:**"]
        for index, entry in enumerate(entries, 1):
            title = discord.utils.escape_markdown(entry.title)
            duration = format_duration(entry.duration)
            suffix = f" ({duration})" if duration else ""
            lines.append(f"{index}. [{title}](<{entry.url}>){suffix}")
        await ctx.send("\n".join(lines))

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
        await ctx.send(f"{confirmation}: **{title}** (queue position {position})")

    async def cog_command_error(
        self,
        ctx: commands.Context[Any],
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing `{error.param.name}`. Use `{ctx.prefix}help {ctx.command}`.")
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("This command can only be used in a server.")
            return

        original = getattr(error, "original", error)
        log.error(
            "Command %s failed",
            ctx.command,
            exc_info=(type(original), original, original.__traceback__),
        )
        await ctx.send("The command failed unexpectedly. Check the bot logs.")
