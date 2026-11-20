"""Terminal entry point: `python -m taskman [chat|list|report]`."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .report import THEMES, write_report
from .store import TaskStore
from .tools import ToolExecutor

DATA_DIR = Path(os.environ.get("TASKMAN_HOME", Path.cwd() / "data"))


def _store() -> TaskStore:
    return TaskStore(DATA_DIR / "tasks.json")


def _confirm(msg: str) -> bool:
    try:
        return input(f"  ? {msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_chat(_args) -> int:
    from .agent import MODEL, TaskAgent

    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("The 'anthropic' package is required for chat: pip install anthropic", file=sys.stderr)
        return 1
    executor = ToolExecutor(_store(), DATA_DIR / "reports", confirm=_confirm)
    agent = TaskAgent(executor, on_tool=lambda name, args: print(f"  · {name} {args}", file=sys.stderr))
    print(f"taskman · {MODEL} · data in {DATA_DIR}\nTell me what to do. 'exit' or Ctrl-D to quit.\n")
    while True:
        try:
            line = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in ("exit", "quit", "q"):
            return 0
        turn_start = len(agent.messages)
        try:
            reply = agent.send(line)
        except KeyboardInterrupt:
            print("\n(interrupted)")
            del agent.messages[turn_start:]  # drop the half-finished turn so history stays valid
            continue
        except Exception as exc:  # API/network errors: report and keep the session alive
            print(f"error: {exc}", file=sys.stderr)
            del agent.messages[turn_start:]
            continue
        print(f"\nclaude › {reply}\n")


def cmd_list(args) -> int:
    store = _store()
    tasks = store.query(include_done=args.all)
    if not tasks:
        print("No tasks.")
    for t in tasks:
        mark = {"DONE": "x", "IN_PROGRESS": "~", "PLANNED": "p", "PLANNING": "."}.get(t.status, " ")
        extra = " ".join(filter(None, [t.status or "backlog", t.priority, f"due {t.due}" if t.due else "",
                                       "OVERDUE" if t.is_overdue() else "", t.project or "",
                                       " ".join(f"#{x}" for x in t.tags)]))
        print(f"[{mark}] #{t.id:<3} {t.title}  ({extra})")
    challenges = store.list_challenges()
    if challenges:
        print("\nOpen challenges:")
        for c in challenges:
            print(f"  ! C{c.id} {c.text}" + (f"  (task #{c.task_id})" if c.task_id else ""))
    return 0


def cmd_report(args) -> int:
    week_of = date.fromisoformat(args.week) if args.week else None
    md, html, replaced = write_report(_store(), DATA_DIR / "reports", week_of, theme=args.theme)
    verb = "Updated" if replaced else "Wrote"
    print(f"{verb} {md}\n{verb} {html}")
    return 0


def cmd_mcp(_args) -> int:
    from .mcp_server import MCPServer

    print(f"taskman MCP server · data in {DATA_DIR}", file=sys.stderr)  # stdout is reserved for JSON-RPC
    MCPServer(DATA_DIR).serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="taskman", description="Personal task manager with a Claude agent.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("chat", help="Chat with the agent (default).").set_defaults(func=cmd_chat)
    lp = sub.add_parser("list", help="Print open tasks (no AI).")
    lp.add_argument("-a", "--all", action="store_true", help="Include done tasks.")
    lp.set_defaults(func=cmd_list)
    rp = sub.add_parser("report", help="Write/overwrite this week's report (no AI; rule-based notes).")
    rp.add_argument("--week", help="Any date in the target week, YYYY-MM-DD (default: this week).")
    rp.add_argument("--theme", choices=THEMES, default="auto",
                    help="HTML colour theme: auto (follow the system setting, default), light or dark.")
    rp.set_defaults(func=cmd_report)
    sub.add_parser("mcp", help="Run as an MCP server over stdio (for Claude Code).").set_defaults(func=cmd_mcp)
    args = p.parse_args(argv)
    return getattr(args, "func", cmd_chat)(args)
