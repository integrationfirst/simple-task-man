"""Weekly status report (Markdown + HTML), one file pair per ISO week.

Layout matches the chat listing template: Tasks table, Executive Notes, Challenges.
Executive Notes: one summary bullet (written by Claude, or rule-based) followed by the notes of open tasks.
Challenges come from the saved challenge list.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .store import DONE, Task, TaskStore, sort_tasks

PRIORITY_LABEL = {"low": "Low", "medium": "Medium", "high": "High", "urgent": "Urgent"}
THEMES = ("auto", "light", "dark")


@dataclass
class Note:
    """A report bullet, optionally tied to task numbers shown as "(#3, #5)"."""
    text: str
    task_ids: list[int] = field(default_factory=list)
    resolved: bool = False  # challenges only


@dataclass
class WeekData:
    start: date
    end: date
    today: date
    tasks: list[Task]  # open tasks + tasks completed during the week
    challenges: list[Note]  # open at week end + resolved during it

    @property
    def label(self) -> str:
        y, w, _ = self.start.isocalendar()
        return f"{y}-W{w:02d}"

    @property
    def period(self) -> str:
        return f"{self.start:%d %b %Y} – {self.end:%d %b %Y}"


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())  # Monday
    return start, start + timedelta(days=6)


def _in_range(ts: str | None, start: date, end: date) -> bool:
    return ts is not None and start <= datetime.fromisoformat(ts).date() <= end


def collect(store: TaskStore, week_of: date, today: date | None = None) -> WeekData:
    """`today` is the reference for "overdue"; defaults to min(real today, end of week)."""
    start, end = week_bounds(week_of)
    today = today or min(date.today(), end)
    tasks = [t for t in store.tasks.values()
             if t.status != DONE or _in_range(t.completed_at, start, end)]
    saved = store.list_challenges(include_resolved=True)
    ids = lambda c: [c.task_id] if c.task_id else []  # noqa: E731
    challenges = [Note(c.text, ids(c)) for c in saved if c.open_on(today)]
    challenges += [Note(c.text, ids(c), resolved=True) for c in saved
                   if c.resolved_at and _in_range(c.resolved_at, start, today)]
    return WeekData(start, end, today, sort_tasks(tasks), challenges)


# ---- rule-based bullets (used when Claude doesn't provide them) ----------------
def _fmt(d: str) -> str:
    return date.fromisoformat(d).strftime("%a %d %b")


def default_summary(d: WeekData) -> str:
    open_t = [t for t in d.tasks if t.status != DONE]
    done = len(d.tasks) - len(open_t)
    hi = sum(t.priority in ("high", "urgent") for t in open_t)
    text = f"**{done} task{'s' if done != 1 else ''} completed** this week; **{len(open_t)} remain open**"
    if hi:
        text += f", {hi} of them **high priority**"
    upcoming = sorted(t.due for t in open_t if t.due and t.due >= d.today.isoformat())
    if upcoming:
        text += f"; next deadline **{_fmt(upcoming[0])}**"
    return text + "."


def apply_highlights(text: str, phrases: list[str]) -> str:
    """Wrap each phrase (exact, case-insensitive match) in **…**; the text itself is never changed."""
    for phrase in phrases:
        phrase = phrase.strip().strip("*")
        if not phrase:
            continue
        m = re.search(re.escape(phrase), text, re.IGNORECASE)
        if not m:
            raise ValueError(f"Highlight {phrase!r} does not appear in the note: {text!r}.")
        text = f"{text[:m.start()]}**{m.group(0)}**{text[m.end():]}"
    return text


def task_notes(d: WeekData, highlights: dict[int, list[str]] | None = None) -> list[Note]:
    """The notes of open tasks, verbatim (whitespace collapsed), in table order, tagged with the task number."""
    highlights = highlights or {}
    unknown = [i for i in highlights if not any(t.id == i and t.status != DONE and t.notes.strip() for t in d.tasks)]
    if unknown:
        raise ValueError(f"No open task with notes for highlight task number(s): {', '.join(map(str, unknown))}.")
    return [Note(apply_highlights(" ".join(t.notes.split()), highlights.get(t.id, [])), [t.id])
            for t in d.tasks if t.status != DONE and t.notes.strip()]


# ---- rendering ---------------------------------------------------------------
def _refs(ids: list[int]) -> str:
    return "(" + ", ".join(f"#{i}" for i in ids) + ")" if ids else ""


def _md_note(n: Note) -> str:
    return "- " + ("*Resolved:* " if n.resolved else "") + " ".join(filter(None, [n.text, _refs(n.task_ids)]))


def render_markdown(d: WeekData, notes: list[Note]) -> str:
    lines = [f"# Weekly Status Report — {d.label}", "", f"**Reporting period:** {d.period}", "",
             "## Tasks", "", "| # | Task | Priority | Status |", "|---|---|---|---|"]
    for t in d.tasks:
        pri = PRIORITY_LABEL[t.priority]
        pri = f"**{pri}**" if t.priority in ("high", "urgent") else pri
        lines.append(f"| {t.id} | {t.title.replace('|', '/')} | {pri} | {t.status} |")
    if not d.tasks:
        lines.append("| – | No tasks | – | – |")
    lines += ["", "## Executive Notes", "", *map(_md_note, notes), "", "## Challenges", ""]
    lines += list(map(_md_note, d.challenges)) or ["- No open challenges."]
    lines.append("")
    return "\n".join(lines)


_HIGHLIGHT = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    """Escape HTML, then turn **phrase** into a highlighted <mark>."""
    return _HIGHLIGHT.sub(r"<mark>\1</mark>", html.escape(text))


# HTML palettes as CSS custom properties; dark keeps the light bar colours.
_LIGHT = ("--bg:#f4f5f7;--card:#fff;--fg:#1f2937;--muted:#6b7280;--line:#e5e7eb;--head:#1e3a5f;\n"
          "--high-bg:#fff4e5;--high-bar:#f59e0b;--urgent-bg:#fdecec;--urgent-bar:#dc2626;--mark-bg:#fff3b0;--mark-fg:#1f2937")
_DARK = ("--bg:#111827;--card:#1f2937;--fg:#f3f4f6;--muted:#9ca3af;--line:#374151;\n"
         "--head:#93c5fd;--high-bg:#3b2a12;--urgent-bg:#3f1d1d;--mark-bg:#5b4a0f;--mark-fg:#fef9c3")
_THEME_CSS = {
    "auto": f":root{{{_LIGHT}}}\n@media (prefers-color-scheme:dark){{:root{{{_DARK}}}}}",
    "light": f":root{{{_LIGHT}}}",
    "dark": f":root{{{_LIGHT}}}\n:root{{{_DARK}}}",
}


def _html_note(n: Note) -> str:
    tag = '<span class="tag">Resolved</span> ' if n.resolved else ""
    refs = "".join(f' <span class="ref">#{i}</span>' for i in n.task_ids)
    return f'<li{" class=\"resolved\"" if n.resolved else ""}>{tag}{_inline(n.text)}{refs}</li>'


def render_html(d: WeekData, notes: list[Note], theme: str = "auto") -> str:
    e = html.escape
    rows = "".join(
        f'<tr class="p-{t.priority} s-{(t.status or 'backlog').lower()}"><td class="num">{t.id}</td><td>{e(t.title)}</td>'
        f'<td><span class="pri">{PRIORITY_LABEL[t.priority]}</span></td><td class="status">{t.status}</td></tr>'
        for t in d.tasks
    ) or '<tr><td colspan="4" class="empty">No tasks</td></tr>'
    note_items = "".join(map(_html_note, notes))
    challenge_items = "".join(map(_html_note, d.challenges)) or "<li>No open challenges.</li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weekly Status Report {d.label}</title>
<style>
{_THEME_CSS[theme]}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 "Segoe UI",system-ui,sans-serif}}
main{{max-width:820px;margin:32px auto;padding:32px;background:var(--card);border-radius:8px;
box-shadow:0 1px 3px rgba(0,0,0,.08)}}
header{{border-bottom:3px solid var(--head);padding-bottom:12px;margin-bottom:24px}}
h1{{margin:0;font-size:24px;color:var(--head)}} .period{{margin:4px 0 0;color:var(--muted)}}
h2{{font-size:16px;text-transform:uppercase;letter-spacing:.06em;color:var(--head);margin:28px 0 10px}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
border-bottom:2px solid var(--line);padding:8px 12px}}
td{{padding:10px 12px;border-bottom:1px solid var(--line)}}
tr.p-high td{{background:var(--high-bg)}} tr.p-high td:first-child{{box-shadow:inset 4px 0 var(--high-bar)}}
tr.p-urgent td{{background:var(--urgent-bg)}} tr.p-urgent td:first-child{{box-shadow:inset 4px 0 var(--urgent-bar)}}
tr.p-high .pri,tr.p-urgent .pri{{font-weight:600}} tr.s-done td{{color:var(--muted)}}
td.status{{font-family:ui-monospace,Consolas,monospace;font-size:13px;letter-spacing:.02em}}
td.empty{{color:var(--muted);text-align:center}}
th.num,td.num{{width:1%;white-space:nowrap;color:var(--muted);font-variant-numeric:tabular-nums}}
.ref{{font-size:12px;font-weight:600;color:var(--head);border:1px solid var(--line);border-radius:10px;
padding:0 7px;white-space:nowrap}}
ul{{margin:0;padding-left:20px}} li{{margin:4px 0}}
li.resolved{{color:var(--muted)}}
.tag{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;border:1px solid var(--line);border-radius:4px;
padding:0 5px;margin-right:4px}}
mark{{background:var(--mark-bg);color:var(--mark-fg);font-weight:600;padding:0 3px;border-radius:3px}}
@media print{{mark{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
footer{{margin-top:32px;font-size:12px;color:var(--muted)}}
@media (max-width:600px){{main{{margin:0;border-radius:0;padding:20px 16px}}}}
@media print{{:root{{{_LIGHT}}} body{{background:#fff}} main{{box-shadow:none;margin:0}}}}
</style></head>
<body><main>
<header><h1>Weekly Status Report — {d.label}</h1><p class="period">Reporting period: {d.period}</p></header>
<h2>Tasks</h2>
<table><thead><tr><th class="num">#</th><th>Task</th><th>Priority</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Executive Notes</h2><ul>{note_items}</ul>
<h2>Challenges</h2><ul>{challenge_items}</ul>
<footer>Prepared {datetime.now():%d %b %Y, %H:%M}</footer>
</main></body></html>
"""


def write_report(store: TaskStore, out_dir: str | Path, week_of: date | None = None, summary: str | None = None,
                 highlights: dict[int, list[str]] | None = None, theme: str = "auto") -> tuple[Path, Path, bool]:
    """Write week-YYYY-Www.{md,html}; a report for the same week is overwritten.

    Executive Notes = one summary bullet (Claude's, or rule-based) + the notes of open tasks.
    Returns (md_path, html_path, replaced_existing).
    """
    if theme not in THEMES:
        raise ValueError(f"Unknown theme {theme!r}; use auto, light or dark.")
    d = collect(store, week_of or date.today())
    notes = [Note((summary or "").strip() or default_summary(d)), *task_notes(d, highlights)]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path, html_path = out / f"week-{d.label}.md", out / f"week-{d.label}.html"
    replaced = md_path.exists() or html_path.exists()
    md_path.write_text(render_markdown(d, notes), encoding="utf-8")
    html_path.write_text(render_html(d, notes, theme), encoding="utf-8")
    return md_path, html_path, replaced
