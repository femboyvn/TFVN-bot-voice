import discord
from discord.ext import commands
import pyaudio
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

TARGET_CHANNEL_ID = 1399783602417434675

class MicrophoneStream(discord.AudioSource):
    def __init__(self):
        self.p = pyaudio.PyAudio()
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=2,
            rate=48000,
            input=True,
            frames_per_buffer=960  # 20ms
        )

    def read(self):
        return self.stream.read(960, exception_on_overflow=False)

    def cleanup(self):
        self.stream.stop_stream()
        self.stream.close()
        self.p.terminate()

@bot.event
async def on_ready():
    print(f"Bot ready - {bot.user}")
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    
    if isinstance(channel, discord.VoiceChannel):
        await channel.connect()
        print(f"Joined voice channel: {channel.name}")
        
        vc = channel.guild.voice_client
        if vc:
            source = MicrophoneStream()
            vc.play(source, after=lambda e: source.cleanup())
            print("Started streaming microphone")
    else:
        print("Channel not found or not a voice channel")

bot.run("xxxx")