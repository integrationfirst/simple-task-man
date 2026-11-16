"""Local JSON task store."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

# Workflow: "" (backlog) -> PLANNING -> PLANNED -> IN_PROGRESS -> DONE
BACKLOG, DONE = "", "DONE"
STATUSES = (BACKLOG, "PLANNING", "PLANNED", "IN_PROGRESS", DONE)
# Older files and loose input ("in progress", "backlog", ...) map onto the workflow.
_STATUS_ALIASES = {"TODO": BACKLOG, "BACKLOG": BACKLOG, "NONE": BACKLOG, "INPROGRESS": "IN_PROGRESS"}


def normalize_status(value: str | None) -> str:
    key = (value or "").strip().upper().replace(" ", "_").replace("-", "_")
    key = _STATUS_ALIASES.get(key.replace("_", ""), _STATUS_ALIASES.get(key, key))
    if key not in STATUSES:
        raise ValueError(f"Invalid status {value!r}; choose one of: \"\" (backlog), "
                         + ", ".join(STATUSES[1:]) + ".")
    return key
PRIORITIES = ("low", "medium", "high", "urgent")
PRIORITY_RANK = {p: i for i, p in enumerate(reversed(PRIORITIES))}  # urgent=0 ... low=3


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_date(value: str | None) -> str | None:
    """Validate a YYYY-MM-DD date string; empty/None clears it."""
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"Invalid date {value!r}; use YYYY-MM-DD.") from None


@dataclass
class Task:
    id: int
    title: str
    notes: str = ""
    status: str = BACKLOG
    priority: str = "medium"
    due: str | None = None
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str | None = None
    notes_original: str = ""  # the user's own wording, kept when `notes` was rephrased

    def is_overdue(self, today: date | None = None) -> bool:
        today = today or date.today()
        return self.status != DONE and self.due is not None and date.fromisoformat(self.due) < today


CHALLENGE_STATUSES = ("OPEN", "RESOLVED")


@dataclass
class Challenge:
    """A risk/blocker that persists across listings and reports until resolved."""
    id: int
    text: str
    status: str = "OPEN"
    task_id: int | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    resolved_at: str | None = None
    text_original: str = ""  # the user's own wording, kept when `text` was rephrased

    def open_on(self, day: date) -> bool:
        """Was this challenge open at the end of `day`?"""
        if datetime.fromisoformat(self.created_at).date() > day:
            return False
        return self.resolved_at is None or datetime.fromisoformat(self.resolved_at).date() > day


class TaskStore:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._next_id = 1
        self._next_challenge_id = 1
        self.tasks: dict[int, Task] = {}
        self.challenges: dict[int, Challenge] = {}
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._next_id = data.get("next_id", 1)
        migrated = False
        for raw in data.get("tasks", []):
            t = Task(**raw)
            status = normalize_status(t.status)
            if status != t.status:
                t.status, migrated = status, True
            self.tasks[t.id] = t
        if migrated:  # one-time upgrade from the old todo/in_progress/done values; keep a backup
            backup = self.path.with_name(self.path.name + ".bak")
            if not backup.exists():
                backup.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
        self._next_id = max([self._next_id, *(i + 1 for i in self.tasks)])
        for raw in data.get("challenges", []):
            c = Challenge(**raw)
            self.challenges[c.id] = c
        self._next_challenge_id = max([data.get("next_challenge_id", 1), *(i + 1 for i in self.challenges)])
        if migrated:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"next_id": self._next_id, "tasks": [asdict(t) for t in self.tasks.values()],
                   "next_challenge_id": self._next_challenge_id,
                   "challenges": [asdict(c) for c in self.challenges.values()]}
        # Atomic write: temp file in the same dir, then replace.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---- CRUD --------------------------------------------------------
    def get(self, task_id: int) -> Task:
        if task_id not in self.tasks:
            raise KeyError(f"No task with id {task_id}.")
        return self.tasks[task_id]

    def add(self, title: str, notes: str = "", priority: str = "medium", due: str | None = None,
            project: str | None = None, tags: list[str] | None = None, status: str = BACKLOG,
            notes_original: str = "") -> Task:
        if not title.strip():
            raise ValueError("Title must not be empty.")
        _check_choice("priority", priority, PRIORITIES)
        status = normalize_status(status)
        task = Task(id=self._next_id, title=title.strip(), notes=notes, priority=priority, status=status,
                    due=parse_date(due), project=project or None, tags=_clean_tags(tags),
                    completed_at=now_iso() if status == DONE else None, notes_original=notes_original.strip())
        self.tasks[task.id] = task
        self._next_id += 1
        self.save()
        return task

    def update(self, task_id: int, **changes) -> Task:
        task = self.get(task_id)
        for key, value in changes.items():
            if value is None:
                continue
            if key == "title":
                if not value.strip():
                    raise ValueError("Title must not be empty.")
                task.title = value.strip()
            elif key == "notes":
                task.notes = value
                task.notes_original = (changes.get("notes_original") or "").strip()
            elif key == "notes_original":
                continue  # handled together with notes
            elif key == "priority":
                _check_choice("priority", value, PRIORITIES)
                task.priority = value
            elif key == "status":
                task.status = normalize_status(value)
                task.completed_at = now_iso() if task.status == DONE else None
            elif key == "due":
                task.due = parse_date(value)
            elif key == "project":
                task.project = value or None
            elif key == "tags":
                task.tags = _clean_tags(value)
            else:
                raise ValueError(f"Unknown field {key!r}.")
        task.updated_at = now_iso()
        self.save()
        return task

    def complete(self, task_id: int) -> Task:
        return self.update(task_id, status=DONE)

    def delete(self, task_id: int) -> Task:
        task = self.get(task_id)
        del self.tasks[task_id]
        self.save()
        return task

    def query(self, status: str | None = None, priority: str | None = None, project: str | None = None,
              tag: str | None = None, text: str | None = None, due_before: str | None = None,
              include_done: bool = False) -> list[Task]:
        due_before = parse_date(due_before)
        out = []
        for t in self.tasks.values():
            if status is not None and t.status != normalize_status(status):
                continue
            if status is None and not include_done and t.status == DONE:
                continue
            if priority and t.priority != priority:
                continue
            if project and (t.project or "").lower() != project.lower():
                continue
            if tag and tag.lower() not in t.tags:
                continue
            if text and text.lower() not in f"{t.title}\n{t.notes}".lower():
                continue
            if due_before and (t.due is None or t.due > due_before):
                continue
            out.append(t)
        return sort_tasks(out)


    # ---- challenges --------------------------------------------------
    def get_challenge(self, challenge_id: int) -> Challenge:
        if challenge_id not in self.challenges:
            raise KeyError(f"No challenge with id {challenge_id}.")
        return self.challenges[challenge_id]

    def add_challenge(self, text: str, task_id: int | None = None, text_original: str = "") -> Challenge:
        if not text.strip():
            raise ValueError("Challenge text must not be empty.")
        if task_id is not None:
            self.get(task_id)
        c = Challenge(id=self._next_challenge_id, text=text.strip(), task_id=task_id,
                      text_original=text_original.strip())
        self.challenges[c.id] = c
        self._next_challenge_id += 1
        self.save()
        return c

    def update_challenge(self, challenge_id: int, text: str | None = None, status: str | None = None,
                         task_id: int | None = None, text_original: str | None = None) -> Challenge:
        c = self.get_challenge(challenge_id)
        if text is not None:
            if not text.strip():
                raise ValueError("Challenge text must not be empty.")
            c.text = text.strip()
            c.text_original = (text_original or "").strip()
        if status is not None:
            status = status.strip().upper()
            _check_choice("challenge status", status, CHALLENGE_STATUSES)
            if status != c.status:
                c.status = status
                c.resolved_at = now_iso() if status == "RESOLVED" else None
        if task_id is not None:
            if task_id == 0:
                c.task_id = None
            else:
                self.get(task_id)
                c.task_id = task_id
        c.updated_at = now_iso()
        self.save()
        return c

    def delete_challenge(self, challenge_id: int) -> Challenge:
        c = self.get_challenge(challenge_id)
        del self.challenges[challenge_id]
        self.save()
        return c

    def list_challenges(self, include_resolved: bool = False) -> list[Challenge]:
        return sorted((c for c in self.challenges.values() if include_resolved or c.status == "OPEN"),
                      key=lambda c: (c.status != "OPEN", c.id))


def sort_tasks(tasks: list[Task]) -> list[Task]:
    """Open first, then by due date (none last), then priority, then id."""
    return sorted(tasks, key=lambda t: (t.status == DONE, t.due or "9999-99-99",
                                        PRIORITY_RANK[t.priority], t.id))


def _check_choice(name: str, value: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ValueError(f"Invalid {name} {value!r}; choose one of {', '.join(choices)}.")


def _clean_tags(tags: list[str] | None) -> list[str]:
    return sorted({t.strip().lower().lstrip("#") for t in (tags or []) if t.strip()})
