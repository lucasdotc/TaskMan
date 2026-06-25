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
  "reminder_offsets_minutes": [0]
}}

reminder_offsets_minutes controls when to send reminders relative to due_datetime:
- 0 means at the exact due time
- 60 means 1 hour before
- 1440 means 1 day before
- Always include 0 (the actual due time reminder)
- If the task has a due date, also include 60 (1 hour before) and 1440 (1 day before)
- If no due date, just use [0] and set due_datetime to null

Examples:
User: "remind me to call the dentist tomorrow at 2pm"
Response: {{"description": "call the dentist", "due_datetime": "2025-01-16T14:00:00Z", "reminder_offsets_minutes": [0, 60, 1440]}}

User: "don't forget to buy groceries"
Response: {{"description": "buy groceries", "due_datetime": null, "reminder_offsets_minutes": [0]}}

User: "remind me everyday to text my dad at 10:30am"
Response: {{"description": "text dad", "due_datetime": "2026-06-25T10:30:00Z", "recurrence_rule": "daily", "reminder_offsets_minutes": [0]}}

User: "remind me to call mom on weekdays at 9am"
Response: {{"description": "call mom", "due_datetime": "2026-06-26T09:00:00Z", "recurrence_rule": "weekdays", "reminder_offsets_minutes": [0]}}

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
    return parsed

INTENT_PROMPT = """
You are a task bot intent classifier. Your job is to classify the user's intent through their natural language prompt, returning ONLY a valid JSON containing the intent and any relevant data.

Possible intents:
"add_task": The user wants to add a task to the list.
"list_tasks": The user wants all their current/pending tasks listed.
"complete_task": The user wants to mark a task as done.
"delete_task": The user wants to remove a task from the list.
"clear_tasks": The user wants to clear their list of tasks.
"unknown": Anything else.

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

User: "mark the dentist one as done"
Response: {"intent": "complete_task", "raw_message": "mark the dentist one as done"}

User: "delete the grocery task"
Response: {"intent": "delete_task", "raw_message": "delete the grocery task"}

User: "clear the task list"
Response: {"intent": "clear_tasks", "raw_message": "clear the task list"}
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
    return None if result == "None" else result