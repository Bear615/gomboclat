"""Discord entrypoint: client, intents, addressing logic, confirmation flow, and
the slash command for configuring the per-guild log channel.

Addressing: the AI is invoked ONLY when the bot is @mentioned in a guild text
channel. We never run an LLM call on every message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

from .ai import Agent
from .audit import AuditLogger
from .config import Config, GuildSettingsStore
from .ratelimit import RateLimiter
from .tools import ToolContext

_YES = {"yes", "y", "yeah", "yep", "confirm", "do it", "ok", "okay", "sure"}


@dataclass
class BotHooks:
    """Optional callbacks so a front-end (the TUI) can observe bot lifecycle."""

    on_ready: Callable[[discord.ClientUser, list[discord.Guild]], None] | None = None
    on_status: Callable[[str], None] | None = None
    on_message_seen: Callable[[str], None] | None = None

    def status(self, msg: str) -> None:
        if self.on_status:
            self.on_status(msg)


def _strip_mentions(message: discord.Message, me: discord.ClientUser) -> str:
    content = message.content or ""
    content = re.sub(rf"<@!?{me.id}>", "", content)
    return content.strip()


def create_bot(
    config: Config,
    audit: AuditLogger,
    settings_store: GuildSettingsStore,
    ratelimiter: RateLimiter,
    agent: Agent,
    hooks: BotHooks | None = None,
) -> commands.Bot:
    hooks = hooks or BotHooks()

    intents = discord.Intents.default()
    intents.message_content = True  # privileged — enable in the Developer Portal
    intents.members = True          # privileged — enable in the Developer Portal
    intents.guilds = True

    bot = commands.Bot(command_prefix="!moderator-unused-prefix ", intents=intents, help_command=None)

    # -- confirmation flow ------------------------------------------------- #
    def make_confirm(message: discord.Message) -> Callable[..., Awaitable[bool]]:
        async def confirm(prompt: str, *, required: str | None = None) -> bool:
            await _reply(message, prompt)

            def check(m: discord.Message) -> bool:
                return m.author.id == message.author.id and m.channel.id == message.channel.id

            try:
                reply = await bot.wait_for("message", check=check, timeout=60.0)
            except Exception:
                await _reply(message, "⏳ Timed out waiting for confirmation — nothing was done.")
                return False

            answer = (reply.content or "").strip()
            if required is not None:
                return answer == required
            return answer.lower() in _YES

        return confirm

    # -- events ------------------------------------------------------------ #
    @bot.event
    async def on_ready() -> None:
        try:
            await bot.tree.sync()
        except Exception:
            pass
        if hooks.on_ready and bot.user:
            hooks.on_ready(bot.user, list(bot.guilds))
        hooks.status(f"Connected as {bot.user} — watching {len(bot.guilds)} guild(s).")

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot or bot.user is None:
            return
        if message.guild is None:
            # DMs can't touch a server, but silence is confusing — nudge instead.
            await _reply(
                message,
                "👋 I work **inside a server**, where I can see its roles and channels. "
                "Mention me there and tell me what you'd like — e.g. *“give me a purple role”*. "
                "Run `/help` in the server for the full rundown.",
            )
            return
        if bot.user not in message.mentions:
            return  # addressed only via @mention

        settings = settings_store.get(message.guild.id)
        if not settings.enabled:
            return

        text = _strip_mentions(message, bot.user)
        if not text:
            await _reply(
                message,
                "Hi! Tell me what you'd like — e.g. *“give me a purple role”* or "
                "*“make a role that can only see #secret-lab and give it to me”*. "
                "Run `/help` to see everything I can do.",
            )
            return

        hooks.on_message_seen and hooks.on_message_seen(
            f"{message.author} in #{getattr(message.channel, 'name', '?')}: {text}"
        )

        requester = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(message.author.id)
        if requester is None:
            return
        bot_member = message.guild.me
        log_channel = None
        if settings.log_channel_id:
            log_channel = message.guild.get_channel(settings.log_channel_id)

        ctx = ToolContext(
            guild=message.guild,
            requester=requester,
            channel=message.channel,
            bot_member=bot_member,
            config=config,
            audit=audit,
            ratelimiter=ratelimiter,
            settings=settings,
            raw_message=text,
            confirm=make_confirm(message),
            log_channel=log_channel,
        )

        acked = await _react(message, "👀")  # visible "I'm on it" while thinking
        try:
            async with message.channel.typing():
                reply_text, outcomes = await agent.run(ctx, text)
        except Exception as e:
            await _unreact(message, "👀", acked)
            await _react(message, "⚠️")
            await _reply(message, f"Something went wrong while handling that: `{e}`")
            hooks.status(f"Error handling message: {e}")
            return

        await _unreact(message, "👀", acked)
        await _react(message, "✅")

        body = reply_text
        if not body:
            body = "\n".join(outcomes) if outcomes else "I didn't take any action."
        await _reply(message, body)

    # -- slash command: set the audit log channel -------------------------- #
    @bot.tree.command(name="setlogchannel", description="Set the channel where moderation actions are logged.")
    @app_commands.describe(channel="The text channel to send audit logs to (leave empty to use this one).")
    async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        if not _require_manage_guild(interaction):
            await _deny(interaction)
            return
        target = channel or interaction.channel
        settings_store.set_log_channel(interaction.guild.id, target.id)
        await interaction.response.send_message(f"✅ Audit logs will go to {target.mention}.", ephemeral=True)

    @bot.tree.command(name="modstatus", description="Show the AI moderator's status in this server.")
    async def modstatus(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        s = settings_store.get(interaction.guild.id)
        log_ch = interaction.guild.get_channel(s.log_channel_id) if s.log_channel_id else None
        limit = s.rate_limit_max or config.rate_limit_max
        limit_note = "" if s.rate_limit_max is None else " (server override)"
        me = interaction.guild.me
        await interaction.response.send_message(
            f"**AI Moderator status**\n"
            f"• Active here: {'✅ yes' if s.enabled else '⏸️ disabled (use /togglebot)'}\n"
            f"• Model: `{config.anthropic_model}`\n"
            f"• Log channel: {log_ch.mention if log_ch else '_not set_ (use /setlogchannel)'}\n"
            f"• Rate limit: {limit} write actions / {config.rate_limit_window}s per user{limit_note}\n"
            f"• Bulk-confirm threshold: {config.bulk_confirm_threshold} writes/turn\n"
            f"• Punitive tools: {'enabled (typed CONFIRM required)' if config.enable_punitive else 'disabled'}\n"
            f"• My top role position: {me.top_role.position}\n"
            f"Mention me and describe what you'd like, or run `/help`.",
            ephemeral=True,
        )

    # -- slash command: friendly help -------------------------------------- #
    @bot.tree.command(name="help", description="Show what the AI moderator can do, with examples.")
    async def help_command(interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🤖 AI Moderator — help",
            description=(
                "Mention me and describe what you want in plain English. I only act when "
                "**@mentioned**, and every change is re-checked against *your* real Discord "
                "permissions before it happens — I can't let you do anything you couldn't do yourself."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Examples",
            value=(
                "• `@AI Moderator give me a purple role`\n"
                "• `@AI Moderator make a role that can only see #secret-lab and give it to me`\n"
                "• `@AI Moderator rename Dave to \"On Vacation\"`\n"
                "• `@AI Moderator create a voice channel called Lounge`"
            ),
            inline=False,
        )
        embed.add_field(
            name="What I can do",
            value=(
                "Create/assign/remove roles · set role colours · create channels & categories · "
                "scope channel access · change nicknames. With confirmation: kick / ban / timeout."
            ),
            inline=False,
        )
        embed.add_field(
            name="Slash commands",
            value=(
                "`/help` — this message\n"
                "`/modstatus` — current settings\n"
                "`/setlogchannel` — where audit logs go *(Manage Server)*\n"
                "`/setratelimit` — writes allowed per user *(Manage Server)*\n"
                "`/togglebot` — enable/disable me here *(Manage Server)*\n"
                "`/auditlog` — recent actions *(Manage Server)*"
            ),
            inline=False,
        )
        embed.set_footer(text="I parse intent; Python enforces permissions. The model is not a security boundary.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- slash command: per-guild rate limit ------------------------------- #
    @bot.tree.command(name="setratelimit", description="Set how many write actions each user may run per window.")
    @app_commands.describe(max_actions="Writes allowed per user per window. Leave empty to reset to the default.")
    async def setratelimit(
        interaction: discord.Interaction,
        max_actions: app_commands.Range[int, 1, 100] | None = None,
    ) -> None:
        if not _require_manage_guild(interaction):
            await _deny(interaction)
            return
        settings_store.set_rate_limit(interaction.guild.id, max_actions)  # type: ignore[union-attr]
        if max_actions is None:
            await interaction.response.send_message(
                f"✅ Rate limit reset to the default ({config.rate_limit_max} / {config.rate_limit_window}s).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ Rate limit set to **{max_actions}** write actions / {config.rate_limit_window}s per user.",
                ephemeral=True,
            )

    # -- slash command: enable/disable in this guild ----------------------- #
    @bot.tree.command(name="togglebot", description="Enable or disable the AI moderator in this server.")
    async def togglebot(interaction: discord.Interaction) -> None:
        if not _require_manage_guild(interaction):
            await _deny(interaction)
            return
        current = settings_store.get(interaction.guild.id)  # type: ignore[union-attr]
        new_state = not current.enabled
        settings_store.set_enabled(interaction.guild.id, new_state)  # type: ignore[union-attr]
        await interaction.response.send_message(
            f"{'✅ I’m now **active**' if new_state else '⏸️ I’m now **disabled**'} in this server."
            + ("" if new_state else " Mentions will be ignored until you re-enable me."),
            ephemeral=True,
        )

    # -- slash command: recent audit log ----------------------------------- #
    @bot.tree.command(name="auditlog", description="Show the most recent moderation actions in this server.")
    @app_commands.describe(limit="How many recent entries to show (1–25, default 10).")
    async def auditlog(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        if not _require_manage_guild(interaction):
            await _deny(interaction)
            return
        records = audit.recent_for_guild(interaction.guild.id, limit)  # type: ignore[union-attr]
        if not records:
            await interaction.response.send_message("No moderation actions logged here yet.", ephemeral=True)
            return
        lines = []
        for r in records:  # newest first
            mark = "✅" if r.allowed else "⛔"
            ts = r.timestamp[:19].replace("T", " ")
            lines.append(f"{mark} `{ts}` **{r.action}** by {r.requester_name} — {r.outcome}")
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + "\n…"
        await interaction.response.send_message(
            f"**Last {len(records)} action(s):**\n{body}", ephemeral=True
        )

    return bot


def _require_manage_guild(interaction: discord.Interaction) -> bool:
    """True only if this is a guild interaction by a member with Manage Server."""
    if interaction.guild is None:
        return False
    member = interaction.user
    return isinstance(member, discord.Member) and member.guild_permissions.manage_guild


async def _deny(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "You need the **Manage Server** permission to use that command.", ephemeral=True
    )


async def _reply(message: discord.Message, text: str) -> None:
    """Reply, chunked to Discord's 2000-char limit."""
    if not text:
        return
    for chunk in _chunks(text, 1900):
        try:
            await message.reply(chunk, mention_author=False)
        except discord.HTTPException:
            try:
                await message.channel.send(chunk)
            except discord.HTTPException:
                pass


def _chunks(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


async def _react(message: discord.Message, emoji: str) -> bool:
    """Best-effort reaction. Returns True if it landed (so we can remove it later)."""
    try:
        await message.add_reaction(emoji)
        return True
    except discord.HTTPException:
        return False


async def _unreact(message: discord.Message, emoji: str, added: bool) -> None:
    """Remove a reaction we added earlier; silent if we can't."""
    if not added or message.guild is None or message.guild.me is None:
        return
    try:
        await message.remove_reaction(emoji, message.guild.me)
    except discord.HTTPException:
        pass
