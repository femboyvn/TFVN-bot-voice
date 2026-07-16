"""Application bootstrap."""

from __future__ import annotations

import sys

from .bot import create_bot
from .config import ConfigurationError, Settings
from .logging import configure_logging


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    configure_logging(settings.log_level)
    bot = create_bot(settings)
    bot.run(settings.discord_token, log_handler=None)
