from __future__ import annotations

import unittest
from unittest.mock import patch

from src.bot import create_bot
from src.config import Settings
from src.help_ui import COMMAND_HELP, InteractiveHelpCommand
from src.soundboard import DictObjectStore


class BotConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_music_commands(self) -> None:
        bot = create_bot(Settings(discord_token="test-token"))
        await bot.setup_hook()
        try:
            command_names = {command.name for command in bot.commands}
            self.assertTrue(
                {
                    "music",
                    "play",
                    "next",
                    "pause",
                    "resume",
                    "jump",
                    "skip",
                    "loop",
                    "stop",
                    "search",
                    "join",
                    "leave",
                    "nameannounce",
                    "soundboard",
                    "playlist",
                }
                <= command_names
            )
            self.assertIn("help", command_names)
            self.assertIsInstance(bot.help_command, InteractiveHelpCommand)
            self.assertEqual(command_names, set(COMMAND_HELP))
            self.assertTrue(hasattr(bot, "sessions"))
            self.assertIs(bot.get_cog("Music").playlists, bot.playlists)
            self.assertIsNone(bot.playlist_backups)

        finally:
            await bot.close()

    async def test_optional_playlist_backup_starts_and_closes_with_bot(self) -> None:
        settings = Settings.from_env({
            "DISCORD_TOKEN": "test-token", "PLAYLIST_BACKUP_MINUTES": "60",
            "R2_ACCOUNT_ID": "account", "R2_BUCKET": "bucket",
            "R2_ACCESS_KEY_ID": "key", "R2_SECRET_ACCESS_KEY": "secret",
        })
        with patch("src.bot.S3ObjectStore.from_settings", return_value=DictObjectStore()):
            bot = create_bot(settings)
        await bot.setup_hook()
        runner = bot.playlist_backups
        self.assertIsNotNone(runner)
        assert runner is not None
        try:
            self.assertIs(runner.store, bot.playlists)
            self.assertEqual(runner.interval_seconds, 3600)
            self.assertFalse(runner._task.done())
        finally:
            await bot.close()
        self.assertTrue(runner._task.done())


if __name__ == "__main__":
    unittest.main()
