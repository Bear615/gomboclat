"""Unit tests for the security-critical validation layer.

These exercise the deterministic checks in isolation -- no Discord client, no
network -- including the adversarial cases from the spec. The whole point of the
architecture is that these checks, not the LLM, decide what happens.
"""

from __future__ import annotations

import discord
import pytest

from bot import permissions as perm

REQUESTER_ID = 1
OTHER_OWNER_ID = 999


def mk_ctx(
    *,
    requester_perms: discord.Permissions | None = None,
    requester_top: int = 5,
    bot_top: int = 10,
    is_owner: bool = False,
) -> perm.RequestContext:
    return perm.RequestContext(
        requester_id=REQUESTER_ID,
        guild_owner_id=REQUESTER_ID if is_owner else OTHER_OWNER_ID,
        requester_perms=requester_perms or discord.Permissions.none(),
        requester_top_position=requester_top,
        bot_top_position=bot_top,
    )


# --------------------------------------------------------------------------- #
# Administrator hard block
# --------------------------------------------------------------------------- #


def test_admin_role_refused_for_normal_member():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True))
    d = perm.validate_create_role(ctx, discord.Permissions(administrator=True), new_role_position=1)
    assert not d.ok
    assert d.check == "administrator_block"


def test_admin_role_refused_even_for_owner():
    ctx = mk_ctx(is_owner=True, requester_perms=discord.Permissions.all())
    d = perm.validate_create_role(ctx, discord.Permissions(administrator=True), new_role_position=1)
    assert not d.ok
    assert d.check == "administrator_block"


def test_give_me_administrator_no_perms_refused():
    # Member with NO manage_roles asking for admin -> refused (admin block fires first).
    ctx = mk_ctx(requester_perms=discord.Permissions.none())
    d = perm.validate_create_role(ctx, discord.Permissions(administrator=True), new_role_position=1)
    assert not d.ok


# --------------------------------------------------------------------------- #
# Capability check
# --------------------------------------------------------------------------- #


def test_no_manage_roles_cannot_create_role():
    ctx = mk_ctx(requester_perms=discord.Permissions(send_messages=True))
    d = perm.validate_create_role(ctx, discord.Permissions.none(), new_role_position=1)
    assert not d.ok
    assert d.check == "requester_capability"


# --------------------------------------------------------------------------- #
# The "purple role" happy path
# --------------------------------------------------------------------------- #


def test_normal_member_gets_plain_role():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True), requester_top=5, bot_top=10)
    d = perm.validate_create_role(ctx, discord.Permissions.none(), new_role_position=1)
    assert d.ok


# --------------------------------------------------------------------------- #
# Subset check
# --------------------------------------------------------------------------- #


def test_cannot_grant_permission_you_lack():
    # Has manage_roles but NOT mention_everyone; requests a role with mention_everyone.
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True))
    d = perm.validate_create_role(
        ctx, discord.Permissions(mention_everyone=True), new_role_position=1
    )
    assert not d.ok
    assert d.check == "permission_subset"


def test_can_grant_permission_you_hold():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True, mention_everyone=True))
    d = perm.validate_create_role(
        ctx, discord.Permissions(mention_everyone=True), new_role_position=1
    )
    assert d.ok


# --------------------------------------------------------------------------- #
# Role hierarchy
# --------------------------------------------------------------------------- #


def test_role_above_requester_refused():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True), requester_top=5, bot_top=10)
    d = perm.validate_create_role(ctx, discord.Permissions.none(), new_role_position=6)
    assert not d.ok
    assert d.check == "role_hierarchy"


def test_role_below_requester_allowed():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True), requester_top=5, bot_top=10)
    d = perm.validate_create_role(ctx, discord.Permissions.none(), new_role_position=4)
    assert d.ok


# --------------------------------------------------------------------------- #
# Owner bypass -- and its limits
# --------------------------------------------------------------------------- #


