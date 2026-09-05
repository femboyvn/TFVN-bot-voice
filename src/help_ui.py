"""Interactive Vietnamese help menu for prefix commands and the music panel.

The menu is requester-bound and expires. Topic copy stays here so the default
English discord.py help command is never shown to users.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands

HELP_VIEW_TIMEOUT = 180.0
DEFAULT_COMMAND_PREFIX = "!tfd "

# command name -> (argument usage, summary). Usage is omitted when empty.
COMMAND_HELP: dict[str, tuple[str, str]] = {
    "help": (
        "[lệnh]",
        "Mở menu trợ giúp này. Thêm tên lệnh để xem chi tiết lệnh đó.",
    ),
    "music": (
        "",
        "Vào kênh thoại của bạn và gửi bảng điều khiển nhạc dùng chung.",
    ),
    "join": (
        "",
        "Vào kênh thoại và bắt đầu đọc chat văn bản của kênh đó bằng TTS.",
    ),
    "leave": (
        "",
        "Dừng nhạc, tắt đọc chat, và rời kênh thoại.",
    ),
    "nameannounce": (
        "on | off",
        "Bật hoặc tắt đọc tên người gửi trước nội dung tin nhắn TTS.",
    ),
    "play": (
        "<URL hoặc từ khóa>",
        "Vào kênh thoại (nếu chưa) và xếp bài, playlist, hoặc kết quả tìm kiếm.",
    ),
    "next": (
        "<URL hoặc từ khóa>",
        "Thêm bài hoặc playlist vào hàng đợi mà không cắt bài đang phát.",
    ),
    "pause": (
        "",
        "Tạm dừng bài đang phát.",
    ),
    "resume": (
        "",
        "Tiếp tục bài đang tạm dừng.",
    ),
    "jump": (
        "HH:MM:SS",
        "Tua bài hiện tại đến mốc thời gian. Ví dụ: 00:01:30.",
    ),
    "skip": (
        "",
        "Bỏ qua bài đang phát.",
    ),
    "loop": (
        "",
        "Bật hoặc tắt lặp bài hiện tại.",
    ),
    "stop": (
        "",
        "Dừng bài hiện tại và xóa hàng đợi. Bot ở lại kênh; phiên đọc chat không bị tắt.",
    ),
    "search": (
        "<từ khóa>",
        "Hiện tối đa năm kết quả YouTube (không tự xếp hàng).",
    ),
}


@dataclass(frozen=True, slots=True)
class HelpTopic:
    key: str
    label: str
    emoji: str
    description: str


HELP_TOPICS: tuple[HelpTopic, ...] = (
    HelpTopic("overview", "Bắt đầu", "📘", "Cách mở bảng điều khiển"),
    HelpTopic("panel", "Bảng điều khiển", "🎛️", "Các nút trên bảng nhạc"),
    HelpTopic("commands", "Lệnh chữ", "⌨️", "Các lệnh với tiền tố bot"),
    HelpTopic("tts", "Đọc tin nhắn", "💬", "TTS trong kênh thoại"),
    HelpTopic("settings", "Cài đặt âm thanh", "⚙️", "Âm lượng, TTS, và đọc tên"),
)

HELP_TOPIC_KEYS: tuple[str, ...] = tuple(topic.key for topic in HELP_TOPICS)


def normalize_help_prefix(prefix: object) -> str:
    """Return a usable command prefix, falling back to the application default."""
    if isinstance(prefix, str) and prefix:
        return prefix
    return DEFAULT_COMMAND_PREFIX


def _command(prefix: str, name: str, usage: str = "") -> str:
    extra = f" {usage}" if usage else ""
    return f"`{prefix}{name}{extra}`"


def build_help_embed(page: str, prefix: str) -> discord.Embed:
    """Render one help topic. Unknown keys fall back to the overview page."""
    prefix = normalize_help_prefix(prefix)
    builder = _PAGE_BUILDERS.get(page, _build_overview)
    return builder(prefix)


def build_command_help_embed(name: str, prefix: str) -> discord.Embed | None:
    """Render a single command page, or ``None`` when the name is unknown."""
    prefix = normalize_help_prefix(prefix)
    entry = COMMAND_HELP.get(name)
    if entry is None:
        return None
    usage, summary = entry
    embed = discord.Embed(
        title=f"Lệnh {_command(prefix, name, usage)}",
        description=summary,
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Cách dùng",
        value=_command(prefix, name, usage),
        inline=False,
    )
    extra = _COMMAND_DETAILS.get(name)
    if extra:
        embed.add_field(name="Chi tiết", value=extra, inline=False)
    embed.set_footer(text="Chọn một chủ đề bên dưới để xem menu đầy đủ.")
    return embed


def _base_embed(title: str, description: str) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Chỉ người mở menu này mới chọn được chủ đề. Hết hạn sau 3 phút.")
    return embed


def _build_overview(prefix: str) -> discord.Embed:
    music = _command(prefix, "music")
    help_cmd = _command(prefix, "help", "[lệnh]")
    return _base_embed(
        "Trợ giúp TFD Voice",
        "Bot nhạc và đọc chat trong kênh thoại.\n\n"
        "**Bắt đầu nhanh**\n"
        f"1. Vào một kênh thoại.\n"
        f"2. Gõ {music} để bot vào phòng và gửi **bảng điều khiển nhạc**.\n"
        "3. Bấm **Thêm nhạc** để tìm bài, dán URL, hoặc playlist YouTube.\n\n"
        "Chỉ thành viên trong **đúng kênh thoại** của bot mới dùng được nút "
        "và lệnh điều khiển — kể cả quản trị viên cũng phải vào cùng phòng.\n\n"
        f"Chọn chủ đề bên dưới, hoặc dùng {help_cmd} để xem một lệnh cụ thể.",
    )


def _build_panel(prefix: str) -> discord.Embed:
    embed = _base_embed(
        "Bảng điều khiển nhạc",
        f"Mở bảng bằng {_command(prefix, 'music')} khi bạn đang trong kênh thoại. "
        "Mọi thành viên trong phòng đó dùng chung một bảng. "
        "Tìm kiếm, hàng đợi đầy đủ, xác nhận xóa, và lỗi chỉ hiện với người bấm.",
    )
    embed.add_field(
        name="Thêm và phát",
        value=(
            "**Thêm nhạc** — tên bài, URL video, hoặc playlist YouTube. "
            "Tìm kiếm hiện tối đa 5 kết quả riêng; playlist thêm tối đa 25 video mỗi lần.\n"
            "**Tạm dừng / Tiếp tục** — dừng hoặc phát tiếp bài hiện tại.\n"
            "**Bài tiếp** — bỏ qua bài đang phát, kể cả khi bài đang tải.\n"
            "**Lặp** — bật hoặc tắt lặp bài hiện tại.\n"
            "**Tua đến** — nhảy tới mốc `HH:MM:SS`."
        ),
        inline=False,
    )
    embed.add_field(
        name="Hàng đợi",
        value=(
            "**Hàng đợi** — danh sách chờ, 10 bài mỗi trang.\n"
            "**Xóa hàng đợi** — xóa bài đang chờ; bài hiện tại vẫn phát. Có bước xác nhận.\n"
            "**Dừng** — dừng bài hiện tại và xóa hàng đợi. Bot ở lại kênh; "
            "phiên đọc chat không bị tắt."
        ),
        inline=False,
    )
    embed.add_field(
        name="Giọng nói và phòng",
        value=(
            "**Đọc tên bài** — đọc to tiêu đề khi bài mới bắt đầu. "
            "Dòng chữ **Đang phát** vẫn được gửi khi tắt đọc.\n"
            "**Đọc tin nhắn** — đọc chat văn bản của kênh thoại.\n"
            "**Cài đặt** — âm lượng nhạc, mức nhạc khi TTS, ngôn ngữ TTS, "
            "đọc tên người gửi, và tự đưa bảng lên.\n"
            "**Rời** — dừng nhạc, tắt đọc chat, và rời kênh thoại.\n"
            "**Trợ giúp** — menu này, không cần ở trong kênh thoại."
        ),
        inline=False,
    )
    return embed


def _command_lines(prefix: str, names: tuple[str, ...]) -> str:
    lines = []
    for name in names:
        usage, summary = COMMAND_HELP[name]
        lines.append(f"{_command(prefix, name, usage)} — {summary}")
    return "\n".join(lines)


def _build_commands(prefix: str) -> discord.Embed:
    embed = _base_embed(
        "Lệnh chữ",
        f"Tiền tố hiện tại gồm khoảng trắng cuối nếu có. Ví dụ: {_command(prefix, 'music')}.",
    )
    embed.add_field(
        name="Nhạc",
        value=_command_lines(
            prefix,
            (
                "music",
                "play",
                "next",
                "pause",
                "resume",
                "jump",
                "skip",
                "loop",
                "stop",
                "search",
            ),
        ),
        inline=False,
    )
    embed.add_field(
        name="Thoại và TTS",
        value=_command_lines(prefix, ("join", "leave", "nameannounce")),
        inline=False,
    )
    embed.add_field(
        name="Menu trợ giúp",
        value=(
            f"{_command(prefix, 'help')} mở menu này. "
            f"{_command(prefix, 'help', COMMAND_HELP['help'][0])} xem một lệnh."
        ),
        inline=False,
    )
    return embed


def _build_tts(prefix: str) -> discord.Embed:
    embed = _base_embed(
        "Đọc tin nhắn (TTS)",
        "Bot có thể đọc chat văn bản của kênh thoại và đọc tiêu đề bài hát. "
        "Cần `TTS_ENABLED=true` (mặc định).",
    )
    embed.add_field(
        name="Bắt đầu và dừng",
        value=(
            f"{_command(prefix, 'join')} hoặc nút **Đọc tin nhắn** trên bảng — "
            "bắt đầu đọc chat kênh thoại, không dừng nhạc.\n"
            f"{_command(prefix, 'leave')} hoặc nút **Rời** — tắt đọc chat, dừng nhạc, và rời phòng.\n"
            f"{_command(prefix, 'stop')} hoặc nút **Dừng** — chỉ dừng nhạc; phiên đọc chat vẫn chạy."
        ),
        inline=False,
    )
    embed.add_field(
        name="Cách đọc",
        value=(
            "Mặc định chỉ đọc nội dung tin nhắn. "
            "Đặt **Đọc tên người gửi** thành `on` trong **Cài đặt**, hoặc "
            f"{_command(prefix, 'nameannounce', 'on')}, để đọc `Tên nói …`; "
            f"{_command(prefix, 'nameannounce', 'off')} tắt tên.\n"
            "Lệnh bot (tin bắt đầu bằng tiền tố) không được đọc. "
            "Khi nhạc đang phát, bot giảm âm lượng nhạc, đọc tin, rồi khôi phục."
        ),
        inline=False,
    )
    embed.add_field(
        name="Đọc tên bài",
        value=(
            "Nút **Đọc tên bài** bật hoặc tắt đọc tiêu đề khi bài mới bắt đầu. "
            "Tắt đọc thì dòng chữ **Đang phát** vẫn được gửi. "
            "Cả hai nút đọc sẽ mờ khi TTS bị tắt trên bot."
        ),
        inline=False,
    )
    return embed


def _build_settings(prefix: str) -> discord.Embed:
    embed = _base_embed(
        "Cài đặt âm thanh",
        "Nút **Cài đặt** trên bảng mở form riêng. "
        "Thông số dùng chung cho cả máy chủ, không theo từng người. "
        "Mất khi bot khởi động lại.",
    )
    embed.add_field(
        name="Các mục",
        value=(
            "**Âm lượng nhạc** — `0`–`200` (phần trăm). Chỉ ảnh hưởng nhạc, không đổi độ lớn TTS.\n"
            "**Âm lượng nhạc khi TTS** — `0`–`100`. `0` tạm tắt nhạc lúc đọc; `100` không giảm.\n"
            "**Ngôn ngữ TTS** — mã gTTS như `vi`, `en`, `ja`, `ko`.\n"
            "**Đọc tên người gửi** — `on` hoặc `off` (mặc định `off`). Khi `on`, "
            "TTS đọc `Tên nói …` trước nội dung tin nhắn.\n"
            "**Đưa bảng lên lại** — `0` tắt; `1`–`1440` phút tự gửi lại bảng."
        ),
        inline=False,
    )
    embed.add_field(
        name="Khi nào có hiệu lực",
        value=(
            "Âm lượng nhạc và mức giảm khi TTS áp ngay cho bài đang phát. "
            "Đổi ngôn ngữ dùng cho tin TTS tiếp theo; đoạn đang đọc có thể giữ ngôn ngữ cũ.\n"
            "Khi TTS tắt trên bot, form còn âm lượng nhạc và đưa bảng lên; "
            "các mục TTS bị ẩn và không đổi."
        ),
        inline=False,
    )
    embed.add_field(
        name="Lệnh liên quan",
        value=f"{_command(prefix, 'music')} mở bảng để vào **Cài đặt**.",
        inline=False,
    )
    return embed


_PAGE_BUILDERS = {
    "overview": _build_overview,
    "panel": _build_panel,
    "commands": _build_commands,
    "tts": _build_tts,
    "settings": _build_settings,
}

_COMMAND_DETAILS: dict[str, str] = {
    "music": (
        "Chỉ có một bảng hoạt động trên mỗi máy chủ. Mở bảng mới sẽ tắt bảng cũ. "
        "Sau khi bot khởi động lại, chạy lại lệnh này."
    ),
    "join": (
        "Gõ tin trong **chat của kênh thoại** để bot đọc. "
        "Bot ở lại phòng ngay cả khi hàng đợi nhạc trống."
    ),
    "leave": "Đây là cách tắt hết nhạc, TTS, và kết nối thoại.",
    "nameannounce": (
        "Chỉ dùng khi phiên đọc chat đang chạy. Giá trị được lưu cho máy chủ "
        "(đến khi bot khởi động lại) và dùng cho phiên sau. "
        "Cũng có trong form **Cài đặt** trên bảng. Mặc định là tắt."
    ),
    "play": (
        "Chấp nhận URL video, playlist YouTube, hoặc từ khóa tìm kiếm. "
        "Nếu bot đang ở kênh khác, hãy vào đúng kênh đó thay vì chuyển bot."
    ),
    "next": "Giống play nhưng luôn thêm vào hàng đợi, không cắt bài hiện tại.",
    "jump": "Bài phải đang phát hoặc tạm dừng. Mốc ngoài độ dài bài sẽ bị từ chối.",
    "stop": (
        "Không rời kênh thoại. Nếu phiên đọc chat đang chạy, bot vẫn đọc tin nhắn."
    ),
    "search": "Kết quả là liên kết. Dùng play, next, hoặc **Thêm nhạc** để xếp hàng.",
}


async def _send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
        return
    await interaction.response.send_message(content, ephemeral=True)


def _disable(view: discord.ui.View) -> None:
    for child in view.children:
        if hasattr(child, "disabled"):
            child.disabled = True


class HelpTopicSelect(discord.ui.Select["HelpMenuView"]):
    """Single-choice topic selector for the help menu."""

    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(
                label=topic.label,
                value=topic.key,
                description=topic.description,
                emoji=topic.emoji,
                default=topic.key == current,
            )
            for topic in HELP_TOPICS
        ]
        super().__init__(
            placeholder="Chọn chủ đề trợ giúp",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="tfd_help_topic",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, HelpMenuView):
            await _send_ephemeral(interaction, "Menu trợ giúp đã hết hạn.")
            return
        try:
            page = self.values[0]
        except IndexError:
            await _send_ephemeral(interaction, "Chủ đề không còn hợp lệ.")
            return
        if page not in HELP_TOPIC_KEYS:
            await _send_ephemeral(interaction, "Chủ đề không còn hợp lệ.")
            return
        view.set_page(page)
        await interaction.response.edit_message(embed=view.render_embed(), view=view)


class HelpMenuView(discord.ui.View):
    """Requester-only topic menu used by ``!tfd help`` and the panel Help button."""

    def __init__(
        self,
        requester_id: int,
        prefix: str,
        *,
        page: str = "overview",
        timeout: float = HELP_VIEW_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.prefix = normalize_help_prefix(prefix)
        self.page = page if page in HELP_TOPIC_KEYS else "overview"
        self.message: object | None = None
        self.add_item(HelpTopicSelect(self.page))

    def render_embed(self) -> discord.Embed:
        return build_help_embed(self.page, self.prefix)

    def set_page(self, page: str) -> None:
        self.page = page if page in HELP_TOPIC_KEYS else "overview"
        for child in self.children:
            if isinstance(child, HelpTopicSelect):
                for option in child.options:
                    option.default = option.value == self.page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await _send_ephemeral(
            interaction,
            "Chỉ người mở menu này mới có thể chọn chủ đề.",
        )
        return False

    async def on_timeout(self) -> None:
        _disable(self)
        if self.message is not None:
            with contextlib.suppress(discord.HTTPException, AttributeError):
                await self.message.edit(view=self)


class InteractiveHelpCommand(commands.HelpCommand):
    """Replace the default English help dump with the interactive topic menu."""

    def __init__(self) -> None:
        super().__init__(
            command_attrs={
                "name": "help",
                "help": "Hiện menu trợ giúp tương tác.",
                "brief": "Hiện menu trợ giúp.",
            },
            verify_checks=False,
        )

    def command_not_found(self, string: str, /) -> str:
        prefix = normalize_help_prefix(self.context.prefix)
        return (
            f"Không tìm thấy lệnh `{string}`. "
            f"Dùng `{prefix}help` hoặc chọn một chủ đề bên dưới."
        )

    def subcommand_not_found(
        self,
        command: commands.Command[Any, Any, Any],
        string: str,
        /,
    ) -> str:
        prefix = normalize_help_prefix(self.context.prefix)
        return (
            f"Lệnh `{command.qualified_name}` không có lệnh con `{string}`. "
            f"Dùng `{prefix}help` để mở menu trợ giúp."
        )

    async def send_bot_help(
        self,
        mapping: Mapping[commands.Cog | None, list[commands.Command[Any, Any, Any]]],
    ) -> None:
        await self._send_menu()

    async def send_cog_help(self, cog: commands.Cog, /) -> None:
        await self._send_menu(page="commands")

    async def send_group_help(self, group: commands.Group[Any, Any, Any], /) -> None:
        await self.send_command_help(group)

    async def send_command_help(self, command: commands.Command[Any, Any, Any], /) -> None:
        prefix = normalize_help_prefix(self.context.prefix)
        embed = build_command_help_embed(command.name, prefix)
        if embed is None:
            await self._send_menu(page="commands")
            return
        await self._send_menu(embed=embed)

    async def send_error_message(self, error: str, /) -> None:
        await self._send_menu(content=error)

    async def _send_menu(
        self,
        *,
        page: str = "overview",
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        prefix = normalize_help_prefix(self.context.prefix)
        view = HelpMenuView(self.context.author.id, prefix, page=page)
        destination = self.get_destination()
        kwargs: dict[str, Any] = {
            "embed": embed or view.render_embed(),
            "view": view,
        }
        message = (
            await destination.send(content, **kwargs)
            if content
            else await destination.send(**kwargs)
        )
        view.message = message
