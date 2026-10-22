"""The radar-page-watch freshness evidence (noteflow-auto delivery-freshness contract).

The end-to-end cases run the real `python3 -m pairadar`-equivalent `cli.main([...])`,
with the real FetchReport and its `to_dict()`, in a throwaway copy of the repository
(a real run writes the radar files under the repository root) and with
PAIRADAR_STATE_DIR pointing into it. Only the network is replaced: `fetch.http_get`
serves one synthetic feed entry, `fetch.firecrawl_post` answers from a scenario, and
any socket connection fails the test. Nothing is fetched and ~/.local/state is never
touched.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pairadar import cli, fetch, health

ROOT = Path(__file__).resolve().parent.parent
DAY = "2026-10-21"

DRIVER = r'''
import json, socket, sys, time
scenario, argv = sys.argv[1], sys.argv[2:]

def no_network(*args, **kwargs):
    raise AssertionError("a test run tried to open a network connection")

socket.socket.connect = no_network
socket.create_connection = no_network
time.sleep = lambda seconds: None

from pairadar import cli, fetch

FEED = (b'<?xml version="1.0"?><rss><channel><item><title>A humanoid robot learns to walk</title>'
        b'<link>https://deepmind.google/a</link><pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate>'
        b'</item></channel></rss>')
fetch.http_get = lambda url, *args, **kwargs: FEED

LISTING = "[Figure ships F.04](https://www.figure.ai/news/f04)\nOct 20, 2026\n"

def page(markdown, status=200):
    return {"success": True, "data": {"markdown": markdown, "metadata": {"statusCode": status}}}

def firecrawl(endpoint, path, body, timeout):
    url = body["url"]
    if scenario == "unavailable":
        raise fetch.FirecrawlUnavailable("URLError")
    if scenario == "raises":
        raise RuntimeError("unexpected failure inside the run")
    if url.endswith("/robots.txt"):
        if scenario == "one-ok" and "1x.tech" in url:
            return {"success": True, "data": {"rawHtml": "User-agent: *\nDisallow: /\n",
                                              "metadata": {"statusCode": 200}}}
        return page("", status=404)
    if scenario in ("one-ok", "writer-raises") and url == "https://www.figure.ai/news":
        return page(LISTING)
    return {"success": False, "code": "SCRAPE_DNS_RESOLUTION_ERROR", "error": "DNS"}

fetch.firecrawl_post = firecrawl
if scenario == "writer-raises":
    def broken(*args, **kwargs):
        raise KeyError("evidence bug")
    cli.page_watch_status = broken
sys.exit(cli.main(argv))
'''


def _copy_repository(target: Path) -> Path:
    def ignore(directory, names):
        return {name for name in names if name in (".git", "__pycache__", "tests")}
    shutil.copytree(ROOT, target, ignore=ignore, symlinks=True)
    return target


class HermeticCopyTests(unittest.TestCase):
    """The copied checkout's own history and baseline never leak into the evidence tests."""

    def test_setup_clears_later_runs_and_baseline_from_the_copy(self):
        case = RealRunEvidenceTests("test_firecrawl_unavailable")
        original = _copy_repository

        def polluted(target):
            repo = original(target)
            history = repo / "radar" / "history.json"
            data = json.loads(history.read_text())
            data["runs"].append({"day": "2099-01-01", "failed": [], "page_failed": [], "unavailable": []})
            history.write_text(json.dumps(data))
            (repo / "radar" / "page-watch.json").write_text('{"schema": 2, "pages": {}}')
            return repo
        globals()["_copy_repository"] = polluted
        try:
            case.setUp()
        finally:
            globals()["_copy_repository"] = original
        self.addCleanup(case.doCleanups)
        self.assertEqual(json.loads((case.repo / "radar" / "history.json").read_text())["runs"], [])
        self.assertEqual(list((case.repo / "radar").glob("*page-watch.json*")), [])


