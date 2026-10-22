import os
import logging
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
from agent import run_agent
from db import (
    get_pending_tasks, mark_task_complete, mark_all_tasks_complete,
    ensure_user, set_display_name, get_user, find_user_by_name, transfer_task,
    sync_admins_from_env, get_all_users, get_task_by_id, get_task_by_number,
    update_task, delete_task,
)
from voice import transcribe_audio
from reminders import check_and_send_reminders, check_overdue_tasks

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

# Per-user "current category" context, set with /category -- works like a
# shell `cd`. Once set, /list, /done, /delete, /edit, /transfer, and /clear
# all default to that category instead of everything, and new tasks default
# into it too (see the AI agent's add_task tool). An explicit category given
# to any of those commands still overrides it for that one call, same as
# `ls <path>` doesn't change your shell's cwd. Deliberately in-memory only
# (not the database): it's UX convenience tied to the ongoing conversation,
# not data worth surviving a restart.
CURRENT_CATEGORY: dict[int, str] = {}

def get_current_category(user_id: int) -> str | None:
    return CURRENT_CATEGORY.get(user_id)

def set_current_category(user_id: int, category: str | None) -> None:
    if category:
        CURRENT_CATEGORY[user_id] = category
    else:
        CURRENT_CATEGORY.pop(user_id, None)

def get_scoped_tasks(user_id: int, category: str = None) -> list:
    """Fetches pending tasks, falling back to the user's /category context
    when no explicit category is given."""
    return get_pending_tasks(user_id, category=category or get_current_category(user_id))

