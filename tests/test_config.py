from __future__ import annotations

import unittest

from tfd_voice_bot.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_loads_defaults(self) -> None:
        settings = Settings.from_env({"DISCORD_TOKEN": "secret"})

        self.assertEqual(settings.command_prefix, "!tfd ")
        self.assertEqual(settings.default_volume, 0.7)
        self.assertEqual(settings.voice_connect_retries, 3)
        self.assertNotIn("secret", repr(settings))

    def test_loads_overrides(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "secret",
                "COMMAND_PREFIX": "?",
                "DEFAULT_VOLUME": "1.25",
                "VOICE_CONNECT_TIMEOUT": "10",
                "VOICE_CONNECT_RETRIES": "5",
                "PLAYER_IDLE_TIMEOUT": "60",
            }
        )

        self.assertEqual(settings.command_prefix, "?")
        self.assertEqual(settings.default_volume, 1.25)
        self.assertEqual(settings.voice_connect_timeout, 10.0)
        self.assertEqual(settings.voice_connect_retries, 5)
        self.assertEqual(settings.player_idle_timeout, 60.0)

    def test_requires_token(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DISCORD_TOKEN"):
            Settings.from_env({})

    def test_rejects_invalid_volume(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DEFAULT_VOLUME"):
            Settings.from_env({"DISCORD_TOKEN": "secret", "DEFAULT_VOLUME": "3"})


if __name__ == "__main__":
    unittest.main()
