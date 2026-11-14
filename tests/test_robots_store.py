"""What the radar remembers about robots.txt between runs, and what it must never do with it.

The unit tests drive RobotsStore directly with a fake clock and a temporary directory. The run tests go through
fetch.fetch_page_watch as the radar runs it, with the test TLS server, and count the requests the server saw.
What this does not show: the real sites, or how long a real 429 lasts.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pairadar import cli, fetch, robots_rules, robots_store, static_fetch  # noqa: E402
from pairadar.config import Config  # noqa: E402
from static_fixture import HOST, Reply, TlsTestCase  # noqa: E402
from test_page_watch_static import LISTING, ROBOTS_OK, V1, sources  # noqa: E402

ORIGIN = f"https://{HOST}"
T0 = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 14)
ROBOTS_TEXT = "User-agent: *\nAllow: /\n"


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def private_dir(parent: Path) -> Path:
    path = parent / "state"
    path.mkdir(mode=0o700)
    return path


class RetryAfterTests(unittest.TestCase):
    def test_a_whole_number_of_seconds_is_read_and_never_shortened(self):
        for value, expected in (("0", 0), ("30", 30), (" 120 ", 120), ("86400", 86400), ("172800", 172800),
                                ("34560000", 34_560_000), ("999999999999", 999_999_999_999), ("9" * 60, int("9" * 60))):
            with self.subTest(value):
                self.assertEqual(static_fetch.parse_retry_after(value, now=0), expected)

    def test_the_longest_wait_that_is_recorded_with_an_end_time_is_366_days(self):
        self.assertEqual(robots_store.MAX_BACKOFF, timedelta(days=366))
        self.assertFalse(hasattr(static_fetch, "MAX_RETRY_AFTER"))                          # the parser has no ceiling of its own

    def test_an_http_date_is_counted_from_now_rounded_up_and_a_past_date_is_nothing(self):
        now = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(static_fetch.parse_retry_after("Sat, 14 Nov 2026 00:05:00 GMT", now=now), 300)
        self.assertEqual(static_fetch.parse_retry_after("Sat, 14 Nov 2026 12:00:00 GMT", now=now), 12 * 3600)
        self.assertEqual(static_fetch.parse_retry_after("Mon, 16 Nov 2026 00:00:00 GMT", now=now), 2 * 86400)   # not cut to a day
        self.assertEqual(static_fetch.parse_retry_after("Sat, 14 Nov 2026 00:05:00 GMT", now=now + 0.4), 300)    # 299.6 s: up
        self.assertEqual(static_fetch.parse_retry_after("Sat, 14 Nov 2026 00:05:00 GMT", now=now - 0.4), 301)    # 300.4 s: up
        self.assertIsNone(static_fetch.parse_retry_after("Fri, 13 Nov 2026 23:00:00 GMT", now=now))
        far = static_fetch.parse_retry_after("Fri, 31 Dec 9999 23:59:59 GMT", now=now)       # thousands of years: not cut, not None
        self.assertGreater(far, 200 * 365 * 86400)

    def test_anything_else_is_nothing(self):
        for value in (None, "", "   ", "-5", "+5", "1.5", "5s", "soon", "0x10", "١٢٣", "Sat, 31 Nov 2026 00:00:00 GMT",
                      "Sat, 14 Nov 2026 00:05:00", "9" * 80, 5, b"5"):
            with self.subTest(value):
                self.assertIsNone(static_fetch.parse_retry_after(value, now=0))


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.directory = self.root / "state"
        self.clock = Clock()

    def store(self, writable=True, directory=None):
        return robots_store.RobotsStore(directory or self.directory, writable=writable, clock=self.clock)

    def test_a_robots_file_that_was_read_is_kept_for_less_than_a_day_and_never_longer(self):
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        for delta, expected in ((timedelta(0), ("rules", ROBOTS_TEXT)), (timedelta(hours=23, minutes=59), ("rules", ROBOTS_TEXT)),
                                (timedelta(hours=24), None), (timedelta(days=3), None)):
            with self.subTest(delta):
                self.clock.now = T0 + delta
                self.assertEqual(self.store().lookup(ORIGIN), expected)

    def test_an_absent_robots_file_is_remembered_the_same_way(self):
        self.store().record_rules(ORIGIN, None)
        self.assertEqual(self.store().lookup(ORIGIN), ("rules", None))
        self.clock.advance(hours=25)
        self.assertIsNone(self.store().lookup(ORIGIN))

    def test_a_429_is_remembered_for_the_wait_it_asked_for_and_six_hours_when_it_asked_for_none(self):
        self.store().record_backoff(ORIGIN, "http-429", 600)
        for minutes, expected in ((0, ("backoff", "http-429")), (9, ("backoff", "http-429")), (10, None), (700, None)):
            with self.subTest(minutes):
                self.clock.now = T0 + timedelta(minutes=minutes)
                self.assertEqual(self.store().lookup(ORIGIN), expected)
        self.clock.now = T0
        self.store().record_backoff(ORIGIN, "http-429", None)
        for hours, expected in ((5, ("backoff", "http-429")), (6, None)):
            self.clock.now = T0 + timedelta(hours=hours, minutes=-1 if hours == 5 else 0)
            self.assertEqual(self.store().lookup(ORIGIN), expected, hours)

    def test_the_wait_has_a_floor_and_is_never_shortened_below_what_was_asked(self):
        self.store().record_backoff(ORIGIN, "http-429", 0)
        self.clock.advance(seconds=59)
        self.assertEqual(self.store().lookup(ORIGIN), ("backoff", "http-429"))          # at least a minute
        self.clock.advance(seconds=2)
        self.assertIsNone(self.store().lookup(ORIGIN))
        self.clock.now = T0
        self.store().record_backoff(ORIGIN, "http-429", 172_800)                          # two days asked for: two days kept
        self.clock.advance(hours=47, minutes=59)
        self.assertEqual(self.store().lookup(ORIGIN), ("backoff", "http-429"))
        self.clock.advance(minutes=2)
        self.assertIsNone(self.store().lookup(ORIGIN))

    def test_a_wait_longer_than_the_longest_that_is_recorded_is_held_for_good_never_cut(self):
        # 01 r3 F3: 34560000 s is 400 days; it used to become 366 and the host was asked 34 days early
        for seconds in (34_560_000, 10 * 365 * 86400, 999_999_999_999):
            with self.subTest(seconds):
                self.setUp()
                self.clock.now = T0
                self.store().record_backoff(ORIGIN, "http-429", seconds)
                self.assertEqual(json.loads(self.store().path.read_text())["origins"][ORIGIN]["until"], robots_store.INDEFINITE)
                for delta in (timedelta(days=1), timedelta(days=366), timedelta(days=367), timedelta(days=400, hours=1),
                              timedelta(days=5000)):
                    self.clock.now = T0 + delta
                    self.assertEqual(self.store().lookup(ORIGIN), ("backoff", "http-429"), delta)

    def test_a_wait_of_exactly_the_longest_recorded_span_ends_and_one_second_more_is_held_for_good(self):
        self.store().record_backoff(ORIGIN, "http-429", int(robots_store.MAX_BACKOFF.total_seconds()))
        self.clock.now = T0 + robots_store.MAX_BACKOFF - timedelta(seconds=1)
        self.assertEqual(self.store().lookup(ORIGIN), ("backoff", "http-429"))
        self.clock.now = T0 + robots_store.MAX_BACKOFF
        self.assertIsNone(self.store().lookup(ORIGIN))
        self.clock.now = T0
        self.store().record_backoff(ORIGIN, "http-429", int(robots_store.MAX_BACKOFF.total_seconds()) + 1)
        self.clock.now = T0 + robots_store.MAX_BACKOFF + timedelta(days=1)
        self.assertEqual(self.store().lookup(ORIGIN), ("backoff", "http-429"))

    def test_an_indefinite_wait_that_claims_to_start_in_the_future_is_not_used_and_the_marker_is_exact(self):
        self.clock.now = T0 + timedelta(hours=1)
        self.store().record_backoff(ORIGIN, "http-429", 10 ** 10)
        self.clock.now = T0
        self.assertIsNone(self.store().lookup(ORIGIN))
        record = json.loads(self.store().path.read_text())
        for until in ("forever", "Indefinite", "", None, [], 5, True):
            with self.subTest(until):
                record["origins"][ORIGIN]["until"] = until
                self.store().path.write_text(json.dumps(record))
                self.clock.now = T0 + timedelta(hours=2)
                self.assertIsNone(robots_store.RobotsStore(self.directory, writable=False, clock=self.clock).lookup(ORIGIN))

    def test_only_a_429_is_ever_remembered_as_a_wait(self):
        for reason in ("http-403", "http-5xx", "timeout", "", "http-429 ", None):
            self.store().record_backoff(ORIGIN, reason, 600)
        self.assertFalse(self.store().path.exists())

    def test_an_entry_from_the_future_is_never_used(self):
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        self.clock.now = T0 - timedelta(hours=1)
        self.assertIsNone(self.store().lookup(ORIGIN))
        self.clock.now = T0 - timedelta(minutes=4)                                     # within the clock skew allowed
        self.assertEqual(self.store().lookup(ORIGIN), ("rules", ROBOTS_TEXT))

    def test_a_wait_recorded_in_the_future_is_never_used(self):
        # 04 (M10): the same rule as for a copy of robots.txt; a wait that claims to start later than the clock allows is
        # not believed, so the host is asked, which is what happens without this module.
        self.clock.now = T0 + timedelta(hours=1)
        self.store().record_backoff(ORIGIN, "http-429", 7200)
        self.clock.now = T0
        self.assertIsNone(self.store().lookup(ORIGIN))
        self.clock.now = T0 + timedelta(hours=1) - timedelta(minutes=4)                 # within the clock skew allowed
        self.assertEqual(self.store().lookup(ORIGIN), ("backoff", "http-429"))

    def test_a_read_only_store_remembers_nothing_and_writes_nothing(self):
        store = self.store(writable=False)
        store.record_rules(ORIGIN, ROBOTS_TEXT)
        store.record_backoff(ORIGIN, "http-429", 600)
        self.assertFalse(self.directory.exists())
        self.assertIsNone(store.lookup(ORIGIN))

    def test_a_read_only_store_reads_what_a_production_round_left(self):
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        self.assertEqual(self.store(writable=False).lookup(ORIGIN), ("rules", ROBOTS_TEXT))

    def test_the_file_and_its_directory_are_private_and_hold_no_url_or_page(self):
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store().path.stat().st_mode), 0o600)
        text = self.store().path.read_text()
        self.assertEqual(sorted(json.loads(text)), ["origins", "schema"])
        self.assertNotIn("http://", text)
        self.assertEqual([p.name for p in self.directory.iterdir()], [robots_store.FILE])      # no temporary file left

    def test_an_unusable_file_is_ignored_whole_so_the_host_is_asked(self):
        good = {"kind": "rules", "fetched_at": T0.isoformat(), "body": ROBOTS_TEXT}
        control = json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: good}})
        cases = {
            "not json": "{broken",
            "an empty file": "",
            # each case below holds a good entry for ORIGIN, so only the named defect can make the file unusable
            "repeated key": '{"schema": "%s", "schema": "%s", "origins": %s}' % (
                robots_store.SCHEMA, robots_store.SCHEMA, json.dumps({ORIGIN: good})),
            "wrong schema": json.dumps({"schema": "other", "origins": {ORIGIN: good}}),
            "extra top-level key": json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: good}, "x": 1}),
            "origins is a list": json.dumps({"schema": robots_store.SCHEMA, "origins": []}),
            "an origin that is not a host": json.dumps({"schema": robots_store.SCHEMA,
                                                        "origins": {ORIGIN: good, "http://a.test": good}}),
            "more origins than the file may hold": json.dumps({"schema": robots_store.SCHEMA, "origins": {
                ORIGIN: good, **{f"https://h{index}.test": good for index in range(robots_store.MAX_ORIGINS)}}}),
            "an unknown kind": json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: {**good, "kind": "x"}}}),
            "an extra field in an entry": json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: {**good, "x": 1}}}),
            "a time without a zone": json.dumps({"schema": robots_store.SCHEMA,
                                                 "origins": {ORIGIN: {**good, "fetched_at": "2026-11-14T00:00:00"}}}),
            "a time that is not a time": json.dumps({"schema": robots_store.SCHEMA,
                                                     "origins": {ORIGIN: {**good, "fetched_at": "soon"}}}),
            "a body that is not text": json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: {**good, "body": 5}}}),
            "a backoff for a reason that is not a 429": json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: {
                "kind": "backoff", "reason": "http-403", "recorded_at": T0.isoformat(),
                "until": (T0 + timedelta(hours=1)).isoformat()}}}),
            "a backoff longer than the largest wait": json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: {
                "kind": "backoff", "reason": "http-429", "recorded_at": T0.isoformat(),
                "until": (T0 + robots_store.MAX_BACKOFF + timedelta(seconds=1)).isoformat()}}}),
            "a backoff that ends before it starts": json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: {
                "kind": "backoff", "reason": "http-429", "recorded_at": T0.isoformat(),
                "until": (T0 - timedelta(hours=1)).isoformat()}}}),
            "a non-finite number": '{"schema": "%s", "origins": {%s: %s}, "n": NaN}' % (
                robots_store.SCHEMA, json.dumps(ORIGIN), json.dumps(good)),
        }
        private_dir(self.root)
        self.store().path.write_text(control)
        self.store().path.chmod(0o600)
        self.assertEqual(self.store().lookup(ORIGIN), ("rules", ROBOTS_TEXT))               # the control file is accepted
        for name, text in cases.items():
            with self.subTest(name):
                self.store().path.write_text(text)
                self.store().path.chmod(0o600)
                self.assertIsNone(self.store().lookup(ORIGIN))
        # and one good entry next to a bad one spoils the file: nothing in it is trusted
        self.store().path.write_text(json.dumps({"schema": robots_store.SCHEMA, "origins": {
            ORIGIN: good, "https://other.test": {"kind": "x"}}}))
        self.assertIsNone(self.store().lookup(ORIGIN))

    def test_a_file_that_is_not_private_or_not_regular_is_ignored(self):
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        path = self.store().path
        path.chmod(0o644)
        self.assertIsNone(self.store().lookup(ORIGIN))
        path.chmod(0o600)
        self.assertEqual(self.store().lookup(ORIGIN), ("rules", ROBOTS_TEXT))
        data = path.read_bytes()
        path.unlink()
        (self.root / "elsewhere.json").write_bytes(data)
        (self.root / "elsewhere.json").chmod(0o600)
        path.symlink_to(self.root / "elsewhere.json")
        self.assertIsNone(self.store().lookup(ORIGIN))

    def test_a_directory_that_is_not_private_is_not_written(self):
        self.directory.mkdir(mode=0o755)
        self.directory.chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertIn("robots state not written", out.getvalue())

    # -- 01 F1: the directory is checked on reading too, and what is checked is what is used
    def test_a_directory_that_is_a_link_is_neither_read_nor_written(self):
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        real = self.root / "real-state"
        self.directory.rename(real)
        self.directory.symlink_to(real)
        self.assertIsNone(self.store().lookup(ORIGIN))                                   # 01's counter-example returned the rules
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.store().record_backoff(ORIGIN, "http-429", 600)
        self.assertIn("robots state not written", out.getvalue())
        self.assertEqual(json.loads((real / robots_store.FILE).read_text())["origins"][ORIGIN]["kind"], "rules")   # untouched

    def test_a_directory_that_is_not_private_is_not_read_even_when_its_file_is(self):
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        self.assertEqual(self.store().lookup(ORIGIN), ("rules", ROBOTS_TEXT))
        for mode in (0o755, 0o750, 0o701):
            with self.subTest(oct(mode)):
                self.directory.chmod(mode)
                self.assertIsNone(self.store().lookup(ORIGIN))
        self.directory.chmod(0o700)
        self.assertEqual(self.store().lookup(ORIGIN), ("rules", ROBOTS_TEXT))

    def test_a_directory_that_is_replaced_while_it_is_read_does_not_redirect_the_read(self):
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        attacker = self.root / "attacker"
        attacker.mkdir(mode=0o700)
        (attacker / robots_store.FILE).write_text(json.dumps({"schema": robots_store.SCHEMA, "origins": {ORIGIN: {
            "kind": "rules", "fetched_at": T0.isoformat(), "body": "User-agent: *\nDisallow: /\n"}}}))
        (attacker / robots_store.FILE).chmod(0o600)
        store, original = self.store(), robots_store.RobotsStore._open_directory

        def check_then_swap(instance, **kwargs):
            descriptor = original(instance, **kwargs)                                    # the directory was just checked
            os.rename(self.directory, self.root / "moved")
            os.symlink(attacker, self.directory)
            return descriptor
        with patch.object(robots_store.RobotsStore, "_open_directory", check_then_swap):
            self.assertEqual(store.lookup(ORIGIN), ("rules", ROBOTS_TEXT))               # what the checked directory held

    def test_a_directory_that_is_replaced_during_a_write_does_not_redirect_the_write(self):
        attacker = self.root / "attacker"
        attacker.mkdir(mode=0o700)
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        moved = self.root / "moved"
        real_replace = os.replace

        def swap_then_replace(*args, **kwargs):
            os.rename(self.directory, moved)                                              # after the directory was checked
            os.symlink(attacker, self.directory)
            return real_replace(*args, **kwargs)
        with patch.object(robots_store.os, "replace", swap_then_replace):
            self.store().record_backoff(ORIGIN, "http-429", 600)
        self.assertEqual(list(attacker.iterdir()), [])                                    # nothing went through the new path
        self.assertEqual(json.loads((moved / robots_store.FILE).read_text())["origins"][ORIGIN]["kind"], "backoff")

    # -- 01 F4: file, directory entry and the chain above it are synced, on every write
    def spy_syncs(self):
        real_fsync, real_replace, log = os.fsync, os.replace, []

        def fsync(descriptor):
            log.append(os.readlink(f"/proc/self/fd/{descriptor}"))
            return real_fsync(descriptor)

        def replace(*args, **kwargs):
            log.append("REPLACE")
            return real_replace(*args, **kwargs)
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(robots_store.os, "fsync", fsync))
        stack.enter_context(patch.object(robots_store.os, "replace", replace))
        self.addCleanup(stack.close)
        return log

    def test_the_file_the_directory_entry_and_every_new_directory_above_are_synced_in_that_order(self):
        self.directory = self.root / "a" / "b" / "state"
        log = self.spy_syncs()
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        file_sync = next(i for i, entry in enumerate(log) if entry.startswith(str(self.directory / ".robots-")))
        replaced = log.index("REPLACE")
        directory_sync = next(i for i, entry in enumerate(log) if entry == str(self.directory) and i > replaced)
        chain = [next(i for i, entry in enumerate(log) if entry == str(path) and i > directory_sync)
                 for path in (self.root / "a" / "b", self.root / "a", self.root)]
        self.assertLess(file_sync, replaced)
        self.assertLess(replaced, directory_sync)
        self.assertEqual(chain, sorted(chain))

    def test_a_chain_that_could_not_be_synced_is_synced_again_by_the_next_write(self):
        self.directory = self.root / "a" / "b" / "state"
        real_fsync = os.fsync
        broken = {"on": True}

        def fsync(descriptor):
            if broken["on"] and os.readlink(f"/proc/self/fd/{descriptor}") == str(self.root / "a"):
                raise OSError(5, "I/O error")
            return real_fsync(descriptor)
        with patch.object(robots_store.os, "fsync", fsync), contextlib.redirect_stdout(io.StringIO()) as out:
            store = self.store()
            store.record_rules(ORIGIN, ROBOTS_TEXT)
        self.assertIn("robots state not written", out.getvalue())                         # it does not claim the wait was saved
        self.assertIsNone(store.lookup(ORIGIN))                                            # and it does not use what it did not save
        broken["on"] = False
        log = self.spy_syncs()
        self.store().record_rules(ORIGIN, ROBOTS_TEXT)                                     # the directories now exist
        for path in (self.directory, self.root / "a" / "b", self.root / "a", self.root):
            self.assertIn(str(path), log, path)

    def test_an_ancestor_that_cannot_be_synced_means_not_saved_whoever_owns_it(self):
        # 01 r3 F4: a PermissionError on a directory that is not ours was taken for "nothing of ours in it"; a shared directory
        # (the temporary one) can hold the entry of a directory made here, so nothing about ownership may excuse it
        for name in ("a directory of ours", "the shared directory above it"):
            with self.subTest(name):
                self.setUp()
                target = self.root if name == "a directory of ours" else self.root.parent
                real_fsync = os.fsync

                def fsync(descriptor, target=target, real=real_fsync):
                    if os.readlink(f"/proc/self/fd/{descriptor}") == str(target):
                        raise PermissionError(13, "denied")
                    return real(descriptor)
                with patch.object(robots_store.os, "fsync", fsync), contextlib.redirect_stdout(io.StringIO()) as out:
                    store = self.store()
                    store.record_backoff(ORIGIN, "http-429", 600)
                self.assertIn("robots state not written", out.getvalue())
                self.assertIsNone(store.lookup(ORIGIN))                                      # and nothing is claimed in memory

    def test_the_chain_is_synced_up_to_the_filesystem_root_whoever_owns_what(self):
        log = self.spy_syncs()
        with patch.object(robots_store.os, "geteuid", return_value=424242):                 # no directory is "ours"
            self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        for path in (self.directory, self.root, self.root.parent, Path("/")):
            self.assertIn(str(path), log, path)

    def test_a_write_that_fails_is_reported_and_changes_nothing_else(self):
        with patch.object(robots_store.os, "replace", side_effect=OSError("disk")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.store().record_rules(ORIGIN, ROBOTS_TEXT)
        self.assertIn("robots state not written", out.getvalue())
        self.assertEqual(list(self.directory.glob(".robots-*")), [])
        self.assertIsNone(self.store().lookup(ORIGIN))

    def test_the_file_holds_a_few_origins_and_drops_the_oldest(self):
        for index in range(robots_store.MAX_ORIGINS + 3):
            self.clock.advance(minutes=1)
            self.store().record_rules(f"https://h{index}.test", ROBOTS_TEXT)
        origins = json.loads(self.store().path.read_text())["origins"]
        self.assertEqual(len(origins), robots_store.MAX_ORIGINS)
        self.assertNotIn("https://h0.test", origins)
        self.assertIn(f"https://h{robots_store.MAX_ORIGINS + 2}.test", origins)


class RunTests(TlsTestCase):
    """fetch.fetch_page_watch with the store, as the radar runs it."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.directory = Path(os.path.realpath(tmp.name)) / "state"
        self.clock = Clock()
        self.server.serve("/robots.txt", ROBOTS_OK)
        self.server.serve("/news", Reply(200, V1))

    def store(self, writable=True):
        return robots_store.RobotsStore(self.directory, writable=writable, clock=self.clock)

    def run_once(self, store="default", entries=(LISTING,)):
        store = self.store() if store == "default" else store
        self.server.accepted = 0
        before = len(self.server.paths())
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {fetch.FIRECRAWL_ENV: "http://127.0.0.1:9", "GITHUB_ACTIONS": ""}))
            stack.enter_context(patch.object(fetch, "STATIC_FETCHER_FACTORY", self.fetcher))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result = fetch.fetch_page_watch(Config(sources=sources(*entries), taxonomy={}, glossary={}, baseline={}), DAY, None,
                                            store)
        self.out = out.getvalue()
        return result, self.server.paths()[before:]

    def robots_requests(self, paths):
        return [path for path in paths if path == "/robots.txt"]

    def test_without_a_store_every_run_asks_for_robots_as_before(self):
        for _ in range(3):
            _, paths = self.run_once(store=None)
            self.assertEqual(paths, ["/robots.txt", "/news"])

    def test_a_robots_file_read_in_one_run_is_not_asked_for_again_for_a_day(self):
        first, paths = self.run_once()
        self.assertEqual((paths, first.checked), (["/robots.txt", "/news"], ["figure"]))
        for hours in (14, 19):                                                           # the two night rounds
            self.clock.now = T0 + timedelta(hours=hours)
            again, paths = self.run_once(store=self.store(writable=False))
            self.assertEqual((paths, again.checked), (["/news"], ["figure"]))           # the page is read; robots is not asked
        self.clock.now = T0 + timedelta(hours=24, minutes=1)
        _, paths = self.run_once()
        self.assertEqual(paths, ["/robots.txt", "/news"])                                # a day later it is asked again

    def test_what_the_cached_robots_file_says_still_decides(self):
        self.server.serve("/robots.txt", Reply(200, "User-agent: *\nDisallow: /news\n", ctype="text/plain"))
        first, paths = self.run_once()
        self.assertEqual((paths, first.robots_skipped), (["/robots.txt"], ["figure"]))
        self.server.serve("/robots.txt", ROBOTS_OK)                                      # the site changes its mind within the day
        again, paths = self.run_once()
        self.assertEqual((paths, again.robots_skipped), ([], ["figure"]))                # the cached rule is what applies (RFC 9309 allows it for 24 h)
        self.clock.advance(hours=25)
        later, paths = self.run_once()
        self.assertEqual((paths, later.checked), (["/robots.txt", "/news"], ["figure"]))

    def test_an_absent_robots_file_is_not_asked_for_again_either(self):
        self.server.serve("/robots.txt", Reply(404, "no", ctype="text/plain"))
        _, paths = self.run_once()
        self.assertEqual(paths, ["/robots.txt", "/news"])
        _, paths = self.run_once()
        self.assertEqual(paths, ["/news"])

    def test_a_429_is_remembered_and_the_host_is_not_asked_again_until_it_is_over(self):
        self.server.serve("/robots.txt", Reply(429, "slow down", ctype="text/plain", headers={"Retry-After": "7200"}))
        first, paths = self.run_once()
        self.assertEqual(paths, ["/robots.txt"])                                         # the page is not requested
        self.assertEqual(first.sources["figure"], {"fetcher": "static", "outcome": "unavailable",
                                                   "reason": "robots-unreadable", "detail": "http-429"})
        self.clock.advance(hours=1)
        second, paths = self.run_once()
        self.assertEqual(paths, [])                                                      # nothing is asked while it is told to wait
        self.assertEqual(second.sources["figure"], {"fetcher": "static", "outcome": "unavailable",
                                                    "reason": "robots-unreadable", "detail": "http-429:backoff"})
        self.assertEqual(second.failed, ["figure"])
        self.clock.advance(hours=1, minutes=1)
        self.server.serve("/robots.txt", ROBOTS_OK)
        third, paths = self.run_once()
        self.assertEqual((paths, third.checked), (["/robots.txt", "/news"], ["figure"]))

    def test_a_429_without_a_retry_after_waits_six_hours(self):
        self.server.serve("/robots.txt", Reply(429, "slow down", ctype="text/plain"))
        self.run_once()
        self.clock.advance(hours=5, minutes=59)
        self.assertEqual(self.run_once()[1], [])
        self.clock.advance(minutes=2)
        self.assertEqual(self.run_once()[1], ["/robots.txt"])

    def test_a_round_that_may_not_write_still_respects_a_wait_and_cannot_start_one(self):
        self.server.serve("/robots.txt", Reply(429, "slow down", ctype="text/plain"))
        _, paths = self.run_once(store=self.store(writable=False))
        self.assertEqual(paths, ["/robots.txt"])
        self.assertFalse(self.directory.exists())                                        # an --out round writes nothing
        _, paths = self.run_once(store=self.store(writable=False))
        self.assertEqual(paths, ["/robots.txt"])                                         # so it asks again, as before
        self.run_once()                                                                  # a production round records the wait
        _, paths = self.run_once(store=self.store(writable=False))
        self.assertEqual(paths, [])                                                      # and a read-only round honours it

    def test_an_answer_that_cannot_be_read_is_never_replaced_by_an_older_one(self):
        self.run_once()                                                                  # a good robots file is on record
        self.clock.advance(hours=25)                                                     # ... and has expired
        self.server.serve("/robots.txt", Reply(429, "slow down", ctype="text/plain"))
        result, paths = self.run_once()
        self.assertEqual(paths, ["/robots.txt"])
        self.assertEqual((result.checked, result.failed), ([], ["figure"]))              # the expired copy is not a fallback
        self.assertEqual(result.sources["figure"]["detail"], "http-429")
        for status in (403, 500):
            self.setUp()
            self.run_once()
            self.clock.advance(hours=25)
            self.server.serve("/robots.txt", Reply(status, "no", ctype="text/plain"))
            result, paths = self.run_once()
            self.assertEqual((paths, result.checked), (["/robots.txt"], []), status)

    def test_a_failure_that_is_not_a_429_is_not_remembered(self):
        for status in (403, 500, 503):
            with self.subTest(status):
                self.setUp()
                self.server.serve("/robots.txt", Reply(status, "no", ctype="text/plain"))
                self.run_once()
                self.assertFalse(self.store().path.exists())
                self.assertEqual(self.run_once()[1], ["/robots.txt"])

    def test_a_robots_file_that_cannot_be_judged_is_not_remembered(self):
        self.server.serve("/robots.txt", Reply(200, b"\xff\xfe not utf-8", ctype="text/plain"))
        result, paths = self.run_once()
        self.assertEqual((paths, result.sources["figure"]["detail"]), (["/robots.txt"], "robots-unsupported"))
        self.assertFalse(self.store().path.exists())

    def test_a_damaged_file_means_the_host_is_asked_as_before(self):
        self.run_once()
        self.store().path.write_text("{broken")
        _, paths = self.run_once()
        self.assertEqual(paths, ["/robots.txt", "/news"])

    def test_a_cached_robots_file_that_cannot_be_parsed_again_is_not_used(self):
        self.run_once()
        record = json.loads(self.store().path.read_text())
        record["origins"][ORIGIN]["body"] = "User-agent: *\n" + "Disallow: /" + "a" * 600_000
        self.store().path.write_text(json.dumps(record))
        self.assertIsNone(self.store().lookup(ORIGIN))                                  # larger than any robots.txt we accept
        _, paths = self.run_once()
        self.assertEqual(paths, ["/robots.txt", "/news"])


    def test_a_cached_robots_file_that_does_not_parse_is_not_used_and_never_allows_everything(self):
        # 04 T2: the earlier test used a body over the size limit, which the store itself refuses; these two are accepted
        # by the store and refused by the parser, which is the branch that must ask the host and never allow everything.
        for body in ("User-agent: *\nDisallow: /%zz\n", "User-agent: *\nDisallow: /" + "a" * 2500 + "\n"):
            with self.subTest(body[:28]):
                with self.assertRaises(robots_rules.RobotsUnsupported):
                    robots_rules.parse(body, fetch.ROBOTS_TOKEN)
                self.setUp()
                self.run_once()
                record = json.loads(self.store().path.read_text())
                record["origins"][ORIGIN]["body"] = body
                self.store().path.write_text(json.dumps(record))
                self.store().path.chmod(0o600)
                self.assertEqual(self.store().lookup(ORIGIN), ("rules", body))               # the store accepts it
                _, paths = self.run_once()
                self.assertEqual(paths, ["/robots.txt", "/news"])                            # the host is asked

    # -- 01 F2: a page that answers 429 is a host that asked to be left alone
    def second_source(self):
        return {**LISTING, "id": "figure-two", "url": f"https://{HOST}/news-two"}

    def page_429(self, wait="3600"):
        self.server.serve("/news", Reply(429, "slow down", ctype="text/html", headers={"Retry-After": wait}))
        self.server.serve("/news-two", Reply(200, V1))

    def test_a_page_that_answers_429_stops_the_next_source_of_that_host_and_the_wait_is_kept(self):
        self.page_429()
        first, paths = self.run_once(entries=(LISTING, self.second_source()))
        self.assertEqual(paths, ["/robots.txt", "/news"])                                    # 01's counter-example asked /news-two too
        self.assertEqual(first.sources["figure"]["reason"], "http-429")
        self.assertEqual(first.sources["figure-two"], {"fetcher": "static", "outcome": "unavailable",
                                                       "reason": "robots-unreadable", "detail": "http-429:backoff"})
        self.clock.advance(minutes=30)
        second, paths = self.run_once(entries=(LISTING, self.second_source()))
        self.assertEqual(paths, [])                                                           # the next round respects it
        self.assertEqual(second.sources["figure"]["detail"], "http-429:backoff")
        self.clock.advance(minutes=31)
        self.server.serve("/news", Reply(200, V1))
        third, paths = self.run_once()
        self.assertEqual((paths, third.checked), (["/robots.txt", "/news"], ["figure"]))     # after the wait it asks again

    def test_a_round_that_may_not_write_stops_asking_in_that_round_and_keeps_nothing(self):
        self.page_429()
        _, paths = self.run_once(store=self.store(writable=False), entries=(LISTING, self.second_source()))
        self.assertEqual(paths, ["/robots.txt", "/news"])                                    # the second source is not asked
        self.assertFalse(self.directory.exists())
        _, paths = self.run_once(store=self.store(writable=False), entries=(LISTING, self.second_source()))
        self.assertEqual(paths, ["/robots.txt", "/news"])                                    # nothing was kept: asked again
        self.run_once(entries=(LISTING, self.second_source()))                              # a production round keeps the wait
        _, paths = self.run_once(store=self.store(writable=False), entries=(LISTING, self.second_source()))
        self.assertEqual(paths, [])

    def test_without_a_store_a_page_429_still_stops_the_rest_of_the_round_for_that_host(self):
        self.page_429()
        _, paths = self.run_once(store=None, entries=(LISTING, self.second_source()))
        self.assertEqual(paths, ["/robots.txt", "/news"])

    # -- 01 F3: the wait that was asked for is the wait that is kept
    def test_a_wait_longer_than_a_year_is_never_ended_by_the_clock(self):
        # 01 r3 F3: Retry-After 34560000 (400 days) used to be recorded as 366 days, so the host was asked 34 days early
        self.server.serve("/robots.txt", Reply(429, "slow down", ctype="text/plain", headers={"Retry-After": "34560000"}))
        self.run_once()
        for delta in (timedelta(days=366, hours=1), timedelta(days=399), timedelta(days=401), timedelta(days=4000)):
            self.clock.now = T0 + delta
            self.server.serve("/robots.txt", ROBOTS_OK)                                      # even a host that has recovered
            _, paths = self.run_once(store=self.store(writable=False))
            self.assertEqual(paths, [], delta)                                               # is not asked: held until a person decides

    def test_a_page_that_asks_for_more_than_a_year_holds_the_host_too(self):
        self.page_429(wait="34560000")
        self.run_once(entries=(LISTING, self.second_source()))
        self.clock.advance(days=401)
        self.server.serve("/news", Reply(200, V1))
        _, paths = self.run_once(store=self.store(writable=False), entries=(LISTING, self.second_source()))
        self.assertEqual(paths, [])

    def test_a_wait_of_two_days_is_two_days(self):
        self.server.serve("/robots.txt", Reply(429, "slow down", ctype="text/plain", headers={"Retry-After": "172800"}))
        self.run_once()
        self.clock.advance(hours=47, minutes=59)
        self.assertEqual(self.run_once()[1], [])
        self.clock.advance(minutes=2)
        self.server.serve("/robots.txt", ROBOTS_OK)
        self.assertEqual(self.run_once()[1], ["/robots.txt", "/news"])


