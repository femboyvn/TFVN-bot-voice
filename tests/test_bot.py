from __future__ import annotations

import unittest

from src.bot import create_bot
from src.config import Settings


class BotConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_music_commands(self) -> None:
        bot = create_bot(Settings(discord_token="test-token"))
        await bot.setup_hook()
        try:
            command_names = {command.name for command in bot.commands}
            self.assertTrue(
                {
                    "play",
                    "next",
                    "pause",
                    "resume",
                    "skip",
                    "loop",
                    "stop",
                    "search",
                    "join",
                    "leave",
                    "nameannounce",
                }
                <= command_names
            )
            self.assertTrue(hasattr(bot, "sessions"))

        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
