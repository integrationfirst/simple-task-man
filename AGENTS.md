# AGENTS.md

simple-task-man: a personal task manager driven by Claude. Tasks and challenges
live in a JSON file; Claude reads and changes them through tools exposed by an
MCP server (`taskman/mcp_server.py`) or the standalone chat (`taskman/agent.py`).
See `README.md` for usage.

## Layout

- `taskman/store.py` — task/challenge model and JSON persistence (`TASKMAN_HOME`, default `data/`)
- `taskman/tools.py` — tool definitions and executor shared by the agent and the MCP server
- `taskman/mcp_server.py` — stdio JSON-RPC MCP server (`python3 -m taskman mcp`)
- `taskman/report.py` — rule-based weekly Markdown report
- `taskman/web.py` — localhost-only report UI (`python3 -m taskman serve`)
- `taskman/cli.py`, `taskman/agent.py` — CLI and the standalone Claude chat
- `tests/` — stdlib `unittest`; `test_mcp.py` drives the real server over stdio

## Setup (fresh checkout)

Python 3.10+. Tests and the MCP server use only the standard library, so a fresh
checkout needs no install. The standalone chat additionally needs
`pip install -e .` and `ANTHROPIC_API_KEY`. No env files to copy.

The local report UI (`taskman serve`) is the only web server. Run it against a
scratch data folder on your port; it prints the URL and binds to 127.0.0.1 only:

```bash
TASKMAN_HOME=$(mktemp -d) python3 -m taskman serve --port <port>
```

To see CLI output, also use a scratch data folder so the real `data/` is never touched:

```bash
TASKMAN_HOME=$(mktemp -d) python3 -m taskman list -a
TASKMAN_HOME=$(mktemp -d) python3 -m taskman report
```

## Validation

Same as `factory/checks.sh`:

```bash
python3 -m unittest discover -s tests -t . -v
```

Tests must create their own temp data folder and never write to `data/`.

## Software factory

`/factory run` builds every open GitHub issue labeled `factory` through
intake → spec → build → gate → review → approve, one PR per issue. State is in
`factory/jobs/` (gitignored); `factory/bin/job` is its only writer. Checks:
`factory/checks.sh`. Dashboard: `cd factory && python3 -m http.server`, then
open http://localhost:8000/dashboard.html.

Each job works in its own git worktree, a full checkout of the repo at
`.worktrees/<job-id>` (gitignored). A tool run from the repo root that doesn't
read `.gitignore` sees those copies too; exclude `.worktrees/` in its config.
Note `unittest discover -s tests` only scans `tests/`, so it is unaffected.

The factory scripts need Linux/macOS (`fcntl`) plus `python3`, `git` and `gh`:
on this Windows machine, run Claude Code for `/factory` from inside WSL.
