"""
Reminder delivery logic.

This used to be two Celery tasks (check_and_send_reminders, send_telegram_message)
run by a separate Celery worker process, fed by a separate Celery Beat process,
coordinated through Redis. Now it's just plain async functions that bot.py's
APScheduler job calls directly, in the same process as the bot -- see the
post_init() hook in bot.py for where check_and_send_reminders() gets scheduled
to run every 60 seconds.
"""
import os
import asyncio
import logging
from datetime import timedelta, datetime
import calendar
import anthropic
import pytz
import requests
from dotenv import load_dotenv
from db import get_due_reminders, mark_reminder_sent, save_reminder

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "UTC")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """
You write reminder notifications for a task management bot. You will receive a task
description and its due date. Write a short message stating what the task is and when
it's due (or how much time is left).

Rules:
- Plain, professional, businesslike tone -- write it like a calendar or scheduling
  notification, not a casual chat message.
- No emojis.
- No jokes, wordplay, or forced enthusiasm.
- One or two sentences. Do not pad it out.
"""

async def check_and_send_reminders():
    """
    The scheduled job itself. Runs every 60 seconds (see bot.py). Finds
    anything due, marks it sent *before* delivery (same reasoning as before:
    better to occasionally miss a reminder on a crash than double-send one),
    then delivers each.
    """
    try:
        due_reminders = get_due_reminders()
    except Exception:
        # A DB hiccup here shouldn't crash the scheduler -- it'll just try
        # again on the next 60-second tick.
        logger.exception("Failed to fetch due reminders")
        return

    for reminder in due_reminders:
        mark_reminder_sent(str(reminder["reminder_id"]))

        try:
            await send_telegram_message(
                chat_id=reminder["telegram_user_id"],
                task_description=reminder["description"],
                scheduled_for=reminder["scheduled_for"],
                task_id=str(reminder["task_id"]),
                recurrence_rule=reminder.get("recurrence_rule")
            )
        except Exception:
            # One bad reminder (e.g. Claude or Telegram erroring) shouldn't
            # stop the rest of the batch from being delivered.
            logger.exception(f"Failed to deliver reminder for task {reminder['task_id']}")

async def send_telegram_message(chat_id: int, task_description: str, scheduled_for: datetime, task_id: str, recurrence_rule: str = None):
    """
    Generates a reminder message with Claude and sends it via the Telegram
    HTTP API directly (python-telegram-bot's Bot object belongs to bot.py's
    Application, so this talks to Telegram's REST API the same simple way
    the original Celery task did, rather than threading a Bot instance
    through here).

    scheduled_for arrives as a timezone-aware UTC datetime straight from the
    database (everything in the DB and everywhere else in this app is UTC).
    It's converted to USER_TIMEZONE here, once, before it's shown anywhere,
    so a reminder says "3:00 PM" in your own timezone instead of a raw UTC
    timestamp.

    Retries up to 3 times on a network error, waiting longer between each
    attempt. This is a smaller, in-process version of what Celery's
    `bind=True, max_retries=3` gave us -- shorter backoff (seconds, not
    minutes) since a slow retry here blocks this one reminder's delivery
    rather than tying up a whole separate worker process.
    """
    local_due = scheduled_for.astimezone(pytz.timezone(USER_TIMEZONE))
    local_due_str = local_due.strftime("%A, %b %d at %I:%M %p %Z")

    try:
        response_obj = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"Task: {task_description}\nDue: {local_due_str}"}
            ]
        )
        message_text = response_obj.content[0].text
    except Exception:
        # If Claude is down, still send *something* rather than silently
        # dropping the reminder.
        logger.exception(f"Claude failed to generate reminder text for task {task_id}, falling back to plain text")
        message_text = f"Reminder: {task_description} (due {local_due_str})"

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            response = requests.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message_text,
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            if attempt == max_attempts - 1:
                logger.error(f"Giving up sending reminder for task {task_id} to {chat_id}: {exc}")
                return
            wait_seconds = 5 * (attempt + 1)
            logger.warning(f"Telegram send failed (attempt {attempt + 1}/{max_attempts}), retrying in {wait_seconds}s: {exc}")
            await asyncio.sleep(wait_seconds)

    if recurrence_rule:
        # scheduled_for is already a datetime -- no need to round-trip it
        # through a string like the old Celery-task version had to.
        next_time = get_next_occurrence(scheduled_for, recurrence_rule)
        if next_time:
            save_reminder(task_id, next_time)

def get_next_occurrence(current_time, recurrence_rule: str):
    if recurrence_rule == "daily":
        return current_time + timedelta(days=1)
    elif recurrence_rule == "weekly":
        return current_time + timedelta(weeks=1)
    elif recurrence_rule == "monthly":
        month = current_time.month % 12 + 1
        year = current_time.year + (1 if current_time.month == 12 else 0)
        max_day = calendar.monthrange(year, month)[1]
        day = min(current_time.day, max_day)
        return current_time.replace(year=year, month=month, day=day)
    elif recurrence_rule == "weekdays":
        next_time = current_time + timedelta(days=1)
        while next_time.weekday() >= 5:
            next_time += timedelta(days=1)
        return next_time
    return None
