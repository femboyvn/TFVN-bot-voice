# TFD Voice Bot

A focused Discord voice bot built with `discord.py` and `yt-dlp`. It supports a shared
Discord music-control panel, URL and YouTube-playlist playback, Spotify links,
YouTube search,
per-server queues, pause/resume, timestamp jumps, skip, looping, a **custom
soundboard** (MyInstants/YouTube clips saved as MP3), TTS "now playing"
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

Spotify **track** links work without extra credentials (metadata via oEmbed, audio
via YouTube). Albums, playlists, and artist links need `SPOTIFY_CLIENT_ID` and
`SPOTIFY_CLIENT_SECRET` from a [Spotify Developer](https://developer.spotify.com/dashboard)
app. Spotify does not provide audio streams to third-party bots, so each track is
matched to a YouTube result.

Default TTS language is Vietnamese (`TTS_LANG=vi`). Override with `TTS_LANG=en` if needed.

## Commands

The default prefix is `!tfd `, including the trailing space.

| Command | Description |
| --- | --- |
| `!tfd help` | Open the interactive help menu (topic selector). `!tfd help <command>` shows one command |
| `!tfd music` | Join your VC and open its shared interactive music panel |
| `!tfd soundboard` | Join your VC, open the music panel, and open the custom soundboard |
| `!tfd playlist` | Open your saved personal playlists; `!tfd help playlist` lists editing commands |
| `!tfd join` | Join your VC and monitor that channel's **text chat** (TTS) |
| `!tfd leave` | Stop music, end chat reading, and leave voice |
| `!tfd nameannounce on` / `off` | Toggle speaker-name prefix in chat TTS (default **off**; also in **Cài đặt**) |
| `!tfd play <URL or query>` | Join voice and queue a YouTube or Spotify track/playlist |
| `!tfd next <URL or query>` | Add another track or playlist to the queue |
| `!tfd pause` | Pause playback |
| `!tfd resume` | Resume playback |
| `!tfd jump HH:MM:SS` | Jump to a timestamp in the current track |
| `!tfd skip` | Skip the current track |
| `!tfd loop` | Toggle looping for the current track |
| `!tfd stop` | Stop the current track and clear the queue without leaving voice |
| `!tfd search <query>` | Show five YouTube search results |

### Shared music panel

1. Join a voice channel and run `!tfd music`.
2. Use **Thêm nhạc** to enter a search phrase, YouTube URL, Spotify URL, or playlist URL.
   Plain queries show up to five ephemeral numbered results; press the matching
   **1**–**5** button to append one to the queue.
3. A playlist appends its available videos in order, inspecting at most the first 25
   entries per request. Unavailable entries are skipped.
4. Everyone in the bot's current voice channel can use pause/resume, next/skip, loop,
   timestamp jump, queue view, clear queue, stop, **Đọc tên bài**,
   **Đọc tin nhắn**, **Bảng âm thanh**, **Cài đặt**, and **Rời**. Members outside that channel,
   including administrators, cannot use these controls or move the bot.
   **Trợ giúp** is an exception: anyone who can see the panel may open the private
   help menu. `!tfd help` opens the same menu from a text command.
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
   TTS language accepts a supported gTTS language code such as `vi`, `en`, `ja`,
   or `ko`; and **Đọc tên người gửi** accepts `on` or `off` (whether chat TTS
   speaks `"{name} nói …"` before the message body). The same form controls
   automatic panel bumping in whole minutes: `0` disables it, while `1`–`1440`
   reposts the panel at that interval.
8. **Bảng âm thanh** (or `!tfd soundboard`) opens a room-bound picker of short
   clips saved for this Discord server. **Thêm** accepts a MyInstants page/mp3,
   a YouTube URL, or a direct audio link; the bot downloads at most 12 seconds,
   stores an MP3 plus a JSON index on disk, and plays the clip over music with
   the same ducking used for TTS. Anyone in the bound voice room can play;
   only the member who added a clip (or someone with **Manage Server**) can
   delete it. The library is capped at 40 clips per server. Clips persist
   across bot restarts (unlike in-memory audio settings). Optional Cloudflare
   R2 (`R2_BUCKET` plus access keys) is the durable store; the server keeps a
   local play cache and deletes unused MP3s after `SOUNDBOARD_CACHE_DAYS`
   (default 7). Without R2, files stay on the data volume.

Audio settings are shared per Discord server, not per user, and changing them from
the panel affects current and future playback in that server. Music volume and the
TTS duck level update an active music mixer immediately. A language change is used
by subsequent song-title and chat messages, including an already-active chat-reading
session; audio that has already started speaking may finish in the old language.
Music volume controls the music track only and does not change TTS loudness.

Runtime audio settings are kept in memory. They reset to `DEFAULT_VOLUME`,
`MUSIC_DUCK_LEVEL`, and `TTS_LANG` from the environment whenever the bot process
restarts; the automatic panel-bump interval also resets to off. No database
persistence is performed.

Both speech controls are unavailable when `TTS_ENABLED=false`. In that mode,
**Cài đặt** still allows music-volume changes, while the inactive TTS fields are hidden
and left unchanged.

Only one panel is active per Discord server during the current process. Opening a new
panel disables the old one. An automatic bump sends a fresh panel at the bottom of
the same text channel and then deletes the superseded panel, without changing music,
queue, voice-room binding, or TTS state. If deleting the old message fails, its
controls are disabled instead. Automatic bumps pause while the bot is disconnected.
After a bot restart, run `!tfd music` again. **Xóa hàng đợi** removes waiting tracks
but leaves the current track playing. **Dừng** stops the current track and clears the
queue without ending chat reading or immediately leaving voice; `!tfd leave` stops
music, ends chat reading, and disconnects. When chat reading is off, the normal player
idle timeout may disconnect the bot later.

All voice and playback commands use the same room rule as the panel. If the bot is
already connected to another voice channel, it stays there and tells the caller to join
that channel instead.

### Saved personal playlists

Click **My Playlist** on the music panel, or run `!tfd playlist` and click
**Danh sách của tôi** to open a private picker. Each member has their own library
in each Discord server. Only that member can browse, edit, delete, or load it.
The picker supports creating and renaming playlists, adding songs from URLs or
five numbered search results, removing songs, and moving a song by its position.
Playlist and track lists are paginated. Editing a song from an outdated form is
rejected and the picker refreshes so a changed position cannot affect another song.

**Lưu hàng đợi** creates a new playlist containing the current song followed by
all waiting songs, in order. **Phát** appends the selected playlist to the shared
queue without interrupting playback. Unavailable tracks are skipped when their
turn arrives. Playlist edits never change music already queued.

Text commands work without joining voice for library management. Playing or
saving the current queue requires the same voice-room access as other music
controls. A picker opened from a panel stays bound to that panel and room.

| Command | Action |
| --- | --- |
| `!tfd playlist list` | Open your library through a private picker |
| `!tfd playlist create "Nhạc tối"` | Create an empty playlist |
| `!tfd playlist add "Nhạc tối" <URL or query>` | Append a track or import an external playlist; a query uses the first search result |
| `!tfd playlist show "Nhạc tối"` | Open that playlist in the picker |
| `!tfd playlist save "Buổi tối"` | Save the current song and queue as a new playlist |
| `!tfd playlist play "Nhạc tối"` | Append the saved songs to the queue |
| `!tfd playlist rename "Nhạc tối" "Nhạc mới"` | Rename a playlist |
| `!tfd playlist remove "Nhạc mới" 2` | Remove song 2 |
| `!tfd playlist move "Nhạc mới" 3 1` | Move song 3 to position 1 |
| `!tfd playlist delete "Nhạc mới"` | Delete a playlist; the picker also offers a confirmation dialog |

Quote names containing spaces. Names are unique per member and server after
Unicode normalization and case folding. Creating or saving with an existing name
returns an error. The default limits are 20 playlists per member per server
(`PLAYLIST_MAX_PER_USER`, range 1–100) and 100 songs per playlist
(`PLAYLIST_MAX_TRACKS`, range 1–1000). External playlist imports inspect at most
25 entries per request, matching normal playback. An add or save exceeding the
song limit is rejected as a whole; existing songs remain intact.

Playlists persist in SQLite at `PLAYLIST_DB_PATH` (local default:
`data/playlists/playlists.db`). Both Compose files mount the dedicated
`playlist-data` volume at `/data/playlists` and set the database path to
`/data/playlists/playlists.db`. The image creates this directory for the unprivileged
`bot` user (UID/GID 10001). The database contains ownership, names, ordered canonical
track URLs, titles, and durations. Stream URLs are resolved afresh for playback.
Database operations run in worker threads with transactions, including concurrent
quota checks, and survive container replacement when the same volume is reused.

Optional R2 backups are enabled with `PLAYLIST_BACKUP_MINUTES` (0 disables, the
default; 1–10080 sets the interval). This requires the complete existing R2
configuration. Every interval, the bot uses SQLite's backup API to create a
consistent snapshot and uploads it to `<R2_PREFIX>/playlists/latest.sqlite3` in
`R2_BUCKET`, replacing the previous snapshot. Temporary snapshots stay on the
playlist volume and are removed after upload or failure. Backup failures are
logged and retried on the next interval; playback and playlist editing continue.
An uninitialized library does not overwrite an existing remote backup.

To restore on another host, stop the bot, download that R2 object, place it at
`PLAYLIST_DB_PATH` on the playlist volume with ownership `10001:10001`, and start
the bot using that volume. The database is the live source of playlist data;
restoring a snapshot is an explicit operator action. Retain the volume during
normal rebuilds; `docker compose down -v` deletes named volumes and their data.

### Voice-chat session (join + monitor)

1. Join a voice channel yourself.
2. Run `!tfd join` (in any text channel, or in the VC chat).
3. Type in that **voice channel's text chat** — the bot speaks the message body
   (name prefix is off by default).
4. `!tfd nameannounce on` or **Cài đặt → Đọc tên người gửi: on** speaks
   `"{display name} nói {message}"`; `off` reads only the message body. The choice
   is kept for later chat-reading sessions until the bot process restarts.
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
  media.py        # yt-dlp, Spotify metadata, and FFmpeg integration
  spotify.py      # Spotify URL parsing and catalog lookup
  player.py       # per-guild queues and playback workers
  session.py      # join-session: monitor VC text chat via TTS
  ducking.py      # mix TTS over music with volume ducking
  tts.py          # text-to-speech for voice announcements
  voice.py        # voice connection and retry policy
  cogs/music.py   # user-facing commands (Vietnamese replies)
  soundboard.py    # per-guild clip store, MyInstants/YouTube ingest
  soundboard_ui.py # soundboard picker, add modal, delete confirm
  playlists.py    # per-user SQLite playlists and optional R2 snapshots
  playlist_ui.py  # private playlist picker, editing, and search results
  help_ui.py      # interactive Vietnamese help menu
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
root filesystem with temporary runtime storage under `/tmp`. Soundboard clips live on
the `soundboard-data` volume at `/data/soundboard` (`SOUNDBOARD_DATA_DIR`). Local runs
default to `data/soundboard/` in the working directory. Set the `R2_*` variables to
keep the JSON index and MP3s in Cloudflare R2; the volume then only caches recently
played clips.
