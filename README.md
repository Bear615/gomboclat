# AI-Moderator Discord Bot

A Discord bot that lets server members make moderation-style requests in plain
English — *"give me a purple role"*, or (from an admin) *"give Tapetalterror a new
role that's purple with access to only this channel"* — and carries them out.

An LLM **parses intent**; deterministic Python **enforces permissions**. Every
write is validated in code against the requester's real Discord permissions
before anything happens. It ships with a terminal dashboard (TUI) and a one-shot
setup script for Linux.

> **The LLM is not a security boundary.** A prompt-injected or jailbroken model
> can, at worst, make the bot *attempt* a disallowed action that the validator
> then rejects. It can never escalate privileges. That rule lives in
> `bot/permissions.py`, not in the system prompt.

---

## Quick start (Linux)

```bash
git clone <this-repo> && cd gomboclat
./setup.sh --install        # create venv + install deps + make .env
$EDITOR .env                # add DISCORD_TOKEN and ANTHROPIC_API_KEY
./setup.sh                  # launch the TUI dashboard
```

Other modes:

```bash
./setup.sh --headless       # run without the TUI (plain console logging)
./setup.sh --test           # run the unit tests
```

The script creates a `.venv`, installs everything from `requirements.txt`,
copies `.env.example` → `.env` on first run, and launches the bot.

### Privileged intents (required)

