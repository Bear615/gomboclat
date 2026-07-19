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

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable

import discord

from . import permissions as perm
from .audit import AuditLogger
from .colours import ColourError, resolve_colour
from .config import Config, GuildSettings
from .ratelimit import RateLimiter

# Type of the confirmation callback injected by the message layer.
ConfirmFn = Callable[..., Awaitable[bool]]


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

    def request_context(self) -> perm.RequestContext:
        return perm.RequestContext(
            requester_id=self.requester.id,
            guild_owner_id=self.guild.owner_id,
            requester_perms=self.requester.guild_permissions,
            requester_top_position=self.requester.top_role.position,
            bot_top_position=self.bot_member.top_role.position,
        )


# --------------------------------------------------------------------------- #
# Resolution helpers (untrusted strings -> real Discord objects)
# --------------------------------------------------------------------------- #


def _as_id(query: str) -> int | None:
    s = str(query).strip().strip("<@!#&>")
    return int(s) if s.isdigit() else None


def match_by_name(
    query: str,
    candidates: list[tuple[Any, list[str]]],
    *,
    kind: str = "match",
    id_phrase: str = "its ID",
) -> Any:
    """Pick a single candidate whose name matches ``query``.

    ``candidates`` is a list of ``(object, names)`` where ``names`` are the
    strings the object can be addressed by (each will be lower-cased and
    stripped here). Matching is case-insensitive and ignores a leading ``@``/``#``.

    An **exact** match wins. If there's no exact match, a **unique**
    case-insensitive *substring* match is accepted so ``"dave"`` resolves
    ``"Dave the Great"``. Ambiguity in either tier, or no match at all, raises
    ``ValueError`` with a friendly, kind-specific message.

    Pure and Discord-free, so it's directly unit-testable.
    """
    q = str(query).strip().lstrip("@#").lower()
    if not q:
        raise ValueError(f"I couldn't find a {kind} matching {query!r}.")

    def names_of(names: list[str]) -> list[str]:
        return [n.strip().lower() for n in names if n and n.strip()]

    exact = [obj for obj, names in candidates if q in names_of(names)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"{query!r} is ambiguous ({len(exact)} matches); use {id_phrase}.")

    partial = [obj for obj, names in candidates if any(q in n for n in names_of(names))]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise ValueError(
            f"{query!r} matches {len(partial)} {kind}s — please be more specific or use {id_phrase}."
        )

    raise ValueError(f"I couldn't find a {kind} matching {query!r}.")


def resolve_member(guild: discord.Guild, query: str) -> discord.Member:
    mid = _as_id(query)
    if mid is not None:
        m = guild.get_member(mid)
        if m:
            return m
    candidates = [
        (
            m,
            [m.name, m.nick or "", m.display_name, f"{m.name}#{m.discriminator}"],
        )
        for m in guild.members
    ]
    return match_by_name(query, candidates, kind="member", id_phrase="their ID")


def resolve_role(guild: discord.Guild, query: str) -> discord.Role:
    rid = _as_id(query)
    if rid is not None:
        r = guild.get_role(rid)
        if r:
            return r
    candidates = [(r, [r.name]) for r in guild.roles]
    return match_by_name(query, candidates, kind="role")


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
    candidates = [(c, [c.name]) for c in guild.channels]
    return match_by_name(query, candidates, kind="channel")


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
# Messaging -- the bot's ONLY way to say anything to users
# --------------------------------------------------------------------------- #

# Discord hard-caps a single message at 2000 characters; leave headroom.
_MESSAGE_CHUNK = 1900


def _chunk_message(text: str) -> list[str]:
    return [text[i : i + _MESSAGE_CHUNK] for i in range(0, len(text), _MESSAGE_CHUNK)]


async def send_message(ctx: ToolContext, content: str, channel: str | None = None) -> str:
    """Post a free-text message into a channel (defaults to the current one).

    Under the internal-by-default answer scheme this is the model's *only* channel
    of communication with users: its own text output is never shown, so anything
    it wants a human to read it must send here. The model may target any channel,
    but the requester must be able to view + send there (owner exempt) -- the guard
    lives in ``permissions.validate_send_message``, not in the prompt.
    """
    args = {"channel": channel, "content": content}

    cooldown = await _rate_limited(ctx, "send_message", args)
    if cooldown:
        return cooldown

    if content is None or not str(content).strip():
        return await _refuse(
            ctx, "send_message", args, perm.refuse("empty", "Nothing to send — the message was empty.")
        )

    try:
        target_channel = resolve_channel(ctx.guild, channel, ctx.channel)
    except ValueError as e:
        return await _refuse(ctx, "send_message", args, perm.refuse("resolve", str(e)))

    if not isinstance(target_channel, discord.abc.Messageable):
        return await _refuse(
            ctx, "send_message", args,
            perm.refuse("channel_type", f"**#{getattr(target_channel, 'name', target_channel)}** isn't a channel I can post text in."),
        )

    # The requester's *effective* permissions in this specific channel (respects
    # per-channel overwrites), reduced to primitives for the pure validator.
    req_perms = target_channel.permissions_for(ctx.requester)
    # Discord gates talking *in a thread* on send_messages_in_threads; on a thread
    # `send_messages` merely mirrors the parent channel. Pick the flag that actually
    # governs the target so thread-only setups aren't wrongly refused (or allowed).
    requester_can_send = (
        req_perms.send_messages_in_threads
        if isinstance(target_channel, discord.Thread)
        else req_perms.send_messages
    )
    decision = perm.validate_send_message(
        ctx.request_context(),
        requester_can_view=req_perms.view_channel,
        requester_can_send=requester_can_send,
    )
    if not decision:
        return await _refuse(ctx, "send_message", args, decision)

    text = str(content)
    try:
        for chunk in _chunk_message(text):
            await target_channel.send(chunk)
    except discord.Forbidden:
        return await _refuse(
            ctx, "send_message", args,
            perm.refuse("discord_forbidden", "Discord refused — I don't have permission to post in that channel."),
        )
    except discord.HTTPException as e:
        return await _refuse(ctx, "send_message", args, perm.refuse("discord_error", f"Discord error: {e}"))

    outcome = f"Sent a message to **#{getattr(target_channel, 'name', target_channel)}** ({len(text)} chars)."
    return await _executed(ctx, "send_message", args, decision, outcome)


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
