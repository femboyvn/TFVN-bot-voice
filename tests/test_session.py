"""Unit tests for voice-chat session filtering and speech prep."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock

from src.session import (
    SessionManager,
    VoiceSession,
    chunk_speech_text,
    is_session_chat_message,
    prepare_chat_speech,
)
from src.tts import TextToSpeech


class ChatFilterTests(unittest.TestCase):
    def test_accepts_vc_chat_from_humans(self) -> None:
        self.assertTrue(
            is_session_chat_message(
                author_is_bot=False,
                guild_id=1,
                channel_id=99,
                session_voice_channel_id=99,
                content="hello everyone",
                command_prefix="!tfd ",
            )
        )

    def test_rejects_bots_commands_wrong_channel_empty(self) -> None:
        base = dict(
            author_is_bot=False,
            guild_id=1,
            channel_id=99,
            session_voice_channel_id=99,
            content="hello",
            command_prefix="!tfd ",
        )
        self.assertFalse(is_session_chat_message(**{**base, "author_is_bot": True}))
        self.assertFalse(is_session_chat_message(**{**base, "guild_id": None}))
        self.assertFalse(is_session_chat_message(**{**base, "channel_id": 1}))
        self.assertFalse(is_session_chat_message(**{**base, "content": "   "}))
        self.assertFalse(
            is_session_chat_message(**{**base, "content": "!tfd leave"})
        )


class PrepareSpeechTests(unittest.TestCase):
    def test_formats_author_and_text(self) -> None:
        self.assertEqual(
            prepare_chat_speech("xin chào", author_name="Alex"),
            "Alex nói xin chào",
        )

    def test_truncates_long_content_at_word_boundary(self) -> None:
        speech = prepare_chat_speech(
            "hello world and more words here",
            author_name="A",
            max_chars=20,
        )
        self.assertIsNotNone(speech)
        assert speech is not None
        self.assertTrue(speech.endswith("…"))
        self.assertLessEqual(len(speech), 20)
        # Should not cut inside the first word when space-splitting is possible.
        self.assertTrue(speech.startswith("A nói "))

    def test_keeps_long_discord_length_messages(self) -> None:
        body = ("xin chào các bạn " * 80).strip()  # well over old 300 limit
        speech = prepare_chat_speech(body, author_name="Lan")
        self.assertIsNotNone(speech)
        assert speech is not None
        self.assertIn("xin chào", speech)
        self.assertGreater(len(speech), 300)

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(prepare_chat_speech("  ", author_name="A"))


class ChunkSpeechTests(unittest.TestCase):
    def test_short_text_single_chunk(self) -> None:
        self.assertEqual(chunk_speech_text("hello"), ["hello"])

    def test_splits_long_text_without_losing_content(self) -> None:
        text = "alpha beta gamma delta epsilon zeta eta theta"
        chunks = chunk_speech_text(text, max_chunk_chars=20)
        self.assertGreater(len(chunks), 1)
        rejoined = " ".join(chunks)
        for word in text.split():
            self.assertIn(word, rejoined)


class VoiceSessionOfferTests(unittest.IsolatedAsyncioTestCase):
    async def test_offer_chat_message_queues_speech(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        guild = Mock()
        guild.id = 10
        guild.voice_client = None
        channel = Mock()
        channel.id = 55
        channel.name = "General"

        tts = TextToSpeech(synthesizer=lambda t, p: p.write_bytes(b"x" * 8))
        session = VoiceSession(bot, guild, channel, tts, volume=0.5)
        try:
            ok = session.offer_chat_message(
                author_is_bot=False,
                author_name="Sam",
                guild_id=10,
                channel_id=55,
                content="good morning",
                command_prefix="!tfd ",
            )
            self.assertTrue(ok)
            # Allow the worker to pick up the item (it will no-op without voice client).
            await asyncio.sleep(0.05)
            self.assertTrue(session.active)
        finally:
            await session.close()

    async def test_offer_ignores_other_channels(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        guild = Mock()
        guild.id = 10
        guild.voice_client = None
        channel = Mock()
        channel.id = 55
        channel.name = "General"
        tts = TextToSpeech(synthesizer=lambda t, p: p.write_bytes(b"x" * 8))
        session = VoiceSession(bot, guild, channel, tts, volume=0.5)
        try:
            self.assertFalse(
                session.offer_chat_message(
                    author_is_bot=False,
                    author_name="Sam",
                    guild_id=10,
                    channel_id=999,
                    content="nope",
                    command_prefix="!tfd ",
                )
            )
        finally:
            await session.close()


class SessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_stop_and_keep_connected(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        tts = TextToSpeech(synthesizer=lambda t, p: p.write_bytes(b"x" * 8))
        manager = SessionManager(bot, tts, volume=0.7)

        guild = Mock()
        guild.id = 42
        channel = Mock()
        channel.id = 7
        channel.name = "Lounge"

        self.assertFalse(manager.keep_connected(42))
        session = manager.start(guild, channel)
        self.assertTrue(manager.is_active(42))
        self.assertTrue(manager.keep_connected(42))
        self.assertEqual(session.voice_channel_id, 7)

        channel2 = Mock()
        channel2.id = 8
        channel2.name = "Other"
        same = manager.start(guild, channel2)
        self.assertIs(same, session)
        self.assertEqual(session.voice_channel_id, 8)

        self.assertTrue(await manager.stop(42))
        self.assertFalse(manager.is_active(42))
        self.assertFalse(await manager.stop(42))


if __name__ == "__main__":
    unittest.main()
