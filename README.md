# TFD Voice Bot

A focused Discord voice bot built with `discord.py` and `yt-dlp`. It supports a shared
Discord music-control panel, URL and YouTube-playlist playback, YouTube search,
per-server queues, pause/resume, timestamp jumps, skip, looping, TTS "now playing"
announcements, and **voice-chat sessions** that join a VC and read that channel's text
chat aloud (via gTTS).

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
| `!tfd music` | Join your VC and open its shared interactive music panel |
| `!tfd join` | Join your VC and monitor that channel's **text chat** (TTS) |
| `!tfd leave` | Stop music, end chat reading, and leave voice |
| `!tfd nameannounce on` / `off` | Toggle speaker-name prefix in chat TTS (default **on** for new sessions) |
| `!tfd play <URL or query>` | Join voice and queue a track |
| `!tfd next <URL or query>` | Add another track to the queue |
| `!tfd pause` | Pause playback |
| `!tfd resume` | Resume playback |
| `!tfd jump HH:MM:SS` | Jump to a timestamp in the current track |
| `!tfd skip` | Skip the current track |
| `!tfd loop` | Toggle looping for the current track |
| `!tfd stop` | Stop the current track and clear the queue without leaving voice |
| `!tfd search <query>` | Show five YouTube search results |

### Shared music panel

1. Join a voice channel and run `!tfd music`.
2. Use **Thêm nhạc** to enter a search phrase, media URL, or YouTube playlist URL.
   Search phrases show five private results; selecting one appends it to the queue.
3. A playlist appends its available videos in order, inspecting at most the first 25
   entries per request. Unavailable entries are skipped.
4. Everyone in the bot's current voice channel can use pause/resume, next/skip, loop,
   timestamp jump, queue view, clear queue, stop, **Đọc tên bài**,
   **Đọc tin nhắn**, **Cài đặt**, and **Rời**. Members outside that channel,
   including administrators, cannot use these controls or move the bot.
5. The public panel shows the current track and the next five queued tracks. Search
   results, queue pages, confirmations, and errors are visible only to the requester.
6. **Đọc tên bài** toggles the spoken song-title announcement; the text
   **Đang phát** announcement is still posted when speech is off. **Đọc tin nhắn**
   starts or stops reading the voice channel's text chat without stopping music or
   making the bot leave voice. **Rời** is the explicit action that stops music,
   clears the queue, turns off chat reading, and disconnects. A phrase already being
   spoken may finish after either reading control is turned off.
7. **Cài đặt** opens a private form for the room's shared runtime audio settings:
   music volume accepts `0`–`200` percent; music level while TTS is speaking accepts
   `0`–`100` percent (`0` mutes the music temporarily and `100` means no reduction);
   and TTS language accepts a supported gTTS language code such as `vi`, `en`, `ja`,
   or `ko`.

Audio settings are shared per Discord server, not per user, and changing them from
the panel affects current and future playback in that server. Music volume and the
TTS duck level update an active music mixer immediately. A language change is used
by subsequent song-title and chat messages, including an already-active chat-reading
session; audio that has already started speaking may finish in the old language.
Music volume controls the music track only and does not change TTS loudness.

Runtime audio settings are kept in memory. They reset to `DEFAULT_VOLUME`,
`MUSIC_DUCK_LEVEL`, and `TTS_LANG` from the environment whenever the bot process
restarts; no database persistence is performed.

Both speech controls are unavailable when `TTS_ENABLED=false`. In that mode,
**Cài đặt** still allows music-volume changes, while the inactive TTS fields are hidden
and left unchanged.

Only one panel is active per Discord server during the current process. Opening a new
panel disables the old one.
After a bot restart, run `!tfd music` again. **Xóa hàng đợi** removes waiting tracks
but leaves the current track playing. **Dừng** stops the current track and clears the
queue without ending chat reading or immediately leaving voice; `!tfd leave` stops
music, ends chat reading, and disconnects. When chat reading is off, the normal player
idle timeout may disconnect the bot later.

All voice and playback commands use the same room rule as the panel. If the bot is
already connected to another voice channel, it stays there and tells the caller to join
that channel instead.

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
