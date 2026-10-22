"""
The AI agent: a Claude tool-use loop with direct, live access to the task
database, used for every free-text (typed or transcribed-from-voice)
message that isn't a slash command.

This replaces the old pipeline (claude.py's parse_intent -> fixed Python
handler -> parse_task/fuzzy_match), where Claude only ever classified text
into JSON that hand-written code then acted on. Here Claude is handed a set
of tools backed directly by db.py (list/add/edit/delete/complete a task,
etc.) and decides for itself which to call, in what order, and what to say
back -- so new phrasings, multi-step requests ("add X and also mark 3
done"), and edits to arbitrary fields all work without a new intent having
to be hand-coded for each one.

Security note: every tool here is scoped to the telegram_user_id and
is_admin flag passed in from bot.py (established by Telegram's own
authentication, via ALLOWED_USER_ID/ADMIN_USER_ID), never from anything the
model is given in its tool-call arguments. A task_number the model picks
(even a hallucinated or user-suggested one) can only ever resolve to a task
actually owned by that telegram_user_id -- see db.get_task_by_number -- and
transfer_task re-checks is_admin itself rather than trusting the system
prompt. So the worst a crafted message can do is act on the sender's own
data, which they could already do through the ordinary commands.
"""
import json
import os
from datetime import datetime, timedelta

import anthropic
import pytz
from dotenv import load_dotenv

import db

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-4-6"

# Mirrors the offsets the old parse_task prompt used: remind at the due
# time itself, an hour before, and a day before. Only applied when a task
# actually has a due_datetime.
REMINDER_OFFSETS_MINUTES = [0, 60, 1440]

MAX_AGENT_STEPS = 8

SYSTEM_PROMPT_TEMPLATE = """You are TaskMan, a personal task-reminder assistant talking to the user \
directly over Telegram. You have tools that read and write the user's own task database -- use \
them to actually fulfill requests rather than just describing what you would do, and never claim \
to have done something you didn't call a tool for.

Current UTC time: {current_utc}
User's timezone: {timezone}
User's active category focus: {category_context}
User's role: {role}

Guidelines:
- Every task has a permanent task number, included in every list_tasks/get_task result. Refer to \
tasks by that number when confirming actions (e.g. "Marked #7 complete"). If the user mentions a \
number ("edit task 3", "done with 7"), resolve it directly with get_task or the relevant tool --
don't ask them to run /list first, and don't assume a number refers to a position in anything.
- If the user refers to a task by description instead of a number, call list_tasks first, find the \
best match yourself, then act on its task_number. If more than one task is a plausible match and \
you can't tell which they mean, ask them to clarify (name the candidates with their numbers) \
instead of guessing.
- due_datetime is always an ISO 8601 UTC datetime string, e.g. "2026-06-26T17:00:00Z". Convert \
whatever the user said (today/tomorrow/Friday/in an hour/etc.) into UTC yourself using the current \
time and timezone above. Leave it null if they mentioned no timing at all.
- Only set priority when the user explicitly signals urgency ("urgent", "priority 1", "asap", "no \
rush", "low priority") -- never infer it from the task's subject matter. Leave it null the vast \
majority of the time.
- category is a short lowercase word (personal/work/school/etc.). Default to "personal" if nothing \
suggests otherwise, unless the user is already focused on a category (see above), in which case new \
tasks go there unless they clearly say otherwise.
- Completing or deleting *every* pending task (or every task in a category) is irreversible and \
affects many tasks at once -- always call request_bulk_action for that instead of looping \
complete_task/delete_task yourself one at a time, and call it at most once per user request.
- delete_task permanently erases a task; only use it when the user clearly wants a task gone \
entirely (e.g. "delete", "remove", "get rid of"), not just finished -- use complete_task for \
"done"/"finished"/"complete".
- transfer_task only works for admins, and only for the sender's own tasks.
- After your tool calls, reply with a short, plain confirmation of what happened (or the direct \
answer to their question) -- one or two sentences, Telegram Markdown allowed (*bold*), no filler, \
and don't restate the whole task list unless they actually asked to see it.
"""

