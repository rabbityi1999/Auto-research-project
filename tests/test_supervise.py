"""Offline supervision tests. Fake CLI results only; no model calls."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import workflow as w
import supervise as s
from process_runner import command, retry_kind, run_attempt, STATE_DIR


def result(error="", code=0, completed=True, reason=None):
    return dict(returncode=code, turn_completed=completed, reason=reason, error_text=error)


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "中文任务"
        w.init(self.root, "Synthetic topic")
        self.args = argparse.Namespace(run=self.root, stop=False, recover_interrupted=False,
            codex="unused", max_attempts=3, attempt_timeout=10, retry_delay=0)

    def runner(self, *args):
        return result()

    def state(self):
        return w.read_json(self.root / STATE_DIR / "supervisor.json")

    def test_network_retry_uses_exact_session_and_cumulative_limit(self):
        calls = []
        def run(cmd, prompt, root, log, timeout, on_session):
            calls.append(cmd)
            on_session("explicit-test-session")
            return result("stream disconnected before completion", 1, False)
        self.assertEqual(s.supervise(self.args, ["fake"], run), 3)
        self.assertEqual(len(calls), 3)
        self.assertIn("explicit-test-session", calls[1])
        self.assertNotIn("--last", calls[1])
        self.assertEqual(s.supervise(self.args, ["fake"], run), 3)
        self.assertEqual(len(calls), 3)

    def test_two_successful_turns_without_progress_stop(self):
        self.assertEqual(s.supervise(self.args, ["fake"], self.runner), 2)
        self.assertEqual(self.state()["attempts"], 2)
        self.assertEqual(self.state()["status"], "no_progress")

    def test_user_selection_gate_stops_without_model_call(self):
        data = [{"id":"P001", "title":"Synthetic", "url":"https://example.org/p", "evidence_level":"abstract"}]
        w.write_json(self.root / "candidates.json", data)
        w.import_papers(self.root, self.root / "candidates.json")
        (self.root / "briefing.md").write_text("Synthetic introduction", encoding="utf-8")
        w.brief(self.root, "briefing.md")
        def unexpected(*args):
            self.fail("Must stop before invoking model")
        self.assertEqual(s.supervise(self.args, ["fake"], unexpected), 2)
        self.assertEqual(self.state()["status"], "awaiting_selection")

    def test_agent_block_stops_continuation(self):
        def run(*args):
            w.set_block(self.root, "Full text unavailable")
            return result()
        self.assertEqual(s.supervise(self.args, ["fake"], run), 2)
        self.assertEqual(self.state()["attempts"], 1)
        self.assertEqual(self.state()["status"], "needs_input")

    def test_auth_failure_not_retried(self):
        self.assertEqual(s.supervise(self.args, ["fake"], lambda *a: result("401 unauthorized", 1, False)), 2)
        self.assertEqual(self.state()["attempts"], 1)

    def test_uncertain_old_process_requires_inspection(self):
        control = self.root / STATE_DIR
        control.mkdir()
        w.write_json(control / "supervisor.json", {"run":str(self.root), "status":"running"})
        with self.assertRaises(RuntimeError):
            s.supervise(self.args, ["fake"], self.runner)

    def test_stop_file_blocks_start(self):
        self.args.stop = True
        self.assertEqual(s.supervise(self.args, ["fake"], self.runner), 130)
        self.args.stop = False
        self.assertEqual(s.supervise(self.args, ["fake"], self.runner), 130)

    def test_sandbox_and_approval_are_preserved(self):
        cmd = command(["codex"], "test-session")
        self.assertIn("workspace-write", cmd)
        self.assertIn('approval_policy="never"', cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_timeout_rate_limit_and_malformed_stream_stop(self):
        for value in (result("", 1, False, "timeout"), result("429 rate limit", 1, False),
                      result("Malformed JSON event", 0, True)):
            self.assertNotIn(retry_kind(value), ("retry", "continue"))

    def test_real_process_ignores_error_text_quoted_in_agent_output(self):
        (self.root / STATE_DIR).mkdir()
        code = "import json; print(json.dumps({'type':'item.completed','item':{'text':'401 stream disconnected'}})); print(json.dumps({'type':'turn.completed'}))"
        r = run_attempt([sys.executable, "-c", code], "", self.root,
                        self.root / STATE_DIR / "test.jsonl", 10, lambda x: None)
        self.assertEqual(retry_kind(r), "continue")

    def test_saved_unregistered_note_is_reported_without_marking_read(self):
        data = [{"id":"P001", "title":"Synthetic", "url":"https://example.org/p", "evidence_level":"abstract"}]
        w.write_json(self.root / "candidates.json", data)
        w.import_papers(self.root, self.root / "candidates.json")
        (self.root / "briefing.md").write_text("intro", encoding="utf-8")
        w.brief(self.root, "briefing.md")
        w.select(self.root, ["P001"], "Select P001")
        (self.root / "notes/P001.md").write_text("Partial note", encoding="utf-8")
        report = w.resume(self.root)
        self.assertEqual(report["saved_notes_to_review"][0]["id"], "P001")
        self.assertEqual(report["verified_notes"], [])
        self.assertEqual(report["saved_tooling"]["version"], w.TOOL_VERSION)


if __name__ == "__main__":
    unittest.main()
