"""DAVE decrypt boundary for per-speaker recording."""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.recording import RecordingService
from src.recording_recv import consume_voice_frame, decrypt_inbound_opus


class _Decoder:
    def __init__(self) -> None:
        self.opus: list[bytes] = []

    def decode(self, opus: bytes, fec: bool = False) -> bytes:
        self.opus.append(opus)
        return b"\x01\x00" * 960


class UnencryptedWhenPassthroughDisabled(Exception):
    pass


def _service(root: str) -> RecordingService:
    def factory(path, bitrate):
        from tests.test_recording import MemoryEncoder

        return MemoryEncoder(path, bitrate)

    return RecordingService(
        Mock(recording_data_dir=root),
        encoder_factory=factory,
    )


class DecryptTests(unittest.TestCase):
    def test_decrypt_is_called_with_the_user_before_decode(self) -> None:
        session = Mock()
        session.ready = True
        session.protocol_version = 1
        session.decrypt.return_value = b"plain-opus"
        decoder = _Decoder()
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            take = service.start(
                guild_id=1,
                voice_channel_id=2,
                text_channel_id=3,
                starter_id=4,
                voice_channel_name="Phòng",
            )
            user = SimpleNamespace(id=15, bot=False, display_name="Lan")
            packet = SimpleNamespace(opus=b"cipher", media_kind="audio")
            result = consume_voice_frame(
                take,
                user,
                packet,
                dave_session=session,
                decoder=decoder,
            )
            self.assertEqual(result, "written")
            session.decrypt.assert_called_once()
            self.assertEqual(session.decrypt.call_args.args[0], 15)
            self.assertEqual(session.decrypt.call_args.args[2], b"cipher")
            self.assertEqual(decoder.opus, [b"plain-opus"])
            service.stop(1)
            self.assertTrue((take.directory / "user-15.mp3").is_file())

    def test_decrypt_error_drops_the_packet(self) -> None:
        session = Mock()
        session.ready = True
        session.protocol_version = 1
        session.decrypt.side_effect = RuntimeError("bad frame")
        decoder = _Decoder()
        opus = decrypt_inbound_opus(session, 15, b"cipher")
        self.assertIsNone(opus)
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            take = service.start(
                guild_id=1,
                voice_channel_id=2,
                text_channel_id=None,
                starter_id=None,
                voice_channel_name="Phòng",
            )
            result = consume_voice_frame(
                take,
                SimpleNamespace(id=15, bot=False, display_name="Lan"),
                SimpleNamespace(opus=b"cipher"),
                dave_session=session,
                decoder=decoder,
            )
            self.assertEqual(result, "dropped")
            self.assertEqual(decoder.opus, [])
            service.abort(1)

    def test_passthrough_silence_is_decoded_as_is(self) -> None:
        session = Mock()
        session.ready = True
        session.protocol_version = 1
        session.decrypt.side_effect = UnencryptedWhenPassthroughDisabled("silence")
        decoder = _Decoder()
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            take = service.start(
                guild_id=1,
                voice_channel_id=2,
                text_channel_id=None,
                starter_id=None,
                voice_channel_name="Phòng",
            )
            result = consume_voice_frame(
                take,
                SimpleNamespace(id=15, bot=False, display_name="Lan"),
                SimpleNamespace(opus=b"silence-opus"),
                dave_session=session,
                decoder=decoder,
            )
            self.assertEqual(result, "written")
            self.assertEqual(decoder.opus, [b"silence-opus"])
            service.abort(1)

    def test_bots_and_video_are_ignored(self) -> None:
        decoder = _Decoder()
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            take = service.start(
                guild_id=1,
                voice_channel_id=2,
                text_channel_id=None,
                starter_id=None,
                voice_channel_name="Phòng",
            )
            bot = consume_voice_frame(
                take,
                SimpleNamespace(id=99, bot=True, display_name="Bot"),
                SimpleNamespace(opus=b"opus"),
                dave_session=None,
                decoder=decoder,
            )
            video = consume_voice_frame(
                take,
                SimpleNamespace(id=15, bot=False, display_name="Lan"),
                SimpleNamespace(opus=b"opus", media_kind="video"),
                dave_session=None,
                decoder=decoder,
            )
            self.assertEqual(bot, "ignored")
            self.assertEqual(video, "ignored")
            self.assertEqual(decoder.opus, [])
            self.assertEqual(take.speaker_count, 0)
            service.abort(1)


if __name__ == "__main__":
    unittest.main()
