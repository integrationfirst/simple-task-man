"""Tool schemas exposed to Claude, and their dispatcher."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Callable

from .report import write_report
from .store import PRIORITIES, STATUSES, TaskStore

WRITING_RULES = """Business writing for task notes and challenges:
When the user gives you a task note or a challenge, act as the business reader (a manager or stakeholder \
reading the weekly report) and rephrase it before saving:
- Plain, professional English (translate if the user wrote in another language), even if the input is \
shorthand or informal. Expand abbreviations a business reader might not know.
- Be frank and specific: lead with the point. For a task note, state the objective and the next step \
(with owner, dependency or date when known). For a challenge, state the problem plainly, its impact on \
the work, and what is needed to resolve it (decision, input, person) when known.
- 1–3 short sentences. Keep every fact, name, system and date; never add facts, owners or deadlines the \
user didn't give, and don't soften real risks.
- No task numbers in the text, no emoji, no **highlight** markers (highlighting happens at report time).
- Save your version in notes/text and the user's exact words in notes_original/text_original, then show \
the saved wording in your confirmation so the user can correct it.
- If the user asks to keep their wording ("verbatim", "as is"), save it unchanged and leave the original empty.

"""

CHALLENGE_RULES = """Challenges are a saved list (the challenge tools), not something you write fresh each time:
- When the user mentions a risk, blocker, dependency or concern, record it with add_challenge, rephrased per \
the business writing rules (link task_id when it concerns one task). Don't record duplicates: check the open \
challenges first and update an existing one instead.
- When the user says a challenge is solved or no longer relevant, set it to RESOLVED with update_challenge.
- When a task linked to open challenges is completed, the result lists them: ask the user whether to resolve them.
- Never invent challenges in listings or reports. If you notice a risk that isn't recorded (e.g. an overdue \
task), suggest recording it in one line after your answer; record it only if the user agrees.
"""

LIST_TEMPLATE = WRITING_RULES + CHALLENGE_RULES + """
Whenever the user asks to see, list, show or review tasks, call list_tasks and ALWAYS reply \
with exactly these three sections, in this order:

## Tasks
| # | Task | Priority | Status |
|---|---|---|---|
| <id> | <title> | <priority> | <status> |
(one row per task returned, in the order returned; show the status exactly as stored — "", PLANNING, \
PLANNED, IN_PROGRESS or DONE — leaving the cell empty for backlog tasks; \
if there are no tasks, write a single row: | – | No tasks | – | – |)

## Executive Notes
- First bullet: a one- or two-sentence summary of the overall status (how many tasks are open/done, what is \
due soonest, what needs attention).
- Then one bullet per returned task that is not DONE and has notes: the task's notes copied exactly (no \
rewording), followed by "(#<id>)".
- In every bullet, wrap the 1–3 phrases the reader must notice (people to contact, decisions, dependencies, \
dates, key figures) in **double asterisks**; never a whole sentence.

