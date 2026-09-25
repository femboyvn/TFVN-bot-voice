"""Discord voice connection helpers."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from typing import Any

import discord
from discord.ext import commands

from .config import Settings

log = logging.getLogger(__name__)
_VOICE_CONNECT_LOCKS: dict[int, asyncio.Lock] = {}
# discord.py waits for a voice-state ack inside disconnect(). After a temporary
# channel is deleted that ack often never arrives, and the websocket close can
# stall, so the await must stay bounded.
_DISCONNECT_TIMEOUT_SECONDS = 25.0
_FORCE_DISCONNECT_TIMEOUT_SECONDS = 5.0
# Extra wait after the library's own connect timeout so a stuck disconnect
# inside that attempt can still be cancelled.
_CONNECT_ATTEMPT_GRACE_SECONDS = 2.0


class VoiceAccessError(RuntimeError):
    """A user-facing reason a member cannot join or control this voice client."""


class VoiceConnectionFailed(VoiceAccessError):
    """Rejoin attempts ended and the guild voice client was dropped."""


def member_voice_channel(member: object) -> object | None:
    """Return a member's current voice channel without assuming a concrete Member."""
    voice_state = getattr(member, "voice", None)
    return getattr(voice_state, "channel", None)


def guild_channel_exists(guild: object, channel_id: object) -> bool:
    """Return False only when the guild cache explicitly lacks *channel_id*."""
    get_channel = getattr(guild, "get_channel", None)
    if not callable(get_channel) or inspect.iscoroutinefunction(get_channel):
        return True
    return get_channel(channel_id) is not None


def voice_client_is_live(guild: object, voice_client: object | None) -> bool:
    """Return True when *voice_client* is connected to a channel still on *guild*.

    A client can remain on ``guild.voice_client`` after Discord already dropped
    it, including when a temporary voice channel is deleted. Those leftovers
    must not count as an occupied room.
    """
    if voice_client is None:
        return False
    is_connected = getattr(voice_client, "is_connected", None)
    if not callable(is_connected) or not is_connected():
        return False
    channel = getattr(voice_client, "channel", None)
    channel_id = getattr(channel, "id", None)
    if channel is None or channel_id is None:
        return False
    return guild_channel_exists(guild, channel_id)


def voice_channel_missing(guild: object, voice_client: object | None) -> bool:
    """Return True when *voice_client* points at a channel the guild no longer has."""
    if voice_client is None:
        return False
    channel = getattr(voice_client, "channel", None)
    if channel is None:
        return True
    channel_id = getattr(channel, "id", None)
    if not isinstance(channel_id, int):
        return False
    return not guild_channel_exists(guild, channel_id)


async def channel_was_deleted(guild: object, channel_id: int) -> bool:
    """Return True when *channel_id* is gone from cache or Discord answers 404.

    A missed channel-delete event leaves the deleted room in cache, so a force
    leave confirms with the API before tearing down a connection that still
    looks live. Transport errors keep the cached answer so a blip cannot kick
    the bot out of a real room.
    """
    if not guild_channel_exists(guild, channel_id):
        return True
    fetch = getattr(guild, "fetch_channel", None)
    if not callable(fetch):
        return False
    try:
        fetched = fetch(channel_id)
        if inspect.isawaitable(fetched):
            fetched = await fetched
    except discord.NotFound:
        return True
    except (discord.HTTPException, OSError, asyncio.TimeoutError):
        log.warning(
            "Could not confirm whether channel %s still exists in guild %s",
            channel_id,
            getattr(guild, "id", None),
        )
        return False
    return fetched is None


def live_voice_channel_id(guild: object) -> int | None:
    """Return the live connected voice channel id, or None if the client is stale."""
    voice_client = getattr(guild, "voice_client", None)
    if not voice_client_is_live(guild, voice_client):
        return None
    channel_id = getattr(getattr(voice_client, "channel", None), "id", None)
    return channel_id if isinstance(channel_id, int) else None


