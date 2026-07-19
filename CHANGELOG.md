# Changelog

## Unreleased — quality-of-life updates

Usability improvements across the board. **No change to the security model:**
permissions are still enforced in `bot/permissions.py`, and the added slash
commands gate admin actions behind Discord's **Manage Server** permission.

### Added
- **`/help`** — a friendly, embedded overview of what the bot can do, with examples.
- **`/setratelimit [max_actions]`** — set the per-user write rate limit for a
  server at runtime (omit the value to reset to the global default). *Manage Server.*
- **`/togglebot`** — enable or disable the bot in a single server. *Manage Server.*
- **`/auditlog [limit]`** — show the most recent moderation actions in the server. *Manage Server.*
- **Acknowledgement reactions** — the bot reacts 👀 while working, then ✅ on
  success or ⚠️ on error, so you get instant feedback on your message.
- **Friendly DM reply** — DMing the bot now returns guidance instead of silence.
- **Richer colours** — three-digit hex shorthand (`#f0f`), `0x`-prefixed hex,
  CSS `rgb(r, g, b)`, the special value `random`, and ~30 more named colours
  (coral, turquoise, lavender, charcoal, …).

### Improved
- **Smarter name resolution** — members, roles, and channels now resolve by an
  exact (case-insensitive) match first, then fall back to a *unique* partial
  match, so *"rename Dave"* finds **Dave the Great**. Ambiguous matches ask you
  to be more specific or use an ID.
- **`/modstatus`** now also shows whether the bot is active in the server, any
  per-server rate-limit override, and the bulk-confirm threshold.

### Internal
- `GuildSettingsStore.set_rate_limit` / `set_enabled` for per-guild writes.
- `AuditLogger.recent_for_guild` plus a `(guild_id, id)` index for fast per-server history.
- New unit tests for colour parsing, name matching, and the settings/audit stores
  (33 → 54 tests).
