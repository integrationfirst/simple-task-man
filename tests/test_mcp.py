import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MCPServerTests(unittest.TestCase):
    """Runs `python -m taskman mcp` as a real subprocess and speaks JSON-RPC to it."""

    def rpc(self, *messages):
        data_dir = tempfile.mkdtemp()
        env = {**os.environ, "PYTHONPATH": str(ROOT), "TASKMAN_HOME": data_dir}
        stdin = "".join(json.dumps(m) + "\n" for m in messages) + "not json\n"
        proc = subprocess.run([sys.executable, "-m", "taskman", "mcp"], input=stdin, env=env,
                              capture_output=True, text=True, timeout=30, cwd=tempfile.gettempdir())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        return {r["id"]: r for r in responses}, Path(data_dir)

    def test_session(self):
        out, data_dir = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "add_task", "arguments": {"title": "Water plants", "due": "2026-11-20"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_tasks", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "complete_task", "arguments": {"ids": [1]}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "update_task", "arguments": {"id": 42}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "generate_weekly_report", "arguments": {"summary": "Quiet week."}}},
            {"jsonrpc": "2.0", "id": 8, "method": "bogus/method"},
        )
        self.assertEqual(out[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", out[1]["result"]["capabilities"])
        names = {t["name"] for t in out[2]["result"]["tools"]}
        self.assertEqual(names, {"add_task", "list_tasks", "update_task", "complete_task",
                                 "delete_task", "generate_weekly_report", "list_challenges",
                                 "add_challenge", "update_challenge", "delete_challenge"})
        self.assertTrue(all("inputSchema" in t for t in out[2]["result"]["tools"]))
        self.assertFalse(out[3]["result"]["isError"])
        listed = json.loads(out[4]["result"]["content"][0]["text"])
        self.assertEqual(listed["tasks"][0]["title"], "Water plants")
        self.assertEqual(json.loads(out[5]["result"]["content"][0]["text"])["completed"], [1])
        self.assertTrue(out[6]["result"]["isError"])  # tool errors come back as results, not crashes
        report = json.loads(out[7]["result"]["content"][0]["text"])
        self.assertTrue(Path(report["html"]).exists())
        self.assertEqual(out[8]["error"]["code"], -32601)
        self.assertEqual(out[None]["error"]["code"], -32700)  # the "not json" line
        saved = json.loads((data_dir / "tasks.json").read_text())
        self.assertEqual(saved["tasks"][0]["status"], "DONE")


if __name__ == "__main__":
    unittest.main()
