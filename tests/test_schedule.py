"""Calendar boundaries, recovery and process lifetime of the real night scheduler."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.job_schedule import night_day, run_day
from scripts.nightly_batch import execute, run_batch
from scripts import improve_repos

ROOT = Path(__file__).resolve().parents[1]


class CalendarTests(unittest.TestCase):
    def test_0740_issue_uses_singapore_date_before_utc_midnight(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(run_day(now=datetime(2026, 9, 26, 23, 40, tzinfo=timezone.utc)),
                             "2026-09-27")

    def test_batch_date_survives_midnight_and_explicit_date_wins(self):
        with patch.dict(os.environ, {"RADAR_RUN_DAY": "2026-09-26"}):
            self.assertEqual(run_day(now=datetime(2026, 9, 27, tzinfo=timezone.utc)), "2026-09-26")
            self.assertEqual(run_day("2026-09-25"), "2026-09-25")

    def test_catchup_handles_year_and_leap_day_boundaries(self):
        self.assertEqual(night_day("2027-01-01", previous_day=True), "2026-12-31")
        self.assertEqual(night_day("2028-03-01", previous_day=True), "2028-02-29")
        for invalid in ("2026-02-30", "../../bad", "20260926"):
            with self.assertRaises(ValueError):
                run_day(invalid)

    def test_companion_catchup_reuses_its_original_day_without_model_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in improve_repos.REPOS:
                result = root / "state/2026-09-26" / name / "result.json"
                result.parent.mkdir(parents=True)
                result.write_text('{"status":"noop"}')
            with patch.object(sys, "argv", [
                "improve_repos", "--date", "2026-09-26", "--state", str(root / "state"),
                "--workspace", str(root / "workspace"),
            ]), patch.object(improve_repos, "snapshot") as snapshot:
                improve_repos.main()
                snapshot.assert_not_called()

    def test_hosted_fallback_pins_same_day_for_every_generation(self):
        text = (ROOT / ".github/workflows/daily.yml").read_text()
        self.assertIn('cron: "20 1 * * *"', text)
        self.assertIn('DAY=$(python scripts/job_schedule.py)', text)
        self.assertIn('echo "RADAR_RUN_DAY=${DAY}" >> "$GITHUB_ENV"', text)
        generations = [line for line in text.splitlines() if "python -m pairadar --limit" in line]
        self.assertEqual(len(generations), 2)
        self.assertTrue(all('--date "$RADAR_RUN_DAY"' in line for line in generations))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.day = "2026-09-26"
        notes = self.root / f"data/notes/{self.day}.json"
        notes.parent.mkdir(parents=True)
        notes.write_text("{}")
        self.calls = []
        self.failures = set()

    def executor(self, argv, root, env, log, seconds):
        name = log.stem
        self.calls.append((name, list(argv), env["RADAR_RUN_DAY"]))
        if name == "fresh":
            latest = self.state / self.day / "fresh-radar/radar/latest.json"
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_text(json.dumps({"date": self.day, "picked": []}))
        return 1 if name in self.failures else 0

    def run_batch(self):
        return run_batch(self.root, self.state, self.day, self.executor)

    def test_order_date_propagation_and_repeat_has_no_work(self):
        result = self.run_batch()
        self.assertEqual(result["status"], "complete")
        self.assertEqual([c[0] for c in self.calls], ["sources", "publish", "fresh", "notes", "repos"])
        self.assertTrue(all(c[2] == self.day for c in self.calls))
        self.assertIn("--radar-snapshot", self.calls[-1][1])
        self.calls.clear()
        self.run_batch()
        self.assertEqual(self.calls, [])

    def test_failed_repair_keeps_independent_work_then_retries_only_repair(self):
        self.failures.add("sources")
        self.assertEqual(self.run_batch()["status"], "retry-needed")
        self.assertIn("repos", [c[0] for c in self.calls])
        self.calls.clear(); self.failures.clear()
        self.assertEqual(self.run_batch()["status"], "complete")
        self.assertEqual([c[0] for c in self.calls], ["sources"])

    def test_failed_publication_blocks_notes_but_not_independent_research(self):
        self.failures.add("publish")
        result = self.run_batch()
        self.assertEqual(result["stages"]["notes"]["status"], "waiting")
        self.assertNotIn("notes", [c[0] for c in self.calls])
        self.assertIn("repos", [c[0] for c in self.calls])
        self.failures.clear(); self.calls.clear()
        self.assertEqual(self.run_batch()["status"], "complete")
        self.assertEqual([c[0] for c in self.calls], ["publish", "notes"])

    def test_notes_that_produce_nothing_are_not_completed(self):
        (self.root / f"data/notes/{self.day}.json").unlink()
        result = self.run_batch()
        self.assertEqual(result["status"], "retry-needed")
        self.assertEqual(result["stages"]["notes"]["status"], "retry-needed")

    def test_failed_fresh_collection_does_not_feed_a_partial_snapshot(self):
        self.failures.add("fresh")
        result = self.run_batch()
        self.assertEqual(result["status"], "retry-needed")
        self.assertNotIn("--radar-snapshot", self.calls[-1][1])

    def test_running_receipt_from_an_interruption_is_retried(self):
        self.run_batch()
        receipt = self.state / self.day / "run.json"
        saved = json.loads(receipt.read_text())
        saved["status"] = "running"
        saved["stages"]["repos"]["status"] = "running"
        receipt.write_text(json.dumps(saved))
        self.calls.clear()
        self.assertEqual(self.run_batch()["status"], "complete")
        self.assertEqual([c[0] for c in self.calls], ["repos"])


@unittest.skipUnless(all(shutil.which(tool) for tool in ("git", "flock")),
                     "The local publisher requires Git and flock")
class PublisherTests(unittest.TestCase):
    def test_manual_night_entry_bootstraps_into_the_configured_dedicated_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); origin = base / "origin"; clone = base / "dedicated"
            origin.mkdir()
            def git(*args):
                subprocess.run(["git", *args], cwd=origin, check=True, capture_output=True)
            git("init", "-b", "main")
            git("config", "user.name", "Schedule test")
            git("config", "user.email", "test@example.invalid")
            (origin / "scripts").mkdir()
            shutil.copy(ROOT / "scripts/nightly.sh", origin / "scripts/nightly.sh")
            (origin / "scripts/nightly.sh").chmod(0o755)
            (origin / "scripts/nightly_batch.py").write_text(
                "import json,os,sys; from pathlib import Path\n"
                "print(json.dumps({'root':str(Path.cwd()),'args':sys.argv[1:],"
                "'lock':os.environ.get('RADAR_LOCK_HELD')}))\n")
            git("add", "."); git("commit", "-m", "fixture")
            env = {**os.environ, "RADAR_REPO": str(clone), "RADAR_CLONE_URL": str(origin),
                   "RADAR_LOCK": str(base / "lock"), "XDG_STATE_HOME": str(base / "state"),
                   "RADAR_TIMEOUT": "10s", "RADAR_BRANCH": "main"}
            env.pop("RADAR_LOCK_HELD", None)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/nightly.sh"), "--previous-day"], cwd=base,
                env=env, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            probe = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(probe, {"root": str(clone), "args": ["--previous-day"], "lock": "1"})

    def test_pinned_day_reaches_collector_and_an_old_catchup_cannot_replace_newer_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); origin = base / "origin"; clone = base / "clone"
            def git(*args):
                return subprocess.run(["git", *args], cwd=origin, check=True,
                                      capture_output=True, text=True)
            origin.mkdir()
            git("init", "-b", "main")
            git("config", "user.name", "Schedule test")
            git("config", "user.email", "test@example.invalid")
            for folder in ("scripts", "pairadar", "radar", "tests"):
                (origin / folder).mkdir()
            shutil.copy(ROOT / "scripts/job_schedule.py", origin / "scripts/job_schedule.py")
            (origin / "pairadar/__init__.py").touch()
            (origin / "tests/__init__.py").touch()
            (origin / "tests/test_generated.py").write_text(
                "import json,unittest\n"
                "class Generated(unittest.TestCase):\n"
                " def test_date(self):\n"
                "  with open('radar/latest.json') as f: value=json.load(f)\n"
                "  self.assertEqual(value['date'],'2026-09-25')\n")
            (origin / "pairadar/__main__.py").write_text(
                "import argparse,json; from pathlib import Path\n"
                "p=argparse.ArgumentParser();p.add_argument('--date',required=True);"
                "p.add_argument('--limit');a=p.parse_args()\n"
                "Path('radar/latest.json').write_text(json.dumps({'date':a.date}))\n")
            (origin / "radar/latest.json").write_text('{"date":"2026-09-24"}')
            git("add", "."); git("commit", "-m", "fixture")
            env = {**os.environ, "RADAR_REPO": str(clone), "RADAR_CLONE_URL": str(origin),
                   "RADAR_RUN_DAY": "2026-09-25", "RADAR_LOCK": str(base / "lock"),
                   "RADAR_BRANCH": "main"}
            command = ["bash", str(ROOT / "scripts/publish_daily.sh"), "--dry-run"]
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((clone / "radar/latest.json").read_text())["date"], "2026-09-25")
            # An issue newer than the saved batch appears before the catch-up.
            (origin / "radar/latest.json").write_text('{"date":"2026-09-26"}')
            git("add", "."); git("commit", "-m", "newer issue")
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing to replace a newer", result.stdout)
            self.assertEqual(json.loads((clone / "radar/latest.json").read_text())["date"], "2026-09-26")


class ResearchInputTests(unittest.TestCase):
    def test_source_date_is_not_replaced_by_issue_or_collection_date(self):
        sources, failures = [], []
        improve_repos.radar_sources({
            "date": "2026-09-26", "generated": "2026-09-26 18:40 UTC",
            "picked": [{"url": "https://example.org/paper", "title": "研究・研究",
                        "published": "2026-09-19"},
                       {"url": "https://example.org/malformed"},
                       {"url": "javascript:alert(1)", "title": "Unsafe"}],
        }, "radar-evening", "fixture", sources, failures)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["date"], "2026-09-19")
        self.assertEqual(sources[0]["issue_date"], "2026-09-26")
        self.assertEqual(sources[0]["collected_at"], "2026-09-26 18:40 UTC")
        self.assertEqual(len(failures), 2)

    def test_utf8_research_is_read_even_under_an_explicit_ascii_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "latest.json"
            source.write_text(json.dumps({
                "date": "2026-09-26", "picked": [
                    {"url": "https://example.org/paper", "title": "研究・研究",
                     "published": "2026-09-19"}],
            }, ensure_ascii=False), encoding="utf-8")
            code = (
                "import json; from pathlib import Path; from unittest.mock import patch\n"
                "from scripts.improve_repos import snapshot\n"
                "with patch('scripts.improve_repos.fetch_json',return_value={'picked':[]}), "
                "patch('scripts.improve_repos.command',return_value='{\"items\":[]}'):\n"
                f" print(json.dumps(snapshot(Path({str(source)!r}))))\n"
            )
            result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                                    env={**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0",
                                         "PYTHONCOERCECLOCALE": "0"}, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            data = json.loads(result.stdout)
            fresh = [x for x in data["sources"] if x["id"].startswith("radar-evening")]
            self.assertEqual(len(fresh), 1)
            self.assertEqual(fresh[0]["title"], "研究・研究")


@unittest.skipUnless(sys.platform == "linux", "Process-group checks require Linux")
class LifetimeTests(unittest.TestCase):
    def test_timed_out_stage_cannot_leave_its_descendant_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = (
                "import os,subprocess,sys,time; "
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "open('child.pid','w').write(str(p.pid)); time.sleep(60)"
            )
            self.assertEqual(execute([sys.executable, "-c", code], root, dict(os.environ),
                                     root / "stage.log", 1), 124)
            pid = int((root / "child.pid").read_text())
            status = Path(f"/proc/{pid}/stat")
            if status.exists():
                self.assertEqual(status.read_text().split()[2], "Z", "descendant is still running")

    def test_bootstrap_termination_also_terminates_active_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The launched Python process uses the same handler as a cron batch.
            code = (
                "from pathlib import Path; import os,sys; from scripts.nightly_batch import execute; "
                "execute([sys.executable,'-c',"
                "'import os,time; open(\"stage.pid\",\"w\").write(str(os.getpid())); time.sleep(60)'], "
                f"Path({str(root)!r}), dict(os.environ), Path({str(root / 'stage.log')!r}), 60)"
            )
            parent = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT)
            try:
                import time
                deadline = time.monotonic() + 5
                while not (root / "stage.pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                pid = int((root / "stage.pid").read_text())
                parent.send_signal(signal.SIGTERM)
                self.assertEqual(parent.wait(timeout=15), 143)
                status = Path(f"/proc/{pid}/stat")
                if status.exists():
                    self.assertEqual(status.read_text().split()[2], "Z")
            finally:
                if parent.poll() is None:
                    parent.kill(); parent.wait()
