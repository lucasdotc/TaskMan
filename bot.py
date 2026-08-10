import os
import logging
from datetime import datetime, timedelta
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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from claude import parse_task, parse_intent, fuzzy_match
from db import (
    save_task, save_reminder, get_pending_tasks, mark_task_complete, mark_all_tasks_complete,
    ensure_user, set_display_name, get_user, find_user_by_name, transfer_task,
    sync_admins_from_env, get_all_users,
)
from voice import transcribe_audio
from reminders import check_and_send_reminders

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def _parse_id_list(env_var: str) -> list[int]:
    """Parses a comma-separated env var of Telegram user IDs, tolerating an
    unset/blank value (returns []) instead of crashing on int('')."""
    raw = os.getenv(env_var, "")
    return [int(x.strip()) for x in raw.split(",") if x.strip()]

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
USER_TIMEZONE = os.getenv("USER_TIMEZONE", "UTC")

# ALLOWED_USER_ID is everyone who's allowed to talk to the bot at all --
# admins and regular users alike. ADMIN_USER_ID is a subset of that list
# with the extra ability to run /transfer. A user must be in ALLOWED_USER_ID
# to receive a transferred task, since that's what lets them list/complete
# it and be reminded.
ALLOWED_USER_IDS = _parse_id_list("ALLOWED_USER_ID")
ADMIN_USER_IDS = _parse_id_list("ADMIN_USER_ID")

