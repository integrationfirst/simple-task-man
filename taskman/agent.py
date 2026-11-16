"""Claude tool-use loop for the task manager."""

from __future__ import annotations

import os
from datetime import date
from typing import Callable

from .tools import LIST_TEMPLATE, TOOLS, ToolExecutor

MODEL = os.environ.get("TASKMAN_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("TASKMAN_EFFORT", "low")  # low is plenty for CRUD chat; try "medium" for planning
MAX_TOOL_ROUNDS = 20

SYSTEM_PROMPT = """You are a personal task-management assistant working in the user's terminal.
You manage their tasks through the provided tools; the tools are the only source of truth, so look tasks up \
with list_tasks rather than relying on memory of earlier turns, and never invent task ids.

Guidelines:
- When the user mentions something to do, add it. Infer priority, due date, project and tags only when the \
user implies them; otherwise leave the defaults.
- When the user refers to a task by description, find it with list_tasks first. If several match, ask which one.
- Batch independent tool calls in parallel when you can.
- After acting, confirm briefly what changed (id + title). Keep other replies short: plain text or \
simple '-' bullets.
- Today is {today} ({weekday}).

""" + LIST_TEMPLATE.replace("{", "{{").replace("}", "}}")


def system_prompt(today: date | None = None) -> str:
    today = today or date.today()
    return SYSTEM_PROMPT.format(today=today.isoformat(), weekday=today.strftime("%A"))


class TaskAgent:
    """Keeps the whole conversation (including tool turns) so history stays append-only."""

    def __init__(self, executor: ToolExecutor, client=None, on_tool: Callable[[str, dict], None] | None = None):
        if client is None:
            import anthropic  # imported lazily so report/list commands work without the SDK

            client = anthropic.Anthropic()
        self.client = client
        self.executor = executor
        self.on_tool = on_tool
        self.messages: list = []
        self.system = system_prompt()

    def _create(self):
        return self.client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=self.system,
            tools=TOOLS,
            messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            # If a request is ever declined by a safety classifier, the API retries it on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def send(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._create()
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return "(The request was declined.)"
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "max_tokens":
                return _text(response) + "\n(Response was cut off.)"

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                return _text(response)

            results = []
            for block in tool_uses:
                if self.on_tool:
                    self.on_tool(block.name, block.input)
                content, is_error = self.executor.run(block.name, dict(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})  # all results in one message
        return "(Stopped: too many tool calls in one turn.)"


def _text(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()
