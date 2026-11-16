"""Minimal MCP server over stdio (JSON-RPC 2.0, one message per line). Standard library only.

Lets Claude Code (or any MCP client) manage tasks with the same tools the API agent uses,
so no Anthropic API key is needed: the client's own Claude does the thinking.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TextIO

from .store import TaskStore
from .tools import LIST_TEMPLATE, TOOLS, ToolExecutor

SERVER_INFO = {"name": "taskman", "version": "0.1.0"}
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

INSTRUCTIONS = """Personal task manager for the user. The tools are the source of truth: \
look tasks up with list_tasks instead of relying on earlier turns, and never invent task ids.
- Task notes and challenges are rephrased for a business reader before saving (rules below).
- When the user mentions something to do, add it; infer priority/due/project/tags only when implied. \
Resolve relative dates to YYYY-MM-DD (list_tasks returns today's date).
- When a task is referred to by description, find it with list_tasks first; if several match, ask which one.
- delete_task is permanent: only call it after the user has clearly asked to delete that specific task. \
Prefer complete_task for finished work.
- For a weekly report, review the tasks first, then pass a professional, business-readable summary (and \
note_highlights) to generate_weekly_report (one report per week; regenerating overwrites it). Challenges come from the saved list.
- Confirm changes briefly (id + title).

""" + LIST_TEMPLATE


def mcp_tools() -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
            for t in TOOLS]


class MCPServer:
    def __init__(self, data_dir: str | Path):
        data_dir = Path(data_dir)
        self.data_dir = data_dir
        # No terminal to prompt in: the MCP client's own permission prompt is the confirmation.
        self.executor_factory = lambda: ToolExecutor(TaskStore(data_dir / "tasks.json"), data_dir / "reports")

    def handle(self, msg: dict) -> dict | None:
        """Handle one JSON-RPC message; returns the response, or None for notifications."""
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:  # notification (e.g. notifications/initialized) — no reply
            return None
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                result = {
                    "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[-1],
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": mcp_tools()}
            elif method == "tools/call":
                # Reload the store on every call so edits made by the CLI/API agent are always visible.
                text, is_error = self.executor_factory().run(params.get("name", ""), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            else:
                return _error(msg_id, -32601, f"Method not found: {method}")
        except Exception as exc:  # never crash the server on one bad request
            return _error(msg_id, -32603, f"Internal error: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                response = _error(None, -32700, "Parse error")
            else:
                response = self.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "Invalid request")
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