def is_authorized(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USER_IDS

def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_USER_IDS

# Remembers, per Telegram user, the ordered task IDs from their most recent
# /list -- so "mark number 1 as done" can be resolved to a task without
# asking Claude to guess from the description. Deliberately in-memory only
# (not in the DB): it's display state tied to one specific message the bot
# already sent, not data worth persisting across a restart. Since the bot is
# a single process now (see the APScheduler swap), a plain module-level dict
# is enough -- there's no second process that would need to share this.
LAST_LISTED_TASK_IDS: dict[int, list[str]] = {}

def remember_shown_tasks(user_id: int, tasks: list) -> None:
    LAST_LISTED_TASK_IDS[user_id] = [str(t["id"]) for t in tasks]

def resolve_list_position(user_id: int, position: int) -> str | None:
    """Maps a 1-based position from the user's last /list to a task UUID.
    Returns None if there's no remembered list, or the position is out of
    range -- callers treat that the same as "no match found", so a stale or
    made-up number fails safely instead of acting on the wrong task."""
    ids = LAST_LISTED_TASK_IDS.get(user_id)
    if not ids or position < 1 or position > len(ids):
        return None
    return ids[position - 1]

# Per-user "current category" context, set with /category -- works like a
# shell `cd`. Once set, /list, /done, delete, transfer, and /clear all
# default to that category instead of everything, and new tasks default
# into it too (see add_task). An explicit category given to any of those
# commands still overrides it for that one call, same as `ls <path>`
# doesn't change your shell's cwd. Same in-memory, single-process reasoning
# as LAST_LISTED_TASK_IDS above -- UX convenience, not data worth
# persisting across a restart.
CURRENT_CATEGORY: dict[int, str] = {}

def get_current_category(user_id: int) -> str | None:
    return CURRENT_CATEGORY.get(user_id)

def get_scoped_tasks(user_id: int, category: str = None) -> list:
    """Fetches pending tasks, falling back to the user's /category context
    when no explicit category is given."""
    return get_pending_tasks(user_id, category=category or get_current_category(user_id))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await process_text_message(update, context, update.message.text)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles both voice notes (update.message.voice) and uploaded audio files
    (update.message.audio) -- Telegram represents them as separate message
    types even though we treat them identically here.
    """
    if not is_authorized(update):
        return

    voice_or_audio = update.message.voice or update.message.audio
    await update.message.reply_text("Listening...")

    try:
        # get_file() gives us a short-lived download URL/handle for the
        # audio blob Telegram is hosting; download_as_bytearray() pulls the
        # actual bytes into memory (these are voice notes, a few seconds to
        # a couple minutes long, so this is small enough not to need disk).
        tg_file = await context.bot.get_file(voice_or_audio.file_id)
        audio_bytes = bytes(await tg_file.download_as_bytearray())
        transcribed_text = transcribe_audio(audio_bytes)
    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        await update.message.reply_text(
            "Sorry, I couldn't understand that voice message. Try again or type it instead."
        )
        return

    if not transcribed_text:
        await update.message.reply_text("I didn't catch any words in that voice message.")
        return

    # Show the user what we heard -- Whisper isn't perfect, and this makes
    # misheard words immediately obvious instead of silently doing the wrong
    # thing with the task.
    await update.message.reply_text(f"Heard: “{transcribed_text}”")
    await process_text_message(update, context, transcribed_text)

async def process_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    """
    The shared pipeline for both typed and transcribed-from-voice messages:
    classify intent, then route to the matching handler.
    """
    logger.info(f"Received message: {user_message}")
    ensure_user(update.effective_user.id, update.effective_user.first_name)

    await update.message.reply_text("Working...")

    try:
        intent_result = parse_intent(user_message)
        intent = intent_result["intent"]

        if intent == "add_task":
            await add_task(update, user_message)
        elif intent == "list_tasks":
            await list_tasks(update, context, category=intent_result.get("category"))
        elif intent == "complete_task":
            await process_task(update, user_message, "complete", intent_result.get("list_position"))
        elif intent == "delete_task":
            await process_task(update, user_message, "delete", intent_result.get("list_position"))
        elif intent == "clear_tasks":
            await handle_clear(update, context, intent_result.get("category"))
        elif intent == "complete_all_tasks":
            await handle_complete_all(update, context, intent_result.get("category"))
        elif intent == "transfer_task":
            await handle_transfer(
                update, context, user_message,
                intent_result.get("recipient_name"), intent_result.get("list_position")
            )
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
    recurrence_rule = parsed.get("recurrence_rule")
    offsets = parsed.get("reminder_offsets_minutes", [0])
    # While "cd'd" into a category via /category, new tasks go there
    # unconditionally -- like creating a file while cd'd into a directory,
    # it lands there regardless of its name. Claude's own inference from
    # the message is only used when there's no active /category context.
    category = get_current_category(update.effective_user.id) or parsed.get("category", "personal")

    task_id = save_task(
        telegram_user_id=update.effective_user.id,
        description=description,
        due_datetime=due_datetime,
        recurrence_rule=recurrence_rule,
        category=category
    )

    if due_datetime:
        for offset_minutes in offsets:
            reminder_time = due_datetime - timedelta(minutes=offset_minutes)

            if reminder_time > datetime.now(pytz.utc):
                save_reminder(task_id, reminder_time)

    if due_datetime:
        local_tz = pytz.timezone(USER_TIMEZONE)
        local_due = due_datetime.astimezone(local_tz)
        due_str = local_due.strftime("%A, %b %d at %I:%M %p")
        reply = f"Saved to *{category}*. I will remind you to *{description}* on {due_str}."
    else:
        reply = f"Saved to *{category}*: *{description}* (no due date set - use /list to see all tasks)"

    await update.message.reply_text(reply, parse_mode="Markdown")

def _format_task_line(i: int, task: dict, local_tz) -> str:
    due_str = ""
    if task["due_datetime"]:
        local_due = task["due_datetime"].astimezone(local_tz)
        due_str = f" -- due {local_due.strftime('%b %d at %I:%M %p')}"
    assigned_note = " (assigned to you)" if task.get("assigned_by") else ""
    # No task ID shown here on purpose -- with /list numbering and /category
    # scoping, referring to a task by its number ("mark number 1 as done")
    # or description covers normal use, and a raw UUID just clutters the
    # display. /done and /transfer still accept a UUID prefix under the
    # hood if you ever have one from elsewhere (e.g. the database).
    return f"{i}. {task['description']}{due_str}{assigned_note}"

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str = None):
    if not is_authorized(update):
        return

    # An explicit category always wins for this one call; otherwise fall
    # back to whatever the user is "cd'd" into via /category.
    category = category or get_current_category(update.effective_user.id)
    tasks = get_pending_tasks(update.effective_user.id, category=category)

    if not tasks:
        msg = f"No pending tasks in *{category}*." if category else "No pending tasks."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    local_tz = pytz.timezone(USER_TIMEZONE)

    if category:
        lines = [f"*Your pending {category} tasks:*\n"]
        lines += [_format_task_line(i, t, local_tz) for i, t in enumerate(tasks, 1)]
    else:
        # get_pending_tasks() orders by category, so grouping by insertion
        # order here reproduces that grouping without a second sort.
        grouped: dict[str, list] = {}
        for t in tasks:
            grouped.setdefault(t["category"], []).append(t)

        # Numbering runs continuously across every category (1, 2, 3, ...)
        # instead of restarting at 1 in each group -- restarting would mean
        # two different tasks both showing as "1." in the same message,
        # which makes "mark number 1 as done" ambiguous.
        lines = ["*Your pending tasks:*"]
        counter = 1
        for cat, cat_tasks in grouped.items():
            lines.append(f"\n_{cat.capitalize()}_")
            for t in cat_tasks:
                lines.append(_format_task_line(counter, t, local_tz))
                counter += 1

    # tasks is already in the same order the numbers above were assigned in
    # (get_pending_tasks orders by category, and grouping above preserves
    # that order), so this is what "number N" will resolve against next.
    remember_shown_tasks(update.effective_user.id, tasks)

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list with no args shows everything grouped by category;
    /list work shows just the work category."""
    category = context.args[0].lower() if context.args else None
    await list_tasks(update, context, category=category)

async def category_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Works like `cd`: /category work scopes /list, /done, delete,
    /transfer, and /clear to the "work" category by default until you
    switch or reset it, and new tasks default into it too. /category alone
    shows what you're currently scoped to; /category all resets to
    everything (any of "all"/"none"/"clear"/"reset" works for that)."""
    if not is_authorized(update):
        return

    if not context.args:
        current = get_current_category(update.effective_user.id)
        if current:
            await update.message.reply_text(
                f"Currently viewing: *{current}*\nUse /category all to see everything again, or /category <name> to switch.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "Not scoped to a category right now -- commands apply to everything. Use /category <name> to switch."
            )
        return

    name = context.args[0].lower()
    if name in ("all", "none", "clear", "reset"):
        CURRENT_CATEGORY.pop(update.effective_user.id, None)
        await update.message.reply_text("Back to viewing everything.")
        return

    CURRENT_CATEGORY[update.effective_user.id] = name
    await update.message.reply_text(
        f"Now viewing *{name}*. /list, /done, /transfer, /clear, and new tasks will default here until you /category all.",
        parse_mode="Markdown"
    )

async def process_task(update: Update, user_message: str, cmd: str, list_position: int = None):
    tasks = get_scoped_tasks(update.effective_user.id)

    if list_position is not None:
        # Referring to a task by its position from the last /list -- no
        # need to ask Claude to guess, just look up what was shown there.
        task_id = resolve_list_position(update.effective_user.id, list_position)
    else:
        task_id = await fuzzy_match(user_message, tasks)

    task = next((t for t in tasks if str(t["id"]) == task_id), None) if task_id else None
    if not task:
        message = (
            "I don't have a task at that position -- run /list first, then refer to a number from it."
            if list_position is not None else
            "I could not find a task with that description. Use /list to see all of your active tasks."
        )
        await update.message.reply_text(message)
        return
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
            "Usage: /done <number, description, or task id>\nRun /list to see task numbers"
        )
        return

    input_text = " ".join(context.args)
    tasks = get_scoped_tasks(update.effective_user.id)

    # A bare number is treated as "position from my last /list" rather than
    # a UUID prefix -- with numbered /list output now available, that's what
    # a plain "1" or "2" almost always means.
    if input_text.isdigit():
        task_id = resolve_list_position(update.effective_user.id, int(input_text))
        task = next((t for t in tasks if str(t["id"]) == task_id), None) if task_id else None
        if task:
            mark_task_complete(str(task["id"]))
            await update.message.reply_text(f"Marked complete: *{task['description']}*", parse_mode="Markdown")
        else:
            await update.message.reply_text("I don't have a task at that position -- run /list first, then use a number from it.")
        return

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

async def handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str = None):
    """Shared by /clear and the natural-language "remove all" flow. Shows a
    confirm/cancel button before acting, since this is bulk and there's no
    "un-clear" -- same reasoning as the single-task /clear always had."""
    if not is_authorized(update):
        return

    category = category or get_current_category(update.effective_user.id)
    tasks = get_pending_tasks(update.effective_user.id, category=category)
    if not tasks:
        msg = f"No pending tasks in *{category}* to clear." if category else "No pending tasks to clear."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    scope = f"{category} " if category else ""
    # The category (or empty string) rides along in callback_data so
    # clear_callback knows what to act on when the button is actually
    # pressed -- by then this function has already returned.
    keyboard = [[
        InlineKeyboardButton("Yes, delete all", callback_data=f"clear_confirm:{category or ''}"),
        InlineKeyboardButton("Cancel", callback_data="clear_cancel"),
    ]]
    await update.message.reply_text(
        f"Are you sure you want to delete all {len(tasks)} pending {scope}task(s)? This cannot be undone.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clear with no args clears everything (or the /category you're
    cd'd into); /clear work clears just the work category."""
    category = context.args[0].lower() if context.args else None
    await handle_clear(update, context, category)


async def clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("clear_confirm"):
        category = query.data.split(":", 1)[1] or None
        count = mark_all_tasks_complete(query.from_user.id, category=category)
        scope = f" {category}" if category else ""
        await query.edit_message_text(f"Cleared {count}{scope} task(s).")
    else:
        await query.edit_message_text("Clear cancelled.")


async def handle_complete_all(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str = None):
    """Shared by /completeall and the natural-language "mark all as done"
    flow. Same confirm/cancel pattern as handle_clear, for the same reason
    -- there's no bulk "un-complete" either."""
    if not is_authorized(update):
        return

    category = category or get_current_category(update.effective_user.id)
    tasks = get_pending_tasks(update.effective_user.id, category=category)
    if not tasks:
        msg = f"No pending tasks in *{category}* to mark complete." if category else "No pending tasks to mark complete."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    scope = f"{category} " if category else ""
    keyboard = [[
        InlineKeyboardButton("Yes, mark all complete", callback_data=f"completeall_confirm:{category or ''}"),
        InlineKeyboardButton("Cancel", callback_data="completeall_cancel"),
    ]]
    await update.message.reply_text(
        f"Mark all {len(tasks)} pending {scope}task(s) as complete?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def complete_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = context.args[0].lower() if context.args else None
    await handle_complete_all(update, context, category)


async def complete_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data.startswith("completeall_confirm"):
        category = query.data.split(":", 1)[1] or None
        count = mark_all_tasks_complete(query.from_user.id, category=category)
        scope = f" {category}" if category else ""
        await query.edit_message_text(f"Marked {count}{scope} task(s) complete.")
    else:
        await query.edit_message_text("Cancelled.")


# ---------------------------------------------------------------------------
# Task transfer (admin only) + user identity commands
# ---------------------------------------------------------------------------

async def do_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id: str, recipient_id: int, recipient_label: str):
    """Shared by both the natural-language transfer flow and /transfer:
    moves the task in the DB, confirms to the sender, and DMs the recipient."""
    updated = transfer_task(task_id, recipient_id, assigned_by=update.effective_user.id)
    if not updated:
        await update.message.reply_text("That task no longer exists.")
        return

    await update.message.reply_text(
        f"Sent *{updated['description']}* to {recipient_label or recipient_id}.",
        parse_mode="Markdown"
    )

    sender = get_user(update.effective_user.id)
    sender_label = (sender and sender.get("display_name")) or update.effective_user.first_name or "An admin"

    due_str = ""
    if updated["due_datetime"]:
        local_tz = pytz.timezone(USER_TIMEZONE)
        local_due = updated["due_datetime"].astimezone(local_tz)
        due_str = f" (due {local_due.strftime('%b %d at %I:%M %p')})"

    try:
        # This only works if the recipient has messaged the bot before --
        # Telegram doesn't let bots DM someone who hasn't started a chat
        # with them, regardless of what's in ALLOWED_USER_ID.
        await context.bot.send_message(
            chat_id=recipient_id,
            text=f"\U0001F4CB {sender_label} sent you a task: *{updated['description']}*{due_str}",
            parse_mode="Markdown"
        )
    except Exception:
        logger.warning(f"Could not notify recipient {recipient_id} of transferred task -- they may not have started a chat with the bot yet")

async def handle_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str, recipient_name: str, list_position: int = None):
    """Natural-language path: 'send the grocery task to Sarah' or 'send #2 to Sarah'."""
    if not is_admin(update):
        await update.message.reply_text("Only admins can transfer tasks to other users.")
        return

    if not recipient_name:
        await update.message.reply_text(
            "I couldn't tell who to send that to. Try being explicit, e.g. "
            "\"send the grocery task to Sarah\", or use /transfer <task> <name or id>."
        )
        return

    recipient = find_user_by_name(recipient_name)
    if not recipient:
        await update.message.reply_text(
            f"No registered user matches \"{recipient_name}\". They need to message the bot "
            f"and run /register <name> first, or you can use /transfer <task> <telegram_id>."
        )
        return

    tasks = get_scoped_tasks(update.effective_user.id)
    if list_position is not None:
        task_id = resolve_list_position(update.effective_user.id, list_position)
    else:
        task_id = await fuzzy_match(user_message, tasks)
    task = next((t for t in tasks if str(t["id"]) == task_id), None) if task_id else None
    if not task:
        message = (
            "I don't have a task at that position -- run /list first, then refer to a number from it."
            if list_position is not None else
            "I could not find a matching task of yours to transfer. Use /list to see your tasks."
        )
        await update.message.reply_text(message)
        return

    await do_transfer(update, context, str(task["id"]), recipient["telegram_user_id"], recipient.get("display_name"))

