# TaskMan 🤖

A personal Telegram task manager and reminder bot powered by Claude AI. Send natural language messages like *"remind me to call mom tomorrow at 3pm"* and the bot parses, stores, and delivers the reminder at the right time.

---

## Architecture Overview

```
Telegram App
     │
     │  you send a message
     ▼
Telegram Servers
     │
     │  POST to your webhook URL
     ▼
Cloudflare Tunnel  ──────────────────────────────────────────
     │                                                       │
     │  forwards HTTPS traffic to your EC2 instance         │
     ▼                                                       │
bot.py  (python-telegram-bot)                               │
     │                                                       │
     ├── calls claude.py to detect intent                   │
     ├── calls claude.py to parse the task                  │
     ├── calls db.py to save task + reminders               │
     └── replies to you in Telegram                         │
                                                            │
celery_app.py + tasks.py  (runs separately on EC2)         │
     │                                                       │
     ├── Celery Beat wakes up every minute                  │
     ├── checks db.py for due reminders                     │
     ├── pushes jobs into Redis queue                       │
     └── Celery Worker picks up jobs, calls Telegram API ───┘
```

**Why each component exists:**

| Component | Role |
|---|---|
| `bot.py` | Receives messages from Telegram, routes them, replies |
| `claude.py` | Uses Claude API to understand natural language |
| `db.py` | All database reads and writes |
| `celery_app.py` | Configures Celery and the beat schedule |
| `tasks.py` | The actual reminder delivery logic |
| Redis | Message queue between Celery Beat and Celery Worker |
| PostgreSQL | Persists tasks and reminders across restarts |
| Cloudflare Tunnel | Gives EC2 a public HTTPS URL without a domain |
| systemd | Keeps all processes running and restarts them if they crash |

---

## File-by-File Breakdown

### `bot.py`

The entry point. This file sets up the Telegram bot, registers all the handlers, and starts the webhook server.

**How messages are received:**

python-telegram-bot uses webhooks — Telegram sends a POST request to your server every time you send a message, rather than your bot constantly polling Telegram asking "any new messages?". The `run_webhook` call at the bottom starts a lightweight HTTP server that listens for these POST requests.

**Intent detection:**

Every plain text message goes through `handle_message`, which calls `parse_intent` in `claude.py` to figure out what the user wants to do. Based on the returned intent (`add_task`, `list_tasks`, `complete_task`, etc.) it routes to the right handler function. This means users don't need to remember slash commands — they just talk naturally.

**Authorization:**

`is_authorized` checks the sender's Telegram user ID against `ALLOWED_USER_IDS` from `.env`. This keeps the bot private — only your ID can use it.

**Inline keyboards:**

The `/done` flow uses Telegram inline keyboards (buttons attached to messages). When Claude fuzzy-matches a task, the bot sends a confirmation message with ✅ and ❌ buttons. The user taps one, Telegram fires a `callback_query`, and `done_callback` handles it. Each button carries a `data` string (the task UUID) so the handler knows which task to act on.

---

### `claude.py`

All Claude API interactions live here. There are two main functions:

**`parse_intent(user_message)`**

Sends the user's message to Claude with a prompt that asks it to classify the intent and return JSON. Claude returns one of: `add_task`, `list_tasks`, `complete_task`, `delete_task`, or `unknown`. This is what makes the bot feel conversational rather than command-driven.

**`parse_task(user_message, timezone)`**

Takes a natural language task description and asks Claude to extract structured data from it — the task description, due date/time in UTC, recurrence rule, and reminder offsets. Claude returns JSON which gets parsed and saved to the database.

The system prompts use `{{` and `}}` to escape literal JSON braces — Python's `.format()` method interprets `{}` as template variables, so any JSON examples in the prompt need doubled braces to be treated as literal characters.

**`match_task_with_claude(user_message, tasks)`**

Used by the complete task flow. Sends the user's message alongside their full task list and asks Claude to return the UUID of the task that best matches. This lets users say "mark the dentist one as done" instead of copy-pasting a UUID.

---

### `db.py`

