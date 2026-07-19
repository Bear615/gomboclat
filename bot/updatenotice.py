"""Announce a self-incrementing changelog whenever the bot is updated.

On startup we compare the running git HEAD against the last SHA we recorded in the
``bot_state`` table. If it changed, the bot was updated since we last ran, so:

- bump the version (``v0.0.N`` -> ``v0.0.N+1``) — it climbs by itself, one per update;
- gather the new commit subjects (``old..HEAD``) as the changelog body;
- post it to every guild's configured log channel, pinging that guild's owner (and
  whoever added the bot, when the audit log lets us tell).

This runs once per process (guarded in ``main.py``), and detects updates from ANY
path — the TUI auto-update, a manual "Update & restart", or any ``git pull`` + restart —
because it keys purely on the HEAD SHA changing between boots.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord

from . import maintenance

# bot_state keys.
_SHA_KEY = "last_sha"
_PATCH_KEY = "version_patch"

_MAX_CHANGELOG = 15  # commit lines to show before summarising the rest


def format_version(patch: int) -> str:
    return f"v0.0.{patch}"


def _read_patch(state) -> int:
    try:
        return int(state.get(_PATCH_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def current_version(state) -> str:
    """The version string to display elsewhere (e.g. /modstatus). Never below v0.0.1."""
    return format_version(_read_patch(state) or 1)


def _plan_update(stored_sha: str | None, stored_patch: int, current_sha: str) -> tuple[str, int]:
    """Decide what to do from the stored vs. current git state (pure, unit-tested).

    Returns (action, new_patch) where action is:
      - 'noop'     : git unavailable, or HEAD unchanged since last boot;
      - 'init'     : first time we've ever seen a SHA -> record baseline v0.0.1 silently;
      - 'announce' : HEAD changed -> an update happened; bump the patch and announce.
    """
    if not current_sha:
        return ("noop", stored_patch)
    if stored_sha is None:
        return ("init", 1)  # baseline; not an "update", so no announcement
    if stored_sha != current_sha:
        return ("announce", stored_patch + 1)
    return ("noop", stored_patch)


@dataclass
class UpdateInfo:
    version: str
    old_short: str
    new_short: str
    changelog: list[str]


def _build_embed(info: UpdateInfo) -> discord.Embed:
    if info.changelog:
        shown = info.changelog[:_MAX_CHANGELOG]
        body = "\n".join(f"• {s}" for s in shown)
        extra = len(info.changelog) - len(shown)
        if extra > 0:
            body += f"\n…and {extra} more change(s)."
    else:
        body = "• Updated to the latest version."
    embed = discord.Embed(
        title=f"📦 {info.version} — Update installed",
        description=body[:4000],
        colour=discord.Colour.green(),
    )
    if info.old_short and info.new_short:
        embed.set_footer(text=f"{info.old_short} → {info.new_short}")
    return embed


async def _find_inviter_id(guild: discord.Guild, bot_user: discord.abc.User) -> int | None:
    """Best-effort: who added the bot to this guild (audit log ``bot_add``). None if we
    lack View Audit Log or can't find it.
    """
    try:
        async for entry in guild.audit_logs(limit=8, action=discord.AuditLogAction.bot_add):
            target = entry.target
            if target is not None and getattr(target, "id", None) == bot_user.id:
                return entry.user.id if entry.user else None
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


async def _broadcast(bot, settings_store, info: UpdateInfo) -> int:
    """Post the announcement to every guild that has a log channel. Returns how many
    channels we actually sent to. Sends are best-effort; a failure in one guild never
    blocks the others.
    """
    embed = _build_embed(info)
    sent = 0
    for guild in list(bot.guilds):
        settings = settings_store.get(guild.id)
        if not settings.log_channel_id:
            continue
        channel = guild.get_channel(settings.log_channel_id)
        if channel is None or not hasattr(channel, "send"):
            continue

        ping_ids: list[int] = []
        if guild.owner_id:
            ping_ids.append(guild.owner_id)
        inviter = await _find_inviter_id(guild, bot.user)
        if inviter and inviter not in ping_ids:
            ping_ids.append(inviter)

        mentions = " ".join(f"<@{uid}>" for uid in ping_ids)
        content = (
            f"{mentions} — the bot just updated to **{info.version}**."
            if mentions
            else f"The bot just updated to **{info.version}**."
        )
        try:
            await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            sent += 1
        except (discord.Forbidden, discord.HTTPException):
            continue
    return sent


async def announce_if_updated(bot, state, settings_store, status=None) -> UpdateInfo | None:
    """Detect an update since last boot and, if so, announce it. Returns the UpdateInfo
    when an announcement was made, else None. Safe to call once per process.
    """
    current_sha = await maintenance.git_head_sha()
    stored_sha = state.get(_SHA_KEY)
    stored_patch = _read_patch(state)

    action, new_patch = _plan_update(stored_sha, stored_patch, current_sha)
    if action == "noop":
        return None
    if action == "init":
        state.set(_SHA_KEY, current_sha)
        state.set(_PATCH_KEY, str(new_patch))
        if status:
            status(f"Version baseline set to {format_version(new_patch)} ({current_sha[:7]}).")
        return None

    # action == "announce"
    version = format_version(new_patch)
    subjects = await maintenance.git_log_subjects(stored_sha or "", "HEAD", limit=_MAX_CHANGELOG + 10)
    info = UpdateInfo(
        version=version,
        old_short=(stored_sha or "")[:7],
        new_short=current_sha[:7],
        changelog=subjects,
    )

    # Record the new state BEFORE sending so a send failure (or a reconnect firing
    # on_ready again) can't produce a re-announce loop for the same update.
    state.set(_SHA_KEY, current_sha)
    state.set(_PATCH_KEY, str(new_patch))

    if status:
        status(f"Update detected — now {version} ({info.old_short}→{info.new_short}); announcing.")
    await _broadcast(bot, settings_store, info)
    return info
