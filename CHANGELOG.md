# Changelog

## Unreleased — web control hub at dcgsl.duckdns.org

The control hub previously existed only as a terminal TUI on the box itself.
This adds a browser twin of that hub plus a one-shot script that publishes it
securely at https://dcgsl.duckdns.org.

### Added
- **`bot/web.py`** — an aiohttp web control hub (`./setup.sh --web` /
  `run.py --web`) mirroring the TUI: Dashboard (start/stop/restart, live status,
  live audit feed), Configure (every `.env` setting, secrets masked and never
  echoed back), and Maintenance (install deps, check/update from GitHub with
  streamed output, Discord update announcements, auto-update loop). Binds to
  `127.0.0.1` only.
- **`bot/websecurity.py`** — stdlib-only security primitives: scrypt password
  hashing (salted, constant-time verify), HMAC-signed expiring session tokens,
  per-session CSRF tokens, and a sliding-window login throttle. Fully unit-tested.
- **`bot/webui/`** — the static frontend (no frameworks, no CDN, CSP-friendly):
  dark-themed dashboard with incremental event polling.
- **`deploy/install-web.sh`** — idempotent root deployment script: installs
  nginx + certbot (apt/dnf), obtains + auto-renews a Let's Encrypt certificate
  for the domain, writes a hardened vhost (TLS 1.2/1.3 only, HSTS, security
  headers, `/login` rate limit, HTTP→HTTPS redirect), installs a hardened
  systemd service (`gomboclat-web`), opens 80/443 in ufw/firewalld, and can
  install a DuckDNS IP-updater timer (`--duckdns-token`).
- **`python run.py --set-web-password`** — interactive admin-password setup;
  only the scrypt hash lands in `.env`. The web hub refuses to start until a
  password is set, and auto-generates its session secret on first launch.
- **`WEB_HOST` / `WEB_PORT` / `WEB_DOMAIN` / `WEB_PASSWORD_HASH` /
  `WEB_SESSION_SECRET` / `WEB_SESSION_HOURS`** env settings.
- 16 new unit tests (81 → 97) covering password round-trips and malformed
  hashes, token expiry/tampering/wrong-secret, CSRF pairing, throttle
  windows/independence, and the incremental event buffer.

### Security notes
- Sessions: `HttpOnly` + `SameSite=Strict` + `Secure` cookies; every
  state-changing request also needs the `X-CSRF-Token` header.
- Failed logins throttled app-side (5 per 5 min per client) *and* rate-limited
  by nginx; `X-Real-IP` is only trusted from localhost (i.e. our nginx).
- Self-only Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff`,
  `no-referrer`, `Cache-Control: no-store` on every app response.
- **No change to the Discord permission model** — the web hub is an operator
  console; moderation writes are still validated in `bot/permissions.py`.

## Unreleased — conversation context

Until now the bot saw only the text of the message that mentioned it — so "ban
them" as a reply, "who is this?", or "undo what you just did" were impossible: the
model had no referent. This adds a focused, security-safe context system.

### Added
- **Reply context** — when you @mention the bot as a *reply*, the replied-to
  message is resolved (author, user ID, body) and handed to the model as the most
  likely referent of "this"/"them"/"that". Deleted or unreadable targets are simply
  skipped, never guessed.
- **Recent history** — a short window of recent channel messages (oldest→newest,
  default 5) is included for background so back-references like "undo what you just
  did" resolve. The reply target is de-duplicated out of this window.
- **`bot/context.py`** — `gather_context()` (the async Discord shell, best-effort
  and non-blocking) plus a pure, fully-tested `MessageContext.render()` that emits
  a labelled block: Discord author/ID metadata is marked **trusted** (for resolving
  *who* is meant) while every message **body stays untrusted data**. Bodies are
  wrapped in explicit `<replied_message>` tags, mentions are cleaned to `@name`,
  and each body is length-capped to protect the token budget.
- **`CONTEXT_ENABLED` / `CONTEXT_INCLUDE_REPLIES` / `CONTEXT_HISTORY_LIMIT` /
  `CONTEXT_MAX_MESSAGE_CHARS`** env settings, surfaced in `/modstatus`.
- 27 new unit tests (54 → 81) covering render structure/security framing, body
  cleaning/truncation, history ordering + reply de-duplication, and every reply
  resolution branch (cached, fetched, deleted, fetch-failure).

### Note
**No change to the security model.** Context only helps the model resolve *who* a
request is about; every write it proposes is still re-validated against the real
requester's live Discord permissions in `permissions.py`. Set `CONTEXT_ENABLED=false`
to send only the raw message.

## Unreleased — internal-by-default answers

A new **answer scheme**: the model no longer speaks by having its text auto-posted.
Its free-text output is now *internal* — a private scratchpad that is never shown —
and it communicates with users only by deliberately calling a `send_message` tool.

### Added
- **`send_message` tool** — the bot's sole channel of communication with humans. It
  posts to the current channel by default, or to any other channel the model names
  (e.g. *"announce it in #general"*). Like every other tool it is re-validated in
  code before it touches Discord: the requester must be able to **view and send** in
  the target channel (guild owner exempt), so the bot can never be used to broadcast
  into a channel the requester couldn't post in themselves. Rate-limited and audited
  like any write. The guard lives in `permissions.validate_send_message`, not the prompt.
- Tests for `validate_send_message` (allow / no-view / view-but-no-send / owner
  bypass / injection-claims-access-doesn't-help).

### Changed
- **The message layer no longer auto-posts the model's reply.** `Agent.run` still
  returns the model's text, but it is treated as internal (kept for logs/TUI only);
  users see only what the model sends via `send_message`, plus the usual 👀/✅/⚠️
  reactions. If the model chooses to say nothing, only the ✅ reaction appears.
- System prompt updated to explain the internal-by-default scheme and that
  `send_message` is the only way to reach a human.

### Note
No change to the security model. Every write — now including `send_message` — is
still re-checked in `bot/permissions.py` against the real requester's permissions.

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
