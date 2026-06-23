import os
import logging
from datetime import datetime
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from claude import parse_task
from db import save_task, save_reminder, get_pending_tasks, mark_task_complete

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "UTC")

def is_authorized(update: Update) -> bool:
    return update.effective_user.id == ALLOWED_USER_ID

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    
    user_message = update.message.text
    logger.info(f"Received message: {user_message}")

    await update.message.reply_text("Working...")

    try:
        parsed = parse_task(user_message, timezone=USER_TIMEZONE)
        logger.info(f"Parsed task: {parsed}")

        description = parsed["description"]
        due_datetime = parsed.get("due_datetime")
        offsets = parsed.get("reminder_offsets_minutes", [0])

        task_id = save_task(
            telegram_user_id=update.effective_user.id,
            description=description,
            due_datetime=due_datetime
        )

        if due_datetime:
            for offset_minutes in offsets:
                from datetime import timedelta
                reminder_time = due_datetime - timedelta(offset_minutes)

                if reminder_time > datetime.now(pytz.utc):
                    save_reminder(task_id, reminder_time)

        if due_datetime:
            local_tz = pytz.timezone(USER_TIMEZONE)
            local_due = due_datetime.astimezone(local_tz)
            due_str = local_due.strftime("%A, %b %d at %I:%M %p")
            reply = f"Saved. I will remind you to *{description}* on {due_str}."
        else:
            reply = f"Saved: *{description}* (no due date set - use /list to see all tasks)"

        await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error handling msg: {e}")
        await update.message.reply_text(
            "Sorry, something went wrong parsing that. Try rephrasing with a clear time, like 'remind me to call mom tomorrow at 3pm'."
        )

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    
    tasks = get_pending_tasks(update.effective_user.id)

    if not tasks:
        await update.message.reply_text("No pending tasks.")
        return
    
    local_tz = pytz.timezone(USER_TIMEZONE)
    lines = ["Your pending tasks:*\n"]

    for i, task in enumerate(tasks, 1):
        due_str = ""
        if task["due_datetime"]:
            local_due = task["due_datetime"].astimezone(local_tz)
            due_str = f"-- due {local_due.strftime('%b %d at %I:%M %p')}"
        lines.append(f"{i}. {task['description']}{due_str}\n    ID: `{str(task['id'])[:8]}...`")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage: /done <task_id>\nGet task IDs from /list"
        )
        return
    partial_id = context.args[0]

    tasks = get_pending_tasks(update.effective_user.id)
    matched = [t for t in tasks if str(t["id"]).startswith(partial_id)]

    if not matched:
        await update.message.reply_text(f"No task found with ID starting with `{partial_id}`")
        return

    if len(matched) > 1:
        await update.message.reply_text("Multiple tasks match that ID. Use more characters.")
        return

    task = matched[0]
    mark_task_complete(str(task["id"]))
    await update.message.reply_text(f"Marked complete: *{task['description']}", parse_mode="Markdown")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start command — welcome message shown when someone first opens the bot.
    """
    if not is_authorized(update):
        return

    await update.message.reply_text(
        "👋 Hi! I'm your personal reminder bot.\n\n"
        "Just tell me what you need to remember, for example:\n"
        "• *remind me to call mom tonight at 8pm*\n"
        "• *buy groceries tomorrow morning*\n"
        "• *submit application by Friday at 5pm*\n\n"
        "Commands:\n"
        "/list — see all pending tasks\n"
        "/done <id> — mark a task complete",
        parse_mode="Markdown"
    )


def main():
    """
    Starts the bot using webhooks.
    Webhooks mean Telegram pushes messages to us (via HTTPS POST),
    rather than us polling Telegram every few seconds.
    Webhooks are more efficient and recommended for production.
    """
    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start the bot with webhook
    # Telegram will POST to WEBHOOK_URL every time you send a message
    app.run_webhook(
        listen="0.0.0.0",     # Listen on all network interfaces
        port=8443,            # Internal port (Nginx will forward to this)
        webhook_url=WEBHOOK_URL,
        secret_token=os.getenv("WEBHOOK_SECRET", ""),  # Optional extra security
    )


if __name__ == "__main__":
    main()