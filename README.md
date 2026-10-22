# TaskMan 🤖

A personal Telegram task manager and reminder bot powered by Claude AI. Send natural language messages like "remind me to call mom tomorrow at 3pm" and the bot parses, stores, and delivers the reminder at the right time. You can also just send a voice message instead of typing.

This README covers how the bot is built and deployed. For a plain-language walkthrough of how to actually use it in Telegram, see [USAGE.md](USAGE.md).

---

## Architecture Overview

```
Telegram App
     │
     │  you send a message
     ▼
Telegram Servers
     │
     │  bot.py polls "any updates for me?" every second or so, over an
     │  outbound HTTPS connection it opens itself (no public URL needed)
     ▼
bot.py  (python-telegram-bot, one process, one event loop)
     │
     ├── a slash command (/list, /done, /edit, ...) is handled directly in
     │   bot.py, straight against db.py -- fast, deterministic, no AI call
     │
     ├── free-text/voice (not a slash command) goes to agent.py instead:
     │     ├── voice.py transcribes voice/audio messages first
     │     ├── agent.py runs a Claude tool-use loop with tools backed
     │     │   directly by db.py (list/add/edit/delete/complete a task, set
     │     │   the active category, request bulk-action confirmation,
     │     │   transfer a task) -- Claude decides which to call and in
     │     │   what order, then writes the reply itself
     │     └── replies to you in Telegram
     │
     └── two APScheduler jobs (started in post_init, see bot.py), both on
         a 60-second interval, independent of each other
             │
             ├── check_and_send_reminders: calls db.py for due reminders,
             │   calls reminders.py to generate + send each one
             │
             └── check_overdue_tasks: calls db.py for tasks overdue and
                 due a nudge, calls reminders.py to generate + send each one
```

### Why each component exists

| Component | Role |
|---|---|
| `bot.py` | Receives messages from Telegram, handles slash commands directly, routes free text/voice to the AI agent, owns the reminder scheduler |
| `agent.py` | A Claude tool-use agent with direct, live access to the task database -- handles anything that isn't a slash command |
| `voice.py` | Transcribes voice/audio messages to text via OpenAI Whisper |
| `reminders.py` | Generates and delivers both due-time reminders and overdue nudges |
| `db.py` | All database reads and writes |
| APScheduler | In-process interval timer running both reminder jobs every 60s (see `post_init` in `bot.py`) |
| PostgreSQL | Persists tasks, reminders, and users |
| systemd | Keeps the process running and restarts it if it crashes |

This used to run as two extra processes, a Celery worker and Celery Beat, coordinated through Redis, purely to check for due reminders every 60 seconds. APScheduler does the same job as one scheduled job living inside `bot.py` itself. See the Reminder scheduling section below for the tradeoffs of that change.

It also used to need a Cloudflare Tunnel forwarding a public webhook URL to the host. The bot now uses polling instead (see How messages are received below), so there's no public networking surface at all anymore.

---

## File-by-File Breakdown

### `bot.py`

The entry point. This file sets up the Telegram bot, registers all the handlers, and starts polling for updates.

#### How messages are received

`main()` ends with `app.run_polling(...)`, which repeatedly asks Telegram's servers "any new updates for me?" over a connection the bot itself opens outbound. This replaced webhooks (Telegram POSTing to a public URL the bot exposes), which needed a Cloudflare Tunnel to give the host an HTTPS address in the first place. Polling needs no public URL and no exposed port, and it can run anywhere with outbound internet access. `post_init` also calls `bot.delete_webhook()` once at startup, since Telegram refuses `getUpdates` (what polling uses) while a webhook URL is still registered from the old setup.

The tradeoff: polling holds an open connection and checks in on an interval rather than being pushed to instantly, so there's a small amount of extra latency (typically well under a second) and it does slightly more idle network chatter than a webhook would. At personal-bot scale neither matters.

#### Free text and voice: the AI agent

