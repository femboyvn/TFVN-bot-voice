from __future__ import annotations

import unittest

from tfd_voice_bot.media import format_duration


class FormatDurationTests(unittest.TestCase):
    def test_formats_minutes(self) -> None:
        self.assertEqual(format_duration(185), "3:05")

    def test_formats_hours(self) -> None:
        self.assertEqual(format_duration(3723), "1:02:03")

    def test_handles_unknown_duration(self) -> None:
        self.assertEqual(format_duration(None), "")


if __name__ == "__main__":
    unittest.main()
