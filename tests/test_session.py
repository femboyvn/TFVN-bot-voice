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
    sanitize_for_speech,
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

    def test_omits_name_when_announce_disabled(self) -> None:
        self.assertEqual(
            prepare_chat_speech(
                "xin chào",
                author_name="Alex",
                announce_name=False,
            ),
            "xin chào",
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

    def test_skips_url_only_and_gif_links(self) -> None:
        self.assertIsNone(
            prepare_chat_speech(
                "https://tenor.com/view/funny-gif-123",
                author_name="Alex",
            )
        )
        self.assertIsNone(
            prepare_chat_speech(
                "http://media.giphy.com/media/abc/giphy.gif",
                author_name="Alex",
            )
        )

    def test_strips_url_but_keeps_surrounding_text(self) -> None:
        speech = prepare_chat_speech(
            "xem này https://example.com/a.gif nhé",
            author_name="Lan",
            announce_name=False,
        )
        self.assertEqual(speech, "xem này nhé")
        self.assertNotIn("http", speech or "")

    def test_skips_emoji_and_icon_only(self) -> None:
        self.assertIsNone(
            prepare_chat_speech("<:wave:123456789012345678>", author_name="A")
        )
        self.assertIsNone(prepare_chat_speech(":smile: :thumbsup:", author_name="A"))
        self.assertIsNone(prepare_chat_speech("😂🔥", author_name="A"))


class SanitizeSpeechTests(unittest.TestCase):
    def test_removes_discord_cdn_and_www(self) -> None:
        text = sanitize_for_speech(
            "hi www.example.com/x.gif and "
            "https://cdn.discordapp.com/emojis/1.png rest"
        )
        self.assertEqual(text, "hi and rest")

    def test_removes_custom_and_unicode_emoji(self) -> None:
        text = sanitize_for_speech("hello <a:dance:99> 😀 there")
        self.assertEqual(text, "hello there")


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
        self.assertTrue(session.name_announce)

        channel2 = Mock()
        channel2.id = 8
        channel2.name = "Other"
        same = manager.start(guild, channel2)
        self.assertIs(same, session)
        self.assertEqual(session.voice_channel_id, 8)

        self.assertTrue(await manager.stop(42))
        self.assertFalse(manager.is_active(42))
        self.assertFalse(await manager.stop(42))

    async def test_name_announce_toggle_defaults_on(self) -> None:
        bot = Mock()
        bot.loop = asyncio.get_running_loop()
        tts = TextToSpeech(synthesizer=lambda t, p: p.write_bytes(b"x" * 8))
        manager = SessionManager(bot, tts, volume=0.7)
        guild = Mock()
        guild.id = 5
        channel = Mock()
        channel.id = 1
        channel.name = "VC"
        session = manager.start(guild, channel)
        try:
            self.assertTrue(session.name_announce)
            self.assertFalse(session.set_name_announce(False))
            self.assertFalse(session.name_announce)
            self.assertTrue(session.set_name_announce(True))
            # offer_chat_message respects the flag
            session.set_name_announce(False)
            ok = session.offer_chat_message(
                author_is_bot=False,
                author_name="Sam",
                guild_id=5,
                channel_id=1,
                content="only body",
                command_prefix="!tfd ",
            )
            self.assertTrue(ok)
        finally:
            await manager.stop(5)


if __name__ == "__main__":
    unittest.main()