class RealRunEvidenceTests(unittest.TestCase):
    """cli.main through the real FetchReport.to_dict(); only the network is mocked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = _copy_repository(Path(tmp.name) / "repo")
        # Hermetic history: the copied checkout may already carry later days (the production
        # clone records today's run before publish_daily runs this suite), which would make
        # runs[-1] someone else's run. Start from the committed schema with no runs.
        history = self.repo / "radar" / "history.json"
        if history.is_file():
            data = json.loads(history.read_text())
            data["runs"] = []
            history.write_text(json.dumps(data))
        # Same for the committed page_watch baseline (05's R155-01): once Firecrawl has run,
        # radar/page-watch.json is in every checkout, and it is not this test's state.
        for leftover in (self.repo / "radar").glob("*page-watch.json*"):
            leftover.unlink()
        self.state = Path(tmp.name) / "state"
        self.evidence = self.state / "page-watch"

    def main(self, scenario, day=DAY):
        env = {**os.environ, "PAIRADAR_STATE_DIR": str(self.state), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("RADAR_FIRECRAWL_URL", None)
        done = subprocess.run([sys.executable, "-c", DRIVER, scenario, "--no-readme", "--date", day],
                              cwd=self.repo, env=env, capture_output=True, text=True, timeout=300)
        return done

    def record(self, day=DAY):
        return json.loads((self.evidence / f"{day}.json").read_text())

    def assertExit0(self, done):
        self.assertEqual(done.returncode, 0, done.stdout[-2000:] + done.stderr[-2000:])
        self.assertNotIn("Traceback", done.stderr)

    def test_firecrawl_unavailable(self):
        done = self.main("unavailable")
        self.assertExit0(done)
        record = self.record()
        self.assertEqual((record["schema"], record["channel"], record["status"], record["day"]),
                         ("pairadar-page-watch-v1", "page_watch", "firecrawl-unavailable", DAY))
        self.assertEqual((record["sources_checked"], record["sources_failed"]), (0, []))
        self.assertRegex(record["finished_at"], r"\+00:00$")
        self.assertEqual(stat.S_IMODE(self.evidence.stat().st_mode), 0o700)
        self.assertEqual(list(self.evidence.glob(".*.tmp")), [])
        run = json.loads((self.repo / "radar" / "history.json").read_text())["runs"][-1]
        self.assertEqual((run["unavailable"], run["page_failed"]), (["page_watch"], []))
        self.assertFalse((self.repo / "radar" / "page-watch.json").exists(),
                         "a round that read no page writes no baseline")

    def test_all_pages_failing_is_all_failed_and_never_struggling(self):
        ids = [e["id"] for e in fetch._resolve_page_watch(cli.load_config().sources)]
        for day in ("2026-10-19", "2026-10-20", DAY):
            self.assertExit0(self.main("all-failed", day))
        record = self.record()
        self.assertEqual((record["status"], record["sources_checked"]), ("all-failed", 0))
        self.assertEqual(record["sources_failed"], sorted(ids))
        runs = json.loads((self.repo / "radar" / "history.json").read_text())["runs"]
        self.assertEqual(runs[-1]["page_failed"], sorted(ids))
        self.assertFalse(set(ids) & set(runs[-1]["failed"]), "page_watch ids never enter `failed`")
        # The source-repair job's own gate: no struggling source, exit 0, nothing to pick.
        gate = subprocess.run([sys.executable, "-m", "pairadar.health", "--date", DAY,
                               "--threshold", "3", "--fail-on-struggling"],
                              cwd=self.repo, capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        verdict = json.loads(gate.stdout)
        self.assertFalse(set(ids) & {entry["source"] for entry in verdict["struggling"]})
        self.assertEqual({entry["source"] for entry in verdict["page_watch_failing"]}, set(ids))

    def test_one_page_ok(self):
        done = self.main("one-ok")
        self.assertExit0(done)
        record = self.record()
        self.assertEqual((record["status"], record["sources_checked"]), ("ok", 1))
        self.assertEqual(record["sources_robots_skipped"], ["1x"])
        self.assertNotIn("figure", record["sources_failed"])
        self.assertNotIn("1x", record["sources_failed"])
        baseline = json.loads((self.repo / "radar" / "page-watch.json").read_text())
        self.assertEqual(baseline["schema"], "pairadar-page-watch-baseline-2")
        self.assertEqual([link["url"] for link in baseline["sources"]["figure"]["links"]],
                         ["https://www.figure.ai/news/f04"])
        self.assertEqual(list((self.repo / "radar").glob(".page-watch.json.*")), [], "atomic write")

    def test_a_run_that_raises_leaves_an_error_record(self):
        done = self.main("raises")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("unexpected failure inside the run", done.stderr)
        record = self.record()
        self.assertEqual((record["status"], record["sources_checked"]), ("error", 0))

    def test_a_broken_evidence_writer_never_fails_the_radar(self):
        done = self.main("writer-raises")
        self.assertExit0(done)
        self.assertIn("page_watch evidence not written: KeyError", done.stdout)
        self.assertFalse((self.evidence / f"{DAY}.json").exists())

    def test_a_corrupt_baseline_is_rebuilt_not_fatal(self):
        (self.repo / "radar" / "page-watch.json").write_text('{"schema": "pairadar-page-watch-baseline-2", "sour')
        done = self.main("one-ok")
        self.assertExit0(done)
        self.assertIn("unreadable", done.stdout)
        baseline = json.loads((self.repo / "radar" / "page-watch.json").read_text())
        self.assertIn("figure", baseline["sources"])
        self.assertEqual(self.record()["status"], "ok")


class StatusTests(unittest.TestCase):
    def test_status_table(self):
        block = {"enabled": True, "attempted": ["a", "b"], "checked": [], "failed": ["a"],
                 "robots_skipped": ["b"], "unavailable": False}
        self.assertEqual(cli.page_watch_status(block), "all-failed")
        self.assertEqual(cli.page_watch_status({**block, "checked": ["a"]}), "ok")
        self.assertEqual(cli.page_watch_status({**block, "unavailable": True}), "firecrawl-unavailable")
        self.assertEqual(cli.page_watch_status({**block, "enabled": False}), "skipped")
        self.assertEqual(cli.page_watch_status({**block, "attempted": []}), "skipped")
        self.assertEqual(cli.page_watch_status(block, error=True), "error")
        self.assertEqual(cli.page_watch_status(None), "skipped")

    def test_the_writer_reads_only_the_page_watch_block(self):
        # The old bug: overall to_dict() counts are ints. They must be ignored entirely.
        with tempfile.TemporaryDirectory() as tmp:
            fetch = {"attempted": 12, "answered": 11, "failed": ["arxiv"], "unavailable": [],
                     "page_watch": {"enabled": True, "attempted": ["figure"], "checked": ["figure"],
                                    "failed": [], "robots_skipped": [], "unavailable": False}}
            path = cli.write_page_watch_evidence(fetch, DAY, Path(tmp) / "pw")
            record = json.loads(path.read_text())
        self.assertEqual((record["status"], record["sources_checked"], record["sources_failed"]),
                         ("ok", 1, []))

    def test_only_real_runs_report(self):
        with mock.patch.object(cli, "run", return_value={"fetch": {"attempted": 0, "answered": 0},
                                                          "date": DAY}), \
             mock.patch.object(cli, "write_page_watch_evidence") as evidence, \
             mock.patch.object(cli, "degraded", return_value=False), \
             tempfile.TemporaryDirectory() as tmp:
            cli.main(["--offline", "--no-readme"])
            cli.main(["--no-readme", "--out", str(Path(tmp) / "out")])
            self.assertEqual(evidence.call_count, 0)
            cli.main(["--no-readme"])
            self.assertEqual(evidence.call_count, 1)
        with mock.patch.object(cli, "run", side_effect=RuntimeError("boom")), \
             mock.patch.object(cli, "write_page_watch_evidence") as evidence:
            with self.assertRaises(RuntimeError):
                cli.main(["--offline", "--no-readme"])
            self.assertEqual(evidence.call_count, 0)

    def test_state_dir_resolution(self):
        with mock.patch.dict(os.environ, {"PAIRADAR_STATE_DIR": "/x/pairadar"}):
            self.assertEqual(cli.page_watch_evidence_dir(), Path("/x/pairadar/page-watch"))
        with mock.patch.dict(os.environ, {"PAIRADAR_STATE_DIR": "", "XDG_STATE_HOME": "/s"}):
            self.assertEqual(cli.page_watch_evidence_dir(), Path("/s/pairadar/page-watch"))

    def test_a_write_failure_never_fails_the_radar(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            self.assertIsNone(cli.write_page_watch_evidence({}, DAY, blocker / "page-watch"))


class HealthSeparationTests(unittest.TestCase):
    def test_page_watch_failures_are_never_struggling(self):
        from datetime import date
        runs = [{"date": f"2026-10-{d}", "failed": ["arxiv"] if d == 21 else [], "fallback": [],
                 "unavailable": [], "page_failed": ["figure"], "page_robots_skipped": ["1x"]}
                for d in (17, 18, 19, 20, 21)]
        verdict = health.report(runs, date(2026, 10, 21), threshold=3)
        self.assertEqual(verdict["struggling"], [])
        self.assertEqual(verdict["page_watch_failing"], [{"source": "figure", "failed_days": 5,
                                                          "streak_days": 5, "failing_since": "2026-10-17"}])
        self.assertEqual(verdict["page_watch_robots_skipped"], {"1x": 5})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text(json.dumps({"runs": runs}))
            with mock.patch("sys.stdout"):
                code = health.main(["--history", str(path), "--date", "2026-10-21",
                                    "--threshold", "3", "--fail-on-struggling"])
        self.assertEqual(code, 0, "the repair job's gate stays shut for page_watch failures")

    def test_a_page_watch_id_under_failed_is_still_never_struggling(self):
        # A run log in the shape the first PR154 head wrote: page_watch ids in `failed`.
        from datetime import date
        runs = [{"date": f"2026-10-{d}", "attempted": ["arxiv", "figure"], "failed": ["figure", "arxiv"],
                 "fallback": [], "unavailable": []} for d in (17, 18, 19, 20, 21)]
        verdict = health.report(runs, date(2026, 10, 21), threshold=3)
        self.assertEqual([entry["source"] for entry in verdict["struggling"]], ["arxiv"])
        self.assertEqual([entry["source"] for entry in verdict["page_watch_failing"]], ["figure"])
        self.assertIn("figure", health.page_watch_ids())


if __name__ == "__main__":
    unittest.main()