def _find_task_for_command(user_id: int, query: str):
    """
    Resolves a slash-command argument to a task, for commands that accept
    either a number or a description (/done, /delete, /transfer). A bare
    number is looked up by its permanent task_number directly -- it always
    resolves, regardless of category scoping or anything previously listed.
    Otherwise it's matched as a case-insensitive substring against pending
    task descriptions, scoped to the active /category if one is set.

    Returns a single task dict, a list of 2+ candidate tasks (caller should
    let the user disambiguate), or None for no match at all.
    """
    query = query.strip()
    if query.isdigit():
        task = get_task_by_number(int(query), user_id)
        return task if task and not task["is_complete"] else None

    tasks = get_scoped_tasks(user_id)
    query_lower = query.lower()
    matches = [t for t in tasks if query_lower in t["description"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches
    return None

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
    hands the message to the AI agent (agent.py), which has direct tool
    access to the task database and decides for itself what to do, then
    sends back whatever it replies with.
    """
    logger.info(f"Received message: {user_message}")
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.first_name)

    await update.message.reply_text("Working...")

    async def _on_confirm_needed(action: str, category: str | None):
        if action == "clear":
            await handle_clear(update, context, category)
        else:
            await handle_complete_all(update, context, category)

    async def _on_transfer(task: dict, recipient_name: str):
        recipient = find_user_by_name(recipient_name)
        if not recipient:
            return {"error": f'No registered user matches "{recipient_name}". They need to message the bot and run /register first.'}
        await do_transfer(update, context, str(task["id"]), recipient["telegram_user_id"], recipient.get("display_name"))
        return {"transferred": {"task_number": task["task_number"], "to": recipient.get("display_name") or recipient["telegram_user_id"]}}

    def _on_set_category(category: str | None):
        set_current_category(user_id, category)

    try:
        reply = await run_agent(
            user_message=user_message,
            telegram_user_id=user_id,
            is_admin=is_admin(update),
            category_context=get_current_category(user_id),
            timezone=USER_TIMEZONE,
            on_confirm_needed=_on_confirm_needed,
            on_transfer=_on_transfer,
            on_set_category=_on_set_category,
        )
        if reply:
            await update.message.reply_text(reply, parse_mode="Markdown")
    except Exception as e:
        import traceback
        logger.error(f"Error running agent: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(
            "Sorry, something went wrong handling that. Try rephrasing, or use a command like /list or /done."
        )

def _format_task_line(task: dict, local_tz, show_category: bool = False) -> str:
    priority_tag = f" [P{task['priority']}]" if task.get("priority") else ""
    due_str = ""
    if task["due_datetime"]:
        local_due = task["due_datetime"].astimezone(local_tz)
        due_str = f" -- due {local_due.strftime('%b %d at %I:%M %p')}"
    assigned_note = " (assigned to you)" if task.get("assigned_by") else ""
    category_tag = f" [{task['category']}]" if show_category else ""
    # The leading number is the task's permanent task_number, not a
    # recomputed list position -- it's the same number in every /list and
    # never changes, so it's always safe to refer back to later (e.g.
    # "/done 7", "/edit 7 priority 1") without listing first.
    return f"{task['task_number']}. {task['description']}{category_tag}{priority_tag}{due_str}{assigned_note}"

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str = None, sort: str = "due"):
    if not is_authorized(update):
        return

    # An explicit category always wins for this one call; otherwise fall
    # back to whatever the user is "cd'd" into via /category.
    category = category or get_current_category(update.effective_user.id)
    tasks = get_pending_tasks(update.effective_user.id, category=category, sort=sort)

    if not tasks:
        msg = f"No pending tasks in *{category}*." if category else "No pending tasks."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    local_tz = pytz.timezone(USER_TIMEZONE)
    by_priority = sort == "priority"

    if category:
        header = f"*Your pending {category} tasks" + (" (by priority):*" if by_priority else ":*")
        lines = [header] + [_format_task_line(t, local_tz) for t in tasks]
    elif by_priority:
        lines = ["*Your pending tasks, by priority:*"]
        lines += [_format_task_line(t, local_tz, show_category=True) for t in tasks]
    else:
        # get_pending_tasks() orders by category when sort="due", so
        # grouping by insertion order here reproduces that grouping without
        # a second sort.
        grouped: dict[str, list] = {}
        for t in tasks:
            grouped.setdefault(t["category"], []).append(t)

        lines = ["*Your pending tasks:*"]
        for cat, cat_tasks in grouped.items():
            lines.append(f"\n_{cat.capitalize()}_")
            lines += [_format_task_line(t, local_tz) for t in cat_tasks]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list with no args shows everything grouped by category, ordered by
    due date; /list work shows just the work category; /list priority sorts
    by priority instead (most urgent first); the two can combine, e.g.
    /list work priority."""
    args = [a.lower() for a in context.args]
    sort = "priority" if "priority" in args else "due"
    category_args = [a for a in args if a != "priority"]
    category = category_args[0] if category_args else None
    await list_tasks(update, context, category=category, sort=sort)

async def category_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Works like `cd`: /category work scopes /list, /done, /delete, /edit,
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
        set_current_category(update.effective_user.id, None)
        await update.message.reply_text("Back to viewing everything.")
        return

    set_current_category(update.effective_user.id, name)
    await update.message.reply_text(
        f"Now viewing *{name}*. /list, /done, /transfer, /clear, and new tasks will default here until you /category all.",
        parse_mode="Markdown"
    )

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /done <task number or description>\nRun /list to see task numbers"
        )
        return

    input_text = " ".join(context.args)
    result = _find_task_for_command(update.effective_user.id, input_text)

    if result is None:
        await update.message.reply_text(f'No pending task matching "{input_text}". Use /list to see your tasks.')
        return

    if isinstance(result, list):
        local_tz = pytz.timezone(USER_TIMEZONE)
        keyboard = []
        for task in result:
            due_str = ""
            if task["due_datetime"]:
                local_due = task["due_datetime"].astimezone(local_tz)
                due_str = f" (due {local_due.strftime('%b %d at %I:%M %p')})"
            keyboard.append([InlineKeyboardButton(
                f"#{task['task_number']}: {task['description']}{due_str}",
                callback_data=f"done_{task['id']}"
            )])
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="done_cancel")])
        await update.message.reply_text(
            f'Multiple tasks match "{input_text}". Which one?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    task = result
    mark_task_complete(str(task["id"]))
    await update.message.reply_text(f"Marked complete: *#{task['task_number']} {task['description']}*", parse_mode="Markdown")

async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "done_cancel":
        await query.edit_message_text("Cancelled.")
        return

    task_id = query.data.removeprefix("done_")
    task = get_task_by_id(task_id, query.from_user.id)

    if not task or task["is_complete"]:
        await query.edit_message_text("Task not found or already completed.")
        return

    mark_task_complete(task_id)
    await query.edit_message_text(f"Marked complete: *#{task['task_number']} {task['description']}*", parse_mode="Markdown")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delete permanently removes a task -- unlike /done, this cannot be
    undone and the task isn't just marked complete, it's gone."""
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /delete <task number or description>\n"
            "This permanently removes the task -- for finishing one instead, use /done."
        )
        return

    input_text = " ".join(context.args)
    result = _find_task_for_command(update.effective_user.id, input_text)

    if result is None:
        await update.message.reply_text(f'No pending task matching "{input_text}". Use /list to see your tasks.')
        return

    if isinstance(result, list):
        keyboard = [
            [InlineKeyboardButton(f"#{t['task_number']}: {t['description']}", callback_data=f"delete_{t['id']}")]
            for t in result
        ]
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="delete_cancel")])
        await update.message.reply_text(
            f'Multiple tasks match "{input_text}". Which one should I delete?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    deleted = delete_task(str(result["id"]))
    await update.message.reply_text(f"Deleted: *#{deleted['task_number']} {deleted['description']}*", parse_mode="Markdown")

async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "delete_cancel":
        await query.edit_message_text("Cancelled.")
        return

    task_id = query.data.removeprefix("delete_")
    task = get_task_by_id(task_id, query.from_user.id)
    if not task:
        await query.edit_message_text("Task not found.")
        return

    deleted = delete_task(task_id)
    await query.edit_message_text(f"Deleted: *#{deleted['task_number']} {deleted['description']}*", parse_mode="Markdown")

_EDIT_FIELDS = ("description", "category", "priority", "recurrence", "note")

async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /edit <task number> <field> <value> -- field is one of description,
    category, priority, recurrence, or note. Changing a due date needs real
    date parsing, which this deterministic command doesn't do -- for that,
    just ask naturally instead (e.g. "move task 3 to tomorrow at 5pm"), and
    the AI agent will handle it.
    """
    if not is_authorized(update):
        return

    if len(context.args) < 2 or not context.args[0].isdigit() or context.args[1].lower() not in _EDIT_FIELDS:
        await update.message.reply_text(
            "Usage: /edit <task number> <field> <value>\n"
            f"Fields: {', '.join(_EDIT_FIELDS)}\n"
            'For a due date, just ask naturally instead, e.g. "move task 3 to tomorrow at 5pm".'
        )
        return

    task = get_task_by_number(int(context.args[0]), update.effective_user.id)
    if not task:
        await update.message.reply_text("No task with that number. Use /list to see your tasks.")
        return

    field = context.args[1].lower()
    value = " ".join(context.args[2:])

    if field == "description":
        if not value:
            await update.message.reply_text("Usage: /edit <task number> description <new text>")
            return
        update_task(str(task["id"]), description=value)
    elif field == "category":
        if not value:
            await update.message.reply_text("Usage: /edit <task number> category <name>")
            return
        update_task(str(task["id"]), category=value.lower())
    elif field == "priority":
        if value.lower() not in ("1", "2", "3", "none"):
            await update.message.reply_text("Usage: /edit <task number> priority <1|2|3|none>")
            return
        update_task(str(task["id"]), priority=None if value.lower() == "none" else int(value))
    elif field == "recurrence":
        if value.lower() not in ("daily", "weekly", "weekdays", "monthly", "none"):
            await update.message.reply_text("Usage: /edit <task number> recurrence <daily|weekly|weekdays|monthly|none>")
            return
        update_task(str(task["id"]), recurrence_rule=None if value.lower() == "none" else value.lower())
    elif field == "note":
        update_task(str(task["id"]), notes=value or None)

    await update.message.reply_text(f"Updated *#{task['task_number']} {task['description']}*.", parse_mode="Markdown")

async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/note <task number> [text] -- only a task number is accepted here,
    not a free-text description, since there's no reliable way to tell
    where a typed task description ends and the note text begins. For a
    free-text description, just ask naturally instead (e.g. "add a note to
    the dentist task: bring my insurance card")."""
    if not is_authorized(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Usage: /note <task number> [text to set -- omit to view the current note]\n"
            "For a free-text description instead of a number, just ask naturally, "
            'e.g. "add a note to the dentist task: bring my insurance card".'
        )
        return

    task = get_task_by_number(int(context.args[0]), update.effective_user.id)
    if not task:
        await update.message.reply_text("No task with that number. Use /list to see your tasks.")
        return

    note_text = " ".join(context.args[1:]) if len(context.args) > 1 else None
    if note_text:
        update_task(str(task["id"]), notes=note_text)
        await update.message.reply_text(f"Note saved for *#{task['task_number']} {task['description']}*.", parse_mode="Markdown")
    else:
        note = task.get("notes")
        if note:
            await update.message.reply_text(f"*#{task['task_number']} {task['description']}*:\n{note}", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"No note set for *#{task['task_number']} {task['description']}* yet.", parse_mode="Markdown")

async def priority_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/priority <task number> <1|2|3> -- same number-only reasoning as /note above."""
    if not is_authorized(update):
        return
    if len(context.args) < 2 or not context.args[0].isdigit() or context.args[1] not in ("1", "2", "3"):
        await update.message.reply_text("Usage: /priority <task number> <1|2|3>")
        return

    task = get_task_by_number(int(context.args[0]), update.effective_user.id)
    if not task:
        await update.message.reply_text("No task with that number. Use /list to see your tasks.")
        return

    update_task(str(task["id"]), priority=int(context.args[1]))
    await update.message.reply_text(f"Set *#{task['task_number']} {task['description']}* to priority {context.args[1]}.", parse_mode="Markdown")

async def handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str = None):
    """Shared by /clear and the AI agent's request_bulk_action tool. Shows a
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
    """Shared by /completeall and the AI agent's request_bulk_action tool.
    Same confirm/cancel pattern as handle_clear, for the same reason --
    there's no bulk "un-complete" either."""
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
    """Shared by both the AI agent's transfer_task tool and /transfer: moves
    the task in the DB, confirms to the sender, and DMs the recipient."""
    updated = transfer_task(task_id, recipient_id, assigned_by=update.effective_user.id)
    if not updated:
        await update.message.reply_text("That task no longer exists.")
        return

    await update.message.reply_text(
        f"Sent *#{updated['task_number']} {updated['description']}* to {recipient_label or recipient_id}.",
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
            text=f"\U0001F4CB {sender_label} sent you a task: *#{updated['task_number']} {updated['description']}*{due_str}",
            parse_mode="Markdown"
        )
    except Exception:
        logger.warning(f"Could not notify recipient {recipient_id} of transferred task -- they may not have started a chat with the bot yet")

async def transfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/transfer <task number or description> <recipient name or telegram id>."""
    if not is_authorized(update):
        return
    if not is_admin(update):
        await update.message.reply_text("Only admins can transfer tasks.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /transfer <task number or description> <recipient telegram id or registered name>"
        )
        return

    *task_words, recipient_arg = context.args
    task_query = " ".join(task_words)

    result = _find_task_for_command(update.effective_user.id, task_query)
    if result is None:
        await update.message.reply_text("Could not find a matching task of yours. Use /list to see your tasks.")
        return
    if isinstance(result, list):
        candidates = ", ".join(f"#{t['task_number']}" for t in result)
        await update.message.reply_text(
            f'Multiple tasks match "{task_query}" -- use its number instead to be specific: {candidates}'
        )
        return
    task = result

    if recipient_arg.isdigit():
        recipient_id = int(recipient_arg)
        recipient_row = get_user(recipient_id)
        recipient_label = recipient_row.get("display_name") if recipient_row else None
    else:
        recipient_row = find_user_by_name(recipient_arg)
        if not recipient_row:
            await update.message.reply_text(f'No registered user matches "{recipient_arg}".')
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
        "You can also edit, delete, sort, or manage several tasks at once in plain language: "
        "\"move task 3 to tomorrow at 5pm\", \"delete the dentist task\", \"show my tasks by priority\", "
        "\"mark all as done\".\n\n"
        "Every task has a permanent number (shown in /list) you can always refer back to.\n\n"
        "Commands:\n"
        "/list [category] [priority] -- see pending tasks, optionally filtered and/or sorted by priority\n"
        "/category <name> -- like `cd`: scope /list, /done, /delete, /edit, /transfer, /clear, and new tasks to one category (/category all to reset)\n"
        "/done <number or description> -- mark a task complete\n"
        "/delete <number or description> -- permanently remove a task (cannot be undone)\n"
        "/edit <number> <field> <value> -- change a task's description, category, priority, recurrence, or note\n"
        "/note <number> [text] -- set a detailed note on a task, or view it if you omit the text\n"
        "/priority <number> <1|2|3> -- set how often overdue nudges repeat (1=hourly, 2=every 6h, 3=every 12h, unset=every 24h)\n"
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

    check_overdue_tasks is a second, independent job on the same schedule --
    it's a genuinely different responsibility (open-ended "keep nudging
    until done" nagging vs. one-time scheduled delivery, see reminders.py),
    so it's kept as its own job rather than folded into
    check_and_send_reminders, even though both happen to run every 60s.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_and_send_reminders,
        trigger=IntervalTrigger(seconds=60),
        id="check_and_send_reminders",
    )
    scheduler.add_job(
        check_overdue_tasks,
        trigger=IntervalTrigger(seconds=60),
        id="check_overdue_tasks",
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
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("completeall", complete_all_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("transfer", transfer_command))
    app.add_handler(CommandHandler("register", register_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CommandHandler("priority", priority_command))
    app.add_handler(CallbackQueryHandler(clear_callback, pattern="^clear_"))
    app.add_handler(CallbackQueryHandler(complete_all_callback, pattern="^completeall_"))
    app.add_handler(CallbackQueryHandler(done_callback, pattern="^done_"))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern="^delete_"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # allowed_updates=Update.ALL_TYPES makes sure polling actually delivers
    # every update type we have a handler for (callback_query from inline
    # keyboards, voice/audio messages, etc.), not just plain messages.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
