# TFD Voice Bot

A focused Discord voice bot built with `discord.py` and `yt-dlp`. It supports URL playback,
YouTube search, per-server queues, pause/resume, skip, and current-track looping.

## Requirements

- Python 3.12+
- FFmpeg available on `PATH`
- A Discord application with the **Message Content Intent** enabled

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m tfd_voice_bot
```

Set `DISCORD_TOKEN` in `.env` before starting the bot. Never commit that file.

## Commands

The default prefix is `!tfd `, including the trailing space.

| Command | Description |
| --- | --- |
| `!tfd play <URL or query>` | Join voice and queue a track |
| `!tfd next <URL or query>` | Add another track to the queue |
| `!tfd pause` | Pause playback |
| `!tfd resume` | Resume playback |
| `!tfd skip` | Skip the current track |
| `!tfd loop` | Toggle looping for the current track |
| `!tfd stop` | Clear the queue and leave voice |
| `!tfd search <query>` | Show five YouTube search results |

## Project layout

```text
tfd_voice_bot/
  app.py          # process bootstrap and logging
  bot.py          # Discord client lifecycle
  config.py       # validated environment settings
  logging.py      # console logging configuration
  media.py        # yt-dlp and FFmpeg integration
  player.py       # per-guild queues and playback workers
  voice.py        # voice connection and retry policy
  cogs/music.py   # user-facing commands
tests/            # fast unit and construction tests
```

`main.py` remains as a compatibility entry point, so `python main.py` also works.

## Verification

```powershell
python -m compileall -q main.py tfd_voice_bot tests
python -m unittest discover -v
```

## Docker

```powershell
docker compose up --build -d
docker compose logs -f bot
```

Stop it with `docker compose down`. Set `BOT_IMAGE` to override the default local image
name. The container includes FFmpeg, runs as an unprivileged user, and uses a read-only
root filesystem with temporary runtime storage under `/tmp`.
