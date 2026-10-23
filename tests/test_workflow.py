"""Behavioral tests. All papers and PPTX packages below are synthetic fixtures."""
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("workflow", Path(__file__).parents[1] / "scripts" / "workflow.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "中文课题"
        w.init(self.run, "测试课题")
        self.source = Path(self.tmp.name) / "candidates.json"
        self.papers = [{"id": "P001", "title": "Synthetic A", "url": "https://example.org/a", "evidence_level": "abstract"},
                       {"id": "P002", "title": "Synthetic B", "url": "https://example.org/b", "evidence_level": "full_text"}]
        self.source.write_text(json.dumps(self.papers), encoding="utf-8")
        w.import_papers(self.run, self.source)
        self.put("briefing.md", "合成测试导读")

    def put(self, path, content):
        target = self.run / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def select(self, ids=None):
        w.brief(self.run, "briefing.md")
        return w.select(self.run, ids or ["P001"], "测试用户明确选择这些论文")

    def read(self, paper_id="P001"):
        self.put("notes/" + paper_id + ".md", "合成测试全文笔记；Section 2 是测试定位")
        return w.read_paper(self.run, paper_id, "notes/" + paper_id + ".md", "https://example.org/fulltext")

    def deck(self):
        path = self.run / "slides" / "test.pptx"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("ppt/presentation.xml", "<presentation/>")
            z.writestr("ppt/slides/slide1.xml", "<slide/>")
        self.put("slides/qa.md", "Synthetic QA fixture, not a real visual inspection.")

    def test_cannot_select_before_briefing(self):
        with self.assertRaises(w.WorkflowError):
            w.select(self.run, ["P001"], "选 P001")

    def test_cannot_read_without_selection(self):
        self.put("notes/P001.md", "测试")
        with self.assertRaises(w.WorkflowError):
            w.read_paper(self.run, "P001", "notes/P001.md", "https://example.org/a")

    def test_unknown_ids_and_missing_decision(self):
        w.brief(self.run, "briefing.md")
        for ids, message in [(["P999"], "选 P999"), (["P001"], " "), (["P001", "P001"], "选")]:
            with self.assertRaises(w.WorkflowError):
                w.select(self.run, ids, message)

    def test_nonselected_paper_rejected(self):
        self.select()
        with self.assertRaises(w.WorkflowError):
            self.read("P002")

    def test_missing_notes_block_delivery(self):
        self.select(["P001", "P002"])
        self.read()
        self.deck()
        with self.assertRaises(w.WorkflowError):
            w.complete(self.run, "slides/test.pptx", "slides/qa.md")

    def test_full_flow_and_unicode_persistence(self):
        self.select(["P001", "P002"])
        self.assertEqual(self.read()["phase"], "reading")
        self.assertEqual(self.read("P002")["phase"], "ready_for_slides")
        self.deck()
        self.assertEqual(w.complete(self.run, "slides/test.pptx", "slides/qa.md")["phase"], "complete")
        self.assertEqual(w.status(self.run)["issues"], [])
        self.assertEqual(w.load(self.run)["topic"], "测试课题")

    def test_modified_notes_block_delivery(self):
        self.select()
        self.read()
        self.put("notes/P001.md", "Changed")
        with self.assertRaises(w.WorkflowError):
            w.check(self.run)
        self.assertTrue(w.status(self.run)["issues"])

    def test_changed_briefing_requires_review(self):
        w.brief(self.run, "briefing.md")
        self.put("briefing.md", "Changed")
        with self.assertRaises(w.WorkflowError):
            w.select(self.run, ["P001"], "选 P001")

    def test_selection_change_invalidates_readings(self):
        self.select()
        self.read()
        state = w.select(self.run, ["P002"], "改选 P002")
        self.assertEqual(state["readings"], {})
        self.assertTrue((self.run / "notes/P001.md").exists())
        with self.assertRaises(w.WorkflowError):
            w.check(self.run)

    def test_import_invalidates_brief_and_blocks_after_selection(self):
        w.brief(self.run, "briefing.md")
        self.assertEqual(w.import_papers(self.run, self.source)["phase"], "collecting")
        self.select()
        with self.assertRaises(w.WorkflowError):
            w.import_papers(self.run, self.source)

    def test_candidate_tampering_detected(self):
        self.select()
        self.put("papers.json", "[]")
        with self.assertRaises(w.WorkflowError):
            self.read()

    def test_duplicate_doi_and_title(self):
        for mode in ("doi", "title"):
            p = json.loads(json.dumps(self.papers))
            if mode == "doi":
                p[0]["doi"], p[1]["doi"] = "10.1000/TEST", "https://doi.org/10.1000/test"
            else:
                p[1]["title"] = "  synthetic  A "
            with self.assertRaises(w.WorkflowError):
                w.validate_papers(p)

    def test_evidence_cannot_escape_run(self):
        outside = Path(self.tmp.name) / "outside.md"
        outside.write_text("fixture", encoding="utf-8")
        with self.assertRaises(w.WorkflowError):
            w.brief(self.run, "../outside.md")

    def test_fake_pptx_rejected(self):
        self.select()
        self.read()
        self.put("slides/fake.pptx", "This is just an outline")
        self.put("slides/qa.md", "fixture")
        with self.assertRaises(w.WorkflowError):
            w.complete(self.run, "slides/fake.pptx", "slides/qa.md")

    def test_existing_run_not_overwritten(self):
        with self.assertRaises(w.WorkflowError):
            w.init(self.run, "overwrite")
        self.assertEqual(w.load(self.run)["topic"], "测试课题")

    def test_missing_local_fulltext_rejected(self):
        self.select()
        self.put("notes/P001.md", "fixture")
        with self.assertRaises(w.WorkflowError):
            w.read_paper(self.run, "P001", "notes/P001.md", "missing.pdf")


if __name__ == "__main__":
    unittest.main()
