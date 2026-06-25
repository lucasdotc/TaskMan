import os
import logging
from datetime import datetime
import pytz
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)
from claude import parse_task, parse_intent, fuzzy_match
from db import save_task, save_reminder, get_pending_tasks, mark_task_complete, clear_all_tasks

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_ID", "").split(",")]
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "UTC")

def is_authorized(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USER_IDS

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    
    user_message = update.message.text
    logger.info(f"Received message: {user_message}")

    await update.message.reply_text("Working...")

    try:
        intent_result = parse_intent(user_message)
        intent = intent_result["intent"]

        if intent == "add_task":
            await add_task(update, user_message)
        elif intent == "list_tasks":
            await list_tasks(update)
        elif intent == "complete_tasks":
            await process_task(update, user_message, "complete")
        elif intent == "delete_tasks":
            await process_task(update, user_message, "delete")
        elif intent == "clear_tasks":
           await clear_command(update)
        else:
            await update.message.reply_text(
                "I'm not sure what you mean. Try something like:\n"
                "• 'Remind me to call mom tomorrow at 3pm'\n"
                "• 'What tasks do I have?'\n"
                "• 'Mark the dentist one as done'"
            )


    except Exception as e:
        import traceback
        logger.error(f"Error handling msg: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(
            "Sorry, something went wrong parsing that. Try rephrasing with a clear time, like 'remind me to call mom tomorrow at 3pm'."
        )

async def add_task(update: Update, user_message: str):
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
    pass

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    
    tasks = get_pending_tasks(update.effective_user.id)

    if not tasks:
        await update.message.reply_text("No pending tasks.")
        return
    
    local_tz = pytz.timezone(USER_TIMEZONE)
    lines = ["*Your pending tasks:*\n"]

    for i, task in enumerate(tasks, 1):
        due_str = ""
        if task["due_datetime"]:
            local_due = task["due_datetime"].astimezone(local_tz)
            due_str = f"-- due {local_due.strftime('%b %d at %I:%M %p')}"
        lines.append(f"{i}. {task['description']}{due_str}\n    ID: `{str(task['id'])[:8]}...`")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def process_task(update: Update, user_message: str, cmd: str):
    tasks = get_pending_tasks(update.effective_user.id)
    task_id = await fuzzy_match(user_message, tasks)
    task = next((t for t in tasks if str(t["id"]) == task_id), None)
    mark_task_complete(task_id)
    if cmd == "complete":
        await update.message.reply_text(f"Marked complete: *{task['description']}*", parse_mode="Markdown")
    elif cmd == "delete":
        await update.message.reply_text(f"Deleted: *{task['description']}*", parse_mode="Markdown")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /done <task_id or description>\nGet task IDs from /list"
        )
        return

    input_text = " ".join(context.args)
    tasks = get_pending_tasks(update.effective_user.id)

    matched_by_id = [t for t in tasks if str(t["id"]).startswith(input_text)]
    if len(matched_by_id) == 1:
        task = matched_by_id[0]
        mark_task_complete(str(task["id"]))
        await update.message.reply_text(f"Marked complete: *{task['description']}*", parse_mode="Markdown")
        return

    input_lower = input_text.lower()
    matched_by_desc = [t for t in tasks if input_lower in t["description"].lower()]

    if len(matched_by_desc) == 1:
        task = matched_by_desc[0]
        mark_task_complete(str(task["id"]))
        await update.message.reply_text(f"Marked complete: *{task['description']}*", parse_mode="Markdown")
        return

    if len(matched_by_desc) > 1:
        local_tz = pytz.timezone(USER_TIMEZONE)
        keyboard = []
        for task in matched_by_desc:
            due_str = ""
            if task["due_datetime"]:
                local_due = task["due_datetime"].astimezone(local_tz)
                due_str = f" (due {local_due.strftime('%b %d at %I:%M %p')})"
            keyboard.append([InlineKeyboardButton(
                f"{task['description']}{due_str}",
                callback_data=f"done_{task['id']}"
            )])
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="done_cancel")])
        await update.message.reply_text(
            f"Multiple tasks match \"{input_text}\". Which one?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if not tasks:
        await update.message.reply_text("No pending tasks found.")
        return

    local_tz = pytz.timezone(USER_TIMEZONE)
    lines = [f"No task matching \"{input_text}\". Here are your pending tasks:\n"]
    for i, task in enumerate(tasks, 1):
        due_str = ""
        if task["due_datetime"]:
            local_due = task["due_datetime"].astimezone(local_tz)
            due_str = f" -- due {local_due.strftime('%b %d at %I:%M %p')}"
        lines.append(f"{i}. {task['description']}{due_str}")
    await update.message.reply_text("\n".join(lines))


async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "done_cancel":
        await query.edit_message_text("Cancelled.")
        return

    task_id = query.data.removeprefix("done_")
    tasks = get_pending_tasks(query.from_user.id)
    task = next((t for t in tasks if str(t["id"]) == task_id), None)

    if not task:
        await query.edit_message_text("Task not found or already completed.")
        return

    mark_task_complete(task_id)
    await query.edit_message_text(f"Marked complete: *{task['description']}*", parse_mode="Markdown")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    tasks = get_pending_tasks(update.effective_user.id)
    if not tasks:
        await update.message.reply_text("No pending tasks to clear.")
        return

    keyboard = [[
        InlineKeyboardButton("Yes, delete all", callback_data="clear_confirm"),
        InlineKeyboardButton("Cancel", callback_data="clear_cancel"),
    ]]
    await update.message.reply_text(
        f"Are you sure you want to delete all {len(tasks)} pending task(s)? This cannot be undone.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "clear_confirm":
        count = clear_all_tasks(query.from_user.id)
        await query.edit_message_text(f"Cleared {count} task(s).")
    else:
        await query.edit_message_text("Clear cancelled.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text(
        "Hi! I'm your personal reminder bot.\n\n"
        "Just tell me what you need to remember, for example:\n"
        "- *remind me to call mom tonight at 8pm*\n"
        "- *buy groceries tomorrow morning*\n"
        "- *submit application by Friday at 5pm*\n\n"
        "Commands:\n"
        "/list -- see all pending tasks\n"
        "/done <id or description> -- mark a task complete\n"
        "/clear -- remove all pending tasks",
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

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("list", list_tasks))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CallbackQueryHandler(clear_callback, pattern="^clear_"))
    app.add_handler(CallbackQueryHandler(done_callback, pattern="^done_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start the bot with webhook
    # Telegram will POST to WEBHOOK_URL every time you send a message
    app.run_webhook(
        listen="0.0.0.0",     # Listen on all network interfaces
        port=8443,            # Internal port (Nginx will forward to this)
        url_path = "webhook",
        webhook_url=WEBHOOK_URL,
    )


if __name__ == "__main__":
    main()