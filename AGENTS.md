# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/`. `src/bot.py` builds the Discord client, `src/cogs/music.py` defines user commands, `src/player.py` owns per-guild playback, and `src/media.py` integrates yt-dlp, Spotify-to-YouTube matching, and FFmpeg. `src/spotify.py` parses Spotify URLs/URIs and fetches catalog metadata (tracks, albums, playlists, artists). `src/soundboard.py` stores per-guild short clips (JSON index + MP3 files under `SOUNDBOARD_DATA_DIR`, optionally mirrored to Cloudflare R2) and ingests MyInstants, YouTube, and direct audio URLs; `src/soundboard_ui.py` is the picker. When R2 is configured, local files are a play cache expired after `SOUNDBOARD_CACHE_DAYS` unused. Personal and server playlists, plus last-room queue restore, live in `src/playlists.py`. Voice sessions, TTS, ducking, and configuration are kept in their matching modules. `main.py` is a compatibility entry point. Tests live in `tests/` and mirror source concerns, for example `tests/test_player.py`. Docker configuration is in `Dockerfile` and `compose.yaml`; CI workflows are under `.github/workflows/`.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt` installs Python dependencies.
- `python -m src` starts the bot locally after `.env` is configured.
- `python -m compileall -q main.py src tests` catches syntax and import-time compilation errors.
- `python -m unittest discover -v` runs the complete test suite.
- `docker compose up --build -d` builds and starts the production-style container; inspect it with `docker compose logs -f bot` and stop it with `docker compose down`.

## Coding Style & Naming Conventions

Target Python 3.12 and use four-space indentation, type annotations, and focused modules. Follow standard Python naming: `snake_case` for functions and variables, `PascalCase` for classes, and `UPPER_CASE` for constants. Keep async network and Discord operations non-blocking; move blocking media work to a thread as existing services do. User-facing Discord text is Vietnamese, while comments, logs, and documentation are English. No formatter or linter is configured, so keep changes PEP 8-compliant and consistent with nearby code.

## Testing Guidelines

Tests use the standard-library `unittest` framework, including `IsolatedAsyncioTestCase` and mocks for Discord clients. Name files `test_<area>.py` and methods `test_<behavior>`. Add regression tests for command replies, queue state, error paths, and asynchronous cleanup. Put Spotify URL-parsing, catalog-lookup, YouTube match-scoring, and playlist skip-versus-fail edge cases in `tests/test_<area>.py` (typically `tests/test_spotify.py` and `tests/test_media.py`); drive the shipped functions and inject HTTP or search only at the I/O boundary. Put soundboard URL classification, JSON/MP3 store, ingest, picker pagination, and `!tfd soundboard` access tests in `tests/test_soundboard.py`, `tests/test_soundboard_ui.py`, and `tests/test_music_commands.py`; inject HTTP, yt-dlp, and FFmpeg only at the ingest boundary. Run both compilation and the full suite before submitting.

## Commit & Pull Request Guidelines

Recent commits use short, imperative, sentence-style subjects such as `Add audio ducking for TTS over music`. Keep each commit focused. Pull requests should explain the user-visible change, identify configuration or dependency updates, link relevant issues, and include exact test commands and results. Add screenshots or Discord log excerpts when command output changes.

## Security & Configuration Tips

Copy `.env.example` to `.env` and never commit tokens. Keep `DISCORD_TOKEN` private, validate new environment settings in `src/config.py`, and preserve the container's unprivileged user and read-only filesystem controls. Soundboard clips must use a writable volume (`SOUNDBOARD_DATA_DIR`, `/data/soundboard` in Compose); do not write them onto the read-only root or `/tmp`. Cloudflare R2 is optional and all-or-nothing: `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ACCOUNT_ID` or `R2_ENDPOINT` must all be set or all omitted (whitespace-only values count as omitted). `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` must both be set or both omitted (whitespace-only values count as omitted); albums, playlists, and artist links need both, while track links still work via oEmbed without them.
