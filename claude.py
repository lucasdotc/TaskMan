import anthropic
import json
import re
import os
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are a task parser. The user sends you a natural language reminder request.
You extract the task information and return ONLY valid JSON — no explanation, no markdown, no backticks.

Return this exact schema:
{{
  "description": "short description of the task",
  "due_datetime": "ISO 8601 datetime string in UTC, or null if no time mentioned",
  "recurrence_rule": "daily | weekly | weekdays | monthly | null",
  "reminder_offsets_minutes": [0],
  "category": "a short single lowercase word for what kind of task this is",
  "priority": "1, 2, 3, or null"
}}

reminder_offsets_minutes controls when to send reminders relative to due_datetime:
- 0 means at the exact due time
- 60 means 1 hour before
- 1440 means 1 day before
- Always include 0 (the actual due time reminder)
- If the task has a due date, also include 60 (1 hour before) and 1440 (1 day before)
- If no due date, just use [0] and set due_datetime to null

category should classify the task so similar tasks can be grouped later:
- Use "work" for job/professional tasks, "school" for classes/homework/studying,
  "personal" for errands/health/social/household tasks
- If the user explicitly names a category (e.g. "add a work task: ...", "for my side project, ..."),
  use that word instead
- Default to "personal" when nothing suggests otherwise

priority should be null UNLESS the user explicitly signals urgency -- never guess it from the
task's subject matter the way category is guessed:
- "priority 1", "urgent", "asap", "highest priority" -> 1
- "priority 2", "medium priority" -> 2
- "priority 3", "low priority", "no rush", "whenever" -> 3
- Nothing said about urgency at all -> null (this should be the common case)

Examples:
User: "remind me to call the dentist tomorrow at 2pm"
Response: {{"description": "call the dentist", "due_datetime": "2025-01-16T14:00:00Z", "reminder_offsets_minutes": [0, 60, 1440], "category": "personal", "priority": null}}

User: "don't forget to buy groceries"
Response: {{"description": "buy groceries", "due_datetime": null, "reminder_offsets_minutes": [0], "category": "personal", "priority": null}}

User: "remind me everyday to text my dad at 10:30am"
Response: {{"description": "text dad", "due_datetime": "2026-06-25T10:30:00Z", "recurrence_rule": "daily", "reminder_offsets_minutes": [0], "category": "personal", "priority": null}}

User: "remind me to call mom on weekdays at 9am"
Response: {{"description": "call mom", "due_datetime": "2026-06-26T09:00:00Z", "recurrence_rule": "weekdays", "reminder_offsets_minutes": [0], "category": "personal", "priority": null}}

User: "add a work task to send the Q3 report to my manager by Friday 5pm, this is urgent"
Response: {{"description": "send Q3 report to manager", "due_datetime": "2026-06-26T17:00:00Z", "reminder_offsets_minutes": [0, 60, 1440], "category": "work", "priority": 1}}

User: "I have a chem lab report due next monday at noon"
Response: {{"description": "chem lab report", "due_datetime": "2026-06-29T12:00:00Z", "reminder_offsets_minutes": [0, 60, 1440], "category": "school", "priority": null}}

Current UTC time: {current_utc}
User's local timezone: {timezone}
"""

def parse_task(user_message: str, timezone: str = "America/Edmonton")->dict:
    current_utc = datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    prompt = SYSTEM_PROMPT.format(
        current_utc=current_utc,
        timezone=timezone
    )

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=prompt,
        messages=[
            {"role": "user", "content": user_message}
        ]
    )

    raw = message.content[0].text.strip()

    parsed = json.loads(raw)

    if parsed.get("due_datetime"):
        parsed["due_datetime"] = datetime.fromisoformat(
            parsed["due_datetime"].replace("Z", "+00:00")
        )
    # Normalize so a missing/blank category (or odd casing from the model)
    # always lands in a predictable bucket instead of null.
    parsed["category"] = (parsed.get("category") or "personal").strip().lower()
    # priority stays None unless it's genuinely 1/2/3 -- anything else the
    # model might return (a stray string, an out-of-range number) is
    # treated as "not set" rather than trusted as-is, since bot.py's
    # get_overdue_tasks_needing_reminder query only knows what to do with
    # exactly those three values (see the CHECK constraint in schema.sql).
    priority = parsed.get("priority")
    parsed["priority"] = priority if priority in (1, 2, 3) else None
    return parsed

INTENT_PROMPT = """
You are a task bot intent classifier. Your job is to classify the user's intent through their natural language prompt, returning ONLY a valid JSON containing the intent and any relevant data.

Possible intents:
"add_task": The user wants to add a task to the list.
"list_tasks": The user wants all their current/pending tasks listed. If they asked for a
    specific category ("what work stuff do I have", "show my school tasks"), include a
    "category" field with that word (lowercase). Otherwise omit "category" entirely.
"complete_task": The user wants to mark ONE specific task as done.
"delete_task": The user wants to remove ONE specific task from the list.
"clear_tasks": The user wants to remove/clear ALL of their pending tasks (or all tasks in
    one category). Covers phrases like "remove all", "clear everything", "delete all my
    tasks". If they scoped it to a category ("clear all my work tasks"), include a
    "category" field with that word (lowercase); otherwise omit it.
"complete_all_tasks": The user wants to mark ALL of their pending tasks as done (or all
    tasks in one category) in one go -- e.g. "mark all as done", "complete everything",
    "mark all my work tasks as done". Same optional "category" field as clear_tasks.
    This is different from complete_task, which is for marking just ONE task done.