async def transfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explicit fallback: /transfer <task id or description> <recipient name or telegram id>."""
    if not is_authorized(update):
        return
    if not is_admin(update):
        await update.message.reply_text("Only admins can transfer tasks.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /transfer <number, description, or task id> <recipient telegram id or registered name>"
        )
        return

    *task_words, recipient_arg = context.args
    task_query = " ".join(task_words)

    tasks = get_scoped_tasks(update.effective_user.id)
    task = None
    if task_query.isdigit():
        # e.g. "/transfer 2 sarah" -- position from the last /list.
        task_id = resolve_list_position(update.effective_user.id, int(task_query))
        task = next((t for t in tasks if str(t["id"]) == task_id), None) if task_id else None
    if not task:
        matched_by_id = [t for t in tasks if str(t["id"]).startswith(task_query)]
        task = matched_by_id[0] if len(matched_by_id) == 1 else None
    if not task:
        matched_id = await fuzzy_match(task_query, tasks)
        task = next((t for t in tasks if str(t["id"]) == matched_id), None)

    if not task:
        await update.message.reply_text("Could not find a matching task of yours. Use /list to see your tasks.")
        return

    if recipient_arg.isdigit():
        recipient_id = int(recipient_arg)
        recipient_row = get_user(recipient_id)
        recipient_label = recipient_row.get("display_name") if recipient_row else None
    else:
        recipient_row = find_user_by_name(recipient_arg)
        if not recipient_row:
            await update.message.reply_text(f"No registered user matches \"{recipient_arg}\".")
            return
        recipient_id = recipient_row["telegram_user_id"]
        recipient_label = recipient_row.get("display_name")

    await do_transfer(update, context, str(task["id"]), recipient_id, recipient_label)

async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets any authorized user set a friendly display name, so admins can
    /transfer to them by name instead of a raw Telegram ID."""
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /register <display name>, e.g. /register Sarah")
        return
    name = " ".join(context.args)
    set_display_name(update.effective_user.id, name)
    await update.message.reply_text(f"Got it, I'll know you as *{name}*.", parse_mode="Markdown")