def same_voice_channel_error(
    guild: discord.Guild,
    member: object,
    *,
    expected_channel_id: int | None = None,
    allow_disconnected: bool = False,
) -> str | None:
    """Validate that *member* can control the guild's connected voice client."""
    member_channel = member_voice_channel(member)
    if member_channel is None:
        return "Hãy vào kênh thoại của bot trước."

    member_channel_id = getattr(member_channel, "id", None)
    if expected_channel_id is not None and member_channel_id != expected_channel_id:
        return "Bạn phải ở đúng kênh thoại của bảng điều khiển này."

    voice_client = guild.voice_client
    if not voice_client_is_live(guild, voice_client):
        if allow_disconnected and expected_channel_id is not None:
            return None
        return "Bot chưa kết nối kênh thoại."

    bot_channel = voice_client.channel
    if bot_channel is None or getattr(bot_channel, "id", None) != member_channel_id:
        return "Chỉ thành viên trong kênh thoại của bot mới có thể điều khiển nhạc."
    return None


async def connect_member_voice_client(
    guild: discord.Guild,
    member: object,
    settings: Settings,
    *,
    expected_channel_id: int | None = None,
) -> discord.VoiceClient:
    """Return a same-room voice client or connect to the member's channel.

    An existing connection is never moved. This prevents a member in another
    channel from taking over a room's music or TTS session.
    """
    lock = _VOICE_CONNECT_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        return await _connect_member_voice_client_locked(
            guild,
            member,
            settings,
            expected_channel_id=expected_channel_id,
        )


def _voice_client_connected(voice_client: object) -> bool:
    is_connected = getattr(voice_client, "is_connected", None)
    if not callable(is_connected):
        return False
    try:
        return bool(is_connected())
    except Exception:
        return False


def _stop_voice_playback(voice_client: object) -> None:
    stop = getattr(voice_client, "stop", None)
    if callable(stop):
        with contextlib.suppress(Exception):
            stop()


def _consume_future_exception(future: asyncio.Future[object]) -> None:
    if future.cancelled():
        return
    with contextlib.suppress(Exception):
        future.exception()


def _cancel_background(task: object) -> None:
    cancel = getattr(task, "cancel", None)
    if callable(cancel):
        with contextlib.suppress(Exception):
            cancel()


def _abandon_voice_client(guild: object, voice_client: object | None) -> None:
    """Drop *voice_client* from discord.py even when its channel object is gone."""
    if voice_client is None:
        return
    _stop_voice_playback(voice_client)
    connection = getattr(voice_client, "_connection", None)
    if connection is not None:
        _cancel_background(getattr(connection, "_connector", None))
        _cancel_background(getattr(connection, "_runner", None))
        reader = getattr(connection, "_socket_reader", None)
        stop_reader = getattr(reader, "stop", None)
        if callable(stop_reader):
            with contextlib.suppress(Exception):
                stop_reader()
    cleanup = getattr(voice_client, "cleanup", None)
    if callable(cleanup):
        with contextlib.suppress(Exception):
            cleanup()
    guild_id = getattr(guild, "id", None)
    client = getattr(voice_client, "client", None)
    state = getattr(client, "_connection", None)
    remove = getattr(state, "_remove_voice_client", None)
    if isinstance(guild_id, int) and callable(remove):
        with contextlib.suppress(Exception):
            remove(guild_id)
    if getattr(guild, "voice_client", None) is not voice_client:
        return
    # Real guilds expose voice_client as a read-only property backed by the
    # cache remove above. Test fakes store the client on a writable attribute.
    descriptor = inspect.getattr_static(type(guild), "voice_client", None)
    if isinstance(descriptor, property):
        return
    with contextlib.suppress(Exception):
        setattr(guild, "voice_client", None)


