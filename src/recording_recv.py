"""Receive one Discord voice user at a time and hand mono PCM to a take.

discord.py decrypts the transport and keeps a DAVE session for outbound
audio. The shipped voice-recv release stops at transport decrypt, so this
adapter opens the DAVE layer with that same session before Opus decode.
Video and screen packets never become tracks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from .recording import RecordingError, Take

log = logging.getLogger(__name__)

_NON_AUDIO = {"video", "screen", "rtx", "screen_share", "screen-share"}


class OpusDecoder(Protocol):
    def decode(self, opus: bytes, fec: bool = False) -> bytes: ...


def speaker_is_recordable(user: object) -> bool:
    """Humans with a numeric id are recorded. Other bots are not."""
    if user is None or getattr(user, "bot", False) is True:
        return False
    return isinstance(getattr(user, "id", None), int)


def packet_is_audio(data: object) -> bool:
    """True when the packet does not declare itself as video or screen share."""
    objects = (data, getattr(data, "packet", None))
    for item in objects:
        if item is None:
            continue
        kind = getattr(item, "media_kind", None)
        if kind is None:
            continue
        text = str(getattr(kind, "value", kind)).replace(" ", "_").lower()
        return text not in _NON_AUDIO
    return True


def opus_payload(data: object) -> bytes | None:
    """Return transport-decrypted Opus bytes from a voice-recv packet."""
    for name in ("opus", "decrypted_data"):
        value = getattr(data, name, None)
        if isinstance(value, (bytes, bytearray)) and value:
            return bytes(value)
    packet = getattr(data, "packet", None)
    if packet is None:
        return None
    value = getattr(packet, "decrypted_data", None)
    if isinstance(value, (bytes, bytearray)) and value:
        return bytes(value)
    return None


def decrypt_inbound_opus(
    dave_session: object | None,
    user_id: int | None,
    payload: bytes,
) -> bytes | None:
    """Return Opus ready to decode, or None when the packet must be dropped.

    A missing session, or protocol version 0, means the payload is already
    Opus. ``UnencryptedWhenPassthroughDisabled`` is silence and is returned
    unchanged. Any other decrypt error drops the packet.
    """
    if not payload:
        return None
    if dave_session is None or getattr(dave_session, "protocol_version", 1) == 0:
        return bytes(payload)
    if getattr(dave_session, "ready", True) is not True or user_id is None:
        return None
    try:
        decrypted = dave_session.decrypt(int(user_id), _audio_media_type(), bytes(payload))
    except Exception as exc:
        if type(exc).__name__ == "UnencryptedWhenPassthroughDisabled":
            return bytes(payload)
        return None
    if not isinstance(decrypted, (bytes, bytearray)) or not decrypted:
        return None
    return bytes(decrypted)


def consume_voice_frame(
    take: Take,
    user: object,
    data: object,
    *,
    dave_session: object | None,
    decoder: OpusDecoder,
) -> str:
    """Write one received packet. Returns ``written``, ``ignored``, or ``dropped``."""
    if not speaker_is_recordable(user) or not packet_is_audio(data):
        return "ignored"
    payload = opus_payload(data)
    if payload is None:
        return "dropped"
    user_id = getattr(user, "id", None)
    if not isinstance(user_id, int):
        return "ignored"
    opus = decrypt_inbound_opus(dave_session, user_id, payload)
    if opus is None:
        return "dropped"
    pcm = _decode(decoder, opus)
    if pcm is None:
        return "dropped"
    name = str(
        getattr(user, "display_name", "")
        or getattr(user, "name", "")
        or user_id
    )
    take.submit_speaker(user_id, name, pcm)
    return "written"


def dave_session_of(voice_client: object) -> object | None:
    """Return the DAVE session discord.py keeps on the voice connection."""
    connection = getattr(voice_client, "_connection", None)
    return getattr(connection, "dave_session", None)


def build_recording_sink(
    take: Take,
    session_getter: Callable[[], object | None],
) -> object:
    """Build a voice-recv sink that decodes DAVE audio into *take*."""
    try:
        from discord.ext import voice_recv
    except ImportError as exc:
        raise RecordingError("Không nhận được tiếng từ kênh thoại.") from exc

    class _RecordingSink(voice_recv.AudioSink):
        def __init__(self) -> None:
            super().__init__()
            self._decoder: OpusDecoder | None = None

        def wants_opus(self) -> bool:
            return True

        def write(self, user: object, data: object) -> None:
            decoder = self._decoder
            if decoder is None:
                decoder = _new_decoder()
                if decoder is None:
                    return
                self._decoder = decoder
            try:
                consume_voice_frame(
                    take,
                    user,
                    data,
                    dave_session=session_getter(),
                    decoder=decoder,
                )
            except Exception:
                log.exception("Could not record a voice packet in guild %s", take.guild_id)

        def cleanup(self) -> None:
            return None

    return _RecordingSink()


def start_receive(voice_client: object, sink: object) -> None:
    """Begin listening. Raises when this client cannot receive."""
    listen = getattr(voice_client, "listen", None)
    if not callable(listen):
        raise RecordingError("Không nhận được tiếng từ kênh thoại.")
    listen(sink)


def stop_receive(voice_client: object) -> None:
    stop = getattr(voice_client, "stop_listening", None)
    if callable(stop):
        try:
            stop()
        except Exception:
            log.exception("Could not stop voice receive")


def _audio_media_type() -> object:
    try:
        import davey
    except ImportError:
        return 0
    return davey.MediaType.audio


def _new_decoder() -> OpusDecoder | None:
    try:
        import discord

        return discord.opus.Decoder()
    except Exception:
        log.exception("Opus decoder is not available for recording")
        return None


def _decode(decoder: OpusDecoder, opus: bytes) -> bytes | None:
    try:
        pcm = decoder.decode(opus, fec=False)
    except TypeError:
        try:
            pcm = decoder.decode(opus)
        except Exception:
            return None
    except Exception:
        return None
    if not isinstance(pcm, (bytes, bytearray)) or not pcm:
        return None
    return bytes(pcm)