async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    user = get_user(update.effective_user.id)
    name = (user and user.get("display_name")) or "not set -- use /register <name>"
    role = "admin" if is_admin(update) else "user"
    await update.message.reply_text(
        f"Telegram ID: `{update.effective_user.id}`\nName: {name}\nRole: {role}",
        parse_mode="Markdown"
    )

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: lists every registered user with their display name and
    Telegram ID, so an admin knows who's available to /transfer a task to
    without having to already know their name or ID from elsewhere."""
    if not is_authorized(update):
        return
    if not is_admin(update):
        await update.message.reply_text("Only admins can list users.")
        return

    users = get_all_users()
    if not users:
        await update.message.reply_text("No registered users yet.")
        return

    lines = ["*Registered users:*\n"]
    for u in users:
        name = u["display_name"] or "(no name set)"
        role_note = " -- admin" if u["is_admin"] else ""
        lines.append(f"{name}{role_note}\n    ID: `{u['telegram_user_id']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text(
        "Hi! I'm your personal reminder bot.\n\n"
        "Just tell me what you need to remember (typed or as a voice message), for example:\n"
        "- *remind me to call mom tonight at 8pm*\n"
        "- *buy groceries tomorrow morning*\n"
        "- *submit application by Friday at 5pm*\n\n"
        "You can also manage several tasks at once: \"mark all as done\", \"remove all my work tasks\".\n\n"
        "Commands:\n"
        "/list [category] -- see pending tasks, optionally filtered\n"
        "/category <name> -- like `cd`: scope /list, /done, /transfer, /clear, and new tasks to one category (/category all to reset)\n"
        "/done <number, description, or id> -- mark a task complete\n"
        "/completeall [category] -- mark all (or one category's) pending tasks complete\n"
        "/clear [category] -- remove all (or one category's) pending tasks\n"
        "/register <name> -- set your display name so admins can send you tasks by name\n"
        "/whoami -- see your Telegram ID, name, and role\n"
        "/transfer <task> <name or id> -- (admins only) send one of your tasks to someone else\n"
        "/users -- (admins only) list everyone registered, to see who you can send tasks to",
        parse_mode="Markdown"
    )


async def post_init(app: Application) -> None:
    """
    Runs once, after the Application's event loop is up but before it starts
    serving updates. This is the correct place to start an AsyncIOScheduler
    -- it needs a running asyncio loop to attach to, which doesn't exist yet
    at the point main() calls Application.builder() (that call is still
    synchronous). This replaces the separate Celery Beat + Celery worker
    processes: instead of Beat dropping a job in Redis every 60 seconds for
    a worker to pick up, APScheduler just calls check_and_send_reminders()
    directly on this same event loop.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_and_send_reminders,
        trigger=IntervalTrigger(seconds=60),
        id="check_and_send_reminders",
    )
    scheduler.start()
    # Stashed on bot_data purely so the scheduler object isn't garbage
    # collected -- APScheduler doesn't keep it alive on its own.
    app.bot_data["scheduler"] = scheduler
    logger.info("Reminder scheduler started (checking every 60s)")

    # This bot used to run in webhook mode, which means Telegram has a
    # webhook URL on file for it. Telegram refuses to serve getUpdates
    # (what polling uses) while a webhook is registered, so this clears it
    # once at startup -- harmless if no webhook was ever set.
    await app.bot.delete_webhook()

