# pip install -U "discord.py[voice]" yt-dlp python-dotenv

import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!tfd ", intents=intents)

YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.7):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data['url']

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS).extract_info(url, download=False))
        if 'entries' in data:
            data = data['entries'][0]
        return cls(discord.FFmpegPCMAudio(data['url'], **FFMPEG_OPTIONS), data=data)

class GuildMusic:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current = None

    async def play_next(self, vc):
        if self.queue.empty():
            self.current = None
            return

        self.current = await self.queue.get()
        vc.play(self.current, after=lambda e: asyncio.run_coroutine_threadsafe(self.play_next(vc), bot.loop))

    def is_playing(self):
        return self.current is not None and self.current.is_playing()  # not used here

guild_music = {}  # guild.id -> GuildMusic

@bot.event
async def on_ready():
    print(f'Bot ready - {bot.user}')

@bot.command()
async def play(ctx, *, url: str):
    if not ctx.author.voice:
        return await ctx.send("Join voice first")

    vc = ctx.voice_client
    if not vc:
        vc = await ctx.author.voice.channel.connect()

    guild_id = ctx.guild.id
    if guild_id not in guild_music:
        guild_music[guild_id] = GuildMusic()

    music = guild_music[guild_id]

    player = await YTDLSource.from_url(url, loop=bot.loop)
    await music.queue.put(player)

    await ctx.send(f"Queued: **{player.title}**")

    if not vc.is_playing():
        await music.play_next(vc)
        await ctx.send(f"Now playing: **{player.title}**")

@bot.command()
async def pause(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("Paused")
    else:
        await ctx.send("Nothing playing")

@bot.command()
async def resume(ctx):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("Resumed")
    else:
        await ctx.send("Not paused")

@bot.command()
async def stop(ctx):
    vc = ctx.voice_client
    if vc:
        vc.stop()
        if ctx.guild.id in guild_music:
            guild_music[ctx.guild.id].queue = asyncio.Queue()  # clear queue
        await vc.disconnect()
        await ctx.send("Stopped & left")

@bot.command()
async def next(ctx, *, url: str):
    if not ctx.author.voice:
        return await ctx.send("Join voice first")

    vc = ctx.voice_client
    if not vc:
        vc = await ctx.author.voice.channel.connect()

    guild_id = ctx.guild.id
    if guild_id not in guild_music:
        guild_music[guild_id] = GuildMusic()

    music = guild_music[guild_id]

    player = await YTDLSource.from_url(url, loop=bot.loop)
    await music.queue.put(player)

    await ctx.send(f"Added to queue: **{player.title}**")

@bot.command()
async def skip(ctx):
    vc = ctx.voice_client
    if not vc or not vc.is_playing():
        return await ctx.send("Nothing playing")

    vc.stop()
    await ctx.send("Skipped")

@bot.command(name="loop")
async def loop(ctx):
    """Toggle loop mode for the current track."""
    vc = ctx.voice_client
    if not vc or not vc.is_playing():
        return await ctx.send("Nothing playing")

    if hasattr(vc, 'looping'):
        vc.looping = not vc.looping
    else:
        vc.looping = True

    status = "enabled" if vc.looping else "disabled"
    await ctx.send(f"Loop mode {status} for current track.")

# Add this command
@bot.command(name="search")
async def youtube_search(ctx, *, query: str):
    """ !tfd search never gonna give you up """
    ydl_opts = {
        'quiet': True,
        'default_search': 'ytsearch5',   # search youtube, return top 5
        'format': 'bestaudio/best',
        'noplaylist': True,
        'extract_flat': True,            # don't download, just metadata
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = await bot.loop.run_in_executor(
                None,
                lambda: ydl.extract_info(query, download=False)
            )

        if 'entries' not in result or not result['entries']:
            return await ctx.send("No results found.")

        entries = result['entries'][:5]  # limit to 5 results

        lines = ["**YouTube search results:**"]
        for i, entry in enumerate(entries, 1):
            title = entry.get('title', '???')
            url   = entry.get('url', '')
            duration = entry.get('duration')
            dur_str = f" ({int(duration//60)}:{duration%60:02d})" if duration else ""
            lines.append(f"{i}. [{title}]({url}){dur_str}")

        await ctx.send("\n".join(lines))

    except Exception as e:
        await ctx.send(f"Error: {str(e)[:200]}")

bot.run(os.getenv("DISCORD_TOKEN"))