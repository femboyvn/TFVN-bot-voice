# pip install -U "discord.py[voice]" yt-dlp

import discord
from discord.ext import commands
import yt_dlp
import dotenv
import asyncio
import os

dotenv.load_dotenv()

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
        self.url = data.get('url')
        self.duration = data.get('duration')
        self.is_playing = False

    def playing(self):
        return self.is_playing
    
    

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS).extract_info(url, download=False))
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url']
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)

class MusicBot:
    queue = asyncio.Queue()

    current_player = None

    @bot.command()
    async def play(self, ctx, *, url: str):
        if not ctx.author.voice:
            return await ctx.send("Join voice first")

        if not ctx.voice_client:
            await ctx.author.voice.channel.connect()

        async with ctx.typing():
            current_player = await YTDLSource.from_url(url, loop=bot.loop)
            ctx.voice_client.play(current_player, after=lambda e: print(f'Player error: {e}') if e else None)

        await ctx.send(f'Now playing: **{current_player.title}**')

    @bot.command('stop')
    async def stop(self, ctx):
        if ctx.voice_client:
            await ctx.voice_client.disconnect()

    @bot.command('pause')
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("Paused the music")

    @bot.command('resume')
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("Resumed the music")

    @bot.event
    async def on_ready():
        print(f'Bot ready - {bot.user}')
        

bot.run(os.getenv("DISCORD_TOKEN"))