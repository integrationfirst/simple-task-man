import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

from taskman.agent import TaskAgent
from taskman.report import collect, default_summary, write_report
from taskman.store import TaskStore
from taskman.tools import TOOLS, ToolExecutor


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = TaskStore(self.tmp / "tasks.json")


class StoreTests(Base):
    def test_add_persist_reload(self):
        t = self.store.add("Pay rent", priority="high", due="2026-11-20", tags=["#Home", "home"])
        self.assertEqual((t.id, t.tags), (1, ["home"]))
        again = TaskStore(self.tmp / "tasks.json")
        self.assertEqual(again.get(1).title, "Pay rent")
        self.assertEqual(again.add("x").id, 2)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.store.add("x", priority="huge")
        with self.assertRaises(ValueError):
            self.store.add("x", due="next friday")
        with self.assertRaises(ValueError):
            self.store.add("   ")

    def test_update_complete_reopen(self):
        t = self.store.add("a", due="2026-01-01")
        self.store.complete(t.id)
        self.assertIsNotNone(t.completed_at)
        self.store.update(t.id, status="", due="")
        self.assertIsNone(t.completed_at)
        self.assertIsNone(t.due)

    def test_status_workflow(self):
        t = self.store.add("a")
        self.assertEqual(t.status, "")  # backlog by default
        for loose, stored in [("planning", "PLANNING"), ("Planned", "PLANNED"), ("in progress", "IN_PROGRESS"),
                              ("done", "DONE"), ("backlog", "")]:
            self.assertEqual(self.store.update(t.id, status=loose).status, stored)
        with self.assertRaises(ValueError):
            self.store.update(t.id, status="blocked")
        self.assertEqual(self.store.add("b", status="PLANNED").status, "PLANNED")
        self.assertEqual([x.title for x in self.store.query(status="")], ["a"])
        self.assertEqual([x.title for x in self.store.query(status="PLANNED")], ["b"])

    def test_migrates_old_statuses_with_backup(self):
        path = self.tmp / "old.json"
        path.write_text(json.dumps({"next_id": 4, "tasks": [
            {"id": 1, "title": "a", "status": "todo"},
            {"id": 2, "title": "b", "status": "in_progress"},
            {"id": 3, "title": "c", "status": "done", "completed_at": "2026-11-10T09:00:00"}]}))
        store = TaskStore(path)
        self.assertEqual([store.get(i).status for i in (1, 2, 3)], ["", "IN_PROGRESS", "DONE"])
        self.assertIn('"todo"', (self.tmp / "old.json.bak").read_text())
        self.assertEqual(json.loads(path.read_text())["tasks"][0]["status"], "")

    def test_query_filters_and_sort(self):
        self.store.add("late", due="2026-11-30", priority="low")
        self.store.add("soon", due="2026-11-14")
        self.store.add("nodue", priority="urgent", notes="call bank")
        d = self.store.add("done one")
        self.store.complete(d.id)
        self.assertEqual([t.title for t in self.store.query()], ["soon", "late", "nodue"])
        self.assertEqual([t.title for t in self.store.query(text="BANK")], ["nodue"])
        self.assertEqual([t.title for t in self.store.query(due_before="2026-11-20")], ["soon"])
        self.assertEqual(len(self.store.query(include_done=True)), 4)


class ChallengeTests(Base):
    def test_lifecycle_and_persistence(self):
        t = self.store.add("Integrate APIs")
        c = self.store.add_challenge("  Vendor scope unclear. ", task_id=t.id)
        self.assertEqual((c.id, c.text, c.status), (1, "Vendor scope unclear.", "OPEN"))
        with self.assertRaises(KeyError):
            self.store.add_challenge("x", task_id=99)
        self.store.update_challenge(c.id, status="resolved")
        self.assertIsNotNone(c.resolved_at)
        self.assertEqual(self.store.list_challenges(), [])
        again = TaskStore(self.tmp / "tasks.json")
        self.assertEqual(again.get_challenge(1).status, "RESOLVED")
        self.assertEqual(again.add_challenge("next").id, 2)

    def test_listing_and_completion_surface_challenges(self):
        ex = ToolExecutor(self.store, self.tmp)
        self.store.add("Integrate APIs")
        ex.run("add_challenge", {"text": "Vendor scope unclear.", "task_id": 1})
        listed = json.loads(ex.run("list_tasks", {})[0])
        self.assertEqual([c["text"] for c in listed["open_challenges"]], ["Vendor scope unclear."])
        done = json.loads(ex.run("complete_task", {"ids": [1]})[0])
        self.assertEqual(done["linked_open_challenges"][0]["id"], 1)  # Claude is prompted to ask about resolving

    def test_report_uses_saved_challenges(self):
        today = date(2026, 11, 13)
        self.store.add_challenge("Budget approval pending.")
        old = self.store.add_challenge("Old issue.")
        self.store.update_challenge(old.id, status="RESOLVED")
        old.created_at, old.resolved_at = "2026-11-02T09:00:00", "2026-11-12T09:00:00"
        gone = self.store.add_challenge("Fixed last week.")
        gone.created_at, gone.resolved_at = "2026-11-01T09:00:00", "2026-11-05T09:00:00"
        gone.status = "RESOLVED"
        d = collect(self.store, today, today=today)
        self.assertEqual([(n.text, n.resolved) for n in d.challenges],
                         [("Budget approval pending.", False), ("Old issue.", True)])
        md, html, _ = write_report(self.store, self.tmp / "r", today)
        self.assertIn("- Budget approval pending.\n- *Resolved:* Old issue.", md.read_text())
        self.assertNotIn("Fixed last week", md.read_text())
        self.assertIn('<span class="tag">Resolved</span>', html.read_text())

    def test_challenge_shows_linked_task_number(self):
        self.store.add("Integrate APIs")
        self.store.add_challenge("Vendor scope unclear.", task_id=1)
        md, _, _ = write_report(self.store, self.tmp / "r", date(2026, 11, 13))
        self.assertIn("- Vendor scope unclear. (#1)", md.read_text())

    def test_no_challenges_message(self):
        md, _, _ = write_report(self.store, self.tmp / "r", date(2026, 11, 13))
        self.assertIn("- No open challenges.", md.read_text())


class OriginalWordingTests(Base):
    def test_rephrased_text_keeps_users_words(self):
        ex = ToolExecutor(self.store, self.tmp)
        ex.run("add_task", {"title": "API design", "notes": "Confirm the data ownership split with Joe.",
                            "notes_original": "ask joe who holds what data"})
        ex.run("add_challenge", {"text": "Scope is unconfirmed, which blocks the design.",
                                 "text_original": "scope unclear", "task_id": 1})
        again = TaskStore(self.tmp / "tasks.json")
        self.assertEqual(again.get(1).notes_original, "ask joe who holds what data")
        self.assertEqual(again.get_challenge(1).text_original, "scope unclear")
        ex.store.update(1, notes="Kept verbatim.")  # new notes without an original clear the old one
        self.assertEqual(ex.store.get(1).notes_original, "")
        ex.store.update(1, title="API design v2")  # unrelated change keeps notes as they are
        self.assertEqual(ex.store.get(1).notes, "Kept verbatim.")


class ToolTests(Base):
    def test_errors_are_reported_not_raised(self):
        ex = ToolExecutor(self.store, self.tmp / "r")
        out, err = ex.run("update_task", {"id": 99, "title": "x"})
        self.assertTrue(err)
        self.assertIn("No task with id 99", out)
        out, err = ex.run("nope", {})
        self.assertTrue(err)

    def test_delete_requires_confirmation(self):
        self.store.add("keep me")
        ex = ToolExecutor(self.store, self.tmp / "r", confirm=lambda _m: False)
        out, _ = ex.run("delete_task", {"id": 1})
        self.assertIn("declined", out)
        self.assertIn(1, self.store.tasks)

    def test_tool_names_match_handlers(self):
        ex = ToolExecutor(self.store, self.tmp)
        for tool in TOOLS:
            self.assertTrue(hasattr(ex, f"_{tool['name']}"), tool["name"])


class ReportTests(Base):
    def setUp(self):
        super().setUp()
        self.today = date(2026, 11, 13)  # Friday of 2026-W46
        a = self.store.add("Close Q4 budget", priority="high", notes="Finished early.")
        self.store.complete(a.id)
        a.completed_at = "2026-11-11T10:00:00"
        old = self.store.add("Done last week")
        self.store.complete(old.id)
        old.completed_at = "2026-11-02T10:00:00"
        self.store.add("Send invoice", priority="urgent", due="2026-11-10",
                       notes="Waiting for  approval from Anna.\nSend by Monday.")
        self.store.add("Book venue", due="2026-11-18")

    def test_table_has_open_and_this_weeks_done_only(self):
        d = collect(self.store, self.today, today=self.today)
        self.assertEqual((d.start, d.end), (date(2026, 11, 9), date(2026, 11, 15)))
        self.assertEqual([t.title for t in d.tasks], ["Send invoice", "Book venue", "Close Q4 budget"])

    def test_rule_based_summary(self):
        d = collect(self.store, self.today, today=self.today)
        self.assertEqual(default_summary(d), "**1 task completed** this week; **2 remain open**, "
                                             "1 of them **high priority**; next deadline **Wed 18 Nov**.")

    def test_executive_notes_are_summary_then_open_task_notes(self):
        out = self.tmp / "reports"
        md1, html1, replaced1 = write_report(self.store, out, self.today)
        md2, html2, replaced2 = write_report(self.store, out, date(2026, 11, 9), summary="A **steady** week.",
                                             highlights={3: ["approval from anna", "Monday"]})
        self.assertEqual((md1, html1, replaced1, replaced2), (md2, html2, False, True))
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["week-2026-W46.html", "week-2026-W46.md"])
        md = md2.read_text()
        for heading in ("## Tasks", "| # | Task | Priority | Status |", "## Executive Notes", "## Challenges"):
            self.assertIn(heading, md)
        notes = md.split("## Executive Notes\n\n")[1].split("\n\n")[0]
        # summary first, then open-task notes verbatim (whitespace collapsed) with highlights; done task excluded
        self.assertEqual(notes, "- A **steady** week.\n"
                                "- Waiting for **approval from Anna**. Send by **Monday**. (#3)")
        self.assertIn("<mark>approval from Anna</mark>", html2.read_text())
        self.assertIn('class="p-urgent', html2.read_text())
        self.assertIn("| 3 | Send invoice | **Urgent** |  |", md)  # backlog shows as an empty status
        self.assertIn("| 1 | Close Q4 budget | **High** | DONE |", md)

    def test_bad_highlights_are_rejected(self):
        with self.assertRaises(ValueError):  # phrase not in the note
            write_report(self.store, self.tmp / "r", self.today, highlights={3: ["Bob"]})
        with self.assertRaises(ValueError):  # task without notes
            write_report(self.store, self.tmp / "r", self.today, highlights={4: ["x"]})

    def test_highlights_render_safely(self):
        _, html, _ = write_report(self.store, self.tmp / "r", self.today,
                                  summary="Invoice is **3 days overdue**; see **<b>x</b>**.")
        text = html.read_text()
        self.assertIn("<mark>3 days overdue</mark>", text)
        self.assertIn("<mark>&lt;b&gt;x&lt;/b&gt;</mark>", text)

    def test_html_escapes(self):
        self.store.add("<script>alert(1)</script>")
        _, html, _ = write_report(self.store, self.tmp / "r", self.today)
        self.assertNotIn("<script>alert", html.read_text())


def _msg(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks))


class FakeClient:
    """Stands in for anthropic.Anthropic(); returns scripted responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.responses.pop(0)


class AgentLoopTests(Base):
    def test_tool_round_trip(self):
        client = FakeClient([
            _msg("tool_use",
                 NS(type="tool_use", id="t1", name="add_task", input={"title": "Buy milk", "tags": ["errand"]}),
                 NS(type="tool_use", id="t2", name="list_tasks", input={})),
            _msg("end_turn", NS(type="text", text="Added #1 Buy milk.")),
        ])
        agent = TaskAgent(ToolExecutor(self.store, self.tmp), client=client)
        self.assertEqual(agent.send("buy milk"), "Added #1 Buy milk.")
        self.assertEqual(self.store.get(1).tags, ["errand"])
        # Both tool results went back in one user message, in order.
        results = client.calls[1]["messages"][-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2"])
        self.assertEqual(json.loads(results[1]["content"])["count"], 1)
        # History: user, assistant(tool_use), user(results), assistant(text)
        self.assertEqual([m["role"] for m in agent.messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(client.calls[0]["model"], "claude-opus-5-5")


class ListTemplateTests(unittest.TestCase):
    def test_both_frontends_carry_the_template(self):
        from taskman.agent import system_prompt
        from taskman.mcp_server import INSTRUCTIONS
        for text in (system_prompt(date(2026, 11, 13)), INSTRUCTIONS):
            for heading in ("## Tasks", "| Task | Priority | Status |", "## Executive Notes", "## Challenges",
                            "act as the business reader", "notes_original"):
                self.assertIn(heading, text)


if __name__ == "__main__":
    unittest.main()
