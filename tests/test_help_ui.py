from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from src.bot import create_bot
from src.config import Settings
from src.help_ui import (
    COMMAND_HELP,
    HELP_TOPIC_KEYS,
    HELP_VIEW_TIMEOUT,
    HelpMenuView,
    HelpTopicSelect,
    InteractiveHelpCommand,
    build_command_help_embed,
    build_help_embed,
    normalize_help_prefix,
)


def _interaction(user_id: int = 10) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = user_id
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _help_context(*, prefix: str = "!tfd ", user_id: int = 10) -> MagicMock:
    ctx = MagicMock()
    ctx.prefix = prefix
    ctx.author.id = user_id
    ctx.channel.send = AsyncMock(return_value=MagicMock())
    return ctx


class HelpCopyTests(unittest.TestCase):
    def test_overview_uses_configured_prefix(self) -> None:
        embed = build_help_embed("overview", "!tfd ")
        self.assertEqual(embed.title, "Trợ giúp TFD Voice")
        self.assertIn("`!tfd music`", embed.description)
        self.assertIn("`!tfd help [lệnh]`", embed.description)
        self.assertIn("Spotify", embed.description or "")
        self.assertLessEqual(len(embed.description or ""), 4096)

    def test_unknown_page_falls_back_to_overview(self) -> None:
        embed = build_help_embed("missing", "!tfd ")
        self.assertEqual(embed.title, "Trợ giúp TFD Voice")

    def test_panel_page_lists_controls(self) -> None:
        embed = build_help_embed("panel", "!bot ")
        values = "\n".join(field.value for field in embed.fields)
        for label in (
            "Thêm nhạc",
            "Tạm dừng",
            "Bài tiếp",
            "Lặp",
            "Tua đến",
            "Hàng đợi",
            "Xóa hàng đợi",
            "Dừng",
            "Đọc tên bài",
            "Đọc tin nhắn",
            "Cài đặt",
            "Rời",
            "Trợ giúp",
        ):
            self.assertIn(label, values)
        self.assertIn("`!bot music`", embed.description)
        self.assertIn("Spotify", values)
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024)

    def test_commands_page_lists_every_documented_command(self) -> None:
        embed = build_help_embed("commands", "!tfd ")
        values = "\n".join(field.value for field in embed.fields)
        for name, (usage, _summary) in COMMAND_HELP.items():
            extra = f" {usage}" if usage else ""
            self.assertIn(f"`!tfd {name}{extra}`", values)
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024)

    def test_tts_and_settings_pages_stay_within_discord_limits(self) -> None:
        for page in HELP_TOPIC_KEYS:
            with self.subTest(page=page):
                embed = build_help_embed(page, "!tfd ")
                self.assertLessEqual(len(embed.title or ""), 256)
                self.assertLessEqual(len(embed.description or ""), 4096)
                total = len(embed.title or "") + len(embed.description or "")
                for field in embed.fields:
                    self.assertLessEqual(len(field.name), 256)
                    self.assertLessEqual(len(field.value), 1024)
                    total += len(field.name) + len(field.value)
                self.assertLessEqual(total, 6000)

        settings = "\n".join(
            field.value for field in build_help_embed("settings", "!tfd ").fields
        )
        self.assertIn("Đọc tên người gửi", settings)
        self.assertIn("`on` hoặc `off`", settings)
        self.assertIn("mặc định `off`", settings)

    def test_command_page_includes_usage_and_details(self) -> None:
        embed = build_command_help_embed("jump", "!tfd ")
        self.assertIsNotNone(embed)
        assert embed is not None
        self.assertIn("`!tfd jump HH:MM:SS`", embed.title)
        values = {field.name: field.value for field in embed.fields}
        self.assertEqual(values["Cách dùng"], "`!tfd jump HH:MM:SS`")
        self.assertIn("độ dài", values["Chi tiết"])

    def test_unknown_command_has_no_embed(self) -> None:
        self.assertIsNone(build_command_help_embed("nope", "!tfd "))

    def test_normalize_prefix_rejects_non_strings(self) -> None:
        self.assertEqual(normalize_help_prefix("!x "), "!x ")
        self.assertEqual(normalize_help_prefix(""), "!tfd ")
        self.assertEqual(normalize_help_prefix(None), "!tfd ")
        self.assertEqual(normalize_help_prefix(object()), "!tfd ")


class HelpMenuViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_edits_to_requested_topic(self) -> None:
        view = HelpMenuView(10, "!tfd ")
        select = next(child for child in view.children if isinstance(child, HelpTopicSelect))
        select._values = ["panel"]
        interaction = _interaction(10)

        await select.callback(interaction)

        self.assertEqual(view.page, "panel")
        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertEqual(kwargs["embed"].title, "Bảng điều khiển nhạc")
        self.assertIs(kwargs["view"], view)
        selected = [option.value for option in select.options if option.default]
        self.assertEqual(selected, ["panel"])

    async def test_invalid_topic_stays_ephemeral(self) -> None:
        view = HelpMenuView(10, "!tfd ")
        select = next(child for child in view.children if isinstance(child, HelpTopicSelect))
        select._values = ["missing"]
        interaction = _interaction(10)

        await select.callback(interaction)

        self.assertEqual(view.page, "overview")
        interaction.response.edit_message.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_menu_is_requester_bound_and_expires(self) -> None:
        view = HelpMenuView(10, "!tfd ")
        self.assertEqual(view.timeout, HELP_VIEW_TIMEOUT)
        outsider = _interaction(11)
        self.assertFalse(await view.interaction_check(outsider))
        outsider.response.send_message.assert_awaited_once()
        self.assertIn("người mở menu", outsider.response.send_message.await_args.args[0])
        self.assertTrue(await view.interaction_check(_interaction(10)))

        view.message = MagicMock()
        view.message.edit = AsyncMock()
        await view.on_timeout()
        self.assertTrue(all(getattr(child, "disabled", False) for child in view.children))
        view.message.edit.assert_awaited_once_with(view=view)


class InteractiveHelpCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.help_command = InteractiveHelpCommand()
        self.ctx = _help_context()
        self.help_command.context = self.ctx

    async def test_bot_help_sends_overview_menu(self) -> None:
        await self.help_command.send_bot_help({})

        self.ctx.channel.send.assert_awaited_once()
        kwargs = self.ctx.channel.send.await_args.kwargs
        self.assertEqual(kwargs["embed"].title, "Trợ giúp TFD Voice")
        self.assertIsInstance(kwargs["view"], HelpMenuView)
        self.assertEqual(kwargs["view"].requester_id, 10)
        self.assertEqual(kwargs["view"].page, "overview")
        self.assertIs(kwargs["view"].message, self.ctx.channel.send.return_value)

    async def test_command_help_sends_command_embed_with_menu(self) -> None:
        command = MagicMock()
        command.name = "play"
        await self.help_command.send_command_help(command)

        kwargs = self.ctx.channel.send.await_args.kwargs
        self.assertIn("`!tfd play <URL hoặc từ khóa>`", kwargs["embed"].title)
        self.assertIsInstance(kwargs["view"], HelpMenuView)

    async def test_unknown_command_sends_vietnamese_error_and_menu(self) -> None:
        message = self.help_command.command_not_found("xyz")
        self.assertIn("xyz", message)
        self.assertIn("!tfd help", message)

        await self.help_command.send_error_message(message)
        kwargs = self.ctx.channel.send.await_args.kwargs
        self.assertEqual(self.ctx.channel.send.await_args.args[0], message)
        self.assertEqual(kwargs["embed"].title, "Trợ giúp TFD Voice")
        self.assertIsInstance(kwargs["view"], HelpMenuView)

    async def test_cog_help_opens_commands_topic(self) -> None:
        await self.help_command.send_cog_help(MagicMock())
        kwargs = self.ctx.channel.send.await_args.kwargs
        self.assertEqual(kwargs["view"].page, "commands")
        self.assertEqual(kwargs["embed"].title, "Lệnh chữ")


class HelpRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_command_covers_every_registered_command(self) -> None:
        bot = create_bot(Settings(discord_token="test-token"))
        await bot.setup_hook()
        try:
            self.assertIsInstance(bot.help_command, InteractiveHelpCommand)
            registered = {command.name for command in bot.commands}
            self.assertEqual(registered, set(COMMAND_HELP))
            self.assertIn("help", registered)
            copied = bot.help_command.copy()
            self.assertIsInstance(copied, InteractiveHelpCommand)
            self.assertIsNot(copied, bot.help_command)
        finally:
            await bot.close()

    async def test_bare_help_callback_sends_interactive_menu(self) -> None:
        bot = create_bot(Settings(discord_token="test-token"))
        await bot.setup_hook()
        try:
            ctx = _help_context()
            ctx.bot = bot
            await bot.help_command.prepare_help_command(ctx)
            bot.help_command.context = ctx
            await bot.help_command.command_callback(ctx)

            ctx.channel.send.assert_awaited_once()
            view = ctx.channel.send.await_args.kwargs["view"]
            self.assertIsInstance(view, HelpMenuView)
            self.assertEqual(view.page, "overview")
        finally:
            await bot.close()

    async def test_help_command_lookup_uses_command_page(self) -> None:
        bot = create_bot(Settings(discord_token="test-token"))
        await bot.setup_hook()
        try:
            ctx = _help_context()
            ctx.bot = bot
            await bot.help_command.prepare_help_command(ctx, "play")
            bot.help_command.context = ctx
            await bot.help_command.command_callback(ctx, command="play")

            embed = ctx.channel.send.await_args.kwargs["embed"]
            self.assertIn("`!tfd play", embed.title)
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
