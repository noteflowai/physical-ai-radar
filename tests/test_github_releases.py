"""GitHub releases read from the API (feed `kind: github-releases`), not from the atom feeds github.com's robots.txt disallows.

The parser and the fetch_feeds tests use fixed replies; the reader tests use a local HTTP server to show what the real opener
does with a 301, a 429 and an oversized reply. What this does not show: the real api.github.com (its fields, its limits, a
real redirect), or how many requests a day the radar may make before the unauthenticated limit is reached.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pairadar import fetch, health  # noqa: E402
from pairadar.config import Item, load_config  # noqa: E402
from pairadar.distill import enrich, prefilter  # noqa: E402
from test_sources_mainstream import config_with, rss  # noqa: E402

NOTES = "private-capture-marker release notes that must never reach an item"
REPO = "huggingface/lerobot"
URL = fetch.GITHUB_API.format(repo=REPO)


def release(tag="v0.4.0", name="Version 0.4.0", repo=REPO, published="2026-11-10T09:30:00Z", **extra):
    return {"tag_name": tag, "name": name, "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
            "published_at": published, "created_at": published, "draft": False, "prerelease": False, "body": NOTES, **extra}


def payload(*releases) -> bytes:
    return json.dumps(list(releases)).encode()


def feed(**extra):
    return {"id": "github-lerobot", "url": URL, "kind": "github-releases", "repo": REPO, "evidence": "O",
            "topical": True, "max_entries": 1, "title_prefix": "LeRobot", "publisher": "LeRobot releases (GitHub)", **extra}


def silent():
    return contextlib.redirect_stdout(io.StringIO())


class ParserTests(unittest.TestCase):
    def test_a_release_gives_its_title_link_and_day_and_never_its_notes(self):
        out = fetch.parse_github_releases(payload(release()), REPO)
        self.assertEqual(out, [("Version 0.4.0", f"https://github.com/{REPO}/releases/tag/v0.4.0", "", "2026-11-10")])
        self.assertNotIn(NOTES, repr(out))

    def test_a_release_with_no_name_is_titled_by_its_tag(self):
        for name in (None, "", "   ", 5):
            with self.subTest(name):
                self.assertEqual(fetch.parse_github_releases(payload(release(name=name)), REPO)[0][0], "v0.4.0")

    def test_a_time_with_an_offset_is_folded_to_the_utc_day(self):
        out = fetch.parse_github_releases(payload(release(published="2026-11-10T23:30:00-05:00")), REPO)
        self.assertEqual(out[0][3], "2026-11-11")

    def test_newest_first(self):
        out = fetch.parse_github_releases(payload(release("v1", published="2026-10-01T00:00:00Z"),
                                                  release("v3", published="2026-11-03T00:00:00Z"),
                                                  release("v2", published="2026-10-15T00:00:00Z")), REPO)
        self.assertEqual([entry[0] for entry in out], ["Version 0.4.0"] * 3)
        self.assertEqual([entry[3] for entry in out], ["2026-11-03", "2026-10-15", "2026-10-01"])

    def test_what_is_not_this_repositorys_published_release_is_skipped(self):
        bad = {"a draft": release(draft=True), "no tag": release(tag=""), "tag not text": release(tag=7),
               "no time": release(published=None), "unreadable time": release(published="soon"),
               "another repository": release(repo="someone/else"),
               "a prefix of the repository's name": release(repo="huggingface/lerobot-evil"),
               "another host": {**release(), "html_url": f"https://evil.example/{REPO}/releases/tag/v1"},
               "plain http": {**release(), "html_url": f"http://github.com/{REPO}/releases/tag/v1"},
               "a query": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v1?x=1"},
               "a fragment": {**release(), "html_url": f"https://github.com/{REPO}/releases/tag/v1#x"},
               "not the releases pages": {**release(), "html_url": f"https://github.com/{REPO}/issues/1"},
               "no link": {**release(), "html_url": None}, "link not text": {**release(), "html_url": 5}}
        for name, one in bad.items():
            with self.subTest(name):
                self.assertEqual(fetch.parse_github_releases(payload(one), REPO), [])

    def test_the_repository_name_is_compared_without_case(self):
        out = fetch.parse_github_releases(payload(release(repo="isaac-sim/IsaacLab")), "isaac-sim/isaaclab")
        self.assertEqual(len(out), 1)

    def test_an_empty_list_is_an_answer_and_anything_else_is_not(self):
        self.assertEqual(fetch.parse_github_releases(b"[]", REPO), [])
        for name, body in {"an object": b'{"message": "Not Found"}', "text": b"not json", "an html page": b"<html></html>",
                           "a list of strings": b'["a"]', "empty": b"", "not utf-8": b"[\xff]", "null": b"null"}.items():
            with self.subTest(name):
                self.assertIsNone(fetch.parse_github_releases(body, REPO))

    def test_a_long_name_is_cut(self):
        out = fetch.parse_github_releases(payload(release(name="x" * 5000)), REPO)
        self.assertEqual(len(out[0][0]), fetch.MAX_TITLE_CHARS)


class Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str]] = []
    reply: tuple[int, dict[str, str], bytes] = (200, {}, b"[]")

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("User-Agent", "")))
        status, headers, body = type(self).reply
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class ReaderTests(unittest.TestCase):
    """`github_get` with the real opener against a local server."""

    def setUp(self):
        Handler.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def get(self, status, headers=None, body=b"[]"):
        Handler.reply = (status, headers or {}, body)
        report: dict = {}
        with silent():
            return fetch.github_get(self.base + "/repos/x/y/releases?per_page=1", report), report

    def test_a_200_is_read_with_the_radars_user_agent_and_one_request(self):
        body, report = self.get(200, body=b'[{"a": 1}]')
        self.assertEqual((body, report["status"]), (b'[{"a": 1}]', 200))
        self.assertEqual(Handler.seen, [("/repos/x/y/releases?per_page=1", fetch.USER_AGENT)])

    def test_a_redirect_is_not_followed_and_is_reported_as_it_was_answered(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status):
                Handler.seen = []
                body, report = self.get(status, {"Location": self.base + "/repositories/1/releases?per_page=1"})
                self.assertEqual((body, report["status"]), (None, status))
                self.assertEqual(len(Handler.seen), 1)                 # the Location was never asked for
                self.assertEqual(fetch._github_failure(report["status"], report), "moved")

    def test_a_refusal_an_error_and_no_answer_are_one_attempt_each(self):
        for status, expected in ((403, "rate-limited"), (429, "rate-limited"), (404, "not-found"), (500, "unavailable"),
                                 (503, "unavailable"), (410, "unavailable")):
            with self.subTest(status):
                Handler.seen = []
                body, report = self.get(status)
                self.assertEqual((body, report["status"], len(Handler.seen)), (None, status, 1))
                self.assertEqual(fetch._github_failure(status, report), expected)
        self.assertEqual(fetch._github_failure(None, {}), "unavailable")

    def test_a_reply_over_the_limit_is_not_read_as_an_answer(self):
        body, report = self.get(200, body=b"[" + b" " * fetch.MAX_API_BYTES + b"]")
        self.assertIsNone(body)
        self.assertEqual(fetch._github_failure(report["status"], report), "bad-reply")

    def test_nothing_listening_is_no_answer(self):
        self.server.shutdown()
        self.server.server_close()
        report: dict = {}
        with silent():
            self.assertIsNone(fetch.github_get(self.base + "/x", report))
        self.assertIsNone(report["status"])


class FeedTests(unittest.TestCase):
    def read(self, feeds, reply, robots=None, config=None):
        calls = []

        def get(url, report):
            calls.append(url)
            status, body = reply(url) if callable(reply) else reply
            report["status"] = status
            return body
        skipped, reasons, failures = [], {}, {}
        config = config or config_with(feeds=feeds)
        with mock.patch.object(fetch, "github_get", get), mock.patch.object(fetch.time, "sleep"), silent():
            items, failed = fetch.fetch_feeds(config, robots=robots, skipped=skipped, reasons=reasons, failures=failures)
        return items, failed, skipped, failures, calls

    def test_the_newest_release_becomes_one_item_with_no_notes(self):
        items, failed, _, failures, calls = self.read(
            [feed()], (200, payload(release("v0.4.0", "v0.4.0", published="2026-11-10T09:30:00Z"),
                                    release("v0.3.0", "v0.3.0", published="2026-09-01T00:00:00Z"))))
        self.assertEqual((failed, failures, calls), ([], {}, [URL]))
        self.assertEqual(len(items), 1)                                         # max_entries 1
        item = items[0]
        self.assertEqual((item.title, item.url, item.summary, item.published, item.source_id, item.evidence, item.topical),
                         ("LeRobot v0.4.0", f"https://github.com/{REPO}/releases/tag/v0.4.0", "", "2026-11-10",
                          "github-lerobot", "O", True))
        self.assertEqual(item.publisher, "LeRobot releases (GitHub)")
        self.assertNotIn(NOTES, repr(items))

    def test_a_repository_with_no_release_yet_is_answered_and_gives_nothing(self):
        items, failed, _, failures, _ = self.read([feed()], (200, b"[]"))
        self.assertEqual((items, failed, failures), ([], [], {}))

    def test_a_301_is_not_followed_and_is_moved_and_the_other_feeds_are_still_read(self):
        other = {"id": "blog", "url": "https://k.example/feed", "evidence": "M", "topical": True}
        config = config_with(feeds=[feed(), other])
        calls = []

        def moved(url, report):
            calls.append(url)
            report["status"] = 301
            return None
        with mock.patch.object(fetch, "github_get", moved), \
                mock.patch.object(fetch, "http_get", return_value=rss(("A robot", "https://k.example/a", "robot"))), \
                mock.patch.object(fetch.time, "sleep"), silent():
            failures: dict = {}
            items, failed = fetch.fetch_feeds(config, failures=failures)
        self.assertEqual((failed, failures, calls), (["github-lerobot"], {"github-lerobot": "moved"}, [URL]))
        self.assertEqual([item.source_id for item in items], ["blog"])

    def test_every_failure_has_one_fixed_reason(self):
        for status, body, expected in ((403, None, "rate-limited"), (429, None, "rate-limited"), (404, None, "not-found"),
                                       (500, None, "unavailable"), (None, None, "unavailable"),
                                       (200, b'{"message": "x"}', "bad-reply"), (200, b"not json", "bad-reply")):
            with self.subTest(status=status, body=body):
                _, failed, _, failures, _ = self.read([feed()], (status, body))
                self.assertEqual((failed, failures), (["github-lerobot"], {"github-lerobot": expected}))
                self.assertIn(expected, fetch.GITHUB_FAILURES)

    def test_a_feed_whose_url_is_not_the_one_built_from_its_repo_is_bad_config_and_nothing_is_requested(self):
        cases = {"another url": feed(url="https://evil.example/repos/x/releases"),
                 "the atom feed": feed(url=f"https://github.com/{REPO}/releases.atom"),
                 "another repo in the url": feed(url=fetch.GITHUB_API.format(repo="someone/else")),
                 "no repo": {k: v for k, v in feed().items() if k != "repo"},
                 "a repo that is not owner/name": feed(repo="../../etc", url=URL),
                 "a repo with a query": feed(repo="a/b?x=1", url=URL),
                 "more pages": feed(url=URL.replace("per_page=1", "per_page=100"))}
        for name, bad in cases.items():
            with self.subTest(name):
                _, failed, _, failures, calls = self.read([bad], (200, payload(release())))
                self.assertEqual((failed, failures, calls), (["github-lerobot"], {"github-lerobot": "bad-config"}, []))

    def test_an_unknown_kind_is_not_read_as_a_feed(self):
        _, failed, _, failures, calls = self.read([feed(kind="something-else")], (200, payload(release())))
        self.assertEqual((failed, failures, calls), (["github-lerobot"], {"github-lerobot": "bad-config"}, []))

    def test_robots_txt_of_the_api_host_still_decides(self):
        cases = {"a rule for the path": (200, b"User-agent: *\nDisallow: /repos/\n", fetch.DISALLOW),
                 "forbidden": (403, b"no", fetch.FORBIDDEN), "unreachable": (503, b"", fetch.UNREACHABLE)}
        for name, (status, body, verdict) in cases.items():
            with self.subTest(name):
                cache = fetch.RobotsCache(getter=lambda url, s=status, b=body: (s, b))
                _, failed, skipped, failures, calls = self.read([feed()], (200, payload(release())), robots=cache)
                self.assertEqual((failed, skipped, failures, calls), ([], ["github-lerobot"], {}, []))

    def test_no_robots_txt_on_the_api_host_allows_it_as_it_did_on_11_16(self):
        cache = fetch.RobotsCache(getter=lambda url: (404, None))
        items, failed, skipped, _, calls = self.read([feed()], (200, payload(release())), robots=cache)
        self.assertEqual((len(items), failed, skipped, calls), (1, [], [], [URL]))

    def test_a_disabled_feed_is_not_read(self):
        _, failed, _, _, calls = self.read([feed(enabled=False)], (200, payload(release())))
        self.assertEqual((failed, calls), ([], []))

    def test_the_report_carries_the_reasons(self):
        report = fetch.FetchReport(failed_reasons={"b": "moved", "a": "not-found"})
        self.assertEqual(list(report.to_dict()["failed_reasons"].items()), [("a", "not-found"), ("b", "moved")])
        self.assertEqual(fetch.FetchReport().to_dict()["failed_reasons"], {})


class ReleaseFeedHealthTest(unittest.TestCase):
    """A release feed read from the GitHub API cannot be repaired by the repair job: its verifier parses RSS/Atom and accepts a
    change of the feed's `url` alone, while a renamed repository needs `repo` and `url` changed together. So a feed that stays
    dark is reported for a person (with its reason) and never wakes the model every night."""

    TODAY = date(2026, 11, 20)

    def runs(self, failed_by_day: dict[int, list[str]], reasons: dict[str, str] | None = None) -> list[dict]:
        runs = []
        for offset in range(5):
            day = date(2026, 11, 16) + timedelta(days=offset)
            entry = {"date": day.isoformat(), "count": 8, "failed": list(failed_by_day.get(offset, [])), "fallback": []}
            if offset == 4 and reasons:
                entry["failed_reasons"] = dict(reasons)
            runs.append(entry)
        return runs

    def test_a_release_feed_that_stays_dark_is_reported_with_its_reason_and_never_struggling(self):
        runs = self.runs({offset: ["github-isaaclab", "plain-feed"] for offset in range(5)}, {"github-isaaclab": "moved"})
        with mock.patch.object(health, "release_feed_ids", return_value=frozenset({"github-isaaclab"})), \
                mock.patch.object(health, "page_watch_ids", return_value=frozenset()):
            verdict = health.report(runs, self.TODAY)
        self.assertEqual([entry["source"] for entry in verdict["struggling"]], ["plain-feed"])        # an RSS feed still is
        self.assertEqual(verdict["release_feeds_failing"], [{"source": "github-isaaclab", "failed_days": 5, "reason": "moved"}])
        self.assertEqual(verdict["failures"], {"github-isaaclab": 5, "plain-feed": 5})               # still counted as failing

    def test_the_repair_gate_stays_quiet_when_only_a_release_feed_is_dark(self):
        runs = self.runs({offset: ["github-mujoco"] for offset in range(5)}, {"github-mujoco": "rate-limited"})
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(health, "page_watch_ids", return_value=frozenset()), \
                mock.patch.object(health, "release_feed_ids", return_value=frozenset({"github-mujoco"})):
            path = Path(folder) / "history.json"
            path.write_text(json.dumps({"runs": runs}))
            with contextlib.redirect_stdout(io.StringIO()):
                code = health.main(["--history", str(path), "--date", self.TODAY.isoformat(), "--fail-on-struggling"])
        self.assertEqual(code, 0)

    def test_the_same_history_wakes_the_repair_gate_for_an_ordinary_feed(self):
        runs = self.runs({offset: ["plain-feed"] for offset in range(5)})
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(health, "page_watch_ids", return_value=frozenset()), \
                mock.patch.object(health, "release_feed_ids", return_value=frozenset({"github-mujoco"})):
            path = Path(folder) / "history.json"
            path.write_text(json.dumps({"runs": runs}))
            with contextlib.redirect_stdout(io.StringIO()):
                code = health.main(["--history", str(path), "--date", self.TODAY.isoformat(), "--fail-on-struggling"])
        self.assertEqual(code, 1)

    def test_a_release_feed_is_recognised_by_its_kind_in_the_committed_sources(self):
        self.assertEqual(health.release_feed_ids(), {"github-lerobot", "github-isaaclab", "github-mujoco"})

    def test_a_release_feed_that_answered_today_or_failed_too_few_days_is_not_listed(self):
        releases = frozenset({"github-mujoco"})
        short = self.runs({3: ["github-mujoco"], 4: ["github-mujoco"]})                                # two days: under 3
        recovered = self.runs({0: ["github-mujoco"], 1: ["github-mujoco"], 2: ["github-mujoco"]})      # not in the latest run
        for runs in (short, recovered):
            self.assertEqual(health.release_feeds_failing(runs, self.TODAY, releases=releases), [])

    def test_a_run_log_without_reasons_is_listed_with_none(self):
        runs = self.runs({offset: ["github-mujoco"] for offset in range(5)})
        got = health.release_feeds_failing(runs, self.TODAY, releases=frozenset({"github-mujoco"}))
        self.assertEqual(got, [{"source": "github-mujoco", "failed_days": 5, "reason": None}])


class PipelineTests(unittest.TestCase):
    def test_a_release_with_no_excerpt_passes_the_filters_and_is_enriched(self):
        config = load_config()
        reference = date(2026, 11, 16)
        item = Item(id="github-lerobot:abc", title="LeRobot v0.4.0", url=f"https://github.com/{REPO}/releases/tag/v0.4.0",
                    publisher="LeRobot releases (GitHub)", source_id="github-lerobot", evidence="O", published="2026-11-10",
                    summary="", topical=True)
        kept = prefilter([item], config, reference)
        self.assertEqual([entry.id for entry in kept], ["github-lerobot:abc"])
        self.assertEqual(len(enrich(kept, config, reference)), 1)


class CommittedSourcesTests(unittest.TestCase):
    def test_the_three_release_feeds_are_on_and_read_the_api_not_the_atom_feed(self):
        sources = load_config().sources
        feeds = {entry["id"]: entry for entry in fetch.enabled_feeds(sources)}
        for name in ("github-lerobot", "github-isaaclab", "github-mujoco"):
            with self.subTest(name):
                self.assertIn(name, feeds)
                entry = feeds[name]
                self.assertEqual(entry["kind"], fetch.GITHUB_RELEASES)
                self.assertEqual(entry["url"], fetch.GITHUB_API.format(repo=entry["repo"]))
                self.assertNotIn(".atom", entry["url"])
                self.assertEqual(entry["max_entries"], 1)

    def test_no_enabled_feed_has_an_unknown_kind_or_asks_for_an_atom_file_on_github(self):
        for entry in fetch.enabled_feeds(load_config().sources):
            with self.subTest(entry["id"]):
                self.assertIn(entry.get("kind"), (None, fetch.GITHUB_RELEASES))
                self.assertFalse(entry["url"].startswith("https://github.com/") and entry["url"].endswith(".atom"))

    def test_openpi_is_not_a_release_source(self):
        ids = {entry["id"] for entry in load_config().sources["feeds"]}
        self.assertFalse(any("openpi" in name for name in ids))


if __name__ == "__main__":
    unittest.main()