TOOLS = [
    {
        "name": "list_tasks",
        "description": "List the user's pending (not yet completed) tasks. Optionally filter to one category and/or sort by priority instead of due date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Only show tasks in this category. Omit to use the user's active category focus (or everything, if none)."},
                "sort": {"type": "string", "enum": ["due", "priority"], "description": "\"due\" (default): order by due date. \"priority\": order by priority, most urgent first."},
            },
        },
    },
    {
        "name": "get_task",
        "description": "Get full details (including its note, if any) for one task by its permanent task number.",
        "input_schema": {
            "type": "object",
            "properties": {"task_number": {"type": "integer"}},
            "required": ["task_number"],
        },
    },
    {
        "name": "add_task",
        "description": "Create a new task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Short description of the task."},
                "due_datetime": {"type": ["string", "null"], "description": "ISO 8601 datetime in UTC, or null if the user mentioned no timing."},
                "recurrence_rule": {"type": ["string", "null"], "enum": ["daily", "weekly", "weekdays", "monthly", None]},
                "category": {"type": "string", "description": "Short lowercase word, e.g. personal, work, school."},
                "priority": {"type": ["integer", "null"], "enum": [1, 2, 3, None], "description": "Only set if the user explicitly signaled urgency; otherwise null."},
            },
            "required": ["description", "category"],
        },
    },
    {
        "name": "edit_task",
        "description": "Change one or more fields of an existing task by its task number. Only include the fields that should change; omit everything else. To clear a field (remove a due date, recurrence, priority, or note), pass it explicitly as null.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_number": {"type": "integer"},
                "description": {"type": "string"},
                "due_datetime": {"type": ["string", "null"]},
                "recurrence_rule": {"type": ["string", "null"], "enum": ["daily", "weekly", "weekdays", "monthly", None]},
                "category": {"type": "string"},
                "priority": {"type": ["integer", "null"], "enum": [1, 2, 3, None]},
                "notes": {"type": ["string", "null"]},
            },
            "required": ["task_number"],
        },
    },
    {
        "name": "delete_task",
        "description": "Permanently delete one task by its task number. This cannot be undone. Only for an explicit delete/remove request -- use complete_task for finishing a task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_number": {"type": "integer"}},
            "required": ["task_number"],
        },
    },
    {
        "name": "complete_task",
        "description": "Mark one task as done by its task number.",
        "input_schema": {
            "type": "object",
            "properties": {"task_number": {"type": "integer"}},
            "required": ["task_number"],
        },
    },
    {
        "name": "request_bulk_action",
        "description": "Ask the user to confirm a bulk, irreversible action -- completing or deleting ALL of their pending tasks, optionally scoped to one category -- via a Yes/Cancel button. Always use this instead of performing a bulk action directly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["complete_all", "clear"]},
                "category": {"type": ["string", "null"]},
            },
            "required": ["action"],
        },
    },
    {
        "name": "set_active_category",
        "description": "Switch (or reset) which category the user is currently focused on, like `cd`. Affects what list_tasks defaults to and what category new tasks default into, until changed again.",
        "input_schema": {
            "type": "object",
            "properties": {"category": {"type": ["string", "null"], "description": "null resets to viewing everything."}},
            "required": ["category"],
        },
    },
    {
        "name": "transfer_task",
        "description": "Admin only. Hand off one of the admin's own tasks to another registered user by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_number": {"type": "integer"},
                "recipient_name": {"type": "string"},
            },
            "required": ["task_number", "recipient_name"],
        },
    },
]


