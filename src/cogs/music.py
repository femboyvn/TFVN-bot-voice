"""User-facing music and voice-session commands."""

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
        """Join your voice channel and monitor that channel's text chat via TTS."""
        voice_client = await get_or_connect_voice_client(ctx, self.settings)
        if voice_client is None:
            return

        channel = voice_client.channel
        if channel is None:
            await ctx.send("Connected, but no voice channel is bound.")
            return

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await ctx.send("Could not start a session on that channel type.")
            return

        already = self.sessions.is_active(ctx.guild.id)
        session = self.sessions.start(ctx.guild, channel)
        action = "Already in session" if already else "Joined"
        await ctx.send(
            f"{action}: monitoring **{discord.utils.escape_markdown(session.voice_channel_name)}** "
            "text chat. Messages there are spoken with TTS. Use "
            f"`{ctx.prefix}leave` to stop."
        )

    @commands.command()
    @commands.guild_only()
    async def leave(self, ctx: commands.Context[Any]) -> None:
        """End the voice-chat session, stop music, and disconnect."""
        await self._leave_voice(ctx, stopped_music=False)

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
        """Stop music, end any chat session, and leave the voice channel."""
        await self._leave_voice(ctx, stopped_music=True)

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
        await ctx.send(f"{confirmation}: **{title}** (queue position {position})")

    async def _leave_voice(
        self,
        ctx: commands.Context[Any],
        *,
        stopped_music: bool,
    ) -> None:
        had_player = await self.players.remove(ctx.guild.id, disconnect=False)
        had_session = await self.sessions.stop(ctx.guild.id)

        voice_client = ctx.voice_client
        disconnected = False
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect(force=True)
            disconnected = True

        if had_player or had_session or disconnected:
            if stopped_music and (had_player or had_session):
                await ctx.send("Stopped, ended chat session, and left the voice channel.")
            elif had_session:
                await ctx.send("Left the voice channel and stopped monitoring chat.")
            else:
                await ctx.send("Left the voice channel.")
            return
        await ctx.send("The bot is not connected to voice.")

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