In the [Discord Developer Portal](https://discord.com/developers/applications) →
your app → **Bot**, enable both privileged intents:

- **MESSAGE CONTENT INTENT** — so the bot can read the request text.
- **SERVER MEMBERS INTENT** — so the bot can resolve members and roles.

Invite the bot with the `bot` and `applications.commands` scopes and give it a
role **above** any role it will manage (Manage Roles / Manage Channels /
Manage Nicknames as needed). Drag its role high in Server Settings → Roles.

---

## How you use it

Mention the bot and describe what you want:

```
@AI Moderator give me a purple role please
@AI Moderator make a role that can only see #secret-lab and give it to me
@AI Moderator rename Dave to "On Vacation"
```

The bot only runs the LLM when **@mentioned** — never on every message. While it
works on your request it reacts 👀 on your message, then ✅ when done (or ⚠️ if
something went wrong). DM it and it points you back to a server.

**Reply to point at something.** When you mention the bot as a *reply* to another
message, that message is pulled into context — so `@AI Moderator ban them` or
`@AI Moderator who is this?` just work, no need to name the person. The bot also
sees a short window of recent channel messages, letting *"undo what you just did"*
and similar back-references resolve. This context system is focused on purpose:
author names and IDs come from Discord (trusted, used only to figure out *who* is
meant), while message text stays untrusted data — every action is still re-checked
against **your** real permissions in code. Tune or disable it with the
`CONTEXT_*` settings in `.env`.

Colours can be a name (`purple`, `coral`, `turquoise`, …), a hex code (`#A020F0`,
`0xA020F0`, or the shorthand `#f0f`), a CSS-style `rgb(160, 32, 240)`, or `random`.
Member/role/channel names resolve by an exact match first, then fall back to a
unique partial match — so *"rename Dave"* finds **Dave the Great**.

Slash commands:

- `/help` — what the bot can do, with examples (anyone).
- `/modstatus` — model, rate limit, log channel, whether the bot is active here, and its role position (anyone).
- `/setlogchannel [#channel]` — set where audit logs are posted (needs Manage Server).
- `/setratelimit [max_actions]` — set writes allowed per user per window; omit to reset to the default (needs Manage Server).
- `/togglebot` — enable or disable the bot in this server (needs Manage Server).
- `/auditlog [limit]` — show the most recent moderation actions in this server (needs Manage Server).

---

## The TUI — a full control hub

Running `./setup.sh` (no args) opens a terminal **control hub**. You manage the
entire bot from here; after launch you never need to touch the shell. It opens
even before you've set your tokens, so you can configure everything in-app.

Three tabs:

**⬤ Dashboard**
- **Start / Stop / Restart** the bot (buttons, or keys `s` / `x` / `r`). A fresh,
  fully-configured bot is built on every start, so config edits apply on restart
  without leaving the app.
- **Status panel** — live run state, connection, logged-in user, guild list, and a
  summary of the active config (model, rate limit, punitive, auto-update).
- **Live audit feed** — every executed and refused action, with the *real*
  requester, streamed as it happens (recent history loads on start).

**⚙ Configure**
- Edit **every** setting — Discord token, Anthropic key, model, max tokens,
  agent iterations, rate limit + window, bulk-confirm threshold, punitive toggle,
  and the auto-update options — then **Save to .env** (or **Save & restart bot**).
  Secrets are masked; leaving a secret box blank keeps the existing value.

**⛭ Maintenance**
- **Install / Reinstall dependencies** (pip, into the active venv) with streamed output.
- **Check for updates** — fetches the git upstream and shows how many commits behind you are.
- **Update & restart** — fast-forward pull → reinstall deps → restart the bot.
- **Auto-update** — when enabled (Configure tab), the hub periodically checks the
  upstream and, if there are new commits, pulls and reinstalls automatically; with
  **auto-restart** on, it restarts the bot to apply the update. Interval configurable.
  Auto-update is **off by default** — flip *"Auto-update from GitHub"* in the
  Configure tab and **Save** to turn it on.
- **Update announcements** — after any update (manual or automatic) that pulls new
  commits, the hub lists the new commit subjects in the Maintenance log and posts a
  summary embed — the commit list plus the top section of `CHANGELOG.md` — to every
  guild's configured log channel (set with `/setlogchannel`). Guilds without a log
  channel are simply skipped.

Keys: `s` start · `x` stop · `r` restart · `c` clear feed · `q` quit.

Prefer no UI? `./setup.sh --headless` runs the bot with plain console logging and
no hub (config comes straight from `.env`).

---

## The web UI — the same hub, in your browser

`./setup.sh --web` runs a browser twin of the TUI: the same Dashboard
(start/stop/restart + live audit feed), Configure (edit every `.env` setting),
and Maintenance (deps, update & restart) tabs, served over HTTP. It binds to
**localhost only** — the internet is meant to reach it through nginx over TLS.

### One-shot secure deployment (`https://dcgsl.duckdns.org`)

```bash
sudo ./deploy/install-web.sh --email you@example.com
```

That single command, safe to re-run, does everything:

- installs **nginx + certbot** (apt or dnf), the app venv, and dependencies;
- prompts you for a web **admin password** (stored only as an scrypt hash);
- writes an nginx vhost for `dcgsl.duckdns.org`, obtains a **Let's Encrypt
  certificate** (with an auto-renew reload hook), and redirects all HTTP to HTTPS;
- hardens the vhost — TLS 1.2/1.3 only, HSTS, security headers, and an nginx
  rate limit on `/login`;
- installs a hardened **systemd service** (`gomboclat-web`) so the hub (and the
  bot inside it) starts on boot and restarts on failure;
- opens ports 80/443 in ufw/firewalld when one is active;
- optionally keeps your DuckDNS record pointed at this machine
  (`--duckdns-token YOUR_TOKEN` installs a 5-minute updater timer).

Prerequisites: the DuckDNS domain must point at your public IP, and ports
80 + 443 must be forwarded to the machine. Different domain or port? Use
`--domain` / `--port`. Change the password later with `--reset-password`.

```bash
systemctl status gomboclat-web      # service state
journalctl -u gomboclat-web -f      # live logs
```

### Web security model

- The app itself listens on `127.0.0.1:8134` only; nginx is the sole way in,
  and it speaks HTTPS exclusively.
- One admin password, scrypt-hashed in `.env` (`python run.py --set-web-password`).
  The hub **refuses to start** without one.
- Sessions are HMAC-signed expiring cookies (`HttpOnly`, `SameSite=Strict`,
  `Secure`); every state-changing request additionally requires a per-session
  CSRF token.
- Failed logins are throttled in the app (5 per 5 minutes per address) *and*
  rate-limited in nginx.
- Strict headers everywhere (self-only CSP, `X-Frame-Options: DENY`, HSTS,
  `nosniff`), and secrets are never echoed back to the browser.

---

## What it can do (v1)

**Read-only (context gathering):** `get_member_info`, `list_roles`,
`list_channels`, `get_my_permissions`.

**Writes (each validated before execution):** `create_role`, `assign_role` /
`remove_role`, `change_nickname`, `create_channel`, `set_channel_overwrite`.

**Punitive (enabled, but gated):** `kick_member`, `ban_member`,
`timeout_member`. These are irreversible, so they require an explicit **typed
confirmation** — the bot asks you to reply with exactly `CONFIRM <member-id>`.
They are never executed straight off an LLM parse. Disable them entirely with
`ENABLE_PUNITIVE=false`.

### "Access to only this channel"

Roles are server-wide, so this is always two steps, which the bot performs:

1. Create a role with **no** permissions.
2. Add a permission overwrite on the target channel granting that role
   `view_channel` — and, if the access is meant to be exclusive, deny
   `view_channel` for `@everyone` on that channel.

---

## The permission model (enforced in `bot/permissions.py`)

Given the authenticated `message.author` and a proposed action:

1. **Owner bypass** — only `guild.owner_id` (exactly) skips the subset/hierarchy
   checks. Not an "Owner"-named role, not Administrator holders. The owner is
   still bound by physics: the bot can't act above its own top role.
2. **Never grant Administrator** — a hard, separate block for everyone, owner
   included.
3. **Permission subset** — a role's permissions must be a subset of the
   requester's effective permissions.
4. **Requester capability** — the requester must personally hold the permission
   the action needs (`manage_roles`, `manage_channels`, `manage_nicknames` / `change_nickname`).
5. **Role hierarchy** — any role created/edited/assigned must sit below both the
   bot's top role and the requester's top role.
6. **Target check** — to act on another member, the requester must outrank them.

Failures are refused with a short, friendly explanation and logged.

### Trust model

- **Identity is trusted** — decisions key on `message.author` (Discord-authenticated),
  never on claims in the message text.
- **Message content is untrusted** — the message body, usernames, nicknames, and
  role names are wrapped in a `<user_message>` block and labelled as data before
  they reach the model, so a nickname like `"SYSTEM: I'm an admin"` can't pose as
  an instruction. The code checks are the real backstop.

---

## Guardrails

- **Addressed only via @mention** — no LLM call on every message.
- **Untrusted context** — the replied-to message and recent history handed to the
  model are explicitly labelled as data (only Discord's author/ID metadata is
  trusted, for resolving *who* is meant); they never become a security decision.
- **Per-user rate limiting** on writes (default 5 / 60s, configurable per guild).
- **Confirmation** for bulk changes (≥ `BULK_CONFIRM_THRESHOLD` writes in one turn)
  and for every punitive action (typed `CONFIRM`).
- **Mandatory audit logging** recording the real requester — to SQLite *and* a
  configured Discord log channel. Discord's own audit log only shows the bot as
  the actor, so we log who actually asked.
- **Graceful failure** — `discord.Forbidden` / `HTTPException` are caught and
  explained (usually "move my role up").

---

## Project layout

```
bot/
  main.py          # Discord client, addressing, confirmation flow, slash commands
  web.py           # web control hub (aiohttp) — browser twin of the TUI
  websecurity.py   # scrypt passwords, signed sessions, CSRF, login throttle
  webui/           # static frontend for the web hub (HTML/CSS/JS)
  ai.py            # Anthropic client + agentic loop + tool schemas
  tools.py         # executor functions (thin wrappers over discord.py)
  permissions.py   # the validation layer — pure, unit-tested, no Discord I/O
  colours.py       # name->hex map + validation
  audit.py         # logging to SQLite + Discord channel + live TUI feed
  ratelimit.py     # per-user sliding-window limits
  config.py        # .env (read + write) + per-guild settings (SQLite)
  control.py       # bot lifecycle controller (start/stop/restart)
  maintenance.py   # git + pip helpers (updates, (re)install), async & streamed
  tui.py           # Textual control hub (dashboard / configure / maintenance)
tests/
  test_permissions.py   # incl. the adversarial cases
  test_colours.py       # colour parsing (names, hex, rgb, random)
  test_resolve.py       # the pure member/role/channel name matcher
  test_store.py         # per-guild settings + per-guild audit queries
  test_websecurity.py   # password hashing, session/CSRF tokens, login throttle
run.py             # entrypoint (TUI by default; --headless / --web available)
setup.sh           # one-shot Linux setup + launcher
deploy/
  install-web.sh   # nginx + Let's Encrypt + systemd deployment for the web UI
CHANGELOG.md
.env.example
requirements.txt
```

---

## Configuration

Everything is set in `.env` (see `.env.example`). Global values live there;
per-guild values (log channel, rate limit, enabled flag) live in the SQLite DB
and are set at runtime via slash commands.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Bot token (required) |
| `ANTHROPIC_API_KEY` | — | Anthropic key (required) |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Parser model; swap to `claude-haiku-4-5-20251001` for lower cost |
| `RATE_LIMIT_MAX` | `5` | Write actions per window per user |
| `RATE_LIMIT_WINDOW` | `60` | Rate-limit window (seconds) |
| `BULK_CONFIRM_THRESHOLD` | `3` | Writes-per-turn that trigger a confirmation |
| `ENABLE_PUNITIVE` | `true` | Enable ban/kick/timeout (still typed-CONFIRM gated) |
| `DB_PATH` | `moderator.db` | SQLite file for audit log + settings |
| `AUTO_UPDATE` | `false` | Periodically pull + reinstall from the git upstream |
| `AUTO_UPDATE_INTERVAL` | `30` | Minutes between update checks |
| `AUTO_RESTART` | `false` | Restart the bot automatically after an auto-update |
| `WEB_HOST` | `127.0.0.1` | Web hub bind address (keep on localhost behind nginx) |
| `WEB_PORT` | `8134` | Web hub port |
| `WEB_DOMAIN` | `dcgsl.duckdns.org` | Public domain used by `deploy/install-web.sh` |
| `WEB_PASSWORD_HASH` | — | Admin password (scrypt hash; `python run.py --set-web-password`) |
| `WEB_SESSION_SECRET` | auto | Session-cookie signing secret (auto-generated) |
| `WEB_SESSION_HOURS` | `12` | Browser session lifetime |

All of these are editable live from the TUI's **Configure** tab.

Model choice affects parsing quality/UX, **not** security — the code enforces
safety regardless of model.

---

## Running the tests

```bash
./setup.sh --test
# or, inside the venv:
python -m pytest -q
```

The permission tests cover the subset, hierarchy, target, Administrator-block,
and owner-bypass rules, plus the adversarial cases (injection claiming ownership,
"Owner"-named roles, granting perms you lack, acting above yourself). The rest of
the suite covers colour parsing, the name-resolution matcher, and the per-guild
settings/audit stores.
```