def main():
    """
    Starts the bot using long polling: instead of Telegram pushing messages
    to a public URL we expose, python-telegram-bot repeatedly asks Telegram
    "any new updates for me?" over an outbound HTTPS connection. That means
    the bot needs no public HTTPS endpoint at all -- no Cloudflare Tunnel,
    no port to expose, no WEBHOOK_URL to keep in sync (the old README's
    "one operational note" about the tunnel URL rotating on every restart
    no longer applies). It can run on any machine with outbound internet
    access, including behind NAT.
    """
    # Keeps the users table's admin flags in sync with ADMIN_USER_ID from
    # .env every time the bot starts, so editing .env and restarting is the
    # only way to change who's an admin.
    sync_admins_from_env(ADMIN_USER_IDS)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("category", category_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("completeall", complete_all_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("transfer", transfer_command))
    app.add_handler(CommandHandler("register", register_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CallbackQueryHandler(clear_callback, pattern="^clear_"))
    app.add_handler(CallbackQueryHandler(complete_all_callback, pattern="^completeall_"))
    app.add_handler(CallbackQueryHandler(done_callback, pattern="^done_"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # allowed_updates=Update.ALL_TYPES makes sure polling actually delivers
    # every update type we have a handler for (callback_query from inline
    # keyboards, voice/audio messages, etc.), not just plain messages.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
