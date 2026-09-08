from __future__ import annotations

import unittest

from src.config import ConfigurationError, Settings


class SettingsTests(unittest.TestCase):
    def test_loads_defaults(self) -> None:
        settings = Settings.from_env({"DISCORD_TOKEN": "secret"})

        self.assertEqual(settings.command_prefix, "!tfd ")
        self.assertEqual(settings.default_volume, 0.7)
        self.assertEqual(settings.voice_connect_retries, 3)
        self.assertTrue(settings.tts_enabled)
        self.assertEqual(settings.tts_lang, "vi")
        self.assertEqual(settings.music_duck_level, 0.2)
        self.assertEqual(settings.spotify_client_id, "")
        self.assertEqual(settings.spotify_client_secret, "")
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
                "TTS_ENABLED": "false",
                "TTS_LANG": "vi",
                "MUSIC_DUCK_LEVEL": "0.15",
                "SPOTIFY_CLIENT_ID": "spot-id",
                "SPOTIFY_CLIENT_SECRET": "spot-secret",
            }
        )

        self.assertEqual(settings.command_prefix, "?")
        self.assertEqual(settings.default_volume, 1.25)
        self.assertEqual(settings.voice_connect_timeout, 10.0)
        self.assertEqual(settings.voice_connect_retries, 5)
        self.assertEqual(settings.player_idle_timeout, 60.0)
        self.assertFalse(settings.tts_enabled)
        self.assertEqual(settings.tts_lang, "vi")
        self.assertEqual(settings.music_duck_level, 0.15)
        self.assertEqual(settings.spotify_client_id, "spot-id")
        self.assertEqual(settings.spotify_client_secret, "spot-secret")
        self.assertNotIn("spot-secret", repr(settings))

    def test_canonicalizes_tts_language_code(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "secret",
                "TTS_LANG": " zh_cn ",
            }
        )

        self.assertEqual(settings.tts_lang, "zh-CN")

    def test_rejects_unsupported_tts_language_code(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "TTS_LANG"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "TTS_LANG": "not-a-language",
                }
            )

    def test_requires_token(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DISCORD_TOKEN"):
            Settings.from_env({})

    def test_rejects_invalid_volume(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "DEFAULT_VOLUME"):
            Settings.from_env({"DISCORD_TOKEN": "secret", "DEFAULT_VOLUME": "3"})

    def test_rejects_partial_spotify_credentials(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "SPOTIFY_CLIENT"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "SPOTIFY_CLIENT_ID": "only-id",
                }
            )
        with self.assertRaisesRegex(ConfigurationError, "SPOTIFY_CLIENT"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "SPOTIFY_CLIENT_SECRET": "only-secret",
                }
            )

    def test_strips_spotify_credentials(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "secret",
                "SPOTIFY_CLIENT_ID": "  spot-id  ",
                "SPOTIFY_CLIENT_SECRET": "  spot-secret  ",
            }
        )
        self.assertEqual(settings.spotify_client_id, "spot-id")
        self.assertEqual(settings.spotify_client_secret, "spot-secret")
        self.assertNotIn("spot-secret", repr(settings))

    def test_whitespace_only_spotify_pair_is_treated_as_unset(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "secret",
                "SPOTIFY_CLIENT_ID": "   ",
                "SPOTIFY_CLIENT_SECRET": "   ",
            }
        )
        self.assertEqual(settings.spotify_client_id, "")
        self.assertEqual(settings.spotify_client_secret, "")

    def test_whitespace_only_spotify_secret_is_treated_as_missing(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "SPOTIFY_CLIENT"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "SPOTIFY_CLIENT_ID": "spot-id",
                    "SPOTIFY_CLIENT_SECRET": "   ",
                }
            )


if __name__ == "__main__":
    unittest.main()
