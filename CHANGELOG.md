# Changelog

## Unreleased — update announcements

The auto-update feature previously pulled code silently, with output only in the
TUI's Maintenance panel — so there was no signal in Discord that anything changed,
and nothing ever surfaced the changelog.

### Added
- **Update announcements** — after any update (manual *Update & restart* or the
  automatic loop) that pulls new commits, the hub now:
  - lists the new commit subjects in the Maintenance log, and
  - posts a summary embed (the commit list plus the top section of `CHANGELOG.md`)
    to every guild's configured log channel. Best-effort and per-guild silent —
    a missing channel or send failure never breaks the update.
- `maintenance.pull_and_install` now returns an `UpdateReport` (pulled/deps status,
  old→new revs, and the list of commit subjects that landed).
- `maintenance.top_changelog_section` / `read_changelog_section` — pure, tested
  helpers that extract the newest section from a Keep-a-Changelog file.
- `BotController.bot` — exposes the live client (only while running) so the hub can
  post to Discord after an update.

### Note
Auto-update remains **off by default** (`AUTO_UPDATE=false`). Enable it in the
Configure tab if you want unattended updates + announcements.

## 0.1.0 — quality-of-life updates

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
