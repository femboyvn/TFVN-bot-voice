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
        self.assertEqual(settings.soundboard_data_dir, "data/soundboard")
        self.assertEqual(settings.soundboard_max_seconds, 12)
        self.assertEqual(settings.soundboard_max_sounds, 40)
        self.assertEqual(settings.soundboard_max_bytes, 1_500_000)
        self.assertEqual(settings.soundboard_cache_days, 7)
        self.assertEqual(settings.r2_bucket, "")
        self.assertFalse(settings.r2_configured)
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
                "SOUNDBOARD_DATA_DIR": "/data/soundboard",
                "SOUNDBOARD_MAX_SECONDS": "8",
                "SOUNDBOARD_MAX_SOUNDS": "20",
                "SOUNDBOARD_MAX_BYTES": "1000000",
                "SOUNDBOARD_CACHE_DAYS": "14",
                "R2_ACCOUNT_ID": "acct",
                "R2_ACCESS_KEY_ID": "r2-key",
                "R2_SECRET_ACCESS_KEY": "r2-secret",
                "R2_BUCKET": "tfvn-sounds",
                "R2_PREFIX": "clips",
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
        self.assertEqual(settings.soundboard_data_dir, "/data/soundboard")
        self.assertEqual(settings.soundboard_max_seconds, 8)
        self.assertEqual(settings.soundboard_max_sounds, 20)
        self.assertEqual(settings.soundboard_max_bytes, 1_000_000)
        self.assertEqual(settings.soundboard_cache_days, 14)
        self.assertEqual(settings.r2_account_id, "acct")
        self.assertEqual(
            settings.r2_endpoint,
            "https://acct.r2.cloudflarestorage.com",
        )
        self.assertEqual(settings.r2_bucket, "tfvn-sounds")
        self.assertEqual(settings.r2_prefix, "clips")
        self.assertTrue(settings.r2_configured)
        self.assertNotIn("spot-secret", repr(settings))
        self.assertNotIn("r2-secret", repr(settings))

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

    def test_rejects_out_of_range_soundboard_limits(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "SOUNDBOARD_MAX_SECONDS"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "SOUNDBOARD_MAX_SECONDS": "0",
                }
            )
        with self.assertRaisesRegex(ConfigurationError, "SOUNDBOARD_MAX_SOUNDS"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "SOUNDBOARD_MAX_SOUNDS": "101",
                }
            )
        with self.assertRaisesRegex(ConfigurationError, "SOUNDBOARD_MAX_BYTES"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "SOUNDBOARD_MAX_BYTES": "100",
                }
            )

    def test_rejects_partial_r2_credentials(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "R2_BUCKET"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "R2_BUCKET": "only-bucket",
                }
            )
        with self.assertRaisesRegex(ConfigurationError, "R2_BUCKET"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "R2_ACCESS_KEY_ID": "key",
                    "R2_SECRET_ACCESS_KEY": "secret-key",
                    "R2_BUCKET": "bucket",
                }
            )

    def test_r2_endpoint_override_skips_account_id(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "secret",
                "R2_ENDPOINT": "https://r2.example.test",
                "R2_ACCESS_KEY_ID": "key",
                "R2_SECRET_ACCESS_KEY": "secret-key",
                "R2_BUCKET": "bucket",
            }
        )
        self.assertEqual(settings.r2_endpoint, "https://r2.example.test")
        self.assertTrue(settings.r2_configured)

    def test_rejects_out_of_range_cache_days(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "SOUNDBOARD_CACHE_DAYS"):
            Settings.from_env(
                {
                    "DISCORD_TOKEN": "secret",
                    "SOUNDBOARD_CACHE_DAYS": "-1",
                }
            )

    def test_blank_soundboard_dir_falls_back_to_default(self) -> None:
        settings = Settings.from_env(
            {
                "DISCORD_TOKEN": "secret",
                "SOUNDBOARD_DATA_DIR": "   ",
            }
        )
        self.assertEqual(settings.soundboard_data_dir, "data/soundboard")

    def test_playlist_defaults_and_overrides(self) -> None:
        defaults = Settings.from_env({"DISCORD_TOKEN": "secret"})
        self.assertEqual(defaults.playlist_db_path, "data/playlists/playlists.db")
        self.assertEqual(defaults.playlist_backup_minutes, 0)
        self.assertEqual(defaults.playlist_max_per_user, 20)
        self.assertEqual(defaults.playlist_max_tracks, 100)
        self.assertEqual(defaults.playlist_max_server, 20)
        settings = Settings.from_env({
            "DISCORD_TOKEN": "secret", "PLAYLIST_DB_PATH": " /data/playlists/playlists.db ",
            "PLAYLIST_MAX_PER_USER": "50", "PLAYLIST_MAX_TRACKS": "250",
            "PLAYLIST_MAX_SERVER": "8",
        })
        self.assertEqual(settings.playlist_db_path, "/data/playlists/playlists.db")
        self.assertEqual(settings.playlist_max_per_user, 50)
        self.assertEqual(settings.playlist_max_tracks, 250)
        self.assertEqual(settings.playlist_max_server, 8)
        blank = Settings.from_env({"DISCORD_TOKEN": "secret", "PLAYLIST_DB_PATH": " "})
        self.assertEqual(blank.playlist_db_path, defaults.playlist_db_path)

    def test_invalid_playlist_configuration_is_rejected(self) -> None:
        for key, value in (
            ("PLAYLIST_DB_PATH", ":memory:"), ("PLAYLIST_DB_PATH", "/"),
            ("PLAYLIST_DB_PATH", "bad\x00path"),
            ("PLAYLIST_MAX_PER_USER", "0"), ("PLAYLIST_MAX_PER_USER", "101"),
            ("PLAYLIST_MAX_TRACKS", "0"), ("PLAYLIST_MAX_TRACKS", "1001"),
            ("PLAYLIST_MAX_SERVER", "0"), ("PLAYLIST_MAX_SERVER", "101"),
            ("PLAYLIST_BACKUP_MINUTES", "-1"), ("PLAYLIST_BACKUP_MINUTES", "10081"),
            ("PLAYLIST_MAX_TRACKS", "1.5"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ConfigurationError, key):
                    Settings.from_env({"DISCORD_TOKEN": "secret", key: value})

    def test_playlist_backups_require_r2_and_accept_complete_configuration(self) -> None:
        environment = {"DISCORD_TOKEN": "secret", "PLAYLIST_BACKUP_MINUTES": "60"}
        with self.assertRaisesRegex(ConfigurationError, "requires R2"):
            Settings.from_env(environment)
        environment.update({"R2_BUCKET": "bucket", "R2_ACCOUNT_ID": "account",
                            "R2_ACCESS_KEY_ID": "key", "R2_SECRET_ACCESS_KEY": "secret"})
        self.assertEqual(Settings.from_env(environment).playlist_backup_minutes, 60)


if __name__ == "__main__":
    unittest.main()
