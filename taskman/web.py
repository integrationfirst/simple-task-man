"""Localhost-only web UI for the weekly reports: `python -m taskman serve [--port N]`. Standard library only.

Binds to 127.0.0.1 only. Every request must carry a localhost `Host` (and `Origin`, when present) for the
bound port, and every POST must send the per-start token in `X-Taskman-Token`, so other websites can't
drive the server. No CORS headers are ever sent.

Endpoints:
  GET  /                    the page (token embedded)
  GET  /api/reports         {"current": "<ISO week>", "reports": ["2026-W47", ...]} newest first
  GET  /reports/<week>.html the stored HTML report for that ISO week, or 404
  POST /api/regenerate      {"week": "<ISO week>"} -> rule-based write_report, never invokes Claude
  POST /api/ask             {"week": "<ISO week>", "instruction": "..."} -> runs Claude Code (`claude -p`)
                            with only the non-deleting taskman MCP tools; one run at a time (409 otherwise)
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .report import write_report
from .store import TaskStore

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
CLAUDE_TIMEOUT = 300  # seconds
MAX_INSTRUCTION = 2000
MAX_BODY = 64 * 1024  # bytes
ERROR_TAIL = 2000
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
REPORT_PATH_RE = re.compile(r"^/reports/([^/]+)\.html$")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # folder containing the taskman package
ALLOWED_TOOLS = ("add_task", "list_tasks", "update_task", "complete_task", "list_challenges",
                 "add_challenge", "update_challenge", "generate_weekly_report")
DISALLOWED_TOOLS = ("delete_task", "delete_challenge")


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def iso_label(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def parse_week(label) -> date:
    """Monday of an ISO week label like 2026-W47; 400 for anything else."""
    if not isinstance(label, str) or not WEEK_RE.match(label):
        raise HTTPError(400, "Week must look like 2026-W47.")
    try:
        return date.fromisocalendar(int(label[:4]), int(label[6:]), 1)
    except ValueError:
        raise HTTPError(400, f"{label} is not a valid ISO week.") from None


def claude_args(exe: str, data_dir: Path, week: str, monday: date, instruction: str) -> list[str]:
    mcp = {"mcpServers": {"taskman": {
        "command": sys.executable, "args": ["-m", "taskman", "mcp"],
        "env": {"TASKMAN_HOME": str(Path(data_dir).resolve()), "PYTHONPATH": str(PACKAGE_ROOT)},
    }}}
    prompt = (f"The selected week is {week} (Monday {monday.isoformat()}).\n"
              f"Carry out the user's instruction below for that week using the taskman tools. When you are done, "
              f"call generate_weekly_report with week_of set to {monday.isoformat()} so the week's report file "
              f"is rewritten.\n\nInstruction:\n{instruction}")
    return [exe, "-p", prompt, "--output-format", "text",
            "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
            "--tools", "",
            "--allowedTools", ",".join(f"mcp__taskman__{n}" for n in ALLOWED_TOOLS),
            "--disallowedTools", ",".join(f"mcp__taskman__{n}" for n in DISALLOWED_TOOLS)]


class ReportServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, data_dir: str | Path, port: int = DEFAULT_PORT):
        self.data_dir = Path(data_dir)
        self.token = secrets.token_urlsafe(32)
        self.ask_lock = threading.Lock()
        super().__init__((HOST, port), Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


class Handler(BaseHTTPRequestHandler):
    server: ReportServer

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # ---- responses -----------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _text(self, status: int, text: str):
        self._send(status, text.encode(), "text/plain; charset=utf-8")

    def _local_ok(self) -> bool:
        port = self.server.port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    # ---- methods -------------------------------------------------------------
    def do_GET(self):
        if not self._local_ok():
            return self._text(403, "Forbidden: localhost only.")
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", self.server.token).encode(), "text/html; charset=utf-8")
        if path == "/api/reports":
            return self._json(200, {"current": iso_label(date.today()), "reports": self._list_reports()})
        m = REPORT_PATH_RE.match(path)
        if m:
            try:
                parse_week(m.group(1))
            except HTTPError as e:
                return self._text(e.status, str(e))
            f = self.server.reports_dir / f"week-{m.group(1)}.html"
            if not f.is_file():
                return self._text(404, "No report for that week.")
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        self._text(404, "Not found.")

    def do_POST(self):
        if not self._local_ok():
            return self._text(403, "Forbidden: localhost only.")
        if not hmac.compare_digest(self.headers.get("X-Taskman-Token", "").encode(), self.server.token.encode()):
            return self._json(403, {"ok": False, "error": "Missing or wrong token. Reload the page."})
        try:
            body = self._read_json()
            if self.path == "/api/regenerate":
                return self._json(200, self._regenerate(body))
            if self.path == "/api/ask":
                return self._json(200, self._ask(body))
            raise HTTPError(404, "Not found.")
        except HTTPError as e:
            self._json(e.status, {"ok": False, "error": str(e)})

    # ---- actions -------------------------------------------------------------
    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                raise HTTPError(400, "Request body is too large.")
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise HTTPError(400, "Body must be JSON.") from None
        if not isinstance(body, dict):
            raise HTTPError(400, "Body must be a JSON object.")
        return body

    def _list_reports(self) -> list[str]:
        d = self.server.reports_dir
        if not d.is_dir():
            return []
        labels = [p.name[5:-5] for p in d.glob("week-*.html")]
        return sorted((x for x in labels if WEEK_RE.match(x)), reverse=True)

    def _regenerate(self, body: dict) -> dict:
        week = body.get("week")
        monday = parse_week(week)
        data = self.server.data_dir
        _, _, replaced = write_report(TaskStore(data / "tasks.json"), data / "reports", monday)
        return {"ok": True, "week": week, "replaced": replaced}

    def _ask(self, body: dict) -> dict:
        week = body.get("week")
        monday = parse_week(week)
        instruction = body.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise HTTPError(400, "Write an instruction for Claude first.")
        if len(instruction) > MAX_INSTRUCTION:
            raise HTTPError(400, f"The instruction is longer than {MAX_INSTRUCTION} characters.")
        if not self.server.ask_lock.acquire(blocking=False):
            raise HTTPError(409, "A Claude request is already running.")
        try:
            exe = shutil.which("claude")
            if not exe:
                raise HTTPError(503, "Claude Code (`claude`) was not found on PATH. Viewing reports and "
                                     "rule-based regenerate still work.")
            args = claude_args(exe, self.server.data_dir, week, monday, instruction)
            self.server.data_dir.mkdir(parents=True, exist_ok=True)
            try:
                proc = subprocess.run(args, shell=False, capture_output=True, text=True,
                                      timeout=CLAUDE_TIMEOUT, cwd=self.server.data_dir)
            except subprocess.TimeoutExpired:
                raise HTTPError(504, f"The Claude request timed out after {CLAUDE_TIMEOUT} seconds.") from None
            except OSError as e:
                raise HTTPError(503, f"Could not start Claude Code: {e}") from None
            if proc.returncode != 0:
                tail = ((proc.stderr or "").strip() or (proc.stdout or "").strip())[-ERROR_TAIL:]
                raise HTTPError(502, f"Claude Code exited with code {proc.returncode}: {tail}")
            return {"ok": True, "output": (proc.stdout or "").strip()}
        finally:
            self.server.ask_lock.release()


def serve(data_dir: str | Path, port: int = DEFAULT_PORT) -> int:
    server = ReportServer(data_dir, port)
    print(f"http://{HOST}:{server.port}/\ndata in {Path(data_dir).resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>taskman reports</title>
<style>
:root{color-scheme:light dark;--fg:#1f2937;--muted:#6b7280;--bg:#f4f5f7;--line:#e5e7eb;--err:#b42318;--acc:#1e3a5f;
  --head-bg:#e6eef8;--acc-fg:#ffffff;--acc-hover:#2b5285;--acc-active:#142a47;--panel:#eef2f7;--info:#1e40af;--err-bg:#fdecec;--ok-bg:#e8f5ec}
@media (prefers-color-scheme:dark){:root{--fg:#f3f4f6;--muted:#9ca3af;--bg:#111827;--line:#374151;--err:#ff7b72;--acc:#93c5fd;
  --head-bg:#1a2b45;--acc-fg:#111827;--acc-hover:#bfdbfe;--acc-active:#60a5fa;--panel:#172033;--info:#93c5fd;--err-bg:#3f1d1d;--ok-bg:#132a1f}}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);
  display:flex;flex-direction:column;height:100vh}
header{display:flex;flex-wrap:wrap;gap:.75rem;align-items:center;padding:.75rem 1rem;background:var(--head-bg);border-bottom:2px solid var(--acc)}
header h1{font-size:1.05rem;margin:0 1rem 0 0}
select,button,textarea{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:.35rem .6rem}
button{cursor:pointer}
button:disabled,textarea:disabled{opacity:.55;cursor:default}
:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
#send,#gen{background:var(--acc);color:var(--acc-fg);border-color:var(--acc)}
#send:not(:disabled):hover,#gen:not(:disabled):hover{background:var(--acc-hover);border-color:var(--acc-hover)}
#send:not(:disabled):active,#gen:not(:disabled):active{background:var(--acc-active);border-color:var(--acc-active)}
#regen:not(:disabled):hover{border-color:var(--acc)}
#week:not(:disabled):hover,#week:focus,#instruction:not(:disabled):hover,#instruction:focus{border-color:var(--acc)}
main{flex:1;display:flex;min-height:0}
#view{flex:1;display:flex;min-width:0}
iframe{flex:1;border:0;width:100%}
#empty{margin:2rem auto;text-align:center}
aside{width:22rem;max-width:40vw;background:var(--panel);border-left:1px solid var(--line);padding:1rem;display:flex;flex-direction:column;gap:.6rem;overflow:auto}
aside h2{font-size:1rem;margin:0}
textarea{width:100%;min-height:8rem;resize:vertical}
#status{color:var(--info)}
#error{color:var(--err);background:var(--err-bg);padding:.4rem .6rem;border-radius:6px;white-space:pre-wrap}
#output{white-space:pre-wrap;background:var(--ok-bg);border-top:1px solid var(--line);padding:.5rem .6rem;border-radius:6px}
[hidden]{display:none!important}
@media (max-width:600px){
  main{flex-direction:column}
  #view{flex:none}
  iframe{min-height:60vh}
  aside{width:auto;max-width:none;border-left:0;border-top:1px solid var(--line);overflow:visible}
}
</style></head>
<body>
<header>
  <h1>taskman reports</h1>
  <label>Week <select id="week"></select></label>
  <button id="regen" type="button">Regenerate (rule-based)</button>
</header>
<main>
  <div id="view">
    <iframe id="frame" title="Weekly report" sandbox="" hidden></iframe>
    <div id="empty" hidden><p id="empty-text"></p><button id="gen" type="button">Generate report</button></div>
  </div>
  <aside>
    <h2>Ask Claude</h2>
    <form id="ask">
      <textarea id="instruction" aria-label="Instruction for Claude" maxlength="2000" placeholder="e.g. make the summary mention the vendor delay"></textarea>
      <button id="send" type="submit">Ask Claude</button>
    </form>
    <div id="status" role="status" hidden></div>
    <div id="error" role="alert" hidden></div>
    <div id="output" hidden></div>
  </aside>
</main>
<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
const weekSel = $("week"), frame = $("frame"), empty = $("empty");
const controls = [$("regen"), $("gen"), $("send"), $("instruction"), weekSel];
let reports = new Set();

function setBusy(busy) { controls.forEach(c => c.disabled = busy); }
function showError(msg) { $("error").textContent = msg; $("error").hidden = !msg; }
function showStatus(msg) { $("status").textContent = msg; $("status").hidden = !msg; }

async function loadReports(select) {
  const r = await fetch("/api/reports");
  const data = await r.json();
  reports = new Set(data.reports);
  const weeks = [...new Set([data.current, ...data.reports, select].filter(Boolean))].sort().reverse();
  const keep = select || weekSel.value || data.current;
  weekSel.replaceChildren(...weeks.map(w => {
    const o = document.createElement("option");
    o.value = w;
    o.textContent = w + (w === data.current ? " (this week)" : "");
    return o;
  }));
  weekSel.value = keep;
  show();
}

function show() {
  const w = weekSel.value;
  if (reports.has(w)) {
    frame.src = "/reports/" + w + ".html?t=" + Date.now();
    frame.hidden = false; empty.hidden = true;
  } else {
    frame.hidden = true; frame.removeAttribute("src");
    $("empty-text").textContent = "No report for " + w + " yet.";
    empty.hidden = false;
  }
}

async function post(path, body) {
  const r = await fetch(path, {method: "POST", body: JSON.stringify(body),
    headers: {"Content-Type": "application/json", "X-Taskman-Token": TOKEN}});
  let data;
  try { data = await r.json(); } catch (e) { data = {ok: false, error: "HTTP " + r.status}; }
  if (!r.ok || !data.ok) throw new Error(data.error || "HTTP " + r.status);
  return data;
}

async function regenerate(ask) {
  const w = weekSel.value;
  if (ask && !confirm("Rebuild the " + w + " report with rule-based notes? This replaces any summary Claude wrote.")) return;
  showError(""); setBusy(true);
  try { await post("/api/regenerate", {week: w}); await loadReports(w); }
  catch (e) { showError(e.message); }
  finally { setBusy(false); }
}

$("regen").onclick = () => regenerate(true);
$("gen").onclick = () => regenerate(false);
weekSel.onchange = () => { showError(""); $("output").hidden = true; show(); };

$("ask").onsubmit = async ev => {
  ev.preventDefault();
  const w = weekSel.value, instruction = $("instruction").value;
  showError(""); $("output").hidden = true;
  if (!instruction.trim()) { showError("Write an instruction for Claude first."); return; }
  setBusy(true); showStatus("Claude is working on " + w + "\\u2026");
  try {
    const data = await post("/api/ask", {week: w, instruction});
    await loadReports(w);
    if (data.output) { $("output").textContent = data.output; $("output").hidden = false; }
  } catch (e) { showError(e.message); }
  finally { showStatus(""); setBusy(false); }
};

loadReports().catch(e => showError("Could not load the report list: " + e.message));
</script>
</body></html>
"""
