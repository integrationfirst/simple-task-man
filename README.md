# simple-task-man

A personal task manager you talk to in the terminal. Claude reads and changes your tasks through a small set of tools; everything is stored in a local JSON file.

Two ways to talk to it — both use the same tools and the same `data/tasks.json`:

| | Needs | Start with |
|---|---|---|
| **Inside Claude Code** (MCP server) | Your Claude subscription; Python 3.10+, no packages | open `claude` in this folder |
| **Standalone `taskman` chat** | An Anthropic API key + `anthropic` package | `taskman` |

## Option A: Claude Code (no API key)

The repo ships `.mcp.json`, so Claude Code starts the server automatically when you run `claude` in this folder. The first time, approve the `taskman` server when asked (or check `/mcp`). Then just talk:

- `add a task: send invoice to ACME by Friday, high priority`
- `what's on my plate this week?`
- `write this week's report`

Claude Code asks permission before each tool call; choose "always allow" for the read-only `list_tasks` if you like.

To use it from **any** folder, register it once at user scope instead:

```bash
claude mcp add --scope user taskman \
  -e PYTHONPATH=/mnt/c/apps/rd/simple-task-man \
  -e TASKMAN_HOME=/mnt/c/apps/rd/simple-task-man/data \
  -- python3 -m taskman mcp
```

## Option B: standalone chat (API key)

```bash
sudo apt install python3-venv        # WSL/Ubuntu only, if venv is missing
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

## Use

```bash
taskman              # chat (same as `taskman chat`)
taskman list [-a]    # print open tasks (-a includes done) — no AI
taskman report [--week 2026-11-13]   # write/overwrite this week's report — no AI, rule-based notes
taskman mcp          # run the MCP server on stdio (Claude Code does this for you)
```

Things to say in chat:

- `add: send invoice to ACME by Friday, high priority, project work`
- `what's overdue?` / `show my work tasks`
- `push the dentist thing to next Tuesday`
- `done with the invoice and the passport`
- `challenge: Joe hasn't confirmed the API scope yet` / `the API scope issue is resolved`
- `delete the test task` (you are asked to confirm in the terminal)
- `write this week's report` (one report per week: Tasks table, Executive Notes, Challenges; re-running overwrites it)

## Files

| Path | What |
|---|---|
| `data/tasks.json` | Your tasks and saved challenges (override the folder with `TASKMAN_HOME`) |
| `data/reports/week-YYYY-Www.{md,html}` | Weekly reports (one per week, overwritten on re-run) |
| `taskman/store.py` | JSON storage + CRUD |
| `taskman/tools.py` | Tool schemas given to Claude + dispatcher |
| `taskman/agent.py` | Claude tool-use loop and system prompt (option B) |
| `taskman/mcp_server.py` | Stdlib MCP server exposing the same tools (option A) |
| `.mcp.json` | Registers the MCP server with Claude Code for this folder |
| `taskman/report.py` | Markdown/HTML report rendering |
| `taskman/cli.py` | Terminal commands |

## Settings

| Env var | Default | |
|---|---|---|
| `TASKMAN_MODEL` | `claude-opus-5-5` | Model id |
| `TASKMAN_EFFORT` | `low` | `low`/`medium`/`high`: more effort = more thinking, slower, costlier |
| `TASKMAN_HOME` | `./data` | Where tasks and reports live |

Requests opt in to server-side refusal fallbacks (`fallbacks: "default"`), so a request declined by a safety classifier is retried on a fallback model instead of just stopping.

## Tests

```bash
python3 -m unittest -v     # no API key needed; fake Claude client + a real MCP subprocess
```