"transfer_task": The user wants to hand off / send / assign one of their tasks to another
    person by name (e.g. "send the grocery task to Sarah", "assign the report to John").
    Include a "recipient_name" field with that person's name as written. This is different
    from add_task/list_tasks — it's specifically about moving a task to someone else.
"add_note": The user wants to attach/set a detailed note on a task, beyond its short
    description (e.g. "add a note to the dentist task: bring my insurance card", "note on
    task 2: park in the back lot"). Include a "note_text" field with just the note content
    (not the task reference).
"view_note": The user wants to see the note on a task (e.g. "what's the note on the dentist
    task", "show me the details for task 2"). No extra field beyond task identification.
"set_priority": The user wants to set or change a task's priority level (e.g. "make the
    dentist task priority 1", "mark task 2 as urgent", "set the report to low priority").
    Include a "priority" field: 1 for urgent/highest/"priority 1"/"asap", 2 for "priority
    2"/medium, 3 for "priority 3"/low/"no rush".
"unknown": Anything else.

For complete_task, delete_task, transfer_task, add_note, view_note, and set_priority: if the
user refers to the task by its position in a list they were just shown, rather than by
describing it, include a "list_position" field with that position as a plain integer
(1-based). This covers things like "mark number 1 as done", "delete item 2", "complete task
3", "the first one", "send #2 to Sarah", "note on task 2: ..." -- convert words like
"first"/"second"/"third" to 1/2/3, and strip any leading "#". Do NOT include "list_position"
if the user described the task instead (e.g. "mark the dentist appointment as done") -- in
that case omit the field entirely. If they said "all" or "everything" rather than a number
or description, that's complete_all_tasks/clear_tasks instead, not a list_position.

Return this schema:
{
  "intent": "add_task",
  "raw_message": "the original message"
}

Examples:
User: "remind me to call mom tomorrow at 3pm"
Response: {"intent": "add_task", "raw_message": "remind me to call mom tomorrow at 3pm"}

User: "what do I have this week"
Response: {"intent": "list_tasks", "raw_message": "what do I have this week"}

User: "what work tasks do I have"
Response: {"intent": "list_tasks", "raw_message": "what work tasks do I have", "category": "work"}

User: "mark the dentist one as done"
Response: {"intent": "complete_task", "raw_message": "mark the dentist one as done"}

User: "mark number 1 as done"
Response: {"intent": "complete_task", "raw_message": "mark number 1 as done", "list_position": 1}

User: "delete the second task"
Response: {"intent": "delete_task", "raw_message": "delete the second task", "list_position": 2}

User: "delete the grocery task"
Response: {"intent": "delete_task", "raw_message": "delete the grocery task"}

User: "mark all as done"
Response: {"intent": "complete_all_tasks", "raw_message": "mark all as done"}

User: "mark all my work tasks as done"
Response: {"intent": "complete_all_tasks", "raw_message": "mark all my work tasks as done", "category": "work"}

User: "remove all my tasks"
Response: {"intent": "clear_tasks", "raw_message": "remove all my tasks"}

User: "clear the task list"
Response: {"intent": "clear_tasks", "raw_message": "clear the task list"}

User: "clear all my school tasks"
Response: {"intent": "clear_tasks", "raw_message": "clear all my school tasks", "category": "school"}

User: "send the grocery run task to Sarah"
Response: {"intent": "transfer_task", "raw_message": "send the grocery run task to Sarah", "recipient_name": "Sarah"}

User: "send #3 to John"
Response: {"intent": "transfer_task", "raw_message": "send #3 to John", "recipient_name": "John", "list_position": 3}

User: "add a note to the dentist task: bring my insurance card"
Response: {"intent": "add_note", "raw_message": "add a note to the dentist task: bring my insurance card", "note_text": "bring my insurance card"}

User: "note on task 2: park in the back lot"
Response: {"intent": "add_note", "raw_message": "note on task 2: park in the back lot", "list_position": 2, "note_text": "park in the back lot"}

User: "what's the note on the dentist task"
Response: {"intent": "view_note", "raw_message": "what's the note on the dentist task"}

User: "make the dentist task priority 1"
Response: {"intent": "set_priority", "raw_message": "make the dentist task priority 1", "priority": 1}

User: "mark task 2 as urgent"
Response: {"intent": "set_priority", "raw_message": "mark task 2 as urgent", "list_position": 2, "priority": 1}
"""

def parse_intent(user_message: str)->dict:
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=100,
        system=INTENT_PROMPT,
        messages=[
            {"role": "user", "content": user_message}
        ]
    )
    raw = message.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"intent": "other", "raw_message": user_message}

async def fuzzy_match(user_message: str, tasks: list):
    task_list = "\n".join([f"ID: {t['id']} — {t['description']}" for t in tasks])
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=100,
        system="You are to match a user's message to a Task ID from the task list. Return ONLY the ID that best matches, or NONE if nothing matches.",
        messages=[
            {"role":"user", "content": f"User said: '{user_message}' \n\nTasks:\n{task_list}"}
        ]
    )
    result = response.content[0].text.strip()
    # The system prompt above tells the model to reply "NONE" for no match,
    # but comparing case-insensitively is cheap insurance against the model
    # replying "None" or "none" instead.
    return None if result.upper() == "NONE" else result