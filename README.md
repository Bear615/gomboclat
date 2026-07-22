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
./setup.sh                  # launch the TUI dashboard, then configure it in-app
```

The bot talks to the model over the **OpenAI-compatible** chat-completions API,
so you bring your own **endpoint**, **model**, and **key**. Set them in the TUI's
**Configure** tab (it writes `.env` for you), or edit `.env` directly:

```bash
$EDITOR .env                # set DISCORD_TOKEN, OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL
```

Point `OPENAI_BASE_URL` at OpenAI itself, or at any compatible server —
OpenRouter, Together, Groq, LM Studio, Ollama, vLLM, LiteLLM, and friends.

Other modes:

```bash
./setup.sh --headless       # run without the TUI (plain console logging)
./setup.sh --web            # browser control hub at http://127.0.0.1:8765
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

The bot only runs the LLM when **@mentioned** — never on every message.

Slash commands:

- `/setlogchannel [#channel]` — set where audit logs are posted (needs Manage Server).
- `/modstatus` — show the model, rate limit, log channel, and the bot's role position.

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
- Edit **every** setting — Discord token, your **API endpoint** (base URL),
  **API key**, and **model** (OpenAI style), max tokens, agent iterations, rate
  limit + window, bulk-confirm threshold, punitive toggle, and the auto-update
  options — then **Save to .env** (or **Save & restart bot**). Secrets are masked;
  leaving a secret box blank keeps the existing value.

**⛭ Maintenance**
- **Install / Reinstall dependencies** (pip, into the active venv) with streamed output.
- **Check for updates** — fetches the git upstream and shows how many commits behind you are.
- **Update & restart** — fast-forward pull → reinstall deps → restart the bot.
- **Auto-update** — when enabled (Configure tab), the hub periodically checks the
  upstream and, if there are new commits, pulls and reinstalls automatically; with
  **auto-restart** on, it restarts the bot to apply the update. Interval configurable.

### Update announcements

Whenever the bot restarts on a newer commit than it last ran (from auto-update, a
manual **Update & restart**, or any `git pull` + restart), it posts a **changelog**
to every server's configured log channel and **pings that server's owner** (and
whoever added the bot, when the audit log allows). The header is a version that
climbs by itself — `v0.0.1` → `v0.0.2` → … one bump per update — and the body lists
the commit subjects the update brought in. The current version also shows in
`/modstatus`. No announcement fires on a plain restart with no new commits.

Keys: `s` start · `x` stop · `r` restart · `c` clear feed · `q` quit.

Prefer no UI? `./setup.sh --headless` runs the bot with plain console logging and
no hub (config comes straight from `.env`).

---

## What it can do

**Read-only (context gathering):** `get_member_info`, `list_roles`,
`list_channels`, `get_my_permissions`, `list_bans`, `read_audit_log`.

**Writes (each validated before execution):**

- *Roles:* `create_role`, `edit_role`, `assign_role` / `remove_role`.
- *Channels:* `create_channel`, `edit_channel`, `set_slowmode`,
  `set_channel_overwrite`, `create_invite`.
- *Messages:* `delete_message`, `purge_messages`, `pin_message` / `unpin_message`.
- *Members:* `change_nickname`, `move_member` (voice), `server_mute` /
  `server_unmute`, `server_deafen` / `server_undeafen`, `untimeout_member`,
  `unban_member`.
- *Expressions:* `delete_emoji`.

Every write still gates on the **requester's own** Discord permission (e.g.
`manage_messages` to delete a message, `move_members` to move someone in voice)
and, for member-targeted actions, the requester must outrank the target. The bot
never lets anyone exceed their real powers — the "mini-admin" is you, clamped.

**Destructive (typed-confirmation gated):** `delete_channel`, `delete_role`,
and `edit_guild` (rename the server) each require an explicit **typed
confirmation** — reply with exactly `CONFIRM <id>` — so they're never executed
straight off an LLM parse. `purge_messages` asks for a `yes`/`no` first and is
capped at 100 messages.

**Punitive (enabled, but gated):** `kick_member`, `ban_member`,
`timeout_member`. These are irreversible, so they require an explicit **typed
confirmation** — the bot asks you to reply with exactly `CONFIRM <member-id>`.
They are never executed straight off an LLM parse. Disable them entirely with
`ENABLE_PUNITIVE=false`.

