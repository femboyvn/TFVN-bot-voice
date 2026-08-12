# TFD Voice Bot

A focused Discord voice bot built with `discord.py` and `yt-dlp`. It supports URL playback,
YouTube search, per-server queues, pause/resume, timestamp jumps, skip, looping, TTS
"now playing" announcements, and **voice-chat sessions** that join a VC and read that
channel's text chat aloud (via gTTS).

User-facing Discord replies and spoken TTS phrases are in **Vietnamese** (customer UI).
Source comments, logs, and this README stay in English.

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
python -m src
```

Set `DISCORD_TOKEN` in `.env` before starting the bot. Never commit that file.

Default TTS language is Vietnamese (`TTS_LANG=vi`). Override with `TTS_LANG=en` if needed.

## Commands

The default prefix is `!tfd `, including the trailing space.

| Command | Description |
| --- | --- |
| `!tfd join` | Join your VC and monitor that channel's **text chat** (TTS) |
| `!tfd leave` | End the chat session and leave voice |
| `!tfd nameannounce on` / `off` | Toggle speaker-name prefix in chat TTS (default **on** for new sessions) |
| `!tfd play <URL or query>` | Join voice and queue a track |
| `!tfd next <URL or query>` | Add another track to the queue |
| `!tfd pause` | Pause playback |
| `!tfd resume` | Resume playback |
| `!tfd jump HH:MM:SS` | Jump to a timestamp in the current track |
| `!tfd skip` | Skip the current track |
| `!tfd loop` | Toggle looping for the current track |
| `!tfd stop` | Stop music only (TTS session keeps running if you used `join`) |
| `!tfd search <query>` | Show five YouTube search results |

### Voice-chat session (join + monitor)

1. Join a voice channel yourself.
2. Run `!tfd join` (in any text channel, or in the VC chat).
3. Type in that **voice channel's text chat** — the bot speaks a Vietnamese line like  
   `"{display name} nói {message}"` (name prefix is on by default).
4. `!tfd nameannounce off` reads only the message body; `on` restores the name prefix.
5. Bot commands (`!tfd …`) are not read aloud.
6. `!tfd stop` stops music but **keeps** the TTS session and stays in VC.
7. `!tfd leave` ends monitoring and disconnects.

While a session is active, the bot stays in the VC even after the music queue goes idle.
Chat TTS requires `TTS_ENABLED=true` (default) and network access for gTTS.

If music is playing when someone types in VC chat, the bot **ducks** the track (lowers
music volume), speaks the message over it, then restores full music volume. Tune with
`MUSIC_DUCK_LEVEL` (default `0.2` = 20% music while speaking).

## Project layout

```text
src/
  app.py          # process bootstrap and logging
  bot.py          # Discord client lifecycle
  config.py       # validated environment settings
  logging.py      # console logging configuration
  media.py        # yt-dlp and FFmpeg integration
  player.py       # per-guild queues and playback workers
  session.py      # join-session: monitor VC text chat via TTS
  ducking.py      # mix TTS over music with volume ducking
  tts.py          # text-to-speech for voice announcements
  voice.py        # voice connection and retry policy
  cogs/music.py   # user-facing commands (Vietnamese replies)
tests/            # fast unit and construction tests
```

When a track starts, the bot posts a Vietnamese **Đang phát** line in the text channel and
also speaks that announcement in the connected voice channel. TTS failures fall back to
text-only and do not stall the music queue. Disable with `TTS_ENABLED=false`.

`main.py` remains as a compatibility entry point, so `python main.py` also works.

## Verification

```powershell
python -m compileall -q main.py src tests
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