def _parse_due_datetime(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _schedule_reminders(task_id: str, due_datetime) -> None:
    if not due_datetime:
        return
    for offset_minutes in REMINDER_OFFSETS_MINUTES:
        reminder_time = due_datetime - timedelta(minutes=offset_minutes)
        if reminder_time > datetime.now(pytz.utc):
            db.save_reminder(task_id, reminder_time)


async def run_agent(
    user_message: str,
    telegram_user_id: int,
    is_admin: bool,
    category_context: str | None,
    timezone: str,
    on_confirm_needed,
    on_transfer,
    on_set_category,
) -> str:
    """
    Runs one user message through the tool-use agent loop and returns the
    final reply text to send back.

    on_confirm_needed(action, category): async callback that shows the
        existing Yes/Cancel Telegram confirmation for a bulk action.
    on_transfer(task, recipient_name): async callback that performs the
        actual transfer (DB update + notifying both parties) and returns a
        dict describing the result for the model to see.
    on_set_category(category): sync callback that updates the user's
        active /category focus.
    """
    local_tz = pytz.timezone(timezone)
    now_utc = datetime.now(pytz.utc)

    # Mutable, unlike the category_context parameter above: if the agent
    # calls set_active_category and then, in the same turn, list_tasks or
    # add_task (e.g. "switch to work and add buy milk"), those later calls
    # need to see the just-changed category, not the one this request
    # started with.
    state = {"category_context": category_context}

    def _fmt_due(dt):
        if not dt:
            return None
        return dt.astimezone(local_tz).strftime("%A, %b %d at %I:%M %p")

    def _task_summary(row: dict) -> dict:
        return {
            "task_number": row["task_number"],
            "description": row["description"],
            "due": _fmt_due(row.get("due_datetime")),
            "category": row.get("category"),
            "priority": row.get("priority"),
            "recurrence_rule": row.get("recurrence_rule"),
            "notes": row.get("notes"),
            "assigned_to_you": bool(row.get("assigned_by")),
        }

    def tool_list_tasks(args):
        category = args.get("category") or state["category_context"]
        if category:
            category = category.strip().lower()
        sort = args.get("sort") or "due"
        tasks = db.get_pending_tasks(telegram_user_id, category=category, sort=sort)
        return {"tasks": [_task_summary(t) for t in tasks], "active_category_focus": state["category_context"]}

    def tool_get_task(args):
        task = db.get_task_by_number(args["task_number"], telegram_user_id)
        if not task or task["is_complete"]:
            return {"error": f"No pending task numbered {args['task_number']} found."}
        return _task_summary(task)

    def tool_add_task(args):
        category = (state["category_context"] or args.get("category") or "personal").strip().lower()
        due_datetime = _parse_due_datetime(args.get("due_datetime"))
        priority = args.get("priority")
        priority = priority if priority in (1, 2, 3) else None
        created = db.save_task(
            telegram_user_id=telegram_user_id,
            description=args["description"],
            due_datetime=due_datetime,
            recurrence_rule=args.get("recurrence_rule"),
            category=category,
            priority=priority,
        )
        _schedule_reminders(created["id"], due_datetime)
        return {
            "created": {
                "task_number": created["task_number"],
                "description": args["description"],
                "due": _fmt_due(due_datetime),
                "category": category,
                "priority": priority,
                "recurrence_rule": args.get("recurrence_rule"),
            }
        }

    def tool_edit_task(args):
        task_number = args["task_number"]
        task = db.get_task_by_number(task_number, telegram_user_id)
        if not task:
            return {"error": f"No task numbered {task_number} found."}

        fields = {}
        for key in ("description", "category", "notes"):
            if key in args:
                fields[key] = args[key].strip().lower() if key == "category" and args[key] else args[key]
        if "priority" in args:
            priority = args["priority"]
            fields["priority"] = priority if priority in (1, 2, 3) else None
        if "recurrence_rule" in args:
            fields["recurrence_rule"] = args["recurrence_rule"]

        due_changed = "due_datetime" in args
        if due_changed:
            fields["due_datetime"] = _parse_due_datetime(args["due_datetime"])

        updated = db.update_task(str(task["id"]), **fields)

        if due_changed:
            db.delete_unsent_reminders(str(task["id"]))
            _schedule_reminders(str(task["id"]), fields["due_datetime"])

        return {"updated": _task_summary(updated)}

    def tool_delete_task(args):
        task = db.get_task_by_number(args["task_number"], telegram_user_id)
        if not task:
            return {"error": f"No task numbered {args['task_number']} found."}
        deleted = db.delete_task(str(task["id"]))
        return {"deleted": {"task_number": deleted["task_number"], "description": deleted["description"]}}

    def tool_complete_task(args):
        task = db.get_task_by_number(args["task_number"], telegram_user_id)
        if not task or task["is_complete"]:
            return {"error": f"No pending task numbered {args['task_number']} found."}
        db.mark_task_complete(str(task["id"]))
        return {"completed": {"task_number": task["task_number"], "description": task["description"]}}

    async def tool_request_bulk_action(args):
        action = args["action"]
        category = args.get("category") or state["category_context"]
        if category:
            category = category.strip().lower()
        await on_confirm_needed(action, category)
        return {"status": "confirmation_shown_to_user", "action": action, "category": category}

    def tool_set_active_category(args):
        category = args.get("category")
        category = category.strip().lower() if category else None
        on_set_category(category)
        state["category_context"] = category
        return {"active_category_focus": category}

    async def tool_transfer_task(args):
        if not is_admin:
            return {"error": "Only admins can transfer tasks."}
        task = db.get_task_by_number(args["task_number"], telegram_user_id)
        if not task:
            return {"error": f"No task numbered {args['task_number']} found."}
        return await on_transfer(task, args["recipient_name"])

    sync_tools = {
        "list_tasks": tool_list_tasks,
        "get_task": tool_get_task,
        "add_task": tool_add_task,
        "edit_task": tool_edit_task,
        "delete_task": tool_delete_task,
        "complete_task": tool_complete_task,
        "set_active_category": tool_set_active_category,
    }
    async_tools = {
        "request_bulk_action": tool_request_bulk_action,
        "transfer_task": tool_transfer_task,
    }

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        current_utc=now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        timezone=timezone,
        category_context=category_context or "none (viewing everything)",
        role="admin" if is_admin else "regular user",
    )

    messages = [{"role": "user", "content": user_message}]

    for _ in range(MAX_AGENT_STEPS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            return text or "Done."

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                if block.name in async_tools:
                    result = await async_tools[block.name](block.input)
                elif block.name in sync_tools:
                    result = sync_tools[block.name](block.input)
                else:
                    result = {"error": f"Unknown tool {block.name}"}
            except Exception as exc:
                result = {"error": str(exc)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    return "That took more steps than expected to sort out -- try breaking your request into smaller parts."