### Conversational context

When you @mention the bot, it pulls in **your last few messages** in that channel
so you can refer back to them (*"@AI Moderator do what I described above"*). And
if your request is a **reply** to another message — by anyone — that message is
included too, so *"@AI Moderator delete this"* or *"pin this"* (as a reply) just
works. All of that recalled/replied text is treated as **untrusted data**: it's
context for the model, never instructions, and never a claim about who you are.

### "Access to only this channel"

Roles are server-wide, so this is always two steps, which the bot performs:

1. Create a role with **no** permissions.
2. Add a permission overwrite on the target channel granting that role
   `view_channel` — and, if the access is meant to be exclusive, deny
   `view_channel` for `@everyone` on that channel.

### Native moderator workflows

Three workflows have deterministic native commands, and warning management is
also available to the conversational bot through permission-checked AI tools:

- **Persistent warnings:** `/warn`, `/warnings`, and `/clearwarnings` maintain a
  server-scoped case history, enforce moderator permissions and role hierarchy,
  DM the member when possible, and write the action to the audit trail. Mentioning
  the bot with requests such as “warn Alex for repeated spam” uses the same store,
  permission checks, rate limits, and audit path.
- **Channel lockdown:** `/lockdown enabled:true` immediately prevents `@everyone`
  from sending in the current text channel; `enabled:false` removes that override.
  It requires Manage Channels and is fully audited.
- **Audit inspection:** `/auditsearch [query]` searches the latest actions by
  action, requester, or outcome from inside Discord. It requires View Audit Log.

## Browser control hub

`./setup.sh --web` launches a rebuilt, responsive control center on
`http://127.0.0.1:8765`. It deliberately binds to localhost. The Overview page
provides live lifecycle controls, connection and guild state, safety metrics,
the audit stream, and runtime messages. Configuration covers every setting in
the TUI (with write-only secret inputs), while Maintenance supports dependency
installation, update checks, and update/restart. The page polls the local API so
bot state and audit activity update without a reload.

### One-time public HTTPS deployment

The bot application owner can send the exact, non-slash message `!startweb` in a
server. The bot privately verifies that DMs work, then installs and configures a
systemd web service, NGINX reverse proxy, HTTP Basic Authentication, and a
Let's Encrypt certificate for `dcgsl.duckdns.org`. Credentials are sent only by
DM. After a successful deployment the command is permanently disabled in the
SQLite bot state and the original process exits so the systemd-managed web hub
can take over.

Before running it, point the DuckDNS hostname at the server, allow inbound TCP
ports 80 and 443, and run the bot as root or give its OS user passwordless sudo.
Interactive sudo is intentionally rejected. If setup fails, the private error is
DM'd and `!startweb` remains available for a retry.

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
  ai.py            # OpenAI-compatible client + agentic loop + tool schemas
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
  test_colours.py
run.py             # entrypoint (TUI by default; --headless available)
setup.sh           # one-shot Linux setup + launcher
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
| `OPENAI_API_KEY` | — | API key for your endpoint (required; any placeholder for keyless local servers) |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (OpenRouter, Groq, LM Studio, Ollama, vLLM, …) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Parser model — any model your endpoint serves |
| `RATE_LIMIT_MAX` | `5` | Write actions per window per user |
| `RATE_LIMIT_WINDOW` | `60` | Rate-limit window (seconds) |
| `BULK_CONFIRM_THRESHOLD` | `3` | Writes-per-turn that trigger a confirmation |
| `ENABLE_PUNITIVE` | `true` | Enable ban/kick/timeout (still typed-CONFIRM gated) |
| `DB_PATH` | `moderator.db` | SQLite file for audit log + settings |
| `AUTO_UPDATE` | `false` | Periodically pull + reinstall from the git upstream |
| `AUTO_UPDATE_INTERVAL` | `30` | Minutes between update checks |
| `AUTO_RESTART` | `false` | Restart the bot automatically after an auto-update |

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
"Owner"-named roles, granting perms you lack, acting above yourself).
```