## Challenges
- One bullet per entry in open_challenges from the list_tasks result, text copied exactly as stored, \
in the order given, followed by "(#<task_id>)" when it has a task_id. If it is empty, write "- No open challenges."
"""

STATUS_HELP = ('Workflow: "" (empty = backlog, the default for new tasks) -> PLANNING -> PLANNED -> '
               "IN_PROGRESS -> DONE.")

_DATE = {"type": "string", "description": "Date as YYYY-MM-DD."}
_TAGS = {"type": "array", "items": {"type": "string"}, "description": "Lowercase tags, no '#'."}

TOOLS: list[dict] = [
    {
        "name": "add_task",
        "description": "Create a new task. Resolve relative dates (e.g. 'next Friday') to YYYY-MM-DD using today's date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short, action-oriented title."},
                "notes": {"type": "string",
                          "description": "Business-friendly rephrasing of the user's note (see writing rules)."},
                "notes_original": {"type": "string",
                                   "description": "The user's exact words, when notes is a rephrasing."},
                "status": {"type": "string", "enum": list(STATUSES),
                           "description": STATUS_HELP + " Omit to leave the task in the backlog."},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "due": _DATE,
                "project": {"type": "string"},
                "tags": _TAGS,
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List/search tasks. With no filters returns all tasks except DONE, sorted by due date then "
                       "priority. Also returns open_challenges (the saved list). Use this to find a task's id "
                       "before updating, completing or deleting it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(STATUSES),
                           "description": STATUS_HELP},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "project": {"type": "string"},
                "tag": {"type": "string"},
                "text": {"type": "string", "description": "Case-insensitive substring match on title and notes."},
                "due_before": {**_DATE, "description": "Only tasks due on or before this date (YYYY-MM-DD)."},
                "include_done": {"type": "boolean", "description": "Include DONE tasks when no status is given."},
            },
        },
    },
    {
        "name": "update_task",
        "description": "Change fields of an existing task. Only pass fields that should change. "
                       "Pass an empty string for due/project to clear them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "notes": {"type": "string",
                          "description": "Business-friendly rephrasing of the user's note (see writing rules)."},
                "notes_original": {"type": "string",
                                   "description": "The user's exact words, when notes is a rephrasing."},
                "status": {"type": "string", "enum": list(STATUSES),
                           "description": STATUS_HELP},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "due": {"type": "string", "description": "YYYY-MM-DD, or empty string to clear."},
                "project": {"type": "string"},
                "tags": {**_TAGS, "description": "Replaces the full tag list."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "complete_task",
        "description": "Set one or more tasks to DONE.",
        "input_schema": {
            "type": "object",
            "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
            "required": ["ids"],
        },
    },
    {
        "name": "delete_task",
        "description": "Permanently delete a task. The user is asked to confirm before it runs. "
                       "Prefer complete_task for finished work; only delete when the user asks to remove/delete.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
    {
        "name": "list_challenges",
        "description": "List saved challenges (open by default). Use to find a challenge's id or review history.",
        "input_schema": {
            "type": "object",
            "properties": {"include_resolved": {"type": "boolean"}},
        },
    },
    {
        "name": "add_challenge",
        "description": "Record a new challenge (risk, blocker, dependency), rephrased for a business reader "
                       "per the writing rules.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "Business-friendly rephrasing: the problem, its impact, what is needed."},
                "text_original": {"type": "string", "description": "The user's exact words, when text is a rephrasing."},
                "task_id": {"type": "integer", "description": "Related task, if the challenge concerns one task."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "update_challenge",
        "description": "Reword a challenge, link it to a task (task_id 0 unlinks), or change its status: "
                       "RESOLVED when it is solved or no longer relevant, OPEN to reopen it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "text": {"type": "string", "description": "New business-friendly wording."},
                "text_original": {"type": "string", "description": "The user's exact words, when text is a rephrasing."},
                "status": {"type": "string", "enum": ["OPEN", "RESOLVED"]},
                "task_id": {"type": "integer"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "delete_challenge",
        "description": "Delete a challenge recorded by mistake. For challenges that were real but are now over, "
                       "use update_challenge with status RESOLVED instead so the history is kept.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
    {
        "name": "generate_weekly_report",
        "description": "Write the weekly status report (Markdown + HTML) for the Mon–Sun week containing week_of "
                       "(default: this week). There is one report per week: generating again in the same week "
                       "overwrites it. The report lists open tasks plus tasks completed that week in a Task / "
                       "Priority / Status table. Executive Notes = your summary bullet, then the notes of every open "
                       "task listed verbatim with its task number. Challenges come from the saved challenge list "
                       "(open ones, plus ones resolved that week). Before calling, review the tasks with "
                       "list_tasks, then write the summary for a business audience: professional, concise, plain "
                       "language, no jargon, no emoji.",
        "input_schema": {
            "type": "object",
            "properties": {
                "week_of": {**_DATE, "description": "Any date inside the target week."},
                "summary": {
                    "type": "string",
                    "description": "The first Executive Notes bullet: one or two sentences on the overall status "
                                   "of the week (progress, completed vs open work, what needs attention). Wrap the "
                                   "1–3 phrases the reader must notice in **double asterisks**.",
                },
                "note_highlights": {
                    "type": "array",
                    "description": "Phrases to highlight inside the open tasks' notes, which the report lists "
                                   "verbatim after the summary. Pick the 1–2 phrases per note a reader must notice "
                                   "(people to contact, decisions, dependencies, dates). Each phrase must be copied "
                                   "exactly from that task's notes; never a whole sentence.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "integer"},
                            "phrases": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["task_id", "phrases"],
                    },
                },
            },
        },
    },
]


def _task_json(t) -> dict:
    d = asdict(t)
    if t.is_overdue():
        d["overdue"] = True
    return d


class ToolExecutor:
    def __init__(self, store: TaskStore, reports_dir: str | Path,
                 confirm: Callable[[str], bool] = lambda _msg: True):
        self.store = store
        self.reports_dir = Path(reports_dir)
        self.confirm = confirm

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """Execute a tool. Returns (result_text, is_error)."""
        try:
            handler = getattr(self, f"_{name}", None)
            if handler is None:
                raise ValueError(f"Unknown tool {name!r}.")
            return json.dumps(handler(**args), ensure_ascii=False), False
        except (KeyError, ValueError, TypeError) as exc:
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            return f"Error: {msg}", True

    # ---- tasks -------------------------------------------------------
    def _add_task(self, **kw):
        return {"created": _task_json(self.store.add(**kw))}

    def _list_tasks(self, **kw):
        tasks = self.store.query(**kw)
        return {"count": len(tasks), "today": date.today().isoformat(), "tasks": [_task_json(t) for t in tasks],
                "open_challenges": [asdict(c) for c in self.store.list_challenges()]}

    def _update_task(self, id: int, **changes):
        task = self.store.update(id, **changes)
        return {"updated": _task_json(task), **self._linked_challenges([task.id] if task.status == "DONE" else [])}

    def _complete_task(self, ids: list[int]):
        done, errors = [], []
        for i in ids:
            try:
                done.append(self.store.complete(i).id)
            except KeyError as exc:
                errors.append(exc.args[0])
        return {"completed": done, "errors": errors, **self._linked_challenges(done)}

    def _linked_challenges(self, task_ids: list[int]) -> dict:
        linked = [asdict(c) for c in self.store.list_challenges() if c.task_id in task_ids]
        if not linked:
            return {}
        return {"linked_open_challenges": linked,
                "note": "These open challenges are linked to the completed task(s); ask the user whether to resolve them."}

    def _delete_task(self, id: int):
        task = self.store.get(id)
        if not self.confirm(f"Delete task #{task.id} '{task.title}'?"):
            return {"deleted": None, "note": "User declined the deletion. Do not retry."}
        self.store.delete(id)
        return {"deleted": id}

    # ---- challenges --------------------------------------------------
    def _list_challenges(self, include_resolved: bool = False):
        return {"challenges": [asdict(c) for c in self.store.list_challenges(include_resolved)]}

    def _add_challenge(self, text: str, task_id: int | None = None, text_original: str = ""):
        return {"created": asdict(self.store.add_challenge(text, task_id, text_original))}

    def _update_challenge(self, id: int, **changes):
        return {"updated": asdict(self.store.update_challenge(id, **changes))}

    def _delete_challenge(self, id: int):
        c = self.store.get_challenge(id)
        if not self.confirm(f"Delete challenge #{c.id} '{c.text}'?"):
            return {"deleted": None, "note": "User declined the deletion. Do not retry."}
        self.store.delete_challenge(id)
        return {"deleted": id}

    # ---- report ------------------------------------------------------
    def _generate_weekly_report(self, week_of: str | None = None, summary: str | None = None,
                                note_highlights: list[dict] | None = None):
        day = date.fromisoformat(week_of) if week_of else date.today()
        highlights: dict[int, list[str]] = {}
        for item in note_highlights or []:
            highlights.setdefault(int(item["task_id"]), []).extend(item.get("phrases") or [])
        md, html, replaced = write_report(self.store, self.reports_dir, day, summary, highlights)
        return {"markdown": str(md), "html": str(html), "replaced_existing_report": replaced}
