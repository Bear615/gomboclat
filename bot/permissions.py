"""The permission validation layer -- the real security boundary.

THE LLM IS NOT A SECURITY BOUNDARY. It parses intent into typed action requests;
*this module* decides whether an action is allowed, using the real permissions of
the real member who sent the message.

Everything here is pure: it operates on plain data and ``discord.Permissions``
value objects (which perform no I/O), so it is fast and thoroughly unit-testable.
No network calls, no Discord API calls, no reads of global state. A caller in
``tools.py`` gathers the live facts (the requester's effective permissions, role
positions, the target's top role, the bot's top role) into a ``RequestContext``
and asks the functions here for a verdict *before* touching Discord.

A prompt-injected or jailbroken model can, at worst, make the bot *attempt* a
disallowed action; the checks below reject it. Privilege escalation must be
impossible here regardless of what the message text claims.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord

# --------------------------------------------------------------------------- #
# Verdict type
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Decision:
    """Outcome of a validation check.

    ``ok`` is the only thing execution keys on. ``reason`` is a short, friendly,
    user-facing explanation of *why* something was refused (or a note on why it
    passed), and ``check`` names the rule that produced the verdict for auditing.
    """

    ok: bool
    reason: str = ""
    check: str = ""

    def __bool__(self) -> bool:  # allows `if decision:`
        return self.ok


def allow(check: str, reason: str = "") -> Decision:
    return Decision(True, reason, check)


def refuse(check: str, reason: str) -> Decision:
    return Decision(False, reason, check)


# --------------------------------------------------------------------------- #
# Request context -- plain data, no Discord objects required to construct it
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RequestContext:
    """Everything the validator needs about *who* is asking and the environment.

    Built from live Discord objects by the caller, but intentionally holds only
    primitives + ``discord.Permissions`` value objects so tests can construct it
    directly without a running client.
    """

    requester_id: int
    guild_owner_id: int
    requester_perms: discord.Permissions
    requester_top_position: int
    bot_top_position: int

    @property
    def is_owner(self) -> bool:
        """True only for the single true guild owner (``guild.owner_id``).

        NOT true for holders of an "Owner"-named role, and NOT true for members
        with the Administrator permission. Exactly the one real owner.
        """
        return self.requester_id == self.guild_owner_id


# --------------------------------------------------------------------------- #
# Primitive checks
# --------------------------------------------------------------------------- #

# Human-readable names for the permission flags, used to build refusal messages.
def _extra_permission_names(
    requested: discord.Permissions, held: discord.Permissions
) -> list[str]:
    """Names of permissions set in ``requested`` but not in ``held``."""
    leftover = requested.value & ~held.value
    if not leftover:
        return []
    return [name for name, value in requested if value and not getattr(held, name)]


def check_administrator_block(requested: discord.Permissions) -> Decision:
    """HARD block: never create/assign a role carrying Administrator -- for anyone,
    owner included. Administrator bypasses every channel overwrite, so it is
    categorically different from any other permission and is refused separately,
    before (and independent of) the owner bypass.
    """
    if requested.administrator:
        return refuse(
            "administrator_block",
            "I will never create or assign a role with the **Administrator** "
            "permission. That one's off-limits for everyone, no exceptions.",
        )
    return allow("administrator_block")


def check_permission_subset(requested: discord.Permissions, ctx: RequestContext) -> Decision:
    """A role the bot creates/edits on the requester's behalf must carry only
    permissions the requester *themselves* effectively hold. Owner is exempt
    (rule 1) -- but the Administrator block above still applies to owners.
    """
    if ctx.is_owner:
        return allow("permission_subset", "owner bypass")

    extra = _extra_permission_names(requested, ctx.requester_perms)
    if extra:
        pretty = ", ".join(f"`{n}`" for n in extra)
        return refuse(
            "permission_subset",
            f"That would give the role permissions you don't have yourself: "
            f"{pretty}. I can only hand out permissions you already hold.",
        )
    return allow("permission_subset")


# Map an action to the guild permission the requester must personally hold.
def check_requester_capability(ctx: RequestContext, required: str) -> Decision:
    """The requester must hold the permission the action requires
    (e.g. ``manage_roles`` to touch roles). Owner is exempt.
    """
    if ctx.is_owner:
        return allow("requester_capability", "owner bypass")

    if not getattr(ctx.requester_perms, required, False):
        return refuse(
            "requester_capability",
            f"You need the **{required.replace('_', ' ')}** permission to do that, "
            f"and you don't have it here.",
        )
    return allow("requester_capability")


def check_bot_hierarchy(role_position: int, ctx: RequestContext, role_label: str = "that role") -> Decision:
    """The *bot* cannot act on a role at or above its own top role -- a hard law of
    physics that applies even to the owner. Discord enforces this server-side, but
    we check first to give a useful message instead of an opaque 403.
    """
    if role_position >= ctx.bot_top_position:
        return refuse(
            "bot_hierarchy",
            f"My own role isn't high enough to manage {role_label}. Move my bot "
            f"role **above** it in Server Settings → Roles, then try again.",
        )
    return allow("bot_hierarchy")


def check_role_hierarchy(role_position: int, ctx: RequestContext, role_label: str = "that role") -> Decision:
    """Any role created/edited/assigned must sit below BOTH the bot's top role and
    (for non-owners) the requester's top role. A member must never end up with, or
    hand out, a role that outranks them.
    """
    bot_check = check_bot_hierarchy(role_position, ctx, role_label)
    if not bot_check:
        return bot_check

    if ctx.is_owner:
        return allow("role_hierarchy", "owner bypass (bot hierarchy still enforced)")

    if role_position >= ctx.requester_top_position:
        return refuse(
            "role_hierarchy",
            f"{role_label.capitalize()} would sit at or above your own top role. "
            f"You can't create or hand out a role that outranks you.",
        )
    return allow("role_hierarchy")


def check_target(ctx: RequestContext, target_top_position: int, target_is_self: bool) -> Decision:
    """When acting on a *different* member, the requester's top role must be
    strictly higher than the target's. You cannot moderate a peer or a superior.
    Owner is exempt. Acting on yourself always passes this check (capability
    checks still apply separately).
    """
    if target_is_self:
        return allow("target", "acting on self")
    if ctx.is_owner:
        return allow("target", "owner bypass")
    if target_top_position >= ctx.requester_top_position:
        return refuse(
            "target",
            "You can only act on members **below** you in the role hierarchy -- "
            "not a peer or someone ranked above you.",
        )
    return allow("target")


# --------------------------------------------------------------------------- #
# Composed, action-level validators (what tools.py actually calls)
# --------------------------------------------------------------------------- #


def _first_refusal(*decisions: Decision) -> Decision:
    """Return the first failing decision, or the last (passing) one."""
    last = decisions[-1]
    for d in decisions:
        if not d.ok:
            return d
    return last


def validate_create_role(
    ctx: RequestContext,
    requested_perms: discord.Permissions,
    new_role_position: int,
) -> Decision:
    """Validate creating (or editing) a role with ``requested_perms`` that will sit
    at ``new_role_position``.
    """
    return _first_refusal(
        check_administrator_block(requested_perms),
        check_requester_capability(ctx, "manage_roles"),
        check_permission_subset(requested_perms, ctx),
        check_role_hierarchy(new_role_position, ctx, "that role"),
    )


def validate_assign_role(
    ctx: RequestContext,
    role_perms: discord.Permissions,
    role_position: int,
    target_top_position: int,
    target_is_self: bool,
    role_label: str = "that role",
) -> Decision:
    """Validate assigning/removing an existing role to/from a member.

    The role's own permissions are re-checked (admin block + subset) so you can't
    launder a too-powerful pre-existing role onto someone via the bot.
    """
    return _first_refusal(
        check_administrator_block(role_perms),
        check_requester_capability(ctx, "manage_roles"),
        check_permission_subset(role_perms, ctx),
        check_role_hierarchy(role_position, ctx, role_label),
        check_target(ctx, target_top_position, target_is_self),
    )


def validate_change_nickname(
    ctx: RequestContext,
    target_top_position: int,
    target_is_self: bool,
) -> Decision:
    """Validate changing a nickname.

    Acting on yourself needs ``change_nickname``; acting on someone else needs
    ``manage_nicknames`` and the target must rank below you.
    """
    if target_is_self:
        # Owner or anyone with change_nickname may rename themselves.
        if ctx.is_owner or getattr(ctx.requester_perms, "change_nickname", False):
            return allow("change_nickname", "self-rename")
        return refuse(
            "change_nickname",
            "You need the **change nickname** permission to rename yourself here.",
        )
    return _first_refusal(
        check_requester_capability(ctx, "manage_nicknames"),
        check_target(ctx, target_top_position, target_is_self),
    )


def validate_create_channel(ctx: RequestContext) -> Decision:
    """Validate creating a channel/category."""
    return check_requester_capability(ctx, "manage_channels")


def validate_set_channel_overwrite(
    ctx: RequestContext,
    allow_perms: discord.Permissions,
    target_top_position: int | None = None,
    target_is_role: bool = True,
    target_is_self: bool = False,
) -> Decision:
    """Validate setting a channel permission overwrite.

    Requires ``manage_channels``. The *allowed* permissions in the overwrite are
    subset-checked against the requester (you can't grant channel powers you don't
    have), and Administrator can never appear in an overwrite. When the overwrite
    targets a role, that role must sit below the requester (hierarchy); when it
    targets a member, that member must rank below the requester (target check).
    """
    checks = [
        check_administrator_block(allow_perms),
        check_requester_capability(ctx, "manage_channels"),
        check_permission_subset(allow_perms, ctx),
    ]
    if target_top_position is not None:
        if target_is_role:
            checks.append(check_role_hierarchy(target_top_position, ctx, "that role"))
        else:
            checks.append(check_target(ctx, target_top_position, target_is_self))
    return _first_refusal(*checks)


# --------------------------------------------------------------------------- #
# Punitive actions (v1: gated behind typed confirmation in the message layer)
# --------------------------------------------------------------------------- #


def validate_punitive(
    ctx: RequestContext,
    required: str,
    target_top_position: int,
    target_is_self: bool,
) -> Decision:
    """Validate a punitive action (ban/kick/timeout) at the *permission* level.

    NOTE: this is only the permission gate. Punitive actions additionally require
    an explicit typed CONFIRM in the message layer before they ever reach an
    executor -- they are never run straight off an LLM parse.
    """
    if target_is_self:
        return refuse("punitive", "You can't apply a moderation action to yourself.")
    return _first_refusal(
        check_requester_capability(ctx, required),
        check_target(ctx, target_top_position, target_is_self),
    )


# --------------------------------------------------------------------------- #
# Mini-admin validators (new tools) -- all compose the primitives above, so the
# "clamped to the requester's own permissions" invariant and the Administrator
# hard-block are preserved automatically. No bot super-admin path exists.
# --------------------------------------------------------------------------- #


def validate_capability(ctx: RequestContext, required: str) -> Decision:
    """Generic single-permission gate for actions with no role/target hierarchy of
    their own (delete/purge/pin messages, edit/delete channels, invites, unban,
    ban-list, audit-log reads, guild edits, emoji management).

    The requester must personally hold ``required`` (owner is exempt). This is the
    whole gate for these tools: they never grant permissions, so no subset check is
    needed, and their targets carry no role rank to compare against.
    """
    return check_requester_capability(ctx, required)


def validate_delete_role(ctx: RequestContext, role_position: int) -> Decision:
    """Validate deleting an existing role.

    Requires ``manage_roles`` and that the role sit below both the bot's and (for
    non-owners) the requester's top role -- you can't delete a role that outranks
    you. No subset/Administrator check: deletion removes power, never grants it.
    """
    return _first_refusal(
        check_requester_capability(ctx, "manage_roles"),
        check_role_hierarchy(role_position, ctx, "that role"),
    )


def validate_edit_role(
    ctx: RequestContext,
    requested_perms: discord.Permissions,
    role_position: int,
) -> Decision:
    """Validate editing an existing role's name/colour/flags/permissions/position.

    Identical discipline to :func:`validate_create_role`: the role's *resulting*
    permissions are Administrator-blocked and subset-checked against the requester,
    so an edit can never raise a role's powers above the requester's own. Callers
    pass ``role_position = max(current_position, intended_new_position)`` so you can
    neither edit a role that already outranks you nor move one above yourself.
    """
    return _first_refusal(
        check_administrator_block(requested_perms),
        check_requester_capability(ctx, "manage_roles"),
        check_permission_subset(requested_perms, ctx),
        check_role_hierarchy(role_position, ctx, "that role"),
    )


def validate_member_action(
    ctx: RequestContext,
    required: str,
    target_top_position: int,
    target_is_self: bool,
) -> Decision:
    """Validate a member-targeted action (voice move/mute/deafen, remove-timeout).

    Requires the gating permission and that the target rank below the requester.
    Unlike :func:`validate_punitive`, acting on *yourself* is allowed (e.g. moving
    or muting yourself in voice), because these actions are not punitive.
    """
    return _first_refusal(
        check_requester_capability(ctx, required),
        check_target(ctx, target_top_position, target_is_self),
    )
