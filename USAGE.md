# How to Use TaskMan

TaskMan is a Telegram bot that remembers things for you. You talk to it the same way you'd text a person, with no special syntax to memorize, and it stores your tasks, reminds you about them, and can do a bunch of handy organizing along the way.

This guide covers everything it can do, in plain language. If you just want a quick cheat sheet of commands, jump to [Quick Reference](#quick-reference) at the bottom.

## Table of Contents

- [Getting started](#getting-started)
- [Adding a task](#adding-a-task)
- [Talking instead of typing (voice messages)](#talking-instead-of-typing-voice-messages)
- [Seeing your tasks](#seeing-your-tasks)
- [Focusing on one category](#focusing-on-one-category)
- [Marking a task done or deleting it](#marking-a-task-done-or-deleting-it)
- [Editing a task](#editing-a-task)
- [Handling several tasks at once](#handling-several-tasks-at-once)
- [Adding extra details to a task](#adding-extra-details-to-a-task)
- [Priority and overdue reminders](#priority-and-overdue-reminders)
- [Sending a task to someone else (admins)](#sending-a-task-to-someone-else-admins)
- [Setting your name](#setting-your-name)
- [Checking your own info](#checking-your-own-info)
- [How reminders work](#how-reminders-work)
- [Quick Reference](#quick-reference)
- [Tips and troubleshooting](#tips-and-troubleshooting)

---

## Getting started

Open a chat with the bot in Telegram and send `/start`. It'll introduce itself and show a short list of commands. You don't need to remember any of them, though, because for almost everything you can just type what you want in your own words, the same way you'd ask a person to remind you of something.

If the bot doesn't respond at all, you may not be on its list of allowed users yet. Ask whoever set it up to add your Telegram ID.

---

## Adding a task

Just tell the bot what you need to remember. All of these work:

- "remind me to call mom tomorrow at 3pm"
- "buy groceries tomorrow morning"
- "submit the application by Friday at 5pm"
- "don't forget to renew my passport" (no specific time needed either)

The bot figures out the description and the due date and time on its own. It'll reply confirming what it saved and when it'll remind you.

For repeating tasks, say it naturally and the bot picks up the pattern:

- "remind me every day to take my vitamins at 8am"
- "call mom on weekdays at 6pm"

Every task gets filed under a category, such as `personal`, `work`, `school`, or whatever fits. The bot guesses one from context, but you can say it directly:

- "add a work task to send the Q3 report by Friday 5pm"

If you don't say anything about urgency, the task just gets normal reminders. If you do, that changes how often you get reminded once it's overdue. See [Priority and overdue reminders](#priority-and-overdue-reminders):

- "this is urgent: submit the application by Friday at 5pm"

---

## Talking instead of typing (voice messages)

You don't have to type at all. Record a voice message in Telegram and send it like you would to a friend. The bot listens to it, tells you what it heard so you can tell right away if it misheard something, and then handles it exactly like a typed message. Everything in this guide works from a voice message too: adding tasks, marking them done, asking what's on your list, all of it.

---

## Seeing your tasks

Ask, and the bot lists everything you still have pending:

- "what tasks do I have?"
- "what do I have this week?"

Or use the command: `/list`

Your tasks show up numbered and grouped by category, with their due dates. That number next to each task is permanent -- it's that task's number for as long as it exists, not just for this one list. You can refer to it any time afterward, in any message, even much later and even if you've switched `/category` or other tasks have come and gone since: "mark number 7 as done", "edit task 3", `/done 7`. You never have to run `/list` again just to "refresh" a number.

To see just one category, say so, or add it after the command:

- "what work tasks do I have?"
- `/list work`

To see them sorted by urgency instead of due date:

- "show me my tasks by priority"
- `/list priority` (or `/list work priority` for just one category)

---

## Focusing on one category

If you're going to be adding or managing several tasks in the same category in a row, you can tell the bot to stay on that category so you don't have to repeat it every time:

```
/category work
```

From then on, listing tasks, marking them done, adding new ones, and clearing them all default to `work`, until you switch. To go back to seeing everything again:

```
/category all
```

Type `/category` by itself any time to check what you're currently focused on.

---

## Marking a task done or deleting it

Just say it:

- "mark the dentist appointment as done"
- "mark number 2 as done" (using the task's number)

Or use the command, which accepts either the task's number or a description:

```
/done 2
/done dentist appointment
```

If your description matches more than one task, the bot will show you buttons to pick the right one instead of guessing.

Marking a task done is different from deleting it. "Done" keeps the task on record as completed; deleting removes it entirely, and can't be undone:

- "delete the grocery task"
- "remove task 5"

Or use the command, same accepted inputs as `/done`:

```
/delete 2
/delete dentist appointment
```

---

## Editing a task

You can change almost anything about a task after you've created it, just by asking:

- "move task 3 to tomorrow at 5pm" (changes the due date/time)
- "rename task 2 to 'buy birthday present'"
- "change the dentist task to work" (changes its category)
- "make task 4 recurring every week"

Or, for description/category/priority/recurrence/notes (not due dates -- see below), the command:

```
/edit <number> description <new text>
/edit <number> category <name>
/edit <number> priority <1|2|3|none>
/edit <number> recurrence <daily|weekly|weekdays|monthly|none>
/edit <number> note <text>
```

Changing a due date needs the bot to actually understand what you mean by "tomorrow" or "next Friday," so `/edit` doesn't handle dates directly -- just ask naturally instead, as in the first example above.

---

## Handling several tasks at once

You don't have to do these one at a time:

- "mark all as done"
- "mark all my work tasks as done"
- "remove all my tasks"
- "clear all my school tasks"

The bot will always double-check with a Yes/Cancel button before actually doing it, since this affects a whole batch and can't be undone.

Command versions, with an optional category:
```
/completeall
/completeall work
/clear
/clear school
```

---

## Adding extra details to a task

Sometimes the short task description isn't enough and you want to jot down something extra, like an address, a confirmation number, or what to bring. That's what notes are for.

To set a note, by number:
```
/note 2 bring my insurance card and arrive 15 minutes early
```

Or naturally, describing the task instead of using a number:

- "add a note to the dentist task: bring my insurance card"

To view a note, just leave the text off:
```
/note 2
```
or ask: "what's the note on the dentist task?"

Notes don't show up in your regular task list. You have to ask for them specifically, so your list stays short and readable.

---

## Priority and overdue reminders

Every task can have a priority from 1 (most urgent) to 3 (least), or no priority at all. This controls what happens if a task's due date passes and you still haven't done it. The bot won't just remind you once and give up; it'll keep nudging you.

| Priority | How often you'll be reminded once it's overdue |
|---|---|
| 1 | Every hour |
| 2 | Every 6 hours |
| 3 | Every 12 hours |
| (none set) | Every 24 hours |

To set priority when you add a task, just say it's urgent, or name a level:

- "remind me to submit the application by Friday, this is urgent" (priority 1)
- "priority 2 task: renew my license"

To change it later, by number:
```
/priority 2 1
```
That sets task #2 to priority 1.

Or naturally: "make the dentist task priority 1" or "mark task 2 as urgent."

The nudges stop as soon as you mark the task done. There's no need to turn them off separately.

---

## Sending a task to someone else (admins)

If you're an admin, you can hand off one of your tasks to another person on the bot. It moves into their list and they start getting reminded about it instead of you.

- "send the grocery task to Sarah"
- "send #2 to Sarah" (using a number from your last list)

Or the command:
```
/transfer 2 Sarah
```

You can use a person's name if they've [set one](#setting-your-name), or their Telegram ID directly. The recipient needs to have messaged the bot at least once before, since Telegram doesn't allow a bot to message someone who's never started a conversation with it.

If you're not sure who's available to send a task to, see [Seeing who else uses the bot](#seeing-who-else-uses-the-bot-admins) below.

### Seeing who else uses the bot (admins)

```
/users
```

Lists everyone who's used the bot, their display name (if they've set one), and whether they're an admin. Handy for looking up who to `/transfer` a task to.

---

## Setting your name

By default, other people can only send you a task using your raw Telegram ID, which isn't very memorable. Set a friendly name instead:

```
/register Sarah
```

Now an admin can just say "send this to Sarah" instead of hunting down your ID.

---

## Checking your own info

```
/whoami
```

Shows your Telegram ID, your registered name (if you've set one), and whether you're a regular user or an admin.

---

## How reminders work

When you add a task with a due date, the bot schedules reminders leading up to it automatically. You don't need to set these yourself. Reminder times are shown in the timezone the bot is set up for, the same one everyone on it shares, worded plainly and to the point: just what's due and when, with no filler.

If a task's due time passes and it's still not marked done, the bot switches into overdue mode and keeps nudging you on a schedule based on its priority (see [Priority and overdue reminders](#priority-and-overdue-reminders)), until you mark it done.

Recurring tasks ("remind me every day...") automatically reschedule themselves after each reminder, so you never need to re-add them.

---

## Quick Reference

Nearly everything below also works as a plain sentence. These are just the direct shortcuts.

| Command | What it does |
|---|---|
| `/start` | Introduction and a short command list |
| `/list [category] [priority]` | Show your pending tasks, optionally filtered to a category and/or sorted by priority |
| `/category <name>` | Focus on one category until you switch back with `/category all` |
| `/done <number or description>` | Mark a task complete |
| `/delete <number or description>` | Permanently remove a task (cannot be undone) |
| `/edit <number> <field> <value>` | Change a task's description, category, priority, recurrence, or note (fields: `description`, `category`, `priority`, `recurrence`, `note`) |
| `/completeall [category]` | Mark all (or one category's) tasks complete, with confirmation |
| `/clear [category]` | Remove all (or one category's) tasks, with confirmation |
| `/note <number> [text]` | Set a note on a task, or view it if you leave the text off |
| `/priority <number> <1, 2, or 3>` | Set how urgently a task should be re-nudged once overdue |
| `/transfer <task> <name or id>` | Admins only. Send one of your tasks to someone else |
| `/users` | Admins only. List everyone who uses the bot |
| `/register <name>` | Set your display name |
| `/whoami` | See your own ID, name, and role |

For almost all of these, you can just say what you want in plain English, or send a voice message, instead of typing the command.

---

## Tips and troubleshooting

If the bot didn't understand a message, try rephrasing it more directly. For example, if a time didn't come through right, spell it out clearly, like "tomorrow at 3pm" rather than something vaguer.

If a voice message got misheard, the bot always echoes back what it heard before acting on it, so you'll see right away if something didn't come through right. Just send it again, or type it instead.

If referring to a task by number doesn't do what you expect, double check you've got the right number with `/list` -- each task's number is permanent and never changes, so unlike before, you don't need to re-run `/list` just to "refresh" it.

If you can't `/transfer` a task to someone, they need to have sent the bot at least one message already. Ask them to say hello to it first, then try again.

If nothing happens when you message the bot at all, you likely need to be added to the allowed users list. Check with whoever administers the bot.
