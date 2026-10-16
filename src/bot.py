"""Discord client construction and lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord.ext import commands

from .cogs.music import MusicCog
from .config import Settings
from .help_ui import InteractiveHelpCommand
from .media import MediaService
from .player import PlayerManager
from .soundboard import S3ObjectStore, SoundboardService
from .playlists import PlaylistBackups, PlaylistStore
from .spotify import SpotifyService
from .session import SessionManager
from .tts import TextToSpeech, normalize_tts_language

log = logging.getLogger(__name__)


class VoiceBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(
            command_prefix=settings.command_prefix,
            intents=intents,
            help_command=InteractiveHelpCommand(),
        )

        self.settings = settings
        self.media = MediaService(
            spotify=SpotifyService(
                client_id=settings.spotify_client_id,
                client_secret=settings.spotify_client_secret,
            )
        )
        self.tts = TextToSpeech(lang=normalize_tts_language(settings.tts_lang))
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
            duck_level=settings.music_duck_level,
            keep_connected=self.sessions.keep_connected,
        )
        self.sessions.bind_players(self.players)
        self.soundboard = SoundboardService.from_settings(settings)
        self.playlists = PlaylistStore(
            Path(settings.playlist_db_path),
            max_per_user=settings.playlist_max_per_user,
            max_tracks=settings.playlist_max_tracks,
            max_per_server=settings.playlist_max_server,
        )
        self.playlist_backups = (
            PlaylistBackups(
                self.playlists,
                S3ObjectStore.from_settings(settings),
                settings.playlist_backup_minutes * 60,
            )
            if settings.playlist_backup_minutes else None
        )

    async def setup_hook(self) -> None:
        await self.add_cog(
            MusicCog(
                self,
                self.settings,
                self.media,
                self.players,
                self.sessions,
                self.soundboard,
                self.playlists,
            )
        )
        if self.playlist_backups is not None:
            self.playlist_backups.start()

    async def on_ready(self) -> None:
        if not getattr(self, "_slash_synced", False):
            self._slash_synced = True
            try:
                synced = await self.tree.sync()
                log.info("Synced %s application commands", len(synced))
            except Exception:
                log.exception("Could not sync application commands")
        log.info("Bot ready as %s (guilds: %s)", self.user, len(self.guilds))

    async def close(self) -> None:
        music_cog = self.get_cog("Music")
        if isinstance(music_cog, MusicCog):
            await music_cog.close()
        await self.players.close_all()
        await self.sessions.close_all()
        await super().close()
        if self.playlist_backups is not None:
            await self.playlist_backups.close()


def create_bot(settings: Settings) -> VoiceBot:
    return VoiceBot(settings)
