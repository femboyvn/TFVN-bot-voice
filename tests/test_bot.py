from __future__ import annotations

import unittest

from src.bot import create_bot
from src.config import Settings
from src.help_ui import COMMAND_HELP, InteractiveHelpCommand


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
                }
                <= command_names
            )
            self.assertIn("help", command_names)
            self.assertIsInstance(bot.help_command, InteractiveHelpCommand)
            self.assertEqual(command_names, set(COMMAND_HELP))
            self.assertTrue(hasattr(bot, "sessions"))

        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
