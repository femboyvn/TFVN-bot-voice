from __future__ import annotations

import unittest

from tfd_voice_bot.bot import create_bot
from tfd_voice_bot.config import Settings


class BotConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_music_commands(self) -> None:
        bot = create_bot(Settings(discord_token="test-token"))
        await bot.setup_hook()
        try:
            command_names = {command.name for command in bot.commands}
            self.assertTrue(
                {"play", "next", "pause", "resume", "skip", "loop", "stop", "search"}
                <= command_names
            )
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
