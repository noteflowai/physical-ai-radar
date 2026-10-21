"""The radar-page-watch freshness evidence (noteflow-auto delivery-freshness contract)."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pairadar import cli
from pairadar.config import load_config


class PageWatchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.ids = [e["id"] for e in cli._resolve_page_watch(self.config.sources)]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "page-watch"

    def write(self, fetch):
        path = cli.write_page_watch_evidence(self.config, fetch, "2026-10-21", self.dir)
        return json.loads(path.read_text()), path

    def test_ok_only_when_firecrawl_answered_and_pages_were_checked(self):
        record, path = self.write({"attempted": ["arxiv", *self.ids], "failed": [self.ids[0]], "unavailable": []})
        self.assertEqual((record["schema"], record["channel"], record["status"]), ("pairadar-page-watch-v1", "page_watch", "ok"))
        self.assertEqual((record["sources_checked"], record["sources_failed"]), (len(self.ids) - 1, [self.ids[0]]))
        self.assertRegex(record["finished_at"], r"\+00:00$")
        self.assertEqual(path.name, "2026-10-21.json")
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(list(self.dir.glob(".*.tmp")), [])  # atomic: no temp left behind

    def test_unavailable_and_skipped_are_never_ok(self):
        self.assertEqual(self.write({"attempted": ["arxiv"], "failed": [], "unavailable": ["page_watch"]})[0]["status"],
                         "firecrawl-unavailable")
        self.assertEqual(self.write({"attempted": ["arxiv"], "failed": [], "unavailable": []})[0]["status"], "skipped")

    def test_only_real_runs_report(self):
        with mock.patch.object(cli, "run", return_value={"fetch": {"attempted": [], "failed": [], "unavailable": []},
                                                          "day": "2026-10-21"}), \
             mock.patch.object(cli, "write_page_watch_evidence") as evidence, mock.patch.object(cli, "degraded", return_value=False):
            cli.main(["--offline", "--no-readme"])
            cli.main(["--no-readme", "--out", str(self.dir.parent / "out")])
            self.assertEqual(evidence.call_count, 0)
            cli.main(["--no-readme"])
            self.assertEqual(evidence.call_count, 1)

    def test_state_dir_resolution(self):
        with mock.patch.dict(os.environ, {"PAIRADAR_STATE_DIR": "/x/pairadar"}):
            self.assertEqual(cli.page_watch_evidence_dir(), Path("/x/pairadar/page-watch"))
        with mock.patch.dict(os.environ, {"PAIRADAR_STATE_DIR": "", "XDG_STATE_HOME": "/s"}):
            self.assertEqual(cli.page_watch_evidence_dir(), Path("/s/pairadar/page-watch"))

    def test_a_write_failure_never_fails_the_radar(self):
        blocker = self.dir.parent / "file"
        blocker.write_text("x")
        self.assertIsNone(cli.write_page_watch_evidence(self.config, {"attempted": [], "failed": [], "unavailable": []},
                                                        "2026-10-21", blocker / "page-watch"))


if __name__ == "__main__":
    unittest.main()
