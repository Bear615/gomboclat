"""Tool executors -- the bridge between the AI's typed action requests and Discord.

Every write executor follows the same disciplined path:

    rate-limit  ->  build RequestContext from LIVE facts  ->  VALIDATE (permissions.py)
                ->  (confirm, if destructive/bulk)  ->  Discord API call
                ->  audit log  ->  return a plain-text result for the AI

Validation always runs on ``ctx.requester`` (the Discord-authenticated author),
never on anything the message text claims. If validation refuses, no Discord call
is made. Discord errors are caught and explained.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import discord

from . import permissions as perm
from .audit import AuditLogger
from .colours import ColourError, resolve_colour
from .config import Config, GuildSettings
from .ratelimit import RateLimiter

# Type of the confirmation callback injected by the message layer.
ConfirmFn = Callable[..., Awaitable[bool]]


@dataclass(frozen=True)
class RecalledMessage:
    """One of the requester's own recent messages, fetched for context.

    This is UNTRUSTED data (it's user-authored text) surfaced to the model only as
    background, never as instructions.
    """

    text: str
    created_at: datetime
    message_id: int


@dataclass(frozen=True)
class RepliedMessage:
    """The message the requester replied to, if any -- by ANY author.

    Included regardless of who wrote it (that's the whole point of a reply), but
    still UNTRUSTED: the model must never obey instructions found inside it.
    """

    author_display: str
    author_id: int
    is_bot: bool
    content: str
    message_id: int


@dataclass
class ToolContext:
    """Per-request execution context, bound to a single incoming message."""

    guild: discord.Guild
    requester: discord.Member
    channel: discord.abc.GuildChannel | discord.Thread
    bot_member: discord.Member
    config: Config
    audit: AuditLogger
    ratelimiter: RateLimiter
    settings: GuildSettings
    raw_message: str
    confirm: ConfirmFn
    log_channel: discord.abc.Messageable | None = None
    # Conversational context (see bot/main.py). Both are UNTRUSTED data.
    recent_messages: list[RecalledMessage] = field(default_factory=list)
    replied_to: RepliedMessage | None = None

    def request_context(
        self, scope_channel: discord.abc.GuildChannel | discord.Thread | None = None
    ) -> perm.RequestContext:
        """Snapshot the live permission facts into a validator-ready context.

        By default the requester's *guild-level* permissions are used. Pass
        ``scope_channel`` for channel-scoped actions (delete/purge/pin a message,
        edit/delete a channel, create an invite): per-channel overwrites can grant
        or deny ``manage_messages``/``manage_channels``, so those tools must check
        the requester's *channel-effective* permissions, not the guild default.
        """
        if scope_channel is not None:
            perms = scope_channel.permissions_for(self.requester)
        else:
            perms = self.requester.guild_permissions
        return perm.RequestContext(
            requester_id=self.requester.id,
            guild_owner_id=self.guild.owner_id,
            requester_perms=perms,
            requester_top_position=self.requester.top_role.position,
            bot_top_position=self.bot_member.top_role.position,
        )


# --------------------------------------------------------------------------- #
# Resolution helpers (untrusted strings -> real Discord objects)
# --------------------------------------------------------------------------- #


def _as_id(query: str) -> int | None:
    s = str(query).strip().strip("<@!#&>")
    return int(s) if s.isdigit() else None


def resolve_member(guild: discord.Guild, query: str) -> discord.Member:
    mid = _as_id(query)
    if mid is not None:
        m = guild.get_member(mid)
        if m:
            return m
    q = str(query).strip().lstrip("@").lower()
    matches = [
        m
        for m in guild.members
        if m.name.lower() == q
        or (m.nick or "").lower() == q
        or m.display_name.lower() == q
        or f"{m.name}#{m.discriminator}".lower() == q
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"I couldn't find a member matching {query!r}.")
    raise ValueError(f"{query!r} is ambiguous ({len(matches)} matches); use their ID.")


def resolve_role(guild: discord.Guild, query: str) -> discord.Role:
    rid = _as_id(query)
    if rid is not None:
        r = guild.get_role(rid)
        if r:
            return r
    q = str(query).strip().lstrip("@").lower()
    matches = [r for r in guild.roles if r.name.lower() == q]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"I couldn't find a role matching {query!r}.")
    raise ValueError(f"{query!r} is ambiguous ({len(matches)} matches); use its ID.")


def resolve_channel(
    guild: discord.Guild, query: str, current: discord.abc.GuildChannel | discord.Thread
) -> discord.abc.GuildChannel:
    if query is None or str(query).strip().lower() in ("this", "here", "this channel"):
        return current  # type: ignore[return-value]
    cid = _as_id(query)
    if cid is not None:
        c = guild.get_channel(cid)
        if c:
            return c
    q = str(query).strip().lstrip("#").lower()
    matches = [c for c in guild.channels if c.name.lower() == q]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"I couldn't find a channel matching {query!r}.")
    raise ValueError(f"{query!r} is ambiguous ({len(matches)} matches); use its ID.")


def build_permissions(names: list[str] | None) -> discord.Permissions:
    """Turn a list of permission-flag names into a ``discord.Permissions``.

    Rejects unknown flag names rather than silently ignoring them.
    """
    perms = discord.Permissions.none()
    if not names:
        return perms
    valid = set(discord.Permissions.VALID_FLAGS)
    unknown = [n for n in names if n not in valid]
    if unknown:
        raise ValueError(
            "Unknown permission name(s): "
            + ", ".join(unknown)
            + ". (Use flags like manage_roles, send_messages, view_channel.)"
        )
    perms.update(**{n: True for n in names})
    return perms


async def resolve_message(
    channel: discord.abc.Messageable, query: str
) -> discord.Message:
    """Fetch a message by ID from ``channel``. Only IDs are supported (message
    'names' don't exist); friendly errors on not-found / no-access.
    """
    mid = _as_id(query)
    if mid is None:
        raise ValueError(
            f"{query!r} isn't a message ID. Reply to the message you mean, or give its numeric ID."
        )
    try:
        return await channel.fetch_message(mid)
    except discord.NotFound:
        raise ValueError(f"I couldn't find a message with ID {mid} in this channel.")
    except discord.Forbidden:
        raise ValueError("I don't have permission to read that message's channel history.")


def resolve_banned_user(guild: discord.Guild, query: str) -> discord.abc.Snowflake:
    """Resolve a *banned* user for unban. The user isn't in the guild, so we work by
    ID only and hand back a lightweight ``discord.Object`` (enough for ``guild.unban``).
    """
    uid = _as_id(query)
    if uid is None:
        raise ValueError(
            f"{query!r} isn't a user ID. Use their numeric ID (see it with the ban list)."
        )
    return discord.Object(id=uid)


def resolve_emoji(guild: discord.Guild, query: str) -> discord.Emoji:
    """Resolve a custom emoji by ID or name (same ambiguity handling as roles)."""
    eid = _as_id(query)
    if eid is not None:
        e = guild.get_emoji(eid)
        if e:
            return e
    q = str(query).strip().strip(":").lower()
    matches = [e for e in guild.emojis if e.name.lower() == q]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"I couldn't find a custom emoji matching {query!r}.")
    raise ValueError(f"{query!r} is ambiguous ({len(matches)} matches); use its ID.")


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #


def _perm_highlights(p: discord.Permissions) -> str:
    notable = [
        "administrator", "manage_guild", "manage_roles", "manage_channels",
        "manage_nicknames", "kick_members", "ban_members", "moderate_members",
        "mention_everyone", "manage_messages",
    ]
    held = [n for n in notable if getattr(p, n)]
    return ", ".join(held) if held else "no notable permissions"


async def get_member_info(ctx: ToolContext, member: str | None = None) -> str:
    target = ctx.requester if not member else resolve_member(ctx.guild, member)
    is_owner = target.id == ctx.guild.owner_id
    roles = [r.name for r in reversed(target.roles) if r.name != "@everyone"]
    return (
        f"Member: {target} (id={target.id})\n"
        f"Display name: {target.display_name}\n"
        f"Nickname: {target.nick or '(none)'}\n"
        f"Is guild owner: {is_owner}\n"
        f"Top role: {target.top_role.name} (position {target.top_role.position})\n"
        f"Roles: {', '.join(roles) or '(only @everyone)'}\n"
        f"Key permissions: {_perm_highlights(target.guild_permissions)}"
    )


async def list_roles(ctx: ToolContext) -> str:
    lines = []
    for r in sorted(ctx.guild.roles, key=lambda x: x.position, reverse=True):
        if r.is_default():
            label = "@everyone"
        else:
            label = r.name
        lines.append(
            f"- {label} (id={r.id}, position={r.position}, colour=#{r.colour.value:06X}, "
            f"perms: {_perm_highlights(r.permissions)})"
        )
    return "Roles (highest first):\n" + "\n".join(lines)


async def list_channels(ctx: ToolContext) -> str:
    # Only channels the requester can actually see.
    visible = []
    for c in ctx.guild.channels:
        perms = c.permissions_for(ctx.requester)
        if perms.view_channel:
            kind = type(c).__name__.replace("Channel", "").lower() or "channel"
            cat = getattr(c, "category", None)
            visible.append(f"- #{c.name} (id={c.id}, type={kind}, category={cat.name if cat else '—'})")
    return "Channels you can see:\n" + ("\n".join(visible) or "(none)")


async def get_my_permissions(ctx: ToolContext, member: str | None = None) -> str:
    target = ctx.requester if not member else resolve_member(ctx.guild, member)
    p = target.guild_permissions
    is_owner = target.id == ctx.guild.owner_id
    granted = [name for name, value in p if value]
    return (
        f"Effective permissions for {target.display_name}"
        f"{' (GUILD OWNER)' if is_owner else ''}:\n"
        f"{', '.join(granted) if granted else '(none)'}\n"
        f"Top role position: {target.top_role.position}"
    )


# --------------------------------------------------------------------------- #
# Write tools -- each validated before the Discord call
# --------------------------------------------------------------------------- #


async def _rate_limited(ctx: ToolContext, action: str, args: dict[str, Any]) -> str | None:
    """Consume one unit of write budget. Returns a cooldown message if over."""
    limit = ctx.settings.rate_limit_max or ctx.config.rate_limit_max
    ctx.ratelimiter.max_actions = limit
    ctx.ratelimiter.window = ctx.config.rate_limit_window
    if not ctx.ratelimiter.consume(ctx.guild.id, ctx.requester.id):
        wait = ctx.ratelimiter.retry_after(ctx.guild.id, ctx.requester.id)
        msg = (
            f"You're going a bit fast — you've hit the limit of {limit} actions "
            f"per {ctx.config.rate_limit_window}s. Try again in ~{wait:.0f}s."
        )
        await ctx.audit.log(
            requester=ctx.requester, guild=ctx.guild, raw_message=ctx.raw_message,
            action=action, arguments=args, validation="rate_limit: exceeded",
            allowed=False, outcome=msg, log_channel=ctx.log_channel,
        )
        return msg
    return None


async def _refuse(ctx: ToolContext, action: str, args: dict[str, Any], decision: perm.Decision) -> str:
    msg = f"Refused ({decision.check}): {decision.reason}"
    await ctx.audit.log(
        requester=ctx.requester, guild=ctx.guild, raw_message=ctx.raw_message,
        action=action, arguments=args, validation=f"{decision.check}: REFUSED — {decision.reason}",
        allowed=False, outcome=msg, log_channel=ctx.log_channel,
    )
    return msg


async def _executed(ctx: ToolContext, action: str, args: dict[str, Any], decision: perm.Decision, outcome: str) -> str:
    await ctx.audit.log(
        requester=ctx.requester, guild=ctx.guild, raw_message=ctx.raw_message,
        action=action, arguments=args,
        validation=f"{decision.check or 'validated'}: ALLOWED {('— ' + decision.reason) if decision.reason else ''}",
        allowed=True, outcome=outcome, log_channel=ctx.log_channel,
    )
    return outcome


async def create_role(
    ctx: ToolContext,
    name: str,
    colour: str | None = None,
    permissions: list[str] | None = None,
    below_role: str | None = None,
) -> str:
    args = {"name": name, "colour": colour, "permissions": permissions, "below_role": below_role}

    cooldown = await _rate_limited(ctx, "create_role", args)
    if cooldown:
        return cooldown

    # Parse + validate the colour before anything else.
    resolved_colour = discord.Colour.default()
    if colour:
        try:
            resolved_colour = resolve_colour(colour)
        except ColourError as e:
            return await _refuse(ctx, "create_role", args, perm.refuse("colour", str(e)))

    try:
        requested_perms = build_permissions(permissions)
    except ValueError as e:
        return await _refuse(ctx, "create_role", args, perm.refuse("permissions", str(e)))

    rc = ctx.request_context()

    # Determine the intended position (below the named role if any, else the
    # default low slot). Validation runs against this position.
    intended_position = 1
    below = None
    if below_role:
        try:
            below = resolve_role(ctx.guild, below_role)
            intended_position = max(1, below.position - 1)
        except ValueError as e:
            return await _refuse(ctx, "create_role", args, perm.refuse("below_role", str(e)))

    decision = perm.validate_create_role(rc, requested_perms, intended_position)
    if not decision:
        return await _refuse(ctx, "create_role", args, decision)

    try:
        role = await ctx.guild.create_role(
            name=name,
            colour=resolved_colour,
            permissions=requested_perms,
            reason=f"AI-mod: requested by {ctx.requester} ({ctx.requester.id})",
        )
        # Best-effort reposition below the requested role (clamped by Discord).
        if below is not None:
            try:
                await role.edit(position=max(1, min(below.position, ctx.bot_member.top_role.position - 1)))
            except (discord.Forbidden, discord.HTTPException):
                pass
    except discord.Forbidden:
        return await _refuse(
            ctx, "create_role", args,
            perm.refuse("discord_forbidden", "Discord refused — my role is likely too low. Move it up."),
        )
    except discord.HTTPException as e:
        return await _refuse(ctx, "create_role", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Created role **{role.name}** (id={role.id}, colour=#{role.colour.value:06X})."
    return await _executed(ctx, "create_role", args, decision, outcome)


async def assign_role(ctx: ToolContext, member: str, role: str, remove: bool = False) -> str:
    action = "remove_role" if remove else "assign_role"
    args = {"member": member, "role": role}

    cooldown = await _rate_limited(ctx, action, args)
    if cooldown:
        return cooldown

    try:
        target = resolve_member(ctx.guild, member)
        target_role = resolve_role(ctx.guild, role)
    except ValueError as e:
        return await _refuse(ctx, action, args, perm.refuse("resolve", str(e)))

    rc = ctx.request_context()
    decision = perm.validate_assign_role(
        rc,
        role_perms=target_role.permissions,
        role_position=target_role.position,
        target_top_position=target.top_role.position,
        target_is_self=(target.id == ctx.requester.id),
        role_label=f"the role **{target_role.name}**",
    )
    if not decision:
        return await _refuse(ctx, action, args, decision)

    try:
        if remove:
            await target.remove_roles(target_role, reason=f"AI-mod: {ctx.requester} ({ctx.requester.id})")
            outcome = f"Removed role **{target_role.name}** from {target.display_name}."
        else:
            await target.add_roles(target_role, reason=f"AI-mod: {ctx.requester} ({ctx.requester.id})")
            outcome = f"Gave **{target_role.name}** to {target.display_name}."
    except discord.Forbidden:
        return await _refuse(
            ctx, action, args,
            perm.refuse("discord_forbidden", "Discord refused — my role is likely too low to manage that role."),
        )
    except discord.HTTPException as e:
        return await _refuse(ctx, action, args, perm.refuse("discord_error", f"Discord error: {e}"))

    return await _executed(ctx, action, args, decision, outcome)


async def change_nickname(ctx: ToolContext, member: str, new_nickname: str | None) -> str:
    args = {"member": member, "new_nickname": new_nickname}

    cooldown = await _rate_limited(ctx, "change_nickname", args)
    if cooldown:
        return cooldown

    try:
        target = resolve_member(ctx.guild, member) if member else ctx.requester
    except ValueError as e:
        return await _refuse(ctx, "change_nickname", args, perm.refuse("resolve", str(e)))

    rc = ctx.request_context()
    decision = perm.validate_change_nickname(
        rc,
        target_top_position=target.top_role.position,
        target_is_self=(target.id == ctx.requester.id),
    )
    if not decision:
        return await _refuse(ctx, "change_nickname", args, decision)

    try:
        await target.edit(nick=new_nickname, reason=f"AI-mod: {ctx.requester} ({ctx.requester.id})")
    except discord.Forbidden:
        return await _refuse(
            ctx, "change_nickname", args,
            perm.refuse("discord_forbidden", "Discord refused — my role may be too low, or I can't rename the owner."),
        )
    except discord.HTTPException as e:
        return await _refuse(ctx, "change_nickname", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = (
        f"Cleared {target.display_name}'s nickname."
        if not new_nickname
        else f"Set {target}'s nickname to **{new_nickname}**."
    )
    return await _executed(ctx, "change_nickname", args, decision, outcome)


async def create_channel(
    ctx: ToolContext,
    name: str,
    type: str = "text",
    category: str | None = None,
) -> str:
    args = {"name": name, "type": type, "category": category}

    cooldown = await _rate_limited(ctx, "create_channel", args)
    if cooldown:
        return cooldown

    rc = ctx.request_context()
    decision = perm.validate_create_channel(rc)
    if not decision:
        return await _refuse(ctx, "create_channel", args, decision)

    parent = None
    if category:
        try:
            resolved = resolve_channel(ctx.guild, category, ctx.channel)
            if isinstance(resolved, discord.CategoryChannel):
                parent = resolved
        except ValueError as e:
            return await _refuse(ctx, "create_channel", args, perm.refuse("category", str(e)))

    try:
        reason = f"AI-mod: {ctx.requester} ({ctx.requester.id})"
        kind = type.lower()
        if kind in ("category", "cat"):
            ch = await ctx.guild.create_category(name, reason=reason)
        elif kind in ("voice", "vc"):
            ch = await ctx.guild.create_voice_channel(name, category=parent, reason=reason)
        else:
            ch = await ctx.guild.create_text_channel(name, category=parent, reason=reason)
    except discord.Forbidden:
        return await _refuse(
            ctx, "create_channel", args,
            perm.refuse("discord_forbidden", "Discord refused — I need the Manage Channels permission."),
        )
    except discord.HTTPException as e:
        return await _refuse(ctx, "create_channel", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Created {type} channel **{ch.name}** (id={ch.id})."
    return await _executed(ctx, "create_channel", args, decision, outcome)


async def set_channel_overwrite(
    ctx: ToolContext,
    channel: str,
    role_or_member: str,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
) -> str:
    args = {"channel": channel, "role_or_member": role_or_member, "allow": allow, "deny": deny}

    cooldown = await _rate_limited(ctx, "set_channel_overwrite", args)
    if cooldown:
        return cooldown

    try:
        target_channel = resolve_channel(ctx.guild, channel, ctx.channel)
    except ValueError as e:
        return await _refuse(ctx, "set_channel_overwrite", args, perm.refuse("resolve", str(e)))

    # Resolve the overwrite target: a role (incl. @everyone) or a member.
    target: discord.Role | discord.Member
    target_is_role = True
    target_top_position: int | None = None
    target_is_self = False
    q = str(role_or_member).strip().lower()
    try:
        if q in ("@everyone", "everyone"):
            target = ctx.guild.default_role
            target_top_position = target.position
        else:
            try:
                target = resolve_role(ctx.guild, role_or_member)
                target_top_position = target.position
            except ValueError:
                target = resolve_member(ctx.guild, role_or_member)
                target_is_role = False
                target_top_position = target.top_role.position
                target_is_self = target.id == ctx.requester.id
    except ValueError as e:
        return await _refuse(ctx, "set_channel_overwrite", args, perm.refuse("resolve", str(e)))

    try:
        allow_perms = build_permissions(allow)
        deny_perms = build_permissions(deny)
    except ValueError as e:
        return await _refuse(ctx, "set_channel_overwrite", args, perm.refuse("permissions", str(e)))

    rc = ctx.request_context()
    decision = perm.validate_set_channel_overwrite(
        rc,
        allow_perms=allow_perms,
        target_top_position=target_top_position,
        target_is_role=target_is_role,
        target_is_self=target_is_self,
    )
    if not decision:
        return await _refuse(ctx, "set_channel_overwrite", args, decision)

    overwrite = target_channel.overwrites_for(target)
    for name, value in allow_perms:
        if value:
            setattr(overwrite, name, True)
    for name, value in deny_perms:
        if value:
            setattr(overwrite, name, False)

    try:
        await target_channel.set_permissions(
            target, overwrite=overwrite, reason=f"AI-mod: {ctx.requester} ({ctx.requester.id})"
        )
    except discord.Forbidden:
        return await _refuse(
            ctx, "set_channel_overwrite", args,
            perm.refuse("discord_forbidden", "Discord refused — I need Manage Channels and a high enough role."),
        )
    except discord.HTTPException as e:
        return await _refuse(ctx, "set_channel_overwrite", args, perm.refuse("discord_error", f"Discord error: {e}"))

    tname = target.name if hasattr(target, "name") else str(target)
    outcome = (
        f"Set overwrite on **#{target_channel.name}** for {tname}: "
        f"allow={allow or []}, deny={deny or []}."
    )
    return await _executed(ctx, "set_channel_overwrite", args, decision, outcome)


# --------------------------------------------------------------------------- #
# Punitive tools -- permission-validated AND gated behind typed CONFIRM
# --------------------------------------------------------------------------- #


async def _punitive(
    ctx: ToolContext, action: str, member: str, required: str, reason: str | None,
    do: Callable[[discord.Member], Awaitable[None]], verb: str, extra: dict | None = None,
) -> str:
    args = {"member": member, "reason": reason, **(extra or {})}

    if not ctx.config.enable_punitive:
        return await _refuse(ctx, action, args, perm.refuse("disabled", "Punitive actions are disabled on this bot."))

    cooldown = await _rate_limited(ctx, action, args)
    if cooldown:
        return cooldown

    try:
        target = resolve_member(ctx.guild, member)
    except ValueError as e:
        return await _refuse(ctx, action, args, perm.refuse("resolve", str(e)))

    rc = ctx.request_context()
    decision = perm.validate_punitive(
        rc, required=required,
        target_top_position=target.top_role.position,
        target_is_self=(target.id == ctx.requester.id),
    )
    if not decision:
        return await _refuse(ctx, action, args, decision)

    # Irreversible -> require an explicit typed confirmation naming the exact target.
    token = f"CONFIRM {target.id}"
    ok = await ctx.confirm(
        f"⚠️ You asked me to **{verb} {target}** (id={target.id}). This is irreversible. "
        f"To go ahead, reply with exactly:\n`{token}`",
        required=token,
    )
    if not ok:
        return await _refuse(ctx, action, args, perm.refuse("confirmation", f"{verb.capitalize()} cancelled — no valid confirmation."))

    try:
        await do(target)
    except discord.Forbidden:
        return await _refuse(ctx, action, args, perm.refuse("discord_forbidden", "Discord refused — my role is too low to do that."))
    except discord.HTTPException as e:
        return await _refuse(ctx, action, args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"{verb.capitalize()}ed {target} (id={target.id})." + (f" Reason: {reason}" if reason else "")
    return await _executed(ctx, action, args, decision, outcome)


async def kick_member(ctx: ToolContext, member: str, reason: str | None = None) -> str:
    return await _punitive(
        ctx, "kick_member", member, "kick_members", reason,
        lambda t: t.kick(reason=f"AI-mod: {ctx.requester} — {reason or ''}"), "kick",
    )


async def ban_member(ctx: ToolContext, member: str, reason: str | None = None) -> str:
    return await _punitive(
        ctx, "ban_member", member, "ban_members", reason,
        lambda t: t.ban(reason=f"AI-mod: {ctx.requester} — {reason or ''}", delete_message_days=0), "ban",
    )


async def timeout_member(ctx: ToolContext, member: str, minutes: int = 10, reason: str | None = None) -> str:
    duration = timedelta(minutes=max(1, min(int(minutes), 40320)))  # Discord caps at 28 days.
    return await _punitive(
        ctx, "timeout_member", member, "moderate_members", reason,
        lambda t: t.timeout(duration, reason=f"AI-mod: {ctx.requester} — {reason or ''}"),
        "timeout", extra={"minutes": minutes},
    )


# --------------------------------------------------------------------------- #
# Mini-admin tools -- more of what a real Discord admin can do. Every one is
# clamped to the requester's OWN permission (validate_* in permissions.py) before
# it touches Discord; irreversible ones additionally require a typed CONFIRM.
# --------------------------------------------------------------------------- #


def _mod_reason(ctx: ToolContext) -> str:
    return f"AI-mod: {ctx.requester} ({ctx.requester.id})"


async def _typed_confirm(
    ctx: ToolContext, description: str, target_id: int, *, warning: str = "This can't be undone."
) -> bool:
    """Demand an exact ``CONFIRM <id>`` reply before an irreversible/high-impact act."""
    token = f"CONFIRM {target_id}"
    return await ctx.confirm(
        f"⚠️ You asked me to **{description}** (id={target_id}). {warning} "
        f"To go ahead, reply with exactly:\n`{token}`",
        required=token,
    )


async def _target_message(
    ctx: ToolContext, message: str | None, channel: str | None
) -> tuple[discord.Message, discord.abc.GuildChannel | discord.Thread]:
    """Resolve the message to act on. Explicit ``message`` id (in ``channel`` or the
    current one) wins; otherwise fall back to the message the requester replied to.
    """
    ch = resolve_channel(ctx.guild, channel, ctx.channel) if channel else ctx.channel
    if not hasattr(ch, "fetch_message"):
        raise ValueError(f"#{getattr(ch, 'name', '?')} isn't a text channel I can read messages in.")
    if message:
        return await resolve_message(ch, message), ch
    if ctx.replied_to is not None and hasattr(ctx.channel, "fetch_message"):
        return await resolve_message(ctx.channel, str(ctx.replied_to.message_id)), ctx.channel
    raise ValueError("Which message? Reply to it, or give me its numeric ID.")


# -- Messages ------------------------------------------------------------- #


async def delete_message(ctx: ToolContext, message: str | None = None, channel: str | None = None) -> str:
    args = {"message": message, "channel": channel}
    cooldown = await _rate_limited(ctx, "delete_message", args)
    if cooldown:
        return cooldown
    try:
        msg, ch = await _target_message(ctx, message, channel)
    except ValueError as e:
        return await _refuse(ctx, "delete_message", args, perm.refuse("resolve", str(e)))

    decision = perm.validate_capability(ctx.request_context(scope_channel=ch), "manage_messages")
    if not decision:
        return await _refuse(ctx, "delete_message", args, decision)

    try:
        await msg.delete()  # note: Message.delete takes no reason=
    except discord.Forbidden:
        return await _refuse(ctx, "delete_message", args, perm.refuse("discord_forbidden", "Discord refused — I need Manage Messages here."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "delete_message", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Deleted a message by {msg.author.display_name} in #{getattr(ch, 'name', '?')}."
    return await _executed(ctx, "delete_message", args, decision, outcome)


async def purge_messages(
    ctx: ToolContext,
    count: int,
    channel: str | None = None,
    from_member: str | None = None,
    contains: str | None = None,
) -> str:
    args = {"count": count, "channel": channel, "from_member": from_member, "contains": contains}
    cooldown = await _rate_limited(ctx, "purge_messages", args)
    if cooldown:
        return cooldown

    try:
        target_channel = resolve_channel(ctx.guild, channel, ctx.channel) if channel else ctx.channel
    except ValueError as e:
        return await _refuse(ctx, "purge_messages", args, perm.refuse("resolve", str(e)))
    if not hasattr(target_channel, "purge"):
        return await _refuse(ctx, "purge_messages", args, perm.refuse("resolve", f"#{getattr(target_channel, 'name', '?')} isn't a channel I can purge."))

    try:
        n = int(count)
    except (TypeError, ValueError):
        return await _refuse(ctx, "purge_messages", args, perm.refuse("count", "Count must be a number."))
    if n < 1:
        return await _refuse(ctx, "purge_messages", args, perm.refuse("count", "Count must be at least 1."))
    n = min(n, 100)  # hard cap

    decision = perm.validate_capability(ctx.request_context(scope_channel=target_channel), "manage_messages")
    if not decision:
        return await _refuse(ctx, "purge_messages", args, decision)

    member_obj = None
    if from_member:
        try:
            member_obj = resolve_member(ctx.guild, from_member)
        except ValueError as e:
            return await _refuse(ctx, "purge_messages", args, perm.refuse("resolve", str(e)))
    needle = str(contains).lower() if contains else None

    def _check(m: discord.Message) -> bool:
        if member_obj is not None and m.author.id != member_obj.id:
            return False
        if needle is not None and needle not in (m.content or "").lower():
            return False
        return True

    filt = (f" from {member_obj.display_name}" if member_obj else "") + (f' containing "{contains}"' if contains else "")
    ok = await ctx.confirm(
        f"About to delete up to **{n}** message(s) in #{getattr(target_channel, 'name', '?')}{filt}.\n\n"
        f"Reply `yes` to proceed."
    )
    if not ok:
        return await _refuse(ctx, "purge_messages", args, perm.refuse("confirmation", "Purge cancelled."))

    try:
        deleted = await target_channel.purge(limit=n, check=_check, reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "purge_messages", args, perm.refuse("discord_forbidden", "Discord refused — I need Manage Messages and Read Message History here."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "purge_messages", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Purged {len(deleted)} message(s) from #{getattr(target_channel, 'name', '?')}."
    return await _executed(ctx, "purge_messages", args, decision, outcome)


async def _set_pin(ctx: ToolContext, message: str | None, channel: str | None, *, pin: bool) -> str:
    action = "pin_message" if pin else "unpin_message"
    args = {"message": message, "channel": channel}
    cooldown = await _rate_limited(ctx, action, args)
    if cooldown:
        return cooldown
    try:
        msg, ch = await _target_message(ctx, message, channel)
    except ValueError as e:
        return await _refuse(ctx, action, args, perm.refuse("resolve", str(e)))

    decision = perm.validate_capability(ctx.request_context(scope_channel=ch), "manage_messages")
    if not decision:
        return await _refuse(ctx, action, args, decision)

    try:
        if pin:
            await msg.pin(reason=_mod_reason(ctx))
        else:
            await msg.unpin(reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, action, args, perm.refuse("discord_forbidden", "Discord refused — I need Manage Messages here."))
    except discord.HTTPException as e:
        return await _refuse(ctx, action, args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"{'Pinned' if pin else 'Unpinned'} a message by {msg.author.display_name} in #{getattr(ch, 'name', '?')}."
    return await _executed(ctx, action, args, decision, outcome)


async def pin_message(ctx: ToolContext, message: str | None = None, channel: str | None = None) -> str:
    return await _set_pin(ctx, message, channel, pin=True)


async def unpin_message(ctx: ToolContext, message: str | None = None, channel: str | None = None) -> str:
    return await _set_pin(ctx, message, channel, pin=False)


# -- Roles ---------------------------------------------------------------- #


async def delete_role(ctx: ToolContext, role: str) -> str:
    args = {"role": role}
    cooldown = await _rate_limited(ctx, "delete_role", args)
    if cooldown:
        return cooldown
    try:
        target_role = resolve_role(ctx.guild, role)
    except ValueError as e:
        return await _refuse(ctx, "delete_role", args, perm.refuse("resolve", str(e)))
    if target_role.is_default():
        return await _refuse(ctx, "delete_role", args, perm.refuse("protected", "The @everyone role can't be deleted."))
    if target_role.managed:
        return await _refuse(ctx, "delete_role", args, perm.refuse("protected", f"**{target_role.name}** is managed by an integration and can't be deleted manually."))

    decision = perm.validate_delete_role(ctx.request_context(), target_role.position)
    if not decision:
        return await _refuse(ctx, "delete_role", args, decision)

    if not await _typed_confirm(ctx, f"delete the role {target_role.name}", target_role.id):
        return await _refuse(ctx, "delete_role", args, perm.refuse("confirmation", "Deletion cancelled — no valid confirmation."))

    try:
        await target_role.delete(reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "delete_role", args, perm.refuse("discord_forbidden", "Discord refused — my role is likely too low."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "delete_role", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Deleted the role **{target_role.name}** (id={target_role.id})."
    return await _executed(ctx, "delete_role", args, decision, outcome)


async def edit_role(
    ctx: ToolContext,
    role: str,
    name: str | None = None,
    colour: str | None = None,
    hoist: bool | None = None,
    mentionable: bool | None = None,
    permissions: list[str] | None = None,
    below_role: str | None = None,
) -> str:
    args = {
        "role": role, "name": name, "colour": colour, "hoist": hoist,
        "mentionable": mentionable, "permissions": permissions, "below_role": below_role,
    }
    cooldown = await _rate_limited(ctx, "edit_role", args)
    if cooldown:
        return cooldown
    try:
        target_role = resolve_role(ctx.guild, role)
    except ValueError as e:
        return await _refuse(ctx, "edit_role", args, perm.refuse("resolve", str(e)))
    if target_role.is_default():
        return await _refuse(ctx, "edit_role", args, perm.refuse("protected", "The @everyone role can't be edited this way."))

    # Only subset/admin-check the perms actually being SET. If permissions is
    # omitted, we pass an empty set so the check trivially passes (no perm change).
    if permissions is not None:
        try:
            requested_perms = build_permissions(permissions)
        except ValueError as e:
            return await _refuse(ctx, "edit_role", args, perm.refuse("permissions", str(e)))
    else:
        requested_perms = discord.Permissions.none()

    resolved_colour = None
    if colour is not None:
        try:
            resolved_colour = resolve_colour(colour)
        except ColourError as e:
            return await _refuse(ctx, "edit_role", args, perm.refuse("colour", str(e)))

    intended_position = target_role.position
    if below_role:
        try:
            below = resolve_role(ctx.guild, below_role)
            intended_position = max(1, below.position - 1)
        except ValueError as e:
            return await _refuse(ctx, "edit_role", args, perm.refuse("below_role", str(e)))

    # Validate against the higher of current/new position: you can neither edit a
    # role that already outranks you nor move one above yourself.
    check_position = max(target_role.position, intended_position)
    decision = perm.validate_edit_role(ctx.request_context(), requested_perms, check_position)
    if not decision:
        return await _refuse(ctx, "edit_role", args, decision)

    opts: dict[str, Any] = {}
    if name is not None:
        opts["name"] = name
    if resolved_colour is not None:
        opts["colour"] = resolved_colour
    if hoist is not None:
        opts["hoist"] = bool(hoist)
    if mentionable is not None:
        opts["mentionable"] = bool(mentionable)
    if permissions is not None:
        opts["permissions"] = requested_perms
    if below_role:
        opts["position"] = max(1, min(intended_position, ctx.bot_member.top_role.position - 1))
    if not opts:
        return await _refuse(ctx, "edit_role", args, perm.refuse("noop", "Tell me what to change (name, colour, hoist, mentionable, permissions, or position)."))

    try:
        await target_role.edit(reason=_mod_reason(ctx), **opts)
    except discord.Forbidden:
        return await _refuse(ctx, "edit_role", args, perm.refuse("discord_forbidden", "Discord refused — my role is likely too low."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "edit_role", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Updated the role **{target_role.name}**: changed {', '.join(sorted(opts))}."
    return await _executed(ctx, "edit_role", args, decision, outcome)


# -- Channels ------------------------------------------------------------- #


async def delete_channel(ctx: ToolContext, channel: str) -> str:
    args = {"channel": channel}
    cooldown = await _rate_limited(ctx, "delete_channel", args)
    if cooldown:
        return cooldown
    try:
        target_channel = resolve_channel(ctx.guild, channel, ctx.channel)
    except ValueError as e:
        return await _refuse(ctx, "delete_channel", args, perm.refuse("resolve", str(e)))

    decision = perm.validate_capability(ctx.request_context(scope_channel=target_channel), "manage_channels")
    if not decision:
        return await _refuse(ctx, "delete_channel", args, decision)

    label = f"#{target_channel.name}"
    if not await _typed_confirm(ctx, f"delete {label}", target_channel.id):
        return await _refuse(ctx, "delete_channel", args, perm.refuse("confirmation", "Deletion cancelled — no valid confirmation."))

    try:
        await target_channel.delete(reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "delete_channel", args, perm.refuse("discord_forbidden", "Discord refused — I need the Manage Channels permission."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "delete_channel", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Deleted channel {label} (id={target_channel.id})."
    return await _executed(ctx, "delete_channel", args, decision, outcome)


async def edit_channel(
    ctx: ToolContext,
    channel: str,
    name: str | None = None,
    topic: str | None = None,
    slowmode: int | None = None,
    nsfw: bool | None = None,
) -> str:
    args = {"channel": channel, "name": name, "topic": topic, "slowmode": slowmode, "nsfw": nsfw}
    cooldown = await _rate_limited(ctx, "edit_channel", args)
    if cooldown:
        return cooldown
    try:
        target_channel = resolve_channel(ctx.guild, channel, ctx.channel)
    except ValueError as e:
        return await _refuse(ctx, "edit_channel", args, perm.refuse("resolve", str(e)))

    decision = perm.validate_capability(ctx.request_context(scope_channel=target_channel), "manage_channels")
    if not decision:
        return await _refuse(ctx, "edit_channel", args, decision)

    opts: dict[str, Any] = {}
    if name is not None:
        opts["name"] = name
    if topic is not None:
        opts["topic"] = topic
    if slowmode is not None:
        opts["slowmode_delay"] = max(0, min(int(slowmode), 21600))
    if nsfw is not None:
        opts["nsfw"] = bool(nsfw)
    if not opts:
        return await _refuse(ctx, "edit_channel", args, perm.refuse("noop", "Tell me what to change (name, topic, slowmode, or nsfw)."))

    try:
        await target_channel.edit(reason=_mod_reason(ctx), **opts)
    except discord.Forbidden:
        return await _refuse(ctx, "edit_channel", args, perm.refuse("discord_forbidden", "Discord refused — I need the Manage Channels permission."))
    except (discord.HTTPException, TypeError) as e:
        return await _refuse(ctx, "edit_channel", args, perm.refuse("discord_error", f"That didn't work on this channel type: {e}"))

    outcome = f"Updated #{target_channel.name}: changed {', '.join(sorted(opts))}."
    return await _executed(ctx, "edit_channel", args, decision, outcome)


async def set_slowmode(ctx: ToolContext, channel: str, seconds: int) -> str:
    args = {"channel": channel, "seconds": seconds}
    cooldown = await _rate_limited(ctx, "set_slowmode", args)
    if cooldown:
        return cooldown
    try:
        target_channel = resolve_channel(ctx.guild, channel, ctx.channel)
    except ValueError as e:
        return await _refuse(ctx, "set_slowmode", args, perm.refuse("resolve", str(e)))

    decision = perm.validate_capability(ctx.request_context(scope_channel=target_channel), "manage_channels")
    if not decision:
        return await _refuse(ctx, "set_slowmode", args, decision)

    sec = max(0, min(int(seconds), 21600))
    try:
        await target_channel.edit(slowmode_delay=sec, reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "set_slowmode", args, perm.refuse("discord_forbidden", "Discord refused — I need the Manage Channels permission."))
    except (discord.HTTPException, TypeError) as e:
        return await _refuse(ctx, "set_slowmode", args, perm.refuse("discord_error", f"That channel doesn't support slow mode: {e}"))

    outcome = f"Set slow mode in #{target_channel.name} to {sec}s." if sec else f"Turned off slow mode in #{target_channel.name}."
    return await _executed(ctx, "set_slowmode", args, decision, outcome)


async def create_invite(
    ctx: ToolContext,
    channel: str | None = None,
    max_age_seconds: int | None = None,
    max_uses: int | None = None,
) -> str:
    args = {"channel": channel, "max_age_seconds": max_age_seconds, "max_uses": max_uses}
    cooldown = await _rate_limited(ctx, "create_invite", args)
    if cooldown:
        return cooldown
    try:
        target_channel = resolve_channel(ctx.guild, channel, ctx.channel) if channel else ctx.channel
    except ValueError as e:
        return await _refuse(ctx, "create_invite", args, perm.refuse("resolve", str(e)))
    if not hasattr(target_channel, "create_invite"):
        return await _refuse(ctx, "create_invite", args, perm.refuse("resolve", f"#{getattr(target_channel, 'name', '?')} can't have invites."))

    decision = perm.validate_capability(ctx.request_context(scope_channel=target_channel), "create_instant_invite")
    if not decision:
        return await _refuse(ctx, "create_invite", args, decision)

    ma = max(0, int(max_age_seconds)) if max_age_seconds is not None else 86400  # default 1 day
    mu = max(0, int(max_uses)) if max_uses is not None else 0
    try:
        invite = await target_channel.create_invite(max_age=ma, max_uses=mu, reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "create_invite", args, perm.refuse("discord_forbidden", "Discord refused — I need the Create Invite permission here."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "create_invite", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Invite for #{target_channel.name}: {invite.url}"
    return await _executed(ctx, "create_invite", args, decision, outcome)


# -- Voice / member moderation ------------------------------------------- #


async def move_member(ctx: ToolContext, member: str, channel: str) -> str:
    args = {"member": member, "channel": channel}
    cooldown = await _rate_limited(ctx, "move_member", args)
    if cooldown:
        return cooldown
    try:
        target = resolve_member(ctx.guild, member)
        dest = resolve_channel(ctx.guild, channel, ctx.channel)
    except ValueError as e:
        return await _refuse(ctx, "move_member", args, perm.refuse("resolve", str(e)))
    if not isinstance(dest, (discord.VoiceChannel, discord.StageChannel)):
        return await _refuse(ctx, "move_member", args, perm.refuse("resolve", f"#{getattr(dest, 'name', '?')} isn't a voice channel."))

    decision = perm.validate_member_action(
        ctx.request_context(), "move_members", target.top_role.position, target.id == ctx.requester.id
    )
    if not decision:
        return await _refuse(ctx, "move_member", args, decision)
    if target.voice is None:
        return await _refuse(ctx, "move_member", args, perm.refuse("not_in_voice", f"{target.display_name} isn't in a voice channel right now."))

    try:
        await target.move_to(dest, reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "move_member", args, perm.refuse("discord_forbidden", "Discord refused — I need the Move Members permission."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "move_member", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Moved {target.display_name} to 🔊 {dest.name}."
    return await _executed(ctx, "move_member", args, decision, outcome)


async def _set_voice_state(ctx: ToolContext, member: str, *, kind: str, on: bool) -> str:
    past = {"mute": "muted", "deafen": "deafened"}[kind]
    label = past if on else "un" + past
    action = f"server_{'' if on else 'un'}{kind}"
    required = "mute_members" if kind == "mute" else "deafen_members"
    args = {"member": member}
    cooldown = await _rate_limited(ctx, action, args)
    if cooldown:
        return cooldown
    try:
        target = resolve_member(ctx.guild, member)
    except ValueError as e:
        return await _refuse(ctx, action, args, perm.refuse("resolve", str(e)))

    decision = perm.validate_member_action(
        ctx.request_context(), required, target.top_role.position, target.id == ctx.requester.id
    )
    if not decision:
        return await _refuse(ctx, action, args, decision)
    if target.voice is None:
        return await _refuse(ctx, action, args, perm.refuse("not_in_voice", f"{target.display_name} isn't connected to voice; server-{kind} only applies to someone in a voice channel."))

    try:
        await target.edit(reason=_mod_reason(ctx), **{kind: on})
    except discord.Forbidden:
        return await _refuse(ctx, action, args, perm.refuse("discord_forbidden", f"Discord refused — I need the {required.replace('_', ' ')} permission."))
    except discord.HTTPException as e:
        return await _refuse(ctx, action, args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Server-{label} {target.display_name}."
    return await _executed(ctx, action, args, decision, outcome)


async def server_mute(ctx: ToolContext, member: str) -> str:
    return await _set_voice_state(ctx, member, kind="mute", on=True)


async def server_unmute(ctx: ToolContext, member: str) -> str:
    return await _set_voice_state(ctx, member, kind="mute", on=False)


async def server_deafen(ctx: ToolContext, member: str) -> str:
    return await _set_voice_state(ctx, member, kind="deafen", on=True)


async def server_undeafen(ctx: ToolContext, member: str) -> str:
    return await _set_voice_state(ctx, member, kind="deafen", on=False)


async def untimeout_member(ctx: ToolContext, member: str) -> str:
    args = {"member": member}
    cooldown = await _rate_limited(ctx, "untimeout_member", args)
    if cooldown:
        return cooldown
    try:
        target = resolve_member(ctx.guild, member)
    except ValueError as e:
        return await _refuse(ctx, "untimeout_member", args, perm.refuse("resolve", str(e)))

    decision = perm.validate_member_action(
        ctx.request_context(), "moderate_members", target.top_role.position, target.id == ctx.requester.id
    )
    if not decision:
        return await _refuse(ctx, "untimeout_member", args, decision)

    try:
        await target.timeout(None, reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "untimeout_member", args, perm.refuse("discord_forbidden", "Discord refused — I need the Moderate Members permission."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "untimeout_member", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Cleared the timeout on {target.display_name}."
    return await _executed(ctx, "untimeout_member", args, decision, outcome)


# -- Bans ----------------------------------------------------------------- #


async def unban_member(ctx: ToolContext, user: str) -> str:
    args = {"user": user}
    cooldown = await _rate_limited(ctx, "unban_member", args)
    if cooldown:
        return cooldown

    decision = perm.validate_capability(ctx.request_context(), "ban_members")
    if not decision:
        return await _refuse(ctx, "unban_member", args, decision)
    try:
        obj = resolve_banned_user(ctx.guild, user)
    except ValueError as e:
        return await _refuse(ctx, "unban_member", args, perm.refuse("resolve", str(e)))

    try:
        await ctx.guild.unban(obj, reason=_mod_reason(ctx))
    except discord.NotFound:
        return await _refuse(ctx, "unban_member", args, perm.refuse("not_banned", f"User id={obj.id} isn't banned (or doesn't exist)."))
    except discord.Forbidden:
        return await _refuse(ctx, "unban_member", args, perm.refuse("discord_forbidden", "Discord refused — I need the Ban Members permission."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "unban_member", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Unbanned user id={obj.id}."
    return await _executed(ctx, "unban_member", args, decision, outcome)


async def list_bans(ctx: ToolContext, limit: int | None = None) -> str:
    args = {"limit": limit}
    decision = perm.validate_capability(ctx.request_context(), "ban_members")
    if not decision:
        return await _refuse(ctx, "list_bans", args, decision)

    lim = max(1, min(int(limit or 25), 100))
    try:
        entries = [e async for e in ctx.guild.bans(limit=lim)]
    except discord.Forbidden:
        return "Discord refused — I need the Ban Members permission to read the ban list."
    except discord.HTTPException as e:
        return f"Discord error: {e}"

    if not entries:
        return "No users are banned in this server."
    lines = [f"- {e.user} (id={e.user.id})" + (f" — {e.reason}" if e.reason else "") for e in entries]
    return f"Banned users (showing {len(entries)}):\n" + "\n".join(lines)


# -- Guild / expressions -------------------------------------------------- #


async def edit_guild(ctx: ToolContext, name: str | None = None) -> str:
    args = {"name": name}
    cooldown = await _rate_limited(ctx, "edit_guild", args)
    if cooldown:
        return cooldown

    decision = perm.validate_capability(ctx.request_context(), "manage_guild")
    if not decision:
        return await _refuse(ctx, "edit_guild", args, decision)
    if not name:
        return await _refuse(ctx, "edit_guild", args, perm.refuse("noop", "Tell me the new server name."))

    if not await _typed_confirm(
        ctx, f'rename this server to "{name}"', ctx.guild.id, warning="This changes the whole server."
    ):
        return await _refuse(ctx, "edit_guild", args, perm.refuse("confirmation", "Server edit cancelled — no valid confirmation."))

    try:
        await ctx.guild.edit(name=name, reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "edit_guild", args, perm.refuse("discord_forbidden", "Discord refused — I need the Manage Server permission."))
    except discord.HTTPException as e:
        return await _refuse(ctx, "edit_guild", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Renamed the server to **{name}**."
    return await _executed(ctx, "edit_guild", args, decision, outcome)


async def read_audit_log(
    ctx: ToolContext, limit: int | None = None, member: str | None = None, action: str | None = None
) -> str:
    args = {"limit": limit, "member": member, "action": action}
    decision = perm.validate_capability(ctx.request_context(), "view_audit_log")
    if not decision:
        return await _refuse(ctx, "read_audit_log", args, decision)

    lim = max(1, min(int(limit or 20), 100))
    kwargs: dict[str, Any] = {"limit": lim}
    if member:
        try:
            kwargs["user"] = resolve_member(ctx.guild, member)
        except ValueError as e:
            return await _refuse(ctx, "read_audit_log", args, perm.refuse("resolve", str(e)))
    if action:
        act = getattr(discord.AuditLogAction, str(action).strip().lower(), None)
        if act is not None:
            kwargs["action"] = act

    try:
        entries = [e async for e in ctx.guild.audit_logs(**kwargs)]
    except discord.Forbidden:
        return "Discord refused — I need the View Audit Log permission."
    except discord.HTTPException as e:
        return f"Discord error: {e}"

    if not entries:
        return "No matching audit-log entries."
    lines = []
    for e in entries:
        who = getattr(e.user, "display_name", str(e.user))
        tgt = f" → {e.target}" if e.target is not None else ""
        reason = f" ({e.reason})" if e.reason else ""
        lines.append(f"- {e.action.name}: by {who}{tgt}{reason}")
    return f"Audit log (showing {len(entries)}):\n" + "\n".join(lines)


async def delete_emoji(ctx: ToolContext, emoji: str) -> str:
    args = {"emoji": emoji}
    cooldown = await _rate_limited(ctx, "delete_emoji", args)
    if cooldown:
        return cooldown

    decision = perm.validate_capability(ctx.request_context(), "manage_expressions")
    if not decision:
        return await _refuse(ctx, "delete_emoji", args, decision)
    try:
        e = resolve_emoji(ctx.guild, emoji)
    except ValueError as ex:
        return await _refuse(ctx, "delete_emoji", args, perm.refuse("resolve", str(ex)))

    try:
        await e.delete(reason=_mod_reason(ctx))
    except discord.Forbidden:
        return await _refuse(ctx, "delete_emoji", args, perm.refuse("discord_forbidden", "Discord refused — I need the Manage Expressions permission."))
    except discord.HTTPException as ex:
        return await _refuse(ctx, "delete_emoji", args, perm.refuse("discord_error", f"Discord error: {ex}"))

    outcome = f"Deleted emoji :{e.name}: (id={e.id})."
    return await _executed(ctx, "delete_emoji", args, decision, outcome)
