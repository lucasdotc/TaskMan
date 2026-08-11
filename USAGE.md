# How to Use TaskMan

TaskMan is a Telegram bot that remembers things for you. You talk to it the same way you'd text a person — no special syntax to memorize — and it stores your tasks, reminds you about them, and can do a bunch of handy organizing along the way.

This guide covers everything it can do, in plain language. If you just want a quick cheat sheet of commands, jump to [Quick Reference](#quick-reference) at the bottom.

## Table of Contents

- [Getting started](#getting-started)
- [Adding a task](#adding-a-task)
- [Talking instead of typing (voice messages)](#talking-instead-of-typing-voice-messages)
- [Seeing your tasks](#seeing-your-tasks)
- [Focusing on one category](#focusing-on-one-category)
- [Marking a task done or deleting it](#marking-a-task-done-or-deleting-it)
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

Open a chat with the bot in Telegram and send `/start`. It'll introduce itself and show a short list of commands. You don't need to remember any of them, though — for almost everything, you can just type what you want in your own words, the same way you'd ask a person to remind you of something.

If the bot doesn't respond at all, you may not be on its list of allowed users yet — ask whoever set it up to add your Telegram ID.

---

## Adding a task

Just tell the bot what you need to remember. All of these work:

- *"remind me to call mom tomorrow at 3pm"*
- *"buy groceries tomorrow morning"*
- *"submit the application by Friday at 5pm"*
- *"don't forget to renew my passport"* (no specific time — that's fine too)

The bot figures out the description and the due date/time on its own. It'll reply confirming what it saved and when it'll remind you.

**Repeating tasks:** say it naturally and it'll pick up the pattern —
- *"remind me every day to take my vitamins at 8am"*
- *"call mom on weekdays at 6pm"*

**Sorting into a category:** every task gets filed under a category — `personal`, `work`, `school`, or whatever fits. The bot guesses one from context, but you can say it directly:
- *"add a work task to send the Q3 report by Friday 5pm"*

**Marking something urgent:** if you don't say anything about urgency, the task just gets normal reminders. If you do, that changes how often you get reminded if it goes overdue — see [Priority and overdue reminders](#priority-and-overdue-reminders):
- *"this is urgent: submit the application by Friday at 5pm"*

---

## Talking instead of typing (voice messages)

You don't have to type at all — record a voice message in Telegram and send it like you would to a friend. The bot listens to it, tells you what it heard (so you can tell right away if it misheard something), and then handles it exactly like a typed message. Everything in this guide works from a voice message too — adding tasks, marking them done, asking what's on your list, all of it.

---

## Seeing your tasks

Ask, and the bot lists everything you still have pending:

- *"what tasks do I have?"*
- *"what do I have this week?"*

Or use the command: `/list`

Your tasks show up numbered and grouped by category, with their due dates. That number next to each task is important — you can use it to refer to that task in your very next message (see below), so you don't have to describe it in detail or type the whole thing out again.

**Only want to see one category?** Say so, or add it after the command:
- *"what work tasks do I have?"*
- `/list work`

---

## Focusing on one category

If you're going to be adding or managing several tasks in the same category in a row, you can tell the bot to "stay" on that category so you don't have to repeat it every time:

```
/category work
```

From then on, listing tasks, marking them done, adding new ones, and clearing them all default to `work` — until you switch. To go back to seeing everything again:

```
/category all
```

Type `/category` by itself any time to check what you're currently focused on.

---

## Marking a task done or deleting it

Just say it:

- *"mark the dentist appointment as done"*
- *"delete the grocery task"*
- *"mark number 2 as done"* (using the number from your last `/list`)

Or use the command, which accepts either the task's number or a description:

```
/done 2
/done dentist appointment
```

If your description matches more than one task, the bot will show you buttons to pick the right one instead of guessing.

---

## Handling several tasks at once

You don't have to do these one at a time:

- *"mark all as done"*
- *"mark all my work tasks as done"*
- *"remove all my tasks"*
- *"clear all my school tasks"*

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

Sometimes the short task description isn't enough — you want to jot down something extra like an address, a confirmation number, or what to bring. That's what notes are for.

**Setting a note**, by number:
```
/note 2 bring my insurance card and arrive 15 minutes early
```

Or naturally, describing the task instead of using a number:
- *"add a note to the dentist task: bring my insurance card"*

**Viewing a note** — just leave the text off:
```
/note 2
```
or ask: *"what's the note on the dentist task?"*

Notes don't show up in your regular task list — you have to ask for them specifically, so your list stays short and readable.

---

## Priority and overdue reminders

Every task can have a priority from 1 (most urgent) to 3 (least), or no priority at all. This controls what happens if a task's due date passes and you still haven't done it — the bot won't just remind you once and give up, it'll keep nudging you:

| Priority | How often you'll be reminded once it's overdue |
|---|---|
| **1** | Every hour |
| **2** | Every 6 hours |
| **3** | Every 12 hours |
| *(none set)* | Every 24 hours |

**Setting priority** when you add a task — just say it's urgent, or name a level:
- *"remind me to submit the application by Friday, this is urgent"* → priority 1
- *"priority 2 task: renew my license"*

**Changing it later**, by number:
```
/priority 2 1
```
(that sets task #2 to priority 1)

Or naturally: *"make the dentist task priority 1"* / *"mark task 2 as urgent"*.

The nudges stop as soon as you mark the task done — there's no need to turn them off separately.

---

## Sending a task to someone else (admins)

If you're an admin, you can hand off one of your tasks to another person on the bot — it moves into their list and they start getting reminded about it instead of you.

- *"send the grocery task to Sarah"*
- *"send #2 to Sarah"* (using a number from your last list)

Or the command:
```
/transfer 2 Sarah
```

You can use a person's name if they've [set one](#setting-your-name), or their Telegram ID directly. **The recipient needs to have messaged the bot at least once before** — Telegram doesn't allow a bot to message someone who's never started a conversation with it.

Not sure who's available to send a task to? See [Checking who else uses the bot](#seeing-who-else-uses-the-bot-admins) below.

### Seeing who else uses the bot (admins)

```
/users
```

Lists everyone who's used the bot, their display name (if they've set one), and whether they're an admin — handy for looking up who to `/transfer` a task to.

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

When you add a task with a due date, the bot schedules reminders leading up to it automatically — you don't need to set these yourself. Reminder times are shown in the timezone the bot is set up for (the same one everyone on it shares), worded plainly and to the point — just what's due and when, no filler.

If a task's due time passes and it's still not marked done, the bot switches into overdue mode and keeps nudging you on a schedule based on its priority (see [Priority and overdue reminders](#priority-and-overdue-reminders)) — until you mark it done.

Recurring tasks (*"remind me every day..."*) automatically reschedule themselves after each reminder, so you never need to re-add them.

---

## Quick Reference

Nearly everything below also works as a plain sentence — these are just the direct shortcuts.

| Command | What it does |
|---|---|
| `/start` | Introduction and a short command list |
| `/list [category]` | Show your pending tasks, optionally just one category |
| `/category <name>` | Focus on one category until you switch back with `/category all` |
| `/done <number or description>` | Mark a task complete |
| `/completeall [category]` | Mark all (or one category's) tasks complete, with confirmation |
| `/clear [category]` | Remove all (or one category's) tasks, with confirmation |
| `/note <number> [text]` | Set a note on a task, or view it if you leave the text off |
| `/priority <number> <1, 2, or 3>` | Set how urgently a task should be re-nudged once overdue |
| `/transfer <task> <name or id>` | *(admins)* Send one of your tasks to someone else |
| `/users` | *(admins)* List everyone who uses the bot |
| `/register <name>` | Set your display name |
| `/whoami` | See your own ID, name, and role |

And remember: for almost all of these, you can just say what you want in plain English (or send a voice message) instead of typing the command.

---

## Tips and troubleshooting

- **The bot didn't understand a message.** Try rephrasing it more directly — e.g. if a time didn't come through right, spell it out clearly ("tomorrow at 3pm" rather than something vaguer).
- **A voice message got misheard.** The bot always echoes back what it heard before acting on it, so you'll see right away if something didn't come through right — just send it again, or type it instead.
- **Referring to a task by number stopped working.** That number is tied to your *most recent* `/list` — if you haven't listed your tasks recently, or a task's position shifted since then, run `/list` again first to refresh which number means what.
- **You can't `/transfer` a task to someone.** They need to have sent the bot at least one message already — ask them to say hello to it first, then try again.
- **Nothing happens when you message the bot at all.** You likely need to be added to the allowed users list — check with whoever administers the bot.