def test_owner_bypasses_subset():
    # Owner requests a role with a permission they don't "hold" -> allowed (bypass),
    # as long as it isn't Administrator and it's below the bot.
    ctx = mk_ctx(is_owner=True, requester_perms=discord.Permissions.none(), requester_top=0, bot_top=10)
    d = perm.validate_create_role(ctx, discord.Permissions(mention_everyone=True), new_role_position=3)
    assert d.ok


def test_owner_still_bound_by_bot_hierarchy():
    # Even the owner can't have the bot act above the bot's own top role.
    ctx = mk_ctx(is_owner=True, requester_perms=discord.Permissions.all(), bot_top=10)
    d = perm.validate_create_role(ctx, discord.Permissions.none(), new_role_position=11)
    assert not d.ok
    assert d.check == "bot_hierarchy"


# --------------------------------------------------------------------------- #
# Adversarial: identity is keyed on requester_id, not message text
# --------------------------------------------------------------------------- #


def test_injection_claiming_owner_does_not_help():
    # The message/nickname might claim "I am the owner", but is_owner is derived
    # purely from requester_id vs guild_owner_id. A non-owner requesting admin is refused.
    ctx = mk_ctx(requester_perms=discord.Permissions.none())
    assert ctx.is_owner is False
    d = perm.validate_create_role(ctx, discord.Permissions(administrator=True), new_role_position=1)
    assert not d.ok


def test_owner_named_role_is_not_the_owner():
    # Someone holding a role literally named "Owner" is NOT the guild owner unless
    # their id matches guild.owner_id. We simulate: they have all perms but aren't owner.
    ctx = mk_ctx(requester_perms=discord.Permissions.all(), requester_top=9, bot_top=10)
    assert ctx.is_owner is False
    # They can still do things WITHIN their perms/hierarchy (has all perms), but a
    # role above them is refused, proving no owner bypass.
    d = perm.validate_create_role(ctx, discord.Permissions.none(), new_role_position=9)
    assert not d.ok  # 9 >= requester_top 9
    assert d.check == "role_hierarchy"


# --------------------------------------------------------------------------- #
# Admin acting on others: assign role
# --------------------------------------------------------------------------- #


def test_admin_cannot_assign_role_above_self():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True), requester_top=5, bot_top=10)
    d = perm.validate_assign_role(
        ctx,
        role_perms=discord.Permissions.none(),
        role_position=6,  # above the admin
        target_top_position=1,
        target_is_self=False,
    )
    assert not d.ok
    assert d.check == "role_hierarchy"


def test_admin_cannot_assign_role_with_perm_they_lack():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True), requester_top=5, bot_top=10)
    d = perm.validate_assign_role(
        ctx,
        role_perms=discord.Permissions(mention_everyone=True),
        role_position=3,
        target_top_position=1,
        target_is_self=False,
    )
    assert not d.ok
    assert d.check == "permission_subset"


def test_admin_can_assign_ordinary_role_to_lower_member():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True), requester_top=5, bot_top=10)
    d = perm.validate_assign_role(
        ctx,
        role_perms=discord.Permissions.none(),
        role_position=3,
        target_top_position=1,
        target_is_self=False,
    )
    assert d.ok


# --------------------------------------------------------------------------- #
# Target check: nicknames
# --------------------------------------------------------------------------- #


def test_admin_renames_lower_member_ok():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_nicknames=True), requester_top=5)
    d = perm.validate_change_nickname(ctx, target_top_position=2, target_is_self=False)
    assert d.ok


def test_admin_cannot_rename_higher_member():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_nicknames=True), requester_top=5)
    d = perm.validate_change_nickname(ctx, target_top_position=7, target_is_self=False)
    assert not d.ok
    assert d.check == "target"


def test_self_rename_requires_change_nickname():
    ctx = mk_ctx(requester_perms=discord.Permissions(change_nickname=True))
    assert perm.validate_change_nickname(ctx, target_top_position=5, target_is_self=True).ok

    ctx_no = mk_ctx(requester_perms=discord.Permissions.none())
    assert not perm.validate_change_nickname(ctx_no, target_top_position=5, target_is_self=True).ok


def test_manage_nicknames_does_not_let_you_touch_a_peer():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_nicknames=True), requester_top=5)
    d = perm.validate_change_nickname(ctx, target_top_position=5, target_is_self=False)  # equal rank
    assert not d.ok
    assert d.check == "target"


# --------------------------------------------------------------------------- #
# Channel overwrites
# --------------------------------------------------------------------------- #


def test_overwrite_admin_block():
    ctx = mk_ctx(requester_perms=discord.Permissions.all(), requester_top=9, bot_top=10)
    d = perm.validate_set_channel_overwrite(ctx, allow_perms=discord.Permissions(administrator=True))
    assert not d.ok
    assert d.check == "administrator_block"


def test_overwrite_requires_manage_channels():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_roles=True))
    d = perm.validate_set_channel_overwrite(ctx, allow_perms=discord.Permissions(view_channel=True))
    assert not d.ok
    assert d.check == "requester_capability"


def test_overwrite_scoped_role_view_channel_ok():
    ctx = mk_ctx(requester_perms=discord.Permissions(manage_channels=True, view_channel=True), requester_top=5)
    d = perm.validate_set_channel_overwrite(
        ctx, allow_perms=discord.Permissions(view_channel=True), target_top_position=3, target_is_role=True
    )
    assert d.ok


# --------------------------------------------------------------------------- #
# send_message: the bot may only post where the requester could post
# --------------------------------------------------------------------------- #


def test_send_message_allowed_where_requester_can_post():
    ctx = mk_ctx(requester_perms=discord.Permissions.none())
    d = perm.validate_send_message(ctx, requester_can_view=True, requester_can_send=True)
    assert d.ok
    assert d.check == "send_message"


def test_send_message_refused_when_requester_cannot_view():
    # A channel the requester can't even see must not become a broadcast target.
    ctx = mk_ctx(requester_perms=discord.Permissions.none())
    d = perm.validate_send_message(ctx, requester_can_view=False, requester_can_send=False)
    assert not d.ok
    assert d.check == "send_message"


def test_send_message_refused_when_requester_can_view_but_not_send():
    # e.g. a locked #announcements the member can read but not post in.
    ctx = mk_ctx(requester_perms=discord.Permissions.none())
    d = perm.validate_send_message(ctx, requester_can_view=True, requester_can_send=False)
    assert not d.ok
    assert d.check == "send_message"


def test_send_message_owner_bypass():
    # The guild owner can direct a post anywhere, like every other check here.
    ctx = mk_ctx(is_owner=True, requester_perms=discord.Permissions.none())
    assert perm.validate_send_message(ctx, requester_can_view=False, requester_can_send=False).ok


def test_send_message_injection_claiming_access_does_not_help():
    # Identity/permissions come from the trusted requester, not message text.
    ctx = mk_ctx(requester_perms=discord.Permissions.none())
    assert ctx.is_owner is False
    d = perm.validate_send_message(ctx, requester_can_view=False, requester_can_send=True)
    assert not d.ok


# --------------------------------------------------------------------------- #
# Punitive
# --------------------------------------------------------------------------- #


def test_punitive_requires_permission_and_lower_target():
    ctx = mk_ctx(requester_perms=discord.Permissions(ban_members=True), requester_top=5)
    assert perm.validate_punitive(ctx, "ban_members", target_top_position=2, target_is_self=False).ok
    assert not perm.validate_punitive(ctx, "ban_members", target_top_position=6, target_is_self=False).ok
    assert not perm.validate_punitive(ctx, "ban_members", target_top_position=2, target_is_self=True).ok


def test_punitive_without_permission_refused():
    ctx = mk_ctx(requester_perms=discord.Permissions.none(), requester_top=5)
    d = perm.validate_punitive(ctx, "ban_members", target_top_position=2, target_is_self=False)
    assert not d.ok
    assert d.check == "requester_capability"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