async def _request_gateway_leave(guild: object, timeout: float) -> str:
    """Ask Discord to clear the bot's voice state. See return values below."""
    change = getattr(guild, "change_voice_state", None)
    if not callable(change):
        return "skipped"
    try:
        result = change(channel=None)
    except Exception:
        log.warning(
            "Voice state clear failed for guild %s",
            getattr(guild, "id", None),
            exc_info=True,
        )
        return "error"
    if not inspect.isawaitable(result):
        return "skipped"
    try:
        await asyncio.wait_for(result, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(
            "Voice state clear timed out for guild %s",
            getattr(guild, "id", None),
        )
        return "timeout"
    except Exception:
        log.warning(
            "Voice state clear failed for guild %s",
            getattr(guild, "id", None),
            exc_info=True,
        )
        return "error"
    return "ok"


async def _await_disconnect(voice_client: object, timeout: float) -> str:
    disconnect = getattr(voice_client, "disconnect", None)
    if not callable(disconnect):
        return "skipped"
    try:
        result = disconnect(force=True)
    except Exception:
        log.warning("Voice disconnect raised before awaiting", exc_info=True)
        return "error"
    if not inspect.isawaitable(result):
        return "ok"
    task = asyncio.ensure_future(result)
    task.add_done_callback(_consume_future_exception)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.sleep(0)
        return "timeout"
    except Exception:
        log.warning("Voice disconnect failed", exc_info=True)
        return "error"
    return "ok"


async def _disconnect_voice_client_locked(
    guild: discord.Guild,
    *,
    expected_client: discord.VoiceClient | None,
    force: bool,
    timeout: float | None,
) -> bool:
    voice_client = guild.voice_client
    if voice_client is None:
        return False
    if expected_client is not None and voice_client is not expected_client:
        return False
    missing = voice_channel_missing(guild, voice_client)
    connected = _voice_client_connected(voice_client)
    if not force and not missing and not connected:
        return False

    use_force = force or missing
    limit = timeout
    if limit is None:
        limit = (
            _FORCE_DISCONNECT_TIMEOUT_SECONDS
            if use_force
            else _DISCONNECT_TIMEOUT_SECONDS
        )
    _stop_voice_playback(voice_client)
    if use_force and await _request_gateway_leave(guild, limit) == "timeout":
        log.warning(
            "Dropping stuck voice client in guild %s after gateway leave timed out",
            getattr(guild, "id", None),
        )
        _abandon_voice_client(guild, voice_client)
        return True

    outcome = await _await_disconnect(voice_client, limit)
    if outcome in {"timeout", "error"}:
        log.warning(
            "Dropping stuck voice client in guild %s (%s)",
            getattr(guild, "id", None),
            outcome,
        )
        # disconnect() itself asks Discord to leave. If that call stalled
        # before the gateway send, make one bounded attempt here.
        if not use_force:
            await _request_gateway_leave(guild, _FORCE_DISCONNECT_TIMEOUT_SECONDS)
        _abandon_voice_client(guild, voice_client)
        return True
    if use_force and getattr(guild, "voice_client", None) is voice_client:
        _abandon_voice_client(guild, voice_client)
    return True


async def disconnect_guild_voice_client(
    guild: discord.Guild,
    *,
    expected_client: discord.VoiceClient | None = None,
    force: bool = False,
    timeout: float | None = None,
) -> bool:
    """Serialize disconnects with connects and avoid closing a replaced client.

    ``force=True`` is the deleted-channel path: it asks Discord to clear the
    voice state, bounds the wait, and drops the client if disconnect stalls.
    """
    lock = _VOICE_CONNECT_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        return await _disconnect_voice_client_locked(
            guild,
            expected_client=expected_client,
            force=force,
            timeout=timeout,
        )


async def _connect_member_voice_client_locked(
    guild: discord.Guild,
    member: object,
    settings: Settings,
    *,
    expected_channel_id: int | None,
) -> discord.VoiceClient:
    target_channel = member_voice_channel(member)
    if target_channel is None:
        raise VoiceAccessError("Hãy vào một kênh thoại trước.")

    target_channel_id = getattr(target_channel, "id", None)
    if expected_channel_id is not None and target_channel_id != expected_channel_id:
        raise VoiceAccessError(
            "Bạn phải ở đúng kênh thoại của bảng điều khiển này."
        )

    return await _connect_channel_locked(guild, target_channel, settings)


async def connect_voice_channel(
    guild: discord.Guild,
    channel: object,
    settings: Settings,
) -> discord.VoiceClient:
    """Connect to *channel* without moving an occupied voice client."""
    lock = _VOICE_CONNECT_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        return await _connect_channel_locked(guild, channel, settings)


async def _connect_channel_locked(
    guild: discord.Guild,
    target_channel: object,
    settings: Settings,
) -> discord.VoiceClient:
    target_channel_id = getattr(target_channel, "id", None)
    voice_client = guild.voice_client
    if voice_client_is_live(guild, voice_client):
        connected_channel = voice_client.channel
        if (
            connected_channel is None
            or getattr(connected_channel, "id", None) != target_channel_id
        ):
            raise VoiceAccessError(
                "Bot đang ở kênh thoại khác. Hãy vào cùng kênh để điều khiển."
            )
        return voice_client

    if voice_client is not None:
        await _disconnect_voice_client_locked(
            guild,
            expected_client=voice_client,
            force=True,
            timeout=_FORCE_DISCONNECT_TIMEOUT_SECONDS,
        )

    connect = getattr(target_channel, "connect", None)
    if not callable(connect):
        raise VoiceAccessError("Không thể kết nối kênh thoại này.")

    last_error: Exception | None = None
    attempt_count = settings.voice_connect_retries
    for attempt in range(1, attempt_count + 1):
        try:
            return await _attempt_voice_connect(connect, settings)
        except discord.ConnectionClosed as exc:
            last_error = exc
            if exc.code == 4017:
                await _drop_failed_voice_attempt(guild)
                raise VoiceAccessError(
                    "Kết nối thoại cần hỗ trợ Discord DAVE/E2EE. "
                    "Hãy nâng cấp discord.py[voice] 2.7 trở lên rồi khởi động lại bot."
                ) from exc
        except asyncio.TimeoutError as exc:
            # One timed-out handshake is a failed rejoin. Another attempt would
            # leave the bot looking connected while it keeps retrying.
            last_error = exc
            log.warning(
                "Voice connection attempt %s timed out for guild %s",
                attempt,
                guild.id,
            )
            await _drop_failed_voice_attempt(guild)
            raise VoiceConnectionFailed(
                "Hết thời gian kết nối lại kênh thoại. "
                "Bot đã dừng và ngắt kết nối."
            ) from exc
        except (discord.ClientException, OSError) as exc:
            last_error = exc

        log.warning(
            "Voice connection attempt %s/%s failed for guild %s: %s",
            attempt,
            attempt_count,
            guild.id,
            last_error,
        )
        await _drop_failed_voice_attempt(guild)
        if attempt < attempt_count:
            await asyncio.sleep(attempt * 2)

    raise VoiceConnectionFailed(
        f"Không kết nối lại được kênh thoại sau {attempt_count} lần thử. "
        "Bot đã dừng và ngắt kết nối."
    ) from last_error


async def _attempt_voice_connect(
    connect: Any,
    settings: Settings,
) -> discord.VoiceClient:
    """Run one voice handshake and stop it if it outlives its timeout."""
    task = asyncio.ensure_future(
        connect(
            timeout=settings.voice_connect_timeout,
            reconnect=False,
        )
    )
    task.add_done_callback(_consume_future_exception)
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=settings.voice_connect_timeout + _CONNECT_ATTEMPT_GRACE_SECONDS,
        )
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.sleep(0)
        raise
    except Exception:
        if not task.done():
            task.cancel()
        raise


async def _drop_failed_voice_attempt(guild: discord.Guild) -> None:
    """Drop a half-open client so a failed rejoin cannot keep retrying."""
    if getattr(guild, "voice_client", None) is None:
        return
    await _disconnect_voice_client_locked(
        guild,
        expected_client=None,
        force=True,
        timeout=_FORCE_DISCONNECT_TIMEOUT_SECONDS,
    )


async def get_or_connect_voice_client(
    ctx: commands.Context[Any],
    settings: Settings,
    *,
    expected_channel_id: int | None = None,
) -> discord.VoiceClient | None:
    try:
        return await connect_member_voice_client(
            ctx.guild,
            ctx.author,
            settings,
            expected_channel_id=expected_channel_id,
        )
    except VoiceConnectionFailed:
        raise
    except VoiceAccessError as exc:
        await ctx.send(str(exc))
        return None
