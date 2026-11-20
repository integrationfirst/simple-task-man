import http.client
import json
import os
import re
import stat
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from taskman import web
from taskman.tools import TOOLS


def iso_label(day: date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


class WebServerTests(unittest.TestCase):
    """Runs the report server on port 0 in a thread; `claude` is a stub script on PATH."""

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp())
        self.bin_dir = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"PATH": str(self.bin_dir)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = web.ReportServer(self.data_dir, 0)
        self.port = self.server.port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def stub_claude(self, script: str):
        f = self.bin_dir / "claude"
        f.write_text("#!/bin/sh\nPATH=/usr/bin:/bin\n" + script + "\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def request(self, method, path, body=None, host=None, origin=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin:
            headers["Origin"] = origin
        if method == "POST":
            headers["Content-Type"] = "application/json"
            if token is True:
                headers["X-Taskman-Token"] = self.server.token
            elif token:
                headers["X-Taskman-Token"] = token
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        ctype = r.getheader("Content-Type", "")
        return r.status, (json.loads(data) if ctype.startswith("application/json") else data.decode())

    def reports(self):
        d = self.data_dir / "reports"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def test_bound_to_localhost(self):
        self.assertEqual(self.server.socket.getsockname()[0], "127.0.0.1")

    def test_page_has_token(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.server.token, body)
        status, _ = self.request("GET", "/", host=f"localhost:{self.port}", origin=f"http://localhost:{self.port}")
        self.assertEqual(status, 200)

    def test_framing_and_body_limit(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", "/")
        r = conn.getresponse()
        page = r.read().decode()
        conn.close()
        self.assertEqual(r.getheader("Content-Security-Policy"), "frame-ancestors 'self'")
        self.assertIn('sandbox=""', page)
        big = {"week": "2026-W47", "pad": "x" * (web.MAX_BODY + 1)}
        self.assertEqual(self.request("POST", "/api/regenerate", big)[0], 400)
        self.assertEqual(self.reports(), [])

    def test_bad_host_and_origin(self):
        self.assertEqual(self.request("GET", "/", host="evil.example")[0], 403)
        self.assertEqual(self.request("GET", "/api/reports", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.request("GET", "/", origin="http://evil.example")[0], 403)
        status, _ = self.request("POST", "/api/regenerate", {"week": "2026-W47"}, host=f"evil.example:{self.port}")
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/regenerate", {"week": "2026-W47"}, origin="http://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(self.reports(), [])

    def test_token_required(self):
        self.assertEqual(self.request("POST", "/api/regenerate", {"week": "2026-W47"}, token=None)[0], 403)
        self.assertEqual(self.request("POST", "/api/regenerate", {"week": "2026-W47"}, token="wrong")[0], 403)
        self.stub_claude("touch \"$PWD/ran\"")
        self.assertEqual(self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": "x"}, token=None)[0], 403)
        self.assertEqual(self.reports(), [])
        self.assertFalse((self.data_dir / "ran").exists())

    def test_listing_and_fetch(self):
        status, body = self.request("GET", "/api/reports")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"current": iso_label(date.today()), "reports": []})
        d = self.data_dir / "reports"
        d.mkdir()
        for label in ("2025-W52", "2026-W03", "2026-W47"):
            (d / f"week-{label}.html").write_text(f"<p>{label}</p>")
        (d / "week-2026-W40.md").write_text("md only")
        (d / "notes.html").write_text("other")
        self.assertEqual(self.request("GET", "/api/reports")[1]["reports"], ["2026-W47", "2026-W03", "2025-W52"])
        status, body = self.request("GET", "/reports/2026-W03.html")
        self.assertEqual((status, body), (200, "<p>2026-W03</p>"))
        self.assertEqual(self.request("GET", "/reports/2026-W10.html")[0], 404)
        (self.data_dir / "tasks.html").write_text("secret")
        for bad in ("/reports/2026-W60.html", "/reports/..%2Ftasks.html", "/reports/../tasks.html",
                    "/reports/x.html"):
            status, body = self.request("GET", bad)
            self.assertIn(status, (400, 404), bad)
            self.assertNotIn("secret", body)

    def test_regenerate_without_claude(self):
        status, body = self.request("POST", "/api/regenerate", {"week": "2026-W47"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "week": "2026-W47", "replaced": False})
        self.assertEqual(self.reports(), ["week-2026-W47.html", "week-2026-W47.md"])
        self.assertTrue(self.request("POST", "/api/regenerate", {"week": "2026-W47"})[1]["replaced"])
        self.assertEqual(self.request("POST", "/api/regenerate", {"week": "../x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/regenerate", {"week": "2026-W60"})[0], 400)

    def test_ask_success(self):
        self.stub_claude("echo 'Summary updated.'")
        status, body = self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": "mention the delay"})
        self.assertEqual((status, body), (200, {"ok": True, "output": "Summary updated."}))

    def test_ask_failure(self):
        self.stub_claude("echo 'auth expired' >&2; exit 3")
        status, body = self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": "x"})
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        self.assertIn("auth expired", body["error"])
        self.assertIn("3", body["error"])

    def test_ask_not_installed(self):
        status, body = self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": "x"})
        self.assertEqual(status, 503)
        self.assertIn("not found", body["error"])

    def test_ask_validation(self):
        self.stub_claude("exit 0")
        for instruction in ("", "   ", "x" * 2001):
            self.assertEqual(self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": instruction})[0], 400)
        self.assertEqual(self.request("POST", "/api/ask", {"week": "2026-W60", "instruction": "x"})[0], 400)

    def test_ask_timeout(self):
        self.stub_claude("sleep 5")
        with mock.patch.object(web, "CLAUDE_TIMEOUT", 0.5):
            status, body = self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": "x"})
        self.assertEqual(status, 504)
        self.assertIn("timed out", body["error"])

    def test_one_run_at_a_time(self):
        self.stub_claude("touch \"$PWD/started\"; sleep 2; echo done")
        result = {}
        t = threading.Thread(target=lambda: result.update(
            first=self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": "slow"})))
        t.start()
        deadline = time.time() + 10
        while not (self.data_dir / "started").exists() and time.time() < deadline:
            time.sleep(0.05)
        status, body = self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": "second"})
        self.assertEqual((status, body), (409, {"ok": False, "error": "A Claude request is already running."}))
        self.assertEqual(self.request("GET", "/api/reports")[0], 200)  # page stays usable during a run
        t.join()
        self.assertEqual(result["first"], (200, {"ok": True, "output": "done"}))
        self.stub_claude("echo again")
        self.assertEqual(self.request("POST", "/api/ask", {"week": "2026-W47", "instruction": "x"})[0], 200)


class ClaudeArgsTests(unittest.TestCase):
    def test_arguments(self):
        data_dir = Path(tempfile.mkdtemp())
        args = web.claude_args("/bin/claude", data_dir, "2026-W47", date(2026, 11, 16), "add a task; rm -rf /")
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        prompt = args[args.index("-p") + 1]
        self.assertIn("2026-W47", prompt)
        self.assertIn("2026-11-16", prompt)
        self.assertIn("generate_weekly_report", prompt)
        self.assertIn("Instruction:\nadd a task; rm -rf /", prompt)
        allowed = args[args.index("--allowedTools") + 1].split(",")
        self.assertEqual(allowed, [f"mcp__taskman__{n}" for n in web.ALLOWED_TOOLS])
        self.assertFalse(any("delete" in a for a in allowed))
        denied = args[args.index("--disallowedTools") + 1].split(",")
        self.assertEqual(set(denied), {"mcp__taskman__delete_task", "mcp__taskman__delete_challenge"})
        server = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["taskman"]
        self.assertEqual(server["args"], ["-m", "taskman", "mcp"])
        self.assertEqual(server["env"]["TASKMAN_HOME"], str(data_dir.resolve()))
        self.assertTrue((Path(server["env"]["PYTHONPATH"]) / "taskman" / "web.py").exists())

    def test_allowlist_exists_in_tools(self):
        names = {t["name"] for t in TOOLS}
        self.assertTrue(set(web.ALLOWED_TOOLS) <= names)
        self.assertTrue(set(web.DISALLOWED_TOOLS) <= names)
        self.assertFalse(set(web.ALLOWED_TOOLS) & set(web.DISALLOWED_TOOLS))


class WeekParsingTests(unittest.TestCase):
    def test_parse_week(self):
        self.assertEqual(web.parse_week("2026-W47"), date(2026, 11, 16))
        for bad in ("2026-W60", "2026-W00", "../x", "2026-47", None, 5):
            with self.assertRaises(web.HTTPError):
                web.parse_week(bad)


def contrast(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio of two #rrggbb colours."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class PaletteTests(unittest.TestCase):
    NEW_VARS = ("head-bg", "acc-fg", "acc-hover", "acc-active", "panel", "info", "err-bg", "ok-bg")
    PAIRS = [("fg", "head-bg"), ("acc-fg", "acc"), ("acc-fg", "acc-hover"), ("acc-fg", "acc-active"),
             ("fg", "panel"), ("info", "panel"), ("err", "err-bg"), ("fg", "ok-bg"), ("fg", "bg")]

    def setUp(self):
        style = web.PAGE.split("<style>", 1)[1].split("</style>", 1)[0]
        self.style = style
        light = re.search(r"^:root\{(.*?)\}", style, re.S | re.M).group(1)
        dark = re.search(r"@media \(prefers-color-scheme:dark\)\{:root\{(.*?)\}\}", style, re.S).group(1)
        self.modes = {name: dict(re.findall(r"--([\w-]+):\s*([^;]+?)\s*(?:;|$)", block))
                      for name, block in (("light", light), ("dark", dark))}

    def test_new_variables_defined_in_both_modes(self):
        for mode, palette in self.modes.items():
            for var in self.NEW_VARS:
                with self.subTest(mode=mode, var=var):
                    self.assertRegex(palette.get(var, ""), r"^#[0-9a-fA-F]{6}$")

    def test_contrast(self):
        for mode, palette in self.modes.items():
            for fg, bg in self.PAIRS:
                with self.subTest(mode=mode, pair=(fg, bg)):
                    self.assertGreaterEqual(contrast(palette[fg], palette[bg]), 4.5)

    def test_button_fills(self):
        self.assertRegex(self.style, r"#send,#gen\{background:var\(--acc\)")
        self.assertNotRegex(self.style, r"#regen[^{]*\{[^}]*background:var\(--acc\)")


if __name__ == "__main__":
    unittest.main()
