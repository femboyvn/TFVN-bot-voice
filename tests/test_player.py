from __future__ import annotations

import unittest
from unittest.mock import Mock

from tfd_voice_bot.player import GuildPlayer


class GuildPlayerControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.player = object.__new__(GuildPlayer)
        self.player.loop_current = False
        self.player.guild = Mock()

    def test_toggle_loop_changes_state(self) -> None:
        self.assertTrue(self.player.toggle_loop())
        self.assertFalse(self.player.toggle_loop())

    def test_skip_stops_active_voice_client_and_disables_loop(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = True
        self.player.guild.voice_client = voice_client
        self.player.loop_current = True

        self.assertTrue(self.player.skip())
        self.assertFalse(self.player.loop_current)
        voice_client.stop.assert_called_once_with()

    def test_skip_returns_false_when_idle(self) -> None:
        voice_client = Mock()
        voice_client.is_playing.return_value = False
        voice_client.is_paused.return_value = False
        self.player.guild.voice_client = voice_client

        self.assertFalse(self.player.skip())
        voice_client.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