class WiringTests(unittest.TestCase):
    """04 T1: the command line decides who may write, and the store reaches the fetcher."""

    def call(self, argv):
        seen = {}

        def fake_run(**kwargs):
            seen.update(kwargs)
            return {"fetch": None, "date": "2026-11-14"}
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            with patch.dict(os.environ, {"PAIRADAR_STATE_DIR": tmp}), patch.object(cli, "run", fake_run), \
                    patch.object(cli, "write_page_watch_evidence"), patch.object(cli, "write_page_watch_observation"), \
                    patch.object(cli, "degraded", return_value=False), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(argv)
            created = (Path(tmp) / "robots-state").exists()
            expected = Path(os.path.realpath(tmp)) / "robots-state"
        return seen, code, created, expected

    def test_an_offline_run_has_no_store(self):
        seen, code, created, _ = self.call(["--offline"])
        self.assertEqual(code, 0)
        self.assertIsNone(seen["robots_store"])
        self.assertFalse(created)

    def test_an_out_run_reads_and_never_writes(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as out:
            seen, code, created, expected = self.call(["--out", out])
        store = seen["robots_store"]
        self.assertEqual((code, type(store), store.writable), (0, robots_store.RobotsStore, False))
        self.assertEqual(Path(os.path.realpath(store.directory)), expected)
        self.assertFalse(created)                                                              # asking for the store creates nothing

    def test_a_production_run_may_write_the_store_in_the_state_directory(self):
        seen, code, created, expected = self.call([])
        store = seen["robots_store"]
        self.assertEqual((code, type(store), store.writable), (0, robots_store.RobotsStore, True))
        self.assertEqual(Path(os.path.realpath(store.directory)), expected)
        self.assertFalse(created)

    def test_the_store_given_to_run_reaches_the_fetcher(self):
        marker = object()
        seen = {}

        def fake_fetch_all(*args, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp, \
                patch.dict(os.environ, {"PAIRADAR_STATE_DIR": tmp}), patch.object(cli, "fetch_all", fake_fetch_all):
            with self.assertRaises(RuntimeError):
                cli.build_context(cli.load_config(), offline=False, limit=8, day="2026-11-14", robots_store=marker)
        self.assertIs(seen["robots_store"], marker)

    def test_run_gives_the_store_to_the_context(self):
        marker = object()
        seen = {}

        def fake_build_context(*args, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")
        with patch.object(cli, "build_context", fake_build_context), self.assertRaises(RuntimeError):
            cli.run(offline=False, limit=8, day="2026-11-14", write_readme=False, robots_store=marker)
        self.assertIs(seen["robots_store"], marker)


class AddressCountTests(TlsTestCase):
    def test_a_name_that_resolves_to_too_many_addresses_says_how_many(self):
        for count in (9, 12, 400):
            with self.subTest(count):
                fetcher = self.fetcher(resolver=lambda host, count=count: [f"127.0.0.{i + 1}" for i in range(count)],
                                       policy=lambda address: True)
                with self.assertRaises(static_fetch.StaticFetchError) as caught:
                    fetcher.get(f"https://{HOST}/news", fetcher.budget(5))
                self.assertEqual((caught.exception.reason, caught.exception.detail),
                                 ("address-blocked", f"too-many-{min(count, 99)}"))

    def test_eight_addresses_are_still_accepted_and_a_non_public_one_is_still_refused(self):
        fetcher = self.fetcher(resolver=lambda host: [f"127.0.0.{i + 1}" for i in range(8)], policy=lambda address: True)
        self.server.serve("/news", Reply(200, V1))
        self.assertEqual(len(fetcher._resolve(HOST, fetcher.budget(5))), 8)                  # the limit is not widened
        fetcher = self.fetcher(resolver=lambda host: ["127.0.0.1", "10.0.0.1"], policy=lambda address: address == "127.0.0.1")
        with self.assertRaises(static_fetch.StaticFetchError) as caught:
            fetcher.get(f"https://{HOST}/news", fetcher.budget(5))
        self.assertEqual((caught.exception.reason, caught.exception.detail), ("address-blocked", "not-public"))


if __name__ == "__main__":
    unittest.main()
