"""Topic context keeps fresh evidence visible without forcing speculative integrations."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from scripts.feature_topics import research_context, topic_context


class ResearchContextTests(unittest.TestCase):
    def test_late_evening_and_repository_sources_survive_large_early_rankings(self):
        sources = [{"id": f"{prefix}{n}", "url": f"https://example.org/{prefix}{n}",
                    "date": "2026-09-20", "issue_date": "2026-09-27",
                    "signal": "ranking, not measured adoption", "pushed_at": "2026-09-26"}
                   for prefix in ["radar-", "hf-", "radar-evening-", "gh-"]
                   for n in range(12)]
        result = research_context({"sources": sources})
        self.assertEqual(len(result), 20)
        self.assertTrue(result[0]["id"].startswith("radar-evening-"))
        for prefix in ["radar-", "hf-", "radar-evening-", "gh-"]:
            self.assertTrue(any(r["id"].startswith(prefix) for r in result))
        self.assertEqual(result[0]["date"], "2026-09-20")
        self.assertEqual(result[0]["issue_date"], "2026-09-27")
        self.assertEqual(result[0]["signal"], "ranking, not measured adoption")

    def test_duplicate_urls_and_owned_repo_stats_do_not_fill_the_budget(self):
        result = research_context({"sources": [
            {"id": "radar-0", "url": "https://example.org/shared", "summary": "morning"},
            {"id": "repo-local", "url": "https://github.com/owned/repo"},
            {"id": "radar-evening-first", "url": "https://example.org/first"},
            {"id": "radar-evening-0", "url": "https://example.org/shared", "summary": "evening"},
            {"id": "hf-other", "url": "https://example.org/other", "summary": "x" * 10000},
            {"id": "invalid", "url": "file:///etc/passwd"},
            {"id": "bad-host", "url": "https://[invalid"},
            None,
        ]})
        self.assertEqual(len(result), 3)
        shared = next(r for r in result if r["url"].endswith("/shared"))
        self.assertEqual(shared["summary"], "evening")
        other = next(r for r in result if r["id"] == "hf-other")
        self.assertEqual(len(other["summary"]), 500)

    def test_verbose_research_stays_inside_the_text_budget(self):
        sources = [{"id": f"gh-{n}", "url": f"https://example.org/{n}",
                    **{key: "长" * 10000 for key in
                       ["title", "summary", "date", "issue_date", "collected_at", "signal"]}}
                   for n in range(30)]
        result = research_context({"sources": sources})
        self.assertTrue(result)
        self.assertLessEqual(sum(len(json.dumps(r, ensure_ascii=False).encode()) for r in result), 18000)

    def test_empty_research_is_valid_context(self):
        self.assertEqual(research_context({}), [])


class TopicNotesTests(unittest.TestCase):
    def note(self, **changes):
        return {"id": "candidate", "title": "A decision component",
                "checked_on": "2026-09-27", "expires_on": "2026-10-11",
                "summary": "Research lead, no live integration claimed.",
                "sources": ["https://example.org/official"],
                "constraints": ["Compare with a rule baseline."],
                "opportunities": {"evalarc": "Inspect the existing decision report."},
                **changes}

    def load(self, topics, day=date(2026, 9, 27), repo="evalarc"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topics.json"
            path.write_text(json.dumps({"topics": topics}))
            return topic_context(repo, today=day, path=path)

    def test_only_current_repo_opportunity_is_exposed(self):
        result = self.load([self.note()])
        self.assertEqual(len(result["items"]), 1)
        self.assertIn("opportunity", result["items"][0])
        self.assertNotIn("opportunities", result["items"][0])
        self.assertEqual(self.load([self.note()], repo="robot-reel")["items"], [])

    def test_expired_and_future_notes_cannot_keep_driving_daily_work(self):
        for day in [date(2026, 9, 26), date(2026, 10, 12)]:
            result = self.load([self.note()], day)
            self.assertEqual(result["items"], [])
            self.assertTrue(result["diagnostics"])
        self.assertEqual(len(self.load([self.note()], date(2026, 10, 11))["items"]), 1)

    def test_malformed_note_does_not_hide_valid_research(self):
        result = self.load([None, self.note(sources=["file:///private"]), self.note()])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(result["diagnostics"]), 2)

    def test_missing_and_oversized_file_fail_softly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "topics.json"
            for data in [None, "not json", "x" * 64001, "[]"]:
                if data is not None:
                    path.write_text(data)
                result = topic_context("evalarc", path=path)
                self.assertEqual(result["items"], [])
                self.assertTrue(result["diagnostics"])

    def test_notes_have_a_total_context_budget(self):
        topics = [self.note(id=str(i), summary="x" * 5000) for i in range(4)]
        result = self.load(topics)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["diagnostics"])


if __name__ == "__main__":
    unittest.main()