Every plain text message that isn't a slash command goes through `handle_message`, which hands it to `run_agent` in `agent.py`. The agent is a Claude tool-use loop with tools backed directly by `db.py` -- `list_tasks`, `get_task`, `add_task`, `edit_task`, `delete_task`, `complete_task`, `request_bulk_action`, `set_active_category`, `transfer_task` -- so Claude itself decides which to call, in what order, and what to say back, rather than a fixed Python branch per hand-coded intent. Users don't need to remember slash commands; they can just talk naturally, including multi-step or unusual phrasing ("add X and also mark 3 done", "move task 7 to tomorrow and bump its priority") that a fixed intent list would never have anticipated.

#### Authorization

`is_authorized` checks the sender's Telegram user ID against the comma-separated `ALLOWED_USER_ID` list from `.env`. This keeps the bot private, since only IDs on that list can use it. `is_admin` does the same check against `ADMIN_USER_ID`, a subset of `ALLOWED_USER_ID` with the extra ability to transfer tasks (see Task transfer below).

#### Inline keyboards

The `/done` and `/delete` commands use Telegram inline keyboards (buttons attached to messages) whenever a typed description matches more than one pending task: the bot sends a message with one button per candidate plus Cancel, the user taps one, Telegram fires a `callback_query`, and `done_callback`/`delete_callback` handle it. Each button carries a `data` string (the task's internal UUID) so the handler knows which task to act on. Bulk actions (`/clear`, `/completeall`, and the AI agent's `request_bulk_action` tool) use the same mechanism for their Yes/Cancel confirmation.

#### Voice messages

`handle_voice` is registered for both `filters.VOICE` (voice notes recorded in Telegram) and `filters.AUDIO` (uploaded audio files). It downloads the audio bytes from Telegram, passes them to `transcribe_audio` in `voice.py`, echoes back what it heard so a misheard word is obvious immediately, and then feeds the transcribed text into `process_text_message`, the same function typed messages go through. From that point on, voice and text messages are indistinguishable to the rest of the bot.

#### Categories

Every task has a `category` (e.g. `personal`, `work`, `school`, or anything else you say). The AI agent infers it from the message when you add a task, either from an explicit statement like "add a work task to..." or from context clues, and defaults to `personal` if nothing suggests otherwise. `/list` with no arguments groups your tasks by category; `/list work` shows just one category; `/list priority` (optionally combined, e.g. `/list work priority`) sorts by urgency instead of due date. The same filtering and sorting works in natural language too ("what work tasks do I have", "show me everything by priority").

#### `/category`, scoping commands like `cd`

`/category work` sets a per-user "current category" (`CURRENT_CATEGORY`, an in-memory dict keyed by Telegram user ID -- a UX convenience tied to your ongoing conversation, not data worth surviving a restart). Once set, `/list`, `/done`, `/delete`, `/edit`, `/transfer`, and `/clear` all default to that category instead of everything, and new tasks you add default into it too, the same way `cd`ing into a directory makes new files land there. Giving any of those commands an explicit category still overrides the ambient one for that one call without changing it, the same way `ls <path>` doesn't change your shell's cwd. `/category` alone shows what you're scoped to; `/category all` (or "none", "clear", "reset") goes back to seeing everything. The AI agent can change this too, via its `set_active_category` tool ("let's focus on work", "show me everything again").

New tasks are the one place this isn't a soft default: while scoped to a category, the agent's `add_task` tool uses it unconditionally rather than letting Claude's own content-based guess override it. That mirrors real `cd` semantics: if you want a task filed elsewhere while scoped in, you switch first (`/category personal`), the same way you'd `cd` elsewhere before creating a file there.

#### Referring to a task by its permanent number

Every task gets a permanent `task_number` (a plain incrementing integer, see `schema.sql`) the moment it's created, and keeps it forever -- it's never reused or renumbered, even after the task is completed or deleted. `/list` always shows this number next to each task, and it's what you refer back to afterward: "mark number 7 as done", "edit task 3", `/done 7`, `/edit 3 priority 1`.

This is deliberately *not* a recomputed position in your last `/list` (an earlier design here had that bug: a task's apparent position shifts every time something else is completed, added, or filtered out, so acting on a stale position could silently hit the wrong task, or nothing at all, especially if you'd changed `/category` scope in between). A `task_number` means the same row every time, so `get_task_by_number(number, telegram_user_id)` in `db.py` can resolve it directly -- scoped to the caller's own tasks, so a guessed or hallucinated number can never return someone else's task -- with no need to `/list` first and no risk of category-scope mismatches.

#### Referring to a task by description

If you don't give a number, both the slash commands (`/done`, `/delete`, `/transfer`) and the AI agent can match a task by description instead. The commands do a simple case-insensitive substring match against your pending tasks (`_find_task_for_command` in `bot.py`); if more than one task matches, you get a button to pick the right one. The AI agent is more flexible: it calls its `list_tasks` tool, reads the descriptions itself, and picks the best match (or asks you to clarify if it genuinely can't tell), so phrasing like "the dentist one" or "that thing due Friday" works without an exact substring.

#### Notes and priority (`/note`, `/priority`, `/edit`)

`/note <number> [text]` sets a free-text note (`tasks.notes`) beyond the short description, or shows the current one if you omit the text. The slash command only accepts a number, not a free-text task description, since there's no reliable way to tell where a typed description ends and the note text begins (e.g. `/note dentist appointment remember insurance card`). A free-text reference only works through natural language, where the AI agent does that splitting for you.

`/priority <number> <1|2|3>` sets `tasks.priority`, which controls how often overdue nudges repeat (see Overdue nudges in the `reminders.py` section below). Priority is only ever set when a message explicitly signals urgency ("priority 1", "urgent", "no rush") -- the agent's system prompt (see `agent.py`) is explicit that this must never be guessed from a task's subject matter, unlike category.

`/edit <number> <field> <value>` (field: `description`, `category`, `priority`, `recurrence`, or `note`) is a deterministic shortcut for changing one of those fields without going through the AI agent. Changing a due date needs real date parsing, so that's agent-only -- just ask naturally ("move task 3 to tomorrow at 5pm") and it resolves the date and calls its `edit_task` tool, which also re-schedules that task's reminders around the new due time.

#### Deleting a task (`/delete`)

`/delete <number or description>` permanently removes a task -- and, via a foreign-key cascade, any of its still-pending reminders -- unlike `/done`, which just marks it complete. This can't be undone, so use it when you want a task gone entirely rather than finished. The AI agent has the same distinction built into its system prompt: "done"/"finished" calls `complete_task`, "delete"/"remove"/"get rid of" calls `delete_task`.

#### Bulk actions: "mark all as done" / "remove all"

`handle_complete_all` and `handle_clear` in `bot.py` both wrap `mark_all_tasks_complete` in `db.py`, which soft-completes every pending task for a user (optionally scoped to one category) and silences their unsent reminders -- no hard deletes, unlike single-task `/delete`. Both show a Yes/Cancel confirmation first, since it's bulk and there's no undo. `/clear` and `/completeall` trigger these directly; in natural language, the AI agent calls its `request_bulk_action` tool instead of looping single-task tools itself, which shows the exact same confirmation button before anything happens.

#### Task transfer (admin only)

Admins (Telegram IDs listed in `ADMIN_USER_ID`) can hand a task off to another user, either by typing `/transfer <task number or description> <name or telegram id>` or just saying something like "send task 2 to Sarah." Transferring a task changes its `telegram_user_id` in the database. Since reminders are linked to the task, not the user, and always look up the current owner when they fire, an already-scheduled reminder will just go to the new owner automatically. The recipient needs to be in `ALLOWED_USER_ID` (so they're allowed to use the bot at all) and needs to have messaged the bot at least once already, since Telegram doesn't let a bot DM someone who hasn't started a chat with it first. The AI agent's `transfer_task` tool re-checks admin status itself server-side before doing anything -- it never trusts the model's own judgment about who's allowed to transfer.

Non-admin users can run `/register <name>` to set a friendly display name, so admins can `/transfer` to them by name instead of hunting down their raw Telegram ID. `/whoami` shows your own ID, name, and role. `/users` (admin only) lists everyone registered, with name, admin status, and Telegram ID, so an admin can see who's available to send a task to without already knowing their name or ID.

#### Reminder scheduling

`main()` builds the `Application` with `.post_init(post_init)`. `post_init` is a hook python-telegram-bot calls once its event loop is actually running, but before it starts processing updates. That's the right moment to start an `AsyncIOScheduler` (from the `apscheduler` package), since that scheduler needs a running asyncio loop to attach jobs to, and no loop exists yet back in the synchronous part of `main()`.

The scheduler is given one job: call `check_and_send_reminders` (from `reminders.py`) every 60 seconds via an `IntervalTrigger`. That's the entire reminder pipeline now, with no Redis and no separate worker or beat process. The scheduler object is stashed in `app.bot_data["scheduler"]` only so nothing garbage-collects it, since APScheduler doesn't keep a reference alive on its own.

The tradeoff of moving this in-process: reminder delivery now shares the bot's single event loop with message handling, instead of running in a fully separate process. A slow Telegram API call during reminder delivery can briefly delay the bot from responding to an incoming message, and vice versa. That's a non-issue at personal-bot volume, but worth knowing if this ever needs to scale to many more users.

---

### `agent.py`

The AI agent: a Claude tool-use loop with direct, live access to the task database, used for every free-text or transcribed-from-voice message that isn't a slash command. This replaced the old `claude.py` pipeline (`parse_intent` classifying text into a fixed set of intents, `parse_task` extracting structured fields, `fuzzy_match` picking a task by description), where Claude only ever produced JSON that hand-written Python then acted on.

#### `run_agent(...)`

Takes the user's message plus some context (their Telegram ID, whether they're an admin, their active `/category` focus, and the configured timezone) and three callbacks (`on_confirm_needed`, `on_transfer`, `on_set_category`) that let tool execution reach back into `bot.py` for the few things that need Telegram itself (showing a confirm button, DMing a transfer recipient, updating the in-memory category context). It builds a system prompt with the current time/timezone/category/role baked in, then loops: call Claude with the `TOOLS` list, execute whatever tool calls come back, feed the results back in, and repeat (capped at `MAX_AGENT_STEPS`) until Claude responds with plain text instead of a tool call, which is what gets sent back to the user.

#### Tools

Each tool is a plain Python function (or, for the two that need Telegram, an async one) closed over the current request's `telegram_user_id` and `is_admin` -- never over anything from the model's own tool-call arguments. That's what keeps this safe to expose: `list_tasks`/`get_task`/`add_task`/`edit_task`/`delete_task`/`complete_task` all resolve a `task_number` through `db.get_task_by_number(number, telegram_user_id)`, so even a hallucinated or deliberately-crafted number can only ever touch a task actually owned by whoever is chatting. `transfer_task` goes further and re-checks the `is_admin` flag itself before doing anything, rather than trusting the system prompt to stop a non-admin from asking for one. `request_bulk_action` never touches the database directly -- it calls back into `bot.py`'s existing `handle_clear`/`handle_complete_all`, which show the same Yes/Cancel confirmation button the slash commands use, so a destructive bulk request always gets the same explicit confirmation regardless of how it was asked for.

#### Dates, priority, and category

The system prompt tells Claude to convert whatever the user said ("tomorrow", "Friday at 5pm", "in an hour") into an ISO 8601 UTC `due_datetime` itself, using the current UTC time and the user's timezone, both given in the prompt -- there's no separate date-parsing step. Priority is explicitly never to be inferred from a task's subject matter, only from an explicit signal ("urgent", "priority 1", "no rush"); category is the opposite, meant to be guessed from context when nothing else says otherwise, unless the user is already scoped into one via `/category`.

---

### `voice.py`

Turns Telegram voice/audio messages into text so they can flow through the same pipeline as typed messages.

Claude's Messages API doesn't accept audio input, so this is the one place the bot talks to a model other than Claude: `transcribe_audio` sends the raw audio bytes to OpenAI's `whisper-1` transcription endpoint and returns plain text. This needs its own `OPENAI_API_KEY` in `.env`, separate from `ANTHROPIC_API_KEY`.

If you'd rather not depend on OpenAI, whether for cost reasons or because you want everything self-hosted, the alternative is running Whisper locally via `faster-whisper`. It's the same interface, bytes in and text out, with no per-request API cost, but it needs a chunk of CPU (or a GPU) on whatever machine runs the bot. Worth switching to if you're already paying for a beefier host than a personal bot really needs.

---

### `db.py`

All database operations. Uses `psycopg` (the modern PostgreSQL driver, since `psycopg2` doesn't support Python 3.14) to connect to PostgreSQL.

#### Connection pattern

Every function opens a connection, does its work, and closes it in a `finally` block so the connection is always released even if an error occurs. This is intentionally simple; a connection pool would be more efficient at scale but adds complexity that isn't needed for a personal bot.

#### Key functions

- `save_task` inserts a new task (with its category and priority) and returns its UUID plus its permanent `task_number`
- `save_reminder` inserts a reminder row linked to a task, with a `scheduled_for` timestamp; `delete_unsent_reminders` removes a task's not-yet-fired reminders, used when its due date is edited so stale ones don't also fire
- `get_due_reminders` returns all reminders where `scheduled_for <= NOW()` and `sent = FALSE` and the parent task isn't complete, including a 30-second buffer to catch reminders that are about to fire
- `mark_reminder_sent` sets `sent = TRUE` on a reminder so it doesn't fire again
- `get_pending_tasks` returns incomplete tasks for a user, optionally filtered to one category, sorted by due date (default) or by priority
- `get_task_by_id` and `get_task_by_number` look up a single task by its internal UUID or its permanent user-facing number -- both can be scoped to an owning `telegram_user_id` so a guessed/leaked id can't act on someone else's task
- `update_task` changes only the fields passed to it (a field passed as `None` clears it, a field left out entirely is untouched) -- backs `/edit`, `/note`, `/priority`, and the AI agent's `edit_task` tool
- `delete_task` permanently removes a task row (and, via cascade, its reminders) -- the one place a task is actually erased, rather than just soft-completed
- `mark_task_complete` sets `is_complete = TRUE` on a task
- `mark_all_tasks_complete` is the bulk version: it sets `is_complete = TRUE` on every pending task for a user (optionally scoped to a category) and silences their unset reminders. It backs both `/clear` ("remove all") and "mark all as done," since they're the same operation, just worded differently at the call site
- `get_overdue_tasks_needing_reminder` and `mark_overdue_reminder_sent` back the overdue-nudge job; see Overdue nudges under `reminders.py` below
- `transfer_task` reassigns a task's `telegram_user_id` to a new owner and records who assigned it
- `ensure_user`, `set_display_name`, `get_user`, `find_user_by_name`, and `get_all_users` manage the `users` table backing task transfer and `/users` (display names, admin flags)
- `sync_admins_from_env` makes `users.is_admin` match `ADMIN_USER_ID` from `.env` on every bot startup. There's no bot command that grants admin, so it can only be changed by editing `.env` on the host and restarting

The `row_factory=dict_row` argument on cursors makes rows return as dictionaries (`row["description"]`) instead of tuples (`row[1]`), which makes the code much more readable.

---

### `reminders.py`

The reminder delivery logic covers two independent mechanisms sharing one file, since they both talk to Telegram and Claude the same way, via a shared `_post_to_telegram` retry helper.

#### `check_and_send_reminders()`

The function one of the two scheduler jobs calls every 60 seconds. Calls `get_due_reminders` to find anything that needs to fire, immediately marks each reminder as sent to prevent double-delivery if delivery takes a while, then delivers each one. Marking sent before delivery is intentional: it's safer to occasionally miss a reminder than to send duplicates. Each delivery is wrapped in its own `try/except` so one failing reminder (bad data, a Telegram error) can't stop the rest of the batch, or crash the scheduler itself, which would silently kill all future reminders until the bot restarts.

#### `send_telegram_message(...)`

The actual delivery. `scheduled_for` arrives as a timezone-aware UTC datetime straight from the database (everything in the DB is UTC) and is converted to `USER_TIMEZONE` here, once, before it's shown anywhere, so a reminder says "3:00 PM" in your own timezone (Mountain, by default) instead of a raw UTC timestamp. That localized string is what gets sent to Claude to generate a reminder message, falling back to a plain-text reminder with the same localized time if Claude errors rather than dropping the reminder entirely, then handed to `_post_to_telegram`. The prompt asks for a short, plain, businesslike message, with no emojis and no jokes, like a calendar notification rather than a chatty aside.

#### `_post_to_telegram(chat_id, text, task_id)`

Shared by both mechanisms in this file. POSTs to Telegram's `sendMessage` API with up to 3 retries and a short backoff (5s, 10s, 15s), a smaller, in-process version of what Celery's `bind=True, max_retries=3` gave us. Returns whether delivery actually succeeded, which matters differently to its two callers; see Overdue nudges below for why.

#### Recurring tasks

After sending a reminder, if the task has a `recurrence_rule` (`daily`, `weekly`, `weekdays`, `monthly`), `get_next_occurrence` calculates the next fire time and saves a new reminder row. This way recurring tasks perpetually reschedule themselves without any additional logic.

#### Overdue nudges

A separate, independent mechanism from everything above. `check_and_send_reminders` delivers reminders you scheduled in advance, each firing exactly once. `check_overdue_tasks` (the other scheduler job, also every 60 seconds) is open-ended instead: once a task's due time passes and it's still incomplete, it keeps getting nudged on a repeating schedule until you complete it, at a cadence set by `tasks.priority`: every hour for priority 1, every 6 hours for priority 2, every 12 for priority 3, every 24 for no priority at all. `get_overdue_tasks_needing_reminder` in `db.py` does the actual cadence math in SQL (a `CASE` on `priority` compared against `last_overdue_reminder_at`); this function just generates and sends the message for whatever it returns, via `send_overdue_reminder`, then calls `mark_overdue_reminder_sent`.

That last step is the one place this file makes the opposite tradeoff from `check_and_send_reminders`: it only records a nudge as sent after a successful delivery, not before. That's deliberate. An overdue nudge isn't one-time, so retrying costs nothing, since it just tries again on the next 60-second tick regardless, but recording a failed delivery as sent would mean waiting up to a full 24 hours before trying again, which is a worse failure mode than a few extra attempts.

---

## Database Schema

`schema.sql` is the source of truth, and it's safe to re-run against an existing database since every statement is `IF NOT EXISTS` or `ADD COLUMN IF NOT EXISTS`:

```bash
psql "$DATABASE_URL" -f schema.sql
```

### `tasks`
| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key, auto-generated, internal only |
| `task_number` | BIGINT | Permanent, user-facing task number (unique, never reused); this is what `/list` shows and every command/tool accepts |
| `telegram_user_id` | BIGINT | Telegram user ID of the current owner |
| `description` | TEXT | What the task is |
| `due_datetime` | TIMESTAMPTZ | When it's due (UTC) |
| `recurrence_rule` | TEXT | `daily`, `weekly`, `weekdays`, `monthly`, or null |
| `category` | TEXT | `personal`, `work`, `school`, or anything else; defaults to `personal` |
| `assigned_by` | BIGINT | Telegram ID of the admin who transferred this task, or null if the owner created it themselves |
| `notes` | TEXT | Free-text detail beyond `description`, set/viewed via `/note`; null until set |
| `priority` | SMALLINT | 1/2/3 or null; controls overdue-nudge frequency, see `reminders.py`'s Overdue nudges |
| `last_overdue_reminder_at` | TIMESTAMPTZ | When this task last got an overdue nudge; null means never nudged yet |
| `embedding` | vector(384) | Leftover from an earlier description-matching approach; no longer written or read by any code (see `agent.py`) -- left in place rather than migrated away, since dropping a column is a destructive change nothing requires |
| `is_complete` | BOOLEAN | Whether the task is done |
| `created_at` | TIMESTAMPTZ | When it was created |

### `reminders`
| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `task_id` | UUID | Foreign key to tasks |
| `scheduled_for` | TIMESTAMPTZ | When to send the reminder |
| `sent` | BOOLEAN | Whether it's been delivered |
| `created_at` | TIMESTAMPTZ | When it was created |

### `users`
| Column | Type | Description |
|---|---|---|
| `telegram_user_id` | BIGINT | Primary key |
| `display_name` | TEXT | Set via `/register`, used to look up transfer recipients by name |
| `is_admin` | BOOLEAN | Kept in sync with `ADMIN_USER_ID` in `.env` on every bot startup |
| `created_at` | TIMESTAMPTZ | When this user first messaged the bot |

---

## Deployment

Nothing about this stack requires EC2 specifically. It's a Linux box running one long-lived process plus Postgres, which any VM (a different cloud provider, a home server) or a PaaS with background-worker support (Railway, Fly.io, Render) can run. Because the bot now polls instead of using a webhook, it also doesn't need a public IP, an open port, or a reverse proxy in front of it. It just needs outbound internet access, so it can run on a machine behind a home router just as well as anywhere else. If you move hosts, run `schema.sql` against the new database and copy `.env` over, or better, rotate the secrets in it and start fresh (see the note in `.env.example`).

There are two ways to actually run it, and which one to use depends on whether the host already has Docker.

Docker Compose (`docker-compose.yml` + `Dockerfile`) is the simpler path on any host that already has Docker. `docker compose up -d --build` builds the bot image and starts it alongside a `pgvector/pgvector:pg16` Postgres container (the official Postgres image plus the pgvector extension precompiled in), with `schema.sql` applied automatically the first time (Postgres's image runs any `.sql` file it finds in `/docker-entrypoint-initdb.d/` once, the first time its data volume is created, so there's no manual migration step). Both services are set to `restart: unless-stopped`, and the bot's `depends_on` waits for a Postgres healthcheck rather than just "container started," so it doesn't lose the startup race and try to connect before Postgres is actually accepting connections. It needs `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` in `.env` matching whatever's embedded in `DATABASE_URL` (see the comments in `.env.example`), since Compose only understands the former for bootstrapping the Postgres container and doesn't parse `DATABASE_URL` itself.

systemd with a venv and a host-installed Postgres (`deploy/taskman.service`) is for a host without Docker, such as a bare cloud VM. Run `python3 -m venv venv && pip install -r requirements.txt`, install Postgres separately (plus the `postgresql-16-pgvector` package or equivalent for your distro, since the stock Postgres package doesn't include the extension), and the systemd unit then runs `venv/bin/python bot.py` directly with `Restart=always`.

The bot currently runs via the systemd path on AWS EC2 (Ubuntu). The Cloudflare Tunnel (`cloudflared.service`) it also used to need, along with the recurring annoyance of re-registering the webhook whenever the tunnel URL rotated on restart, are both gone now that the bot doesn't need a public URL at all.

### Automatic updates (Docker Compose hosts)

`deploy/auto-update.sh` pulls the latest code, redeploys with `docker compose up -d --build`, and re-applies `schema.sql`. Point cron at it on whatever host is running the Compose stack:

```bash
crontab -e
# then add:
*/15 * * * * bash /path/to/reminder-bot/deploy/auto-update.sh >> /path/to/reminder-bot/deploy/auto-update.log 2>&1
```

It's written to be safe to run on a timer without checking whether anything actually changed first. `git pull` and `docker compose up -d --build` are no-ops when there's nothing new, since Docker's layer cache and Compose's own change-detection handle that, and `schema.sql` is entirely `IF NOT EXISTS` or `ADD COLUMN IF NOT EXISTS`, so re-running it against an up-to-date database does nothing either. That last part is what makes this "automatic updates" rather than just "automatic code updates": a schema change, like a new column or table, rolls out the same way a code change does, with no separate manual migration step to remember.

This only works against a directory that's an actual `git clone` (it needs `.git` and a configured remote). If you deployed via the tarball-and-scp method instead of cloning, re-deploy with `git clone` first so `git pull` has something to pull from.

---

## Tech Stack

- Python 3.14
- python-telegram-bot for polling and the Telegram API
- Anthropic Claude API, via tool use, as a direct-database-access AI agent (`agent.py`) for every free-text/voice request, plus reminder message generation (`reminders.py`)
- PostgreSQL for persistent storage
- psycopg as the PostgreSQL driver, since psycopg2 doesn't support Python 3.14
- OpenAI Whisper API for voice message transcription, since Claude doesn't accept audio input
- APScheduler for in-process reminder scheduling (runs every 60s inside the bot process, no broker needed)
- AWS EC2 for hosting (not required, see Deployment)
- systemd for process supervision
- GitHub Actions for the CI pipeline
