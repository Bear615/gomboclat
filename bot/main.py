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

from . import updatenotice
from .ai import Agent
from .audit import AuditLogger
from .config import BotStateStore, Config, GuildSettingsStore
from .ratelimit import RateLimiter
from .tools import RecalledMessage, RepliedMessage, ToolContext

_YES = {"yes", "y", "yeah", "yep", "confirm", "do it", "ok", "okay", "sure"}

# Conversational recall (per-user message memory).
_RECALL_LIMIT = 5      # how many of the requester's own recent messages to surface
_RECALL_SCAN = 100     # how far back in channel history to scan for them
_MAX_RECALL_TEXT = 500  # truncate recalled/replied text to bound context size


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


async def _recall_user_messages(
    message: discord.Message,
    requester: discord.Member,
    limit: int = _RECALL_LIMIT,
    scan: int = _RECALL_SCAN,
) -> list[RecalledMessage]:
    """Fetch the requester's OWN most-recent messages in this channel for context.

    Scans recent channel history (newest first), keeps only messages authored by the
    requester, skips the invoking message itself, and returns up to ``limit`` of them
    oldest→newest. Best-effort: needs Read Message History; on any failure returns [].
    The recalled text is UNTRUSTED and surfaced to the model only as background.
    """
    channel = message.channel
    if not hasattr(channel, "history"):
        return []
    out: list[RecalledMessage] = []
    try:
        async for m in channel.history(limit=scan):
            if m.id == message.id or m.author.id != requester.id:
                continue
            text = (m.content or "").strip() or "[attachment/embed, no text]"
            out.append(RecalledMessage(text=text[:_MAX_RECALL_TEXT], created_at=m.created_at, message_id=m.id))
            if len(out) >= limit:
                break
    except (discord.Forbidden, discord.HTTPException):
        return []
    out.reverse()  # chronological
    return out


async def _resolve_replied_to(message: discord.Message) -> RepliedMessage | None:
    """If the invoking message is a reply, resolve the message it replied to.

    Included regardless of who wrote it (that's the point of a reply) — but still
    UNTRUSTED. Uses the cached ``resolved`` message when available, else fetches it;
    returns None if the reference is missing or the message can't be fetched.
    """
    ref = message.reference
    if ref is None or ref.message_id is None:
        return None
    src = ref.resolved if isinstance(ref.resolved, discord.Message) else None
    if src is None:
        try:
            src = await message.channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    content = (src.content or "").strip() or "[attachment/embed, no text]"
    return RepliedMessage(
        author_display=src.author.display_name,
        author_id=src.author.id,
        is_bot=bool(src.author.bot),
        content=content[:_MAX_RECALL_TEXT],
        message_id=src.id,
    )


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

    # Persistent bot state (git version tracking for the update changelog).
    state_store = BotStateStore(config.db_path)
    update_checked = False  # run the update-notice check once per process

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
        nonlocal update_checked
        try:
            await bot.tree.sync()
        except Exception:
            pass
        if hooks.on_ready and bot.user:
            hooks.on_ready(bot.user, list(bot.guilds))
        hooks.status(f"Connected as {bot.user} — watching {len(bot.guilds)} guild(s).")
        # If we were updated since last boot, post the changelog to each log channel.
        # Guarded so reconnects (on_ready can fire more than once) don't re-announce.
        if not update_checked:
            update_checked = True
            try:
                await updatenotice.announce_if_updated(bot, state_store, settings_store, status=hooks.status)
            except Exception as e:
                hooks.status(f"Update-notice check failed: {e}")

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot or bot.user is None:
            return
        if message.guild is None:
            return  # only operate in guilds
        if bot.user not in message.mentions:
            return  # addressed only via @mention

        settings = settings_store.get(message.guild.id)
        if not settings.enabled:
            return

        text = _strip_mentions(message, bot.user)
        if not text:
            await _reply(message, "Hi! Tell me what you'd like — e.g. *“give me a purple role”*.")
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

        # Conversational context: the requester's own recent messages, plus (if this
        # is a reply) the replied-to message by any author. Both are UNTRUSTED.
        recent = await _recall_user_messages(message, requester)
        replied = await _resolve_replied_to(message)

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
            recent_messages=recent,
            replied_to=replied,
        )

        try:
            async with message.channel.typing():
                reply_text, outcomes = await agent.run(ctx, text)
        except Exception as e:
            await _reply(message, f"Something went wrong while handling that: `{e}`")
            hooks.status(f"Error handling message: {e}")
            return

        body = reply_text
        if not body:
            body = "\n".join(outcomes) if outcomes else "I didn't take any action."
        await _reply(message, body)

    # -- slash command: set the audit log channel -------------------------- #
    @bot.tree.command(name="setlogchannel", description="Set the channel where moderation actions are logged.")
    @app_commands.describe(channel="The text channel to send audit logs to (leave empty to use this one).")
    async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You need the **Manage Server** permission to set the log channel.", ephemeral=True
            )
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
        me = interaction.guild.me
        await interaction.response.send_message(
            f"**AI Moderator status**\n"
            f"• Version: `{updatenotice.current_version(state_store)}`\n"
            f"• Model: `{config.model}`\n"
            f"• Log channel: {log_ch.mention if log_ch else '_not set_ (use /setlogchannel)'}\n"
            f"• Rate limit: {limit} write actions / {config.rate_limit_window}s per user\n"
            f"• Punitive tools: {'enabled (typed CONFIRM required)' if config.enable_punitive else 'disabled'}\n"
            f"• My top role position: {me.top_role.position}\n"
            f"Mention me and describe what you'd like.",
            ephemeral=True,
        )

    return bot


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
