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
from publication_fixture import security_home

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

    def test_a_new_process_after_midnight_resumes_the_previous_evening(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(night_day(now=datetime(2026, 9, 26, 17, 40, tzinfo=timezone.utc)),
                             "2026-09-26")  # 01:40 Singapore on Sep 27
            self.assertEqual(night_day(now=datetime(2026, 9, 27, 13, 29, tzinfo=timezone.utc)),
                             "2026-09-26")
            self.assertEqual(night_day(now=datetime(2026, 9, 27, 13, 30, tzinfo=timezone.utc)),
                             "2026-09-27")
            self.assertEqual(night_day("2026-09-27", now=datetime(
                2026, 9, 27, 6, 0, tzinfo=timezone.utc)), "2026-09-27")

    def test_retry_preserves_the_initial_evening_even_after_another_boundary(self):
        with patch.dict(os.environ, {
            "RADAR_RUN_STARTED_AT": "2026-09-26T13:30:00Z", "RADAR_RUN_DAY": "2026-09-26",
        }):
            self.assertEqual(night_day(now=datetime(2026, 9, 27, 14, tzinfo=timezone.utc)),
                             "2026-09-26")

    def test_daily_date_cli_does_not_use_the_night_window(self):
        with patch.dict(os.environ, {
            "RADAR_RUN_STARTED_AT": "2026-09-26T23:40:00Z", "RADAR_RUN_DAY": "2026-09-27",
        }):
            command = [sys.executable, str(ROOT / "scripts/job_schedule.py")]
            daily = subprocess.run(command, check=True, text=True, capture_output=True).stdout.strip()
            night = subprocess.run(command + ["--night"], check=True, text=True,
                                   capture_output=True).stdout.strip()
            self.assertEqual(daily, "2026-09-27")
            self.assertEqual(night, "2026-09-26")

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
        self.calls.append((name, list(argv), env["RADAR_RUN_DAY"], seconds))
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
        self.assertEqual([c[0] for c in self.calls],
                         ["fresh", "repos", "updates", "sources", "publish", "notes", "report"])
        self.assertLessEqual(sum(c[3] for c in self.calls), 4 * 60 * 60)
        self.assertTrue(all(c[2] == self.day for c in self.calls))
        self.assertIn("--radar-snapshot", next(c[1] for c in self.calls if c[0] == "repos"))
        self.calls.clear()
        self.run_batch()
        self.assertEqual(self.calls, [])

    def test_failed_repair_keeps_independent_work_then_retries_only_repair(self):
        self.failures.add("sources")
        self.assertEqual(self.run_batch()["status"], "retry-needed")
        self.assertIn("repos", [c[0] for c in self.calls])
        self.calls.clear(); self.failures.clear()
        self.assertEqual(self.run_batch()["status"], "complete")
        self.assertEqual([c[0] for c in self.calls], ["sources", "report"])

    def test_failed_publication_blocks_notes_but_not_independent_research(self):
        self.failures.add("publish")
        result = self.run_batch()
        self.assertEqual(result["stages"]["notes"]["status"], "waiting")
        self.assertNotIn("notes", [c[0] for c in self.calls])
        self.assertIn("repos", [c[0] for c in self.calls])
        self.failures.clear(); self.calls.clear()
        self.assertEqual(self.run_batch()["status"], "complete")
        self.assertEqual([c[0] for c in self.calls], ["publish", "notes", "report"])

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
        self.assertEqual([c[0] for c in self.calls], ["repos", "updates", "report"])

    def test_deferrals_run_announcements_and_report_then_stop_catchup(self):
        def execute(argv, root, env, log, seconds):
            code = self.executor(argv, root, env, log, seconds)
            return 20 if log.stem == "repos" else code
        result = run_batch(self.root, self.state, self.day, execute)
        self.assertEqual(result["status"], "complete-with-deferrals")
        self.assertEqual(result["stages"]["repos"]["status"], "deferred")
        self.assertEqual(result["stages"]["updates"]["status"], "complete")
        self.assertEqual(result["stages"]["report"]["status"], "complete")
        self.calls.clear()
        self.assertEqual(run_batch(self.root, self.state, self.day, execute), result)
        self.assertEqual(self.calls, [])

    def test_retryable_publication_resumes_even_when_features_are_deferred(self):
        self.failures.add("publish")
        def execute(argv, root, env, log, seconds):
            code = self.executor(argv, root, env, log, seconds)
            return 20 if log.stem == "repos" else code
        result = run_batch(self.root, self.state, self.day, execute)
        self.assertEqual(result["status"], "retry-needed")
        self.failures.clear()
        self.calls.clear()
        result = run_batch(self.root, self.state, self.day, execute)
        self.assertEqual(result["status"], "complete-with-deferrals")
        self.assertEqual([c[0] for c in self.calls], ["publish", "notes", "report"])


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
            env = {**os.environ, "HOME": security_home(base),
                   "RADAR_REPO": str(clone), "RADAR_CLONE_URL": str(origin),
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
    def assert_process_stopped(self, pid):
        # killpg delivers a signal asynchronously. A /proc read on another CPU
        # can still observe R briefly after SIGKILL, before the kernel exits it.
        import time
        deadline = time.monotonic() + 1
        while True:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return
            if state == "Z":
                return
            if time.monotonic() >= deadline:
                self.fail(f"descendant {pid} remains running (state {state})")
            time.sleep(0.01)

    def test_proc_entry_can_disappear_during_read(self):
        with patch.object(Path, "read_text", side_effect=ProcessLookupError):
            self.assert_process_stopped(123456)

    def test_exit_observation_still_rejects_a_live_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with self.assertRaisesRegex(AssertionError, "remains running"):
                self.assert_process_stopped(child.pid)
        finally:
            child.kill()
            child.wait(timeout=5)

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
            self.assert_process_stopped(pid)

    def wait_for_pid(self, path, seconds=5):
        """The pid a stage wrote: polled until the file holds a whole number, never read half-written."""
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                text = path.read_text().strip()
            except FileNotFoundError:
                text = ""
            if text.isdigit():
                return int(text)
            time.sleep(0.02)
        self.fail(f"{path.name} never held a pid")

    def test_bootstrap_termination_also_terminates_active_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The launched Python process uses the same handler as a cron batch. The stage writes its pid to a temporary
            # file and renames it, so the test can never read the empty file between its creation and its content.
            code = (
                "from pathlib import Path; import os,sys; from scripts.nightly_batch import execute; "
                "execute([sys.executable,'-c',"
                "'import os,time; open(\"stage.pid.tmp\",\"w\").write(str(os.getpid())); "
                "os.replace(\"stage.pid.tmp\",\"stage.pid\"); time.sleep(60)'], "
                f"Path({str(root)!r}), dict(os.environ), Path({str(root / 'stage.log')!r}), 60)"
            )
            parent = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT)
            try:
                pid = self.wait_for_pid(root / "stage.pid")
                parent.send_signal(signal.SIGTERM)
                self.assertEqual(parent.wait(timeout=15), 143)
                self.assert_process_stopped(pid)
            finally:
                if parent.poll() is None:
                    parent.kill(); parent.wait()

    # A SIGTERM or SIGINT that arrives while the stage process is being created, or just after, must still end the stage.
    # The stage is started by `Popen`; the signal is sent from inside it, at the point named, by a subclass in the batch
    # process itself (not a thread, not a timer), so the moment is exact rather than lucky.
    INJECTED_BATCH = r'''
import os, signal, subprocess, sys, time
from pathlib import Path
from scripts import nightly_batch

root, point, sig = Path(sys.argv[1]), sys.argv[2], getattr(signal, sys.argv[3])
real = subprocess.Popen


def record(pid):
    (root / "stage.pid.tmp").write_text(str(pid))
    os.replace(root / "stage.pid.tmp", root / "stage.pid")


class Injected(real):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        record(self.pid)
        if point == "inside-popen":
            os.kill(os.getpid(), sig)       # the stage exists; execute() does not have the Popen object yet
            time.sleep(0.3)                 # a Python handler runs here, before Popen returns

    def wait(self, timeout=None):
        if point == "before-wait" and not getattr(Injected, "sent", False):
            Injected.sent = True
            os.kill(os.getpid(), sig)       # execute() has the child and is about to wait for it
            time.sleep(0.3)
        return super().wait(timeout)


nightly_batch.subprocess.Popen = Injected
nightly_batch.execute([sys.executable, "-c", "import time; time.sleep(60)"], root, dict(os.environ),
                      root / "stage.log", 60)
sys.exit(0)           # reached only when the signal did not stop the batch
'''

    def run_injected_batch(self, point, signame):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid = None
            try:
                try:
                    done = subprocess.run([sys.executable, "-c", self.INJECTED_BATCH, str(root), point, signame],
                                          cwd=ROOT, capture_output=True, timeout=15)
                except subprocess.TimeoutExpired:
                    self.fail("the batch was still running 15 s after the signal: it did not stop its stage")
                pid = self.wait_for_pid(root / "stage.pid")
                code = getattr(signal, signame)
                self.assertEqual(done.returncode, 128 + code, done.stderr.decode(errors="replace"))
                self.assert_process_stopped(pid)
            finally:
                # never leave a stage behind, whatever the assertions said (its pid file is written atomically)
                try:
                    os.killpg(int((root / "stage.pid").read_text()), signal.SIGKILL)
                except (FileNotFoundError, ValueError, ProcessLookupError):
                    pass

    def test_a_signal_while_the_stage_is_being_created_still_ends_the_stage(self):
        for signame in ("SIGTERM", "SIGINT"):
            with self.subTest(signame):
                self.run_injected_batch("inside-popen", signame)

    def test_a_signal_just_after_the_stage_exists_still_ends_the_stage(self):
        for signame in ("SIGTERM", "SIGINT"):
            with self.subTest(signame):
                self.run_injected_batch("before-wait", signame)

    def test_the_stage_does_not_inherit_a_changed_signal_disposition(self):
        # the batch handles SIGTERM and SIGINT itself; the stage must see the defaults and an unblocked mask, so that
        # the batch's termination (killpg) and a person's Ctrl-C reach the stage's own code
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = ("import json,signal; print(json.dumps({'term': str(signal.getsignal(signal.SIGTERM)), "
                     "'int': str(signal.getsignal(signal.SIGINT)), "
                     "'blocked': sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, []))}))")
            self.assertEqual(execute([sys.executable, "-c", stage], root, dict(os.environ), root / "stage.log", 20), 0)
            seen = json.loads((root / "stage.log").read_text())
            self.assertEqual(seen, {"term": str(signal.SIG_DFL), "int": str(signal.default_int_handler), "blocked": []})

    def test_the_handlers_are_put_back_after_a_stage_and_after_a_failed_start(self):
        before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(execute([sys.executable, "-c", "pass"], root, dict(os.environ), root / "a.log", 20), 0)
            self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)), before)
            with self.assertRaises(FileNotFoundError):
                execute(["/nonexistent-stage-binary"], root, dict(os.environ), root / "b.log", 20)
            self.assertEqual((signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)), before)

    # 01 and 04, PR217 round 1: the same batch process, with a signal sent at the moment each case names. `mode` is one of
    # before-popen (handlers installed, `Popen` not yet called), popen-fails-after-signal, popen-fails (no signal),
    # wait-lock (inside Popen's own wait, which holds its non-reentrant lock) and install-interrupted (a SIGINT between the
    # installation of the two handlers). The batch runs through `run_batch`, so "carries on to the next stage" is observable.
    SECOND_BATCH = r'''
import errno, json, os, signal, subprocess, sys, time
from pathlib import Path
from scripts import nightly_batch

root, mode, sig = Path(sys.argv[1]), sys.argv[2], getattr(signal, sys.argv[3])
original = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
real_popen = subprocess.Popen
calls = root / "calls.txt"


def mark(name, text=""):
    (root / (name + ".tmp")).write_text(text)
    os.replace(root / (name + ".tmp"), root / name)


class Launches(real_popen):
    """Records that a stage was started, and its pid; the behaviour at each point depends on the mode."""
    def __init__(self, *args, **kwargs):
        if mode == "popen-fails-after-signal":
            os.kill(os.getpid(), sig)
            time.sleep(0.2)                                    # the handler runs here: it records the request
            raise OSError(errno.EAGAIN, "fork failed")
        if mode == "popen-fails":
            raise FileNotFoundError(errno.ENOENT, "no such stage")
        super().__init__(*args, **kwargs)
        mark("launched", str(self.pid))
        mark("stage.pid", str(self.pid))

    def _try_wait(self, wait_flags):
        if mode == "wait-lock" and not getattr(Launches, "sent", False):
            Launches.sent = True
            mark("lock-held", str(self._waitpid_lock.locked()))
            os.kill(os.getpid(), sig)                          # inside the real wait, holding its lock
            time.sleep(0.2)
        return super()._try_wait(wait_flags)

    def wait(self, timeout=None):
        code = super().wait(timeout)
        if mode == "exit-with-request" and not getattr(Launches, "sent2", False):
            Launches.sent2 = True
            os.kill(os.getpid(), sig)                          # the stage has already ended and its code is in hand
            time.sleep(0.2)
        return code


if mode == "before-popen":
    real_mask = signal.pthread_sigmask

    def mask(how, signals):
        old = real_mask(how, signals)
        if how == signal.SIG_SETMASK and not getattr(mask, "sent", False):
            mask.sent = True                                   # the handlers are in, Popen has not been called
            os.kill(os.getpid(), sig)
            time.sleep(0.2)
        return old
    signal.pthread_sigmask = mask

if mode == "install-interrupted":
    real_signal = signal.signal

    def installing(number, handler):
        old = real_signal(number, handler)
        if number == signal.SIGTERM and handler is not original[0] and not getattr(installing, "sent", False):
            installing.sent = True                             # SIGTERM's handler is in, SIGINT's is not
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.2)
        return old
    signal.signal = installing

nightly_batch.subprocess.Popen = Launches


def executor(argv, stage_root, env, log, seconds):
    with calls.open("a") as handle:
        handle.write("call\n")
    stage = [sys.executable, "-c", "pass" if mode == "exit-with-request" else "import time; time.sleep(60)"]
    return nightly_batch.execute(stage, stage_root, env, log, seconds)


outcome = {}
try:
    nightly_batch.run_batch(root, root / "state", "2026-11-16", executor=executor)
    outcome["finished"] = True
except SystemExit as exit_:
    outcome["exit"] = exit_.code
outcome["handlers_restored"] = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == original
print(json.dumps(outcome), flush=True)
if mode == "install-interrupted":
    os.kill(os.getpid(), signal.SIGTERM)                       # must take the default action if the handler was put back
    time.sleep(2)
    print(json.dumps({"swallowed": True}), flush=True)
sys.exit(outcome.get("exit", 0))
'''

    def run_second(self, mode, signame, timeout=20):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state").mkdir()
            try:
                done = subprocess.run([sys.executable, "-c", self.SECOND_BATCH, str(root), mode, signame],
                                      cwd=ROOT, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                self.fail(f"{mode}: the batch was still running after {timeout} s")
            finally:
                try:                                           # never leave a stage behind, whatever the assertions said
                    os.killpg(int((root / "stage.pid").read_text()), signal.SIGKILL)
                except (FileNotFoundError, ValueError, ProcessLookupError):
                    pass
            lines = [json.loads(line) for line in done.stdout.decode().splitlines() if line.startswith("{")]
            calls = (root / "calls.txt").read_text().count("call") if (root / "calls.txt").exists() else 0
            files = {name: (root / name).read_text() for name in ("launched", "lock-held", "stage.pid") if (root / name).exists()}
            return done, lines, calls, files

    def assert_stage_gone(self, files):
        if "stage.pid" in files:
            self.assert_process_stopped(int(files["stage.pid"]))

    def test_a_request_already_known_before_the_stage_starts_starts_no_stage(self):
        # 01 F1 case 1: the handlers are in, the signal has arrived, `Popen` has not been called
        for signame in ("SIGTERM", "SIGINT"):
            with self.subTest(signame):
                done, lines, calls, files = self.run_second("before-popen", signame)
                self.assertEqual(done.returncode, 128 + getattr(signal, signame), done.stderr.decode(errors="replace"))
                self.assertNotIn("launched", files)                      # no stage effect that could not be taken back
                self.assertEqual((calls, lines[-1]["handlers_restored"]), (1, True))

    def test_a_start_that_fails_while_a_request_is_known_ends_the_batch_and_starts_no_next_stage(self):
        # 01 F1 case 2 and 04 F1: the request was swallowed by the failure and the batch went on to the next stage
        for signame in ("SIGTERM", "SIGINT"):
            with self.subTest(signame):
                done, lines, calls, files = self.run_second("popen-fails-after-signal", signame)
                self.assertEqual(done.returncode, 128 + getattr(signal, signame), done.stderr.decode(errors="replace"))
                self.assertEqual(lines[-1], {"exit": 128 + getattr(signal, signame), "handlers_restored": True})
                self.assertEqual(calls, 1)                                # the first stage only

    def test_a_start_failure_with_no_request_is_still_an_ordinary_stage_error(self):
        done, lines, calls, _ = self.run_second("popen-fails", "SIGTERM")
        self.assertEqual(done.returncode, 0, done.stderr.decode(errors="replace"))
        self.assertEqual(lines[-1]["handlers_restored"], True)
        self.assertGreater(calls, 1)                                      # run_batch records retry-needed and goes on, as before

    def test_a_request_that_arrives_while_the_wait_holds_its_lock_does_not_hang_the_batch(self):
        # 01 F2: the handler called wait() again and waited for the lock the interrupted wait() held, for ever
        for signame in ("SIGTERM", "SIGINT"):
            with self.subTest(signame):
                done, lines, calls, files = self.run_second("wait-lock", signame, timeout=25)
                self.assertEqual(files.get("lock-held"), "True")          # the signal really arrived inside the lock
                self.assertEqual(done.returncode, 128 + getattr(signal, signame), done.stderr.decode(errors="replace"))
                self.assertEqual((calls, lines[-1]["handlers_restored"]), (1, True))
                self.assert_stage_gone(files)

    def test_a_request_that_arrives_as_the_stage_ends_still_ends_the_batch(self):
        # the stage's exit code is in hand when the request comes; the batch must still stop, not start the next stage
        for signame in ("SIGTERM", "SIGINT"):
            with self.subTest(signame):
                done, lines, calls, files = self.run_second("exit-with-request", signame)
                self.assertEqual(done.returncode, 128 + getattr(signal, signame), done.stderr.decode(errors="replace"))
                self.assertEqual((calls, lines[-1]["handlers_restored"]), (1, True))

    def test_a_sigint_between_the_two_installations_leaves_no_handler_behind(self):
        # 01 F3: the KeyboardInterrupt came before the protected block, and SIGTERM's handler stayed, swallowing a later SIGTERM
        done, lines, calls, files = self.run_second("install-interrupted", "SIGTERM")
        self.assertEqual(done.returncode, -signal.SIGTERM, done.stdout.decode(errors="replace"))   # the later SIGTERM killed it
        self.assertEqual(lines[0], {"exit": 128 + signal.SIGINT, "handlers_restored": True})
        self.assertNotIn("launched", files)
