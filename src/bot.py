"""Discord client construction and lifecycle."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .cogs.music import MusicCog
from .config import Settings
from .media import MediaService
from .player import PlayerManager
from .session import SessionManager
from .tts import TextToSpeech

log = logging.getLogger(__name__)


class VoiceBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix=settings.command_prefix, intents=intents)

        self.settings = settings
        self.media = MediaService()
        self.tts = TextToSpeech(lang=settings.tts_lang)
        self.sessions = SessionManager(
            self,
            self.tts,
            volume=settings.default_volume,
        )
        self.players = PlayerManager(
            self,
            self.media,
            volume=settings.default_volume,
            idle_timeout=settings.player_idle_timeout,
            tts=self.tts,
            tts_enabled=settings.tts_enabled,
            keep_connected=self.sessions.keep_connected,
        )

    async def setup_hook(self) -> None:
        await self.add_cog(
            MusicCog(
                self,
                self.settings,
                self.media,
                self.players,
                self.sessions,
            )
        )

    async def on_ready(self) -> None:
        log.info("Bot ready as %s (guilds: %s)", self.user, len(self.guilds))

    async def close(self) -> None:
        await self.players.close_all()
        await self.sessions.close_all()
        await super().close()


def create_bot(settings: Settings) -> VoiceBot:
    return VoiceBot(settings)