All database operations. Uses `psycopg` (the modern PostgreSQL driver — `psycopg2` doesn't support Python 3.14) to connect to PostgreSQL.

**Connection pattern:**

Every function opens a connection, does its work, and closes it in a `finally` block so the connection is always released even if an error occurs. This is intentionally simple — a connection pool would be more efficient at scale but adds complexity that isn't needed for a personal bot.

**Key functions:**

- `save_task` — inserts a new task and returns its UUID
- `save_reminder` — inserts a reminder row linked to a task, with a `scheduled_for` timestamp
- `get_due_reminders` — returns all reminders where `scheduled_for <= NOW()` and `sent = FALSE` and the parent task isn't complete. Includes a 30-second buffer to catch reminders that are about to fire.
- `mark_reminder_sent` — sets `sent = TRUE` on a reminder so it doesn't fire again
- `get_pending_tasks` — returns all incomplete tasks for a user, ordered by due date
- `mark_task_complete` — sets `is_complete = TRUE` on a task

The `row_factory=dict_row` argument on cursors makes rows return as dictionaries (`row["description"]`) instead of tuples (`row[1]`), which makes the code much more readable.

---

### `celery_app.py`

Configures Celery — the distributed task queue that handles reminder scheduling.

**Broker vs backend:**

Redis is used as both the broker and the backend. The *broker* is where Celery puts jobs that need to be done (Beat drops a job in, Worker picks it up). The *backend* is where Celery stores the results of completed jobs.

**Beat schedule:**

Celery Beat is configured to run `check_and_send_reminders` every minute via a crontab schedule. This is the heartbeat of the reminder system — every 60 seconds it wakes up, checks the database for anything due, and queues delivery jobs.

---

### `tasks.py`

The two Celery tasks that power reminder delivery.

**`check_and_send_reminders`**

Runs every minute via Celery Beat. Calls `get_due_reminders` to find anything that needs to fire, immediately marks each reminder as sent (to prevent double-delivery if the task takes a while), then queues a `send_telegram_message` job for each one. Marking sent before delivery is intentional — it's safer to occasionally miss a reminder than to send duplicates.

**`send_telegram_message`**

The actual delivery task. Takes the task description and due time, sends them to Claude to generate a creative reminder message, then POSTs that message to the Telegram API. Uses `bind=True` and `max_retries=3` so if the Telegram API is down, Celery will automatically retry up to 3 times with increasing delays.

**Recurring tasks:**

After sending a reminder, if the task has a `recurrence_rule` (`daily`, `weekly`, `weekdays`, `monthly`), `get_next_occurrence` calculates the next fire time and saves a new reminder row. This way recurring tasks perpetually reschedule themselves without any additional logic.

---

## Database Schema

**`tasks`**
| Column | Type | Description |
|---|---|---|
| `id` | UUID | Primary key, auto-generated |
| `telegram_user_id` | BIGINT | Telegram user ID of the owner |
| `description` | TEXT | What the task is |
| `due_datetime` | TIMESTAMPTZ | When it's due (UTC) |
| `recurrence_rule` | TEXT | `daily`, `weekly`, `weekdays`, `monthly`, or null |
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

---

## Deployment

Runs on AWS EC2 (Ubuntu) with four systemd services:

- `taskman.service` — the bot process
- `celery-worker.service` — processes reminder delivery jobs
- `celery-beat.service` — fires the every-minute scheduler
- `cloudflared.service` — maintains the Cloudflare tunnel

All four are set to `Restart=always` so they recover automatically from crashes. Environment variables are loaded from `.env` via `EnvironmentFile` in each service file.

**One operational note:** Cloudflare Quick Tunnel generates a new random URL on every restart. When that happens, the Telegram webhook needs to be re-registered with the new URL:

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<new-url>.trycloudflare.com/webhook"
```

---

## Tech Stack

- **Python 3.14**
- **python-telegram-bot** — webhook handling and Telegram API
- **Anthropic Claude API** — intent detection, task parsing, reminder message generation
- **PostgreSQL** — persistent storage
- **psycopg** — PostgreSQL driver (psycopg2 doesn't support Python 3.14)
- **Celery + Redis** — distributed task scheduling
- **AWS EC2** — hosting
- **Cloudflare Tunnel** — HTTPS without a domain or static IP
- **systemd** — process supervision
- **GitHub Actions** — CI pipeline