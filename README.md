# TaskMan 🤖

A personal Telegram task manager and reminder bot powered by Claude AI. Send natural language messages like *"remind me to call mom tomorrow at 3pm"* and the bot parses, stores, and delivers the reminder at the right time. You can also just send a voice message instead of typing.

This README covers how the bot is built and deployed. For a plain-language walkthrough of how to actually use it in Telegram, see **[USAGE.md](USAGE.md)**.

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
     │  outbound HTTPS connection it opens itself -- no public URL needed
     ▼
bot.py  (python-telegram-bot, one process, one event loop)
     │
     ├── voice.py transcribes voice/audio messages first
     ├── calls claude.py to detect intent
     ├── calls claude.py to parse the task
     ├── calls embeddings.py to embed the task description
     ├── calls db.py to save task + reminders
     ├── replies to you in Telegram
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

**Why each component exists:**

| Component | Role |
|---|---|
| `bot.py` | Receives messages from Telegram, routes them, replies, owns the reminder scheduler |
| `claude.py` | Uses Claude API to understand natural language |
| `voice.py` | Transcribes voice/audio messages to text via OpenAI Whisper |
| `embeddings.py` | Embeds task descriptions via HuggingFace, for pgvector-based `/done` matching |
| `reminders.py` | Generates and delivers both due-time reminders and overdue nudges |
| `db.py` | All database reads and writes |
| APScheduler | In-process interval timer running both reminder jobs every 60s (see `post_init` in `bot.py`) |
| PostgreSQL + pgvector | Persists tasks, reminders, and users; pgvector adds similarity search on task embeddings |
| systemd | Keeps the process running and restarts it if it crashes |

This used to be two extra processes (a Celery worker and Celery Beat) coordinated through Redis, purely to run "check for due reminders every 60 seconds." APScheduler does the same thing as one scheduled job living inside `bot.py` itself — see the **Reminder scheduling** section below for the tradeoffs of that swap.

It also used to need a Cloudflare Tunnel forwarding a public webhook URL to the host. The bot now uses polling instead (see **How messages are received** below), so there's no public networking surface at all anymore.

---

## File-by-File Breakdown

### `bot.py`

The entry point. This file sets up the Telegram bot, registers all the handlers, and starts polling for updates.

**How messages are received:**

`main()` ends with `app.run_polling(...)`, which repeatedly asks Telegram's servers "any new updates for me?" over a connection the bot itself opens outbound. This replaced webhooks (Telegram POSTing to a public URL the bot exposes), which needed a Cloudflare Tunnel to give the host an HTTPS address in the first place. Polling needs no public URL, no exposed port, and no tunnel — the bot can run anywhere with outbound internet access. `post_init` also calls `bot.delete_webhook()` once at startup, since Telegram refuses `getUpdates` (what polling uses) while a webhook URL is still registered from the old setup.

The tradeoff: polling holds an open connection and checks in on an interval rather than being pushed to instantly, so there's a small amount of extra latency (typically well under a second) and it does slightly more idle network chatter than a webhook would. At personal-bot scale neither matters.

**Intent detection:**

Every plain text message goes through `handle_message`, which calls `parse_intent` in `claude.py` to figure out what the user wants to do. Based on the returned intent (`add_task`, `list_tasks`, `complete_task`, etc.) it routes to the right handler function. This means users don't need to remember slash commands — they just talk naturally.

**Authorization:**

`is_authorized` checks the sender's Telegram user ID against the comma-separated `ALLOWED_USER_ID` list from `.env`. This keeps the bot private — only IDs on that list can use it. `is_admin` does the same check against `ADMIN_USER_ID`, a subset of `ALLOWED_USER_ID` with the extra ability to transfer tasks (see Task transfer below).

**Inline keyboards:**

The `/done` flow uses Telegram inline keyboards (buttons attached to messages). When Claude fuzzy-matches a task, the bot sends a confirmation message with ✅ and ❌ buttons. The user taps one, Telegram fires a `callback_query`, and `done_callback` handles it. Each button carries a `data` string (the task UUID) so the handler knows which task to act on.

**Voice messages:**

`handle_voice` is registered for both `filters.VOICE` (voice notes recorded in Telegram) and `filters.AUDIO` (uploaded audio files). It downloads the audio bytes from Telegram, passes them to `transcribe_audio` in `voice.py`, echoes back what it heard (so a misheard word is obvious immediately), and then feeds the transcribed text into `process_text_message` — the same function typed messages go through. From that point on, voice and text messages are indistinguishable to the rest of the bot.

**Categories:**

Every task has a `category` (e.g. `personal`, `work`, `school`, or anything else you say). Claude infers it from the message when you add a task — "add a work task to..." or just context clues — and defaults to `personal` if nothing suggests otherwise. `/list` with no arguments groups your tasks by category; `/list work` shows just one category. The same filtering works in natural language too ("what work tasks do I have"). `/list` no longer shows each task's UUID — with numbered output and `/category` scoping (below) covering normal use, a raw ID was just clutter; `/done` and `/transfer` still accept a UUID prefix under the hood for anyone who has one from elsewhere.

**`/category` — scoping commands like `cd`:**

`/category work` sets a per-user "current category" (`CURRENT_CATEGORY`, an in-memory dict keyed by Telegram user ID — same non-persistence reasoning as `LAST_LISTED_TASK_IDS` below: it's a UX convenience tied to your ongoing conversation, not data worth surviving a restart). Once set, `/list`, `/done`, delete, `/transfer`, and `/clear` all default to that category instead of everything, and new tasks you add default into it too — like `cd`ing into a directory and having new files land there. Giving any of those commands an explicit category still overrides the ambient one for that one call without changing it, the same way `ls <path>` doesn't change your shell's cwd. `/category` alone shows what you're scoped to; `/category all` (or "none"/"clear"/"reset") goes back to seeing everything.

New tasks are the one place this isn't a soft default: while scoped to a category, `add_task` uses it unconditionally rather than letting Claude's own content-based guess override it (`get_current_category(...) or parsed.get("category", "personal")`). The alternative — trying to have Claude decide whether ambient context or message content should win per task — was fuzzy enough to be unpredictable; forcing the ambient category to win, full stop, mirrors real `cd` semantics and means if you want a task filed elsewhere while scoped in, you switch first (`/category personal`), same as you'd `cd` elsewhere before creating a file there.

**Referring to a task by its list number:**

`/list` numbers every task it shows (continuously across category groups, not restarting at 1 per group — restarting would make two different tasks both display as "1." in the same message, which is ambiguous). `list_tasks` records that exact ordering in `LAST_LISTED_TASK_IDS`, an in-memory dict keyed by Telegram user ID, right before it sends the message.

That's what lets you say "mark number 1 as done", "delete item 2", or "send #3 to Sarah" instead of describing the task or copy-pasting a UUID — `parse_intent` returns a `list_position` field when it detects this pattern, and `resolve_list_position` looks it up against what that user was last shown, skipping the vector search / `fuzzy_match` Claude call entirely. If the task at that position was completed, deleted, or transferred away since the list was shown, the lookup against the *current* pending tasks simply comes up empty and the bot says so — it never acts on stale data. `/done <number>` and `/transfer <number> <recipient>` support the same shorthand for consistency with the natural-language flow. This state is deliberately in-memory only (not the database) since it's tied to one specific message the bot already sent, not data worth surviving a restart.

**Referring to a task by description -- vector search first, Claude as fallback:**

When you *don't* give a number, `resolve_task_by_description` in `bot.py` is what turns "mark the dentist one as done" into an actual task. It tries two things in order:

1. **pgvector similarity search** (fast, no LLM call): `embed_text` in `embeddings.py` turns your message into a 384-dimension vector via HuggingFace's hosted Inference API (`sentence-transformers/all-MiniLM-L6-v2`), and `find_similar_task` in `db.py` asks Postgres for the pending task whose *stored* embedding (computed the same way, at creation time, from the task's `description`) is closest to it, using pgvector's `<=>` cosine-distance operator. If the closest match's distance is under `VECTOR_MATCH_THRESHOLD` (0.5, a starting guess -- see the TODO about tuning it once there's real usage to judge against), that task is used directly.
2. **`fuzzy_match` (the original Claude-based approach)** as a fallback, used whenever step 1 isn't confident enough, or embeddings aren't available at all (no `HUGGINGFACE_API_TOKEN` configured, the API call failed, or the task predates this feature and was never embedded). A pure vector search only knows topical similarity -- it has no way to reason about something like "the one due today" -- so a query the vector search can't confidently resolve gets a second chance from an LLM that can actually read the full task list and the phrasing, rather than being rejected outright.

This is deliberately "vector-first, LLM-fallback" rather than a full replacement of `fuzzy_match`: cheaper and faster for the common case, without losing the LLM's ability to handle trickier phrasing. Every call site that used to call `fuzzy_match` directly (`process_task`, `handle_transfer`, `transfer_command`) now goes through `resolve_task_by_description` instead.

**Notes and priority (`/note`, `/priority`):**

Two small, related features, both following the same pattern as everything above: a slash command plus a natural-language equivalent (`add_note`/`view_note`/`set_priority` intents), both resolving the target task the same way (`_resolve_task_for`, by list position or by `resolve_task_by_description`).

- `/note <number> [text]` sets a free-text note (`tasks.notes`) beyond the short description, or shows the current one if you omit the text. The slash command only accepts a *number*, not a free-text task description -- there's no reliable way to tell where a typed description ends and the note text begins (e.g. `/note dentist appointment remember insurance card`), so a free-text reference only works through natural language, where Claude does that splitting for you as structured JSON.
- `/priority <number> <1|2|3>` sets `tasks.priority`, which controls how often overdue nudges repeat -- see **Overdue nudges** in the `reminders.py` section below. `parse_task` only ever sets a priority when the message explicitly signals urgency ("priority 1", "urgent", "no rush") -- unlike category, it's never guessed from the task's subject matter, since guessing wrong on urgency felt like exactly the kind of thing that should require you to actually say it.

**Bulk actions -- "mark all as done" / "remove all":**

`complete_all_tasks` (new) and `clear_tasks` are effectively the same operation under the hood — `mark_all_tasks_complete` in `db.py`, which soft-completes every pending task for a user (optionally scoped to one category) and silences their unset reminders, no hard deletes. `handle_complete_all` and `handle_clear` in `bot.py` are thin, near-identical wrappers around it that just differ in wording ("Marked N complete" vs "Cleared N") and confirm-button `callback_data` prefix. Both show a Yes/Cancel confirmation first, same reasoning as the original single-category `/clear` always had: it's bulk, and there's no "undo" command. Both also fall back to your `/category` context when no explicit category is given, same as everything else above.

**Task transfer (admin only):**

Admins (Telegram IDs listed in `ADMIN_USER_ID`) can hand a task off to another user, either by typing `/transfer <task id, description, or list number> <name or telegram id>` or just saying something like "send the grocery task to Sarah" or "send #2 to Sarah". Transferring a task changes its `telegram_user_id` in the database — since reminders are linked to the task (not the user) and always look up the current owner when they fire, an already-scheduled reminder will just go to the new owner automatically. The recipient needs to be in `ALLOWED_USER_ID` (so they're allowed to use the bot at all) and needs to have messaged the bot at least once already (a Telegram platform requirement — bots can't DM someone who hasn't started a chat with them first).

Non-admin users can run `/register <name>` to set a friendly display name, so admins can `/transfer` to them by name instead of hunting down their raw Telegram ID. `/whoami` shows your own ID, name, and role. `/users` (admin only) lists everyone registered — name, admin status, and Telegram ID — so an admin can see who's available to send a task to without already knowing their name or ID.

**Reminder scheduling:**

`main()` builds the `Application` with `.post_init(post_init)`. `post_init` is a hook python-telegram-bot calls once its event loop is actually running, but before it starts processing updates — the right moment to start an `AsyncIOScheduler` (from the `apscheduler` package), since that scheduler needs a running asyncio loop to attach jobs to, and no loop exists yet back in the synchronous part of `main()`.

The scheduler is given one job: call `check_and_send_reminders` (from `reminders.py`) every 60 seconds via an `IntervalTrigger`. That's the entire reminder pipeline now — no Redis, no separate worker or beat process. The scheduler object is stashed in `app.bot_data["scheduler"]` only so nothing garbage-collects it; APScheduler doesn't keep a reference alive on its own.

The tradeoff of moving this in-process: reminder delivery now shares the bot's single event loop with message handling, instead of running in a fully separate process. A slow Telegram API call during reminder delivery can briefly delay the bot from responding to an incoming message (and vice versa) — a non-issue at personal-bot volume, but worth knowing if this ever needs to scale to many more users.

---

### `claude.py`

All Claude API interactions live here. There are three main functions:

**`parse_intent(user_message)`**

Sends the user's message to Claude with a prompt that asks it to classify the intent and return JSON. Claude returns one of: `add_task`, `list_tasks`, `complete_task`, `delete_task`, `clear_tasks`, `complete_all_tasks`, `transfer_task`, `add_note`, `view_note`, `set_priority`, or `unknown`. `list_tasks`, `clear_tasks`, and `complete_all_tasks` may all return a `category` field if you scoped the request to one; `transfer_task` returns a `recipient_name`; `add_note` returns a `note_text`; `set_priority` returns a `priority` (1/2/3); `complete_task`/`delete_task`/`transfer_task`/`add_note`/`view_note`/`set_priority` may return a `list_position` (converting words like "first"/"second" to 1/2) when you referred to the task by its position in your last `/list` instead of describing it. `complete_all_tasks` is distinct from `complete_task` specifically so "mark all as done" and "mark the dentist one as done" route to different handlers instead of one intent trying to mean both. This is what makes the bot feel conversational rather than command-driven.

**`parse_task(user_message, timezone)`**

Takes a natural language task description and asks Claude to extract structured data from it — the task description, due date/time in UTC, recurrence rule, reminder offsets, category, and priority. Claude returns JSON which gets parsed and saved to the database. Priority is only ever set from an explicit signal ("priority 1", "urgent", "no rush") and stays `null` otherwise — see the **Notes and priority** section under `bot.py` above for why that's treated differently from category, which Claude is allowed to guess from context.

The system prompts use `{{` and `}}` to escape literal JSON braces — Python's `.format()` method interprets `{}` as template variables, so any JSON examples in the prompt need doubled braces to be treated as literal characters.

**`fuzzy_match(user_message, tasks)`**

Used by the complete/delete/transfer flows. Sends the user's message alongside their full task list and asks Claude to return the UUID of the task that best matches. This lets users say "mark the dentist one as done" instead of copy-pasting a UUID.

---

### `voice.py`

Turns Telegram voice/audio messages into text so they can flow through the same pipeline as typed messages.

Claude's Messages API doesn't accept audio input, so this is the one place the bot talks to a model other than Claude: `transcribe_audio` sends the raw audio bytes to OpenAI's `whisper-1` transcription endpoint and returns plain text. This needs its own `OPENAI_API_KEY` in `.env`, separate from `ANTHROPIC_API_KEY`.

If you'd rather not depend on OpenAI (cost, or wanting everything self-hosted), the alternative is running Whisper locally via `faster-whisper` — same interface (bytes in, text out), no per-request API cost, but it needs a chunk of CPU (or a GPU) on whatever machine runs the bot. Worth switching to if you're already paying for a beefier host than a personal bot really needs.

---

### `embeddings.py`

Turns text into a semantic embedding vector, for the pgvector-based task matching described under **Referring to a task by description** above.

`embed_text` calls HuggingFace's hosted Inference API (`InferenceClient.feature_extraction`) using `sentence-transformers/all-MiniLM-L6-v2`, a small, well-established sentence-embedding model that outputs 384-dimension vectors (hence `vector(384)` in `schema.sql`). Chosen over an OpenAI embeddings model specifically to avoid adding a *third* paid API vendor alongside Anthropic and OpenAI — HuggingFace's Inference API has a free tier, and this bot's embedding volume (one call per task added, one per match attempt) is tiny.

Two things worth knowing:

- **Returns `None` on any failure** (no `HUGGINGFACE_API_TOKEN` configured, a network error, a HuggingFace outage) rather than raising. Every caller treats a missing embedding the same as "no confident vector match" and falls back to `fuzzy_match` — a HuggingFace problem degrades this one feature rather than breaking task matching entirely.
- **Handles two possible response shapes.** `feature_extraction` normally returns one pooled vector per input, but depending on the model/endpoint it can come back as one vector *per token* instead. `embed_text` mean-pools across tokens in that case so callers always get a single flat vector either way. This was written without a live HuggingFace token to test against, so it's worth double-checking the actual response shape once real usage exists.

---

### `db.py`

All database operations. Uses `psycopg` (the modern PostgreSQL driver — `psycopg2` doesn't support Python 3.14) to connect to PostgreSQL.

**Connection pattern:**

Every function opens a connection, does its work, and closes it in a `finally` block so the connection is always released even if an error occurs. This is intentionally simple — a connection pool would be more efficient at scale but adds complexity that isn't needed for a personal bot. `get_connection` also calls `pgvector.psycopg.register_vector` on every new connection, which teaches psycopg how to convert a Python list of floats to/from Postgres's `vector` type automatically -- without it, `save_task`'s `embedding` param and `find_similar_task`'s results would need manual vector-literal formatting instead of just passing/receiving plain lists.

**Key functions:**

- `save_task` — inserts a new task (with its category, priority, and embedding) and returns its UUID
- `save_reminder` — inserts a reminder row linked to a task, with a `scheduled_for` timestamp
- `get_due_reminders` — returns all reminders where `scheduled_for <= NOW()` and `sent = FALSE` and the parent task isn't complete. Includes a 30-second buffer to catch reminders that are about to fire.
- `mark_reminder_sent` — sets `sent = TRUE` on a reminder so it doesn't fire again
- `get_pending_tasks` — returns incomplete tasks for a user, optionally filtered to one category
- `find_similar_task` — pgvector's `<=>` cosine-distance search for the pending task closest in meaning to a given embedding; see **Referring to a task by description** under `bot.py`
- `mark_task_complete` — sets `is_complete = TRUE` on a task
- `mark_all_tasks_complete` — the bulk version: sets `is_complete = TRUE` on every pending task for a user (optionally scoped to a category) and silences their unset reminders. Backs both `/clear` ("remove all") and "mark all as done" — they're the same operation, just worded differently at the call site
- `set_task_notes` / `set_task_priority` — back `/note` and `/priority`
- `get_overdue_tasks_needing_reminder` / `mark_overdue_reminder_sent` — back the overdue-nudge job; see **Overdue nudges** under `reminders.py` below
- `transfer_task` — reassigns a task's `telegram_user_id` to a new owner and records who assigned it
- `ensure_user` / `set_display_name` / `get_user` / `find_user_by_name` / `get_all_users` — manage the `users` table backing task transfer and `/users` (display names, admin flags)
- `sync_admins_from_env` — makes `users.is_admin` match `ADMIN_USER_ID` from `.env` on every bot startup; there's no bot command that grants admin, so it can only be changed by editing `.env` on the host and restarting

The `row_factory=dict_row` argument on cursors makes rows return as dictionaries (`row["description"]`) instead of tuples (`row[1]`), which makes the code much more readable.

---

### `reminders.py`

The reminder delivery logic — two independent mechanisms sharing one file since they both talk to Telegram/Claude the same way, via a shared `_post_to_telegram` retry helper.

**`check_and_send_reminders()`**

The function one of the two scheduler jobs calls every 60 seconds. Calls `get_due_reminders` to find anything that needs to fire, immediately marks each reminder as sent (to prevent double-delivery if delivery takes a while), then delivers each one. Marking sent before delivery is intentional — it's safer to occasionally miss a reminder than to send duplicates. Each delivery is wrapped in its own `try/except` so one failing reminder (bad data, a Telegram error) can't stop the rest of the batch — or crash the scheduler itself, which would silently kill all future reminders until the bot restarts.

**`send_telegram_message(...)`**

The actual delivery. `scheduled_for` arrives as a timezone-aware UTC datetime straight from the database (everything in the DB is UTC) and is converted to `USER_TIMEZONE` here, once, before it's shown anywhere — so a reminder says "3:00 PM" in your own timezone (Mountain, by default) instead of a raw UTC timestamp. That localized string is what gets sent to Claude to generate a reminder message (falling back to a plain-text reminder with the same localized time if Claude errors, rather than dropping the reminder entirely), then handed to `_post_to_telegram`. The prompt asks for a short, plain, businesslike message — no emojis, no jokes — like a calendar notification rather than a chatty aside.

**`_post_to_telegram(chat_id, text, task_id)`**

Shared by both mechanisms in this file: POSTs to Telegram's `sendMessage` API with up to 3 retries and a short backoff (5s, 10s, 15s) — a smaller, in-process version of what Celery's `bind=True, max_retries=3` gave us. Returns whether delivery actually succeeded, which matters differently to its two callers — see **Overdue nudges** below for why.

**Recurring tasks:**

After sending a reminder, if the task has a `recurrence_rule` (`daily`, `weekly`, `weekdays`, `monthly`), `get_next_occurrence` calculates the next fire time and saves a new reminder row. This way recurring tasks perpetually reschedule themselves without any additional logic.

**Overdue nudges:**

A separate, independent mechanism from everything above -- `check_and_send_reminders` delivers reminders you scheduled in advance, each firing exactly once. `check_overdue_tasks` (the other scheduler job, also every 60 seconds) is open-ended instead: once a task's due time passes and it's still incomplete, it keeps getting nudged on a repeating schedule until you complete it, at a cadence set by `tasks.priority` — every hour for priority 1, every 6 hours for priority 2, every 12 for priority 3, every 24 for no priority at all. `get_overdue_tasks_needing_reminder` in `db.py` does the actual cadence math in SQL (a `CASE` on `priority` compared against `last_overdue_reminder_at`); this function just generates and sends the message for whatever it returns, via `send_overdue_reminder`, then calls `mark_overdue_reminder_sent`.

That last step is the one place this file makes the *opposite* tradeoff from `check_and_send_reminders`: it only records a nudge as sent *after* a successful delivery, not before. That's deliberate -- an overdue nudge isn't one-time, so retrying costs nothing (it just tries again on the next 60-second tick regardless), but recording a *failed* delivery as sent would mean waiting up to a full 24 hours before trying again, which is a worse failure mode than a few extra attempts.

---

## Database Schema

`schema.sql` is the source of truth (safe to re-run against an existing database — every statement is `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`):

```bash
psql "$DATABASE_URL" -f schema.sql
```

**`tasks`**
| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key, auto-generated |
| `telegram_user_id` | BIGINT | Telegram user ID of the current owner |
| `description` | TEXT | What the task is |
| `due_datetime` | TIMESTAMPTZ | When it's due (UTC) |
| `recurrence_rule` | TEXT | `daily`, `weekly`, `weekdays`, `monthly`, or null |
| `category` | TEXT | `personal`, `work`, `school`, or anything else — defaults to `personal` |
| `assigned_by` | BIGINT | Telegram ID of the admin who transferred this task, or null if the owner created it themselves |
| `notes` | TEXT | Free-text detail beyond `description`, set/viewed via `/note` — null until set |
| `priority` | SMALLINT | 1/2/3 or null; controls overdue-nudge frequency — see `reminders.py`'s Overdue nudges |
| `last_overdue_reminder_at` | TIMESTAMPTZ | When this task last got an overdue nudge; null means never nudged yet |
| `embedding` | vector(384) | Sentence embedding of `description`, for pgvector similarity search — null if never computed (no HuggingFace token, or predates this feature) |
| `is_complete` | BOOLEAN | Whether the task is done |
| `created_at` | TIMESTAMPTZ | When it was created |

**`reminders`**
| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key |
| `task_id` | UUID | Foreign key to tasks |
| `scheduled_for` | TIMESTAMPTZ | When to send the reminder |
| `sent` | BOOLEAN | Whether it's been delivered |
| `created_at` | TIMESTAMPTZ | When it was created |

**`users`**
| Column | Type | Description |
|---|---|---|
| `telegram_user_id` | BIGINT | Primary key |
| `display_name` | TEXT | Set via `/register`, used to look up transfer recipients by name |
| `is_admin` | BOOLEAN | Kept in sync with `ADMIN_USER_ID` in `.env` on every bot startup |
| `created_at` | TIMESTAMPTZ | When this user first messaged the bot |

---

## Deployment

Nothing about this stack requires EC2 specifically — it's a Linux box running one long-lived process plus Postgres, which any VM (a different cloud provider, a home server) or a PaaS with background-worker support (Railway, Fly.io, Render) can run. Because the bot now polls instead of using a webhook, it also doesn't need a public IP, an open port, or a reverse proxy in front of it — it just needs outbound internet access, so it can run literally anywhere, including a machine behind a home router. If you move hosts, run `schema.sql` against the new database and copy `.env` over (or better, rotate the secrets in it and start fresh — see the note in `.env.example`).

There are two ways to actually run it, and which one to use depends on whether the host already has Docker:

**Docker Compose** (`docker-compose.yml` + `Dockerfile`) — the simpler path on any host that already has Docker. `docker compose up -d --build` builds the bot image and starts it alongside a `pgvector/pgvector:pg16` Postgres container (the official Postgres image plus the pgvector extension precompiled in), with `schema.sql` applied automatically the first time (Postgres's image runs any `.sql` file it finds in `/docker-entrypoint-initdb.d/` once, the first time its data volume is created — no manual migration step). Both services are set to `restart: unless-stopped`, and the bot's `depends_on` waits for a Postgres healthcheck rather than just "container started", so it doesn't lose the startup race and try to connect before Postgres is actually accepting connections. Needs `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` in `.env` matching whatever's embedded in `DATABASE_URL` (see the comments in `.env.example`) — Compose only understands the former for bootstrapping the Postgres container, and doesn't parse `DATABASE_URL` itself.

**systemd + a venv + host-installed Postgres** (`deploy/taskman.service`) — for a host without Docker, e.g. a bare cloud VM. `python3 -m venv venv && pip install -r requirements.txt`, install Postgres separately (plus the `postgresql-16-pgvector` package or equivalent for your distro — the stock Postgres package doesn't include the extension), then the systemd unit runs `venv/bin/python bot.py` directly with `Restart=always`.

Currently runs via the systemd path on AWS EC2 (Ubuntu). The Cloudflare Tunnel (`cloudflared.service`) it also used to need, and the recurring "the tunnel URL rotates on every restart, re-register the webhook" annoyance that came with it, are both gone now that the bot doesn't need a public URL at all.

### Automatic updates (Docker Compose hosts)

`deploy/auto-update.sh` pulls the latest code, redeploys with `docker compose up -d --build`, and re-applies `schema.sql`. Point cron at it on whatever host is running the Compose stack:

```bash
crontab -e
# then add:
*/15 * * * * bash /path/to/reminder-bot/deploy/auto-update.sh >> /path/to/reminder-bot/deploy/auto-update.log 2>&1
```

It's written to be safe to run on a timer without checking "did anything actually change" first: `git pull` and `docker compose up -d --build` are no-ops when there's nothing new (Docker's layer cache and Compose's own change-detection handle that), and `schema.sql` is entirely `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`, so re-running it against an up-to-date database does nothing either. That last part is what makes this "automatic updates" and not just "automatic code updates" — a schema change (new column, new table) rolls out the same way a code change does, with no separate manual migration step to remember.

This only works against a directory that's an actual `git clone` (needs `.git` and a configured remote) — if you deployed via the tarball-and-scp method instead of cloning, re-deploy with `git clone` first so `git pull` has something to pull from.

---

## Tech Stack

- **Python 3.14**
- **python-telegram-bot** — polling and the Telegram API
- **Anthropic Claude API** — intent detection, task parsing, reminder message generation
- **PostgreSQL + pgvector** — persistent storage, plus similarity search on task embeddings for `/done` matching
- **psycopg** — PostgreSQL driver (psycopg2 doesn't support Python 3.14)
- **OpenAI Whisper API** — voice message transcription (Claude doesn't accept audio input)
- **HuggingFace Inference API** — task-description embeddings (`sentence-transformers/all-MiniLM-L6-v2`), for vector-based task matching
- **APScheduler** — in-process reminder scheduling (runs every 60s inside the bot process, no broker needed)
- **AWS EC2** — hosting (not required — see Deployment)
- **systemd** — process supervision
- **GitHub Actions** — CI pipeline