"""A switched-off page_watch entry's host is not requested, by any reader, including through a redirect (01, PR216 r1 F1).

`fetch.http_get` follows redirects, so switching an entry off in `page_watch` was not enough: a feed, a robots.txt or an
attention API that answered with a redirect to the host would still have sent a request to it. These tests run urllib's own
redirect handling (the real `_GuardedRedirect` inside the real opener) and the real consumers (`fetch_feeds`,
`RobotsCache`, `heat.fetch_signals`); only the HTTPS transport is replaced by an in-memory one that records every request it
is asked to send. What they do not show: any real site, DNS, or what a real server answers.
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from pairadar import fetch, heat, host_policy
from pairadar.config import Config

PI_PAGE = "https://www.pi.website/blog"
RSS = (b'<?xml version="1.0"?><rss><channel><item><title>A humanoid robot learns to walk</title>'
       b"<link>https://a.example/post</link><pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate>"
       b"<description>robot</description></item></channel></rss>")


def reply(status: int = 200, reason: str = "OK", headers: dict[str, str] | None = None, body: bytes = b"") -> bytes:
    head = {"Content-Length": str(len(body)), "Connection": "close", **(headers or {})}
    lines = [f"HTTP/1.1 {status} {reason}"] + [f"{key}: {value}" for key, value in head.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def redirect(target: str, status: int = 302) -> bytes:
    return reply(status, "Found", {"Location": target})


def text(body: str, content_type: str = "text/plain") -> bytes:
    return reply(200, "OK", {"Content-Type": content_type}, body.encode())


ROBOTS_ALLOW = text("User-agent: *\nAllow: /\n")


class Transport:
    """What the HTTPS handler talks to: answers from `routes` ((host, path) -> raw reply) and records each request."""

    def __init__(self, routes: dict[tuple[str, str], bytes]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str]] = []

    def asked_host(self, name: str) -> list[tuple[str, str]]:
        return [(host, path) for host, path in self.requests if host == name or host.endswith("." + name)]

    def hosts(self) -> list[str]:
        return [host for host, _ in self.requests]

    def connection_class(self):
        transport = self

        class Socket:
            def __init__(self, data: bytes) -> None:
                self.data = data

            def makefile(self, *args, **kwargs):
                return io.BytesIO(self.data)

            def close(self) -> None:
                pass

        class Connection:
            def __init__(self, host, timeout=None, **kwargs) -> None:
                self.host, self.sock, self.key = host, None, None

            def set_debuglevel(self, level) -> None:
                pass

            def set_tunnel(self, *args, **kwargs) -> None:
                pass

            def request(self, method, url, body=None, headers=None, **kwargs) -> None:
                name = self.host.rsplit(":", 1)[0].lower().rstrip(".")
                transport.requests.append((name, url))
                self.key = (name, url.split("?", 1)[0])

            def getresponse(self):
                response = http.client.HTTPResponse(Socket(transport.routes.get(self.key) or reply(404, "Not Found")),
                                                    method="GET")
                response.begin()
                return response

            def close(self) -> None:
                pass

        return Connection


def sources_file(folder: str, *, pi_on: bool = False, extra: dict | None = None) -> Path:
    """data/sources.json as the policy reads it: Physical Intelligence switched off (or on), a normal feed, a disabled feed."""
    pi = {"id": "physical-intelligence", "url": PI_PAGE, "mode": "listing", "link_prefix": "/blog/",
          "hosts": ["pi.website", "physicalintelligence.company"], "fetcher": "static"}
    if not pi_on:
        pi.update(enabled=False, disabled_reason="reads failed; not read, per the rules")
    sources = {"feeds": [{"id": "aws", "url": "https://a.example/feed.xml"},
                         {"id": "github-lerobot", "url": "https://github.com/huggingface/lerobot/releases.atom",
                          "enabled": False, "disabled_reason": "github.com robots.txt disallows /*.atom$"}],
               "page_watch": [pi], **(extra or {})}
    path = Path(folder) / "sources.json"
    path.write_text(json.dumps(sources), encoding="utf-8")
    return path


class PolicyCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.use(sources_file(self.folder))

    def use(self, path: Path) -> None:
        patcher = mock.patch.object(host_policy, "SOURCES_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    @contextlib.contextmanager
    def network(self, routes):
        transport = Transport(routes)
        with mock.patch.object(http.client, "HTTPSConnection", transport.connection_class()), \
                mock.patch.object(fetch.time, "sleep"), mock.patch.object(fetch, "polite"), \
                contextlib.redirect_stdout(io.StringIO()):
            yield transport


class HostPolicyTests(PolicyCase):
    def test_the_hosts_of_a_switched_off_entry_are_blocked_with_their_subdomains(self):
        blocked = host_policy.load()
        self.assertEqual(blocked, frozenset({"www.pi.website", "pi.website", "physicalintelligence.company"}))
        for url in (PI_PAGE, "https://pi.website/", "https://api.pi.website/x", "https://physicalintelligence.company/",
                    "https://www.physicalintelligence.company/robots.txt", "http://www.pi.website/",
                    "https://WWW.PI.WEBSITE./x", "https://www.pi.website:443/x", "https://user:pw@www.pi.website/x",
                    "https://www.pi.website?q=1", "https://a.b.c.pi.website/"):
            with self.subTest(url):
                self.assertEqual(host_policy.refusal(url), host_policy.SWITCHED_OFF)

    def test_other_hosts_are_not_blocked_whatever_they_look_like(self):
        for url in ("https://a.example/feed.xml", "https://notpi.website/", "https://pi.website.example.com/",
                    "https://xpi.website/", "https://example.org/pi.website", "https://a.example/?u=https://www.pi.website/",
                    "https://a.example/www.pi.website", "https://www.pi.website.a.example/", "http://127.0.0.1:8080/x"):
            with self.subTest(url):
                self.assertIsNone(host_policy.refusal(url))

    def test_the_shipped_configuration_blocks_physical_intelligence(self):
        shipped = Path(host_policy.__file__).resolve().parent.parent / "data" / "sources.json"
        blocked = host_policy.load(shipped)
        self.assertTrue({"www.pi.website", "pi.website", "physicalintelligence.company"} <= blocked, blocked)
        self.assertEqual(host_policy.refusal(PI_PAGE, shipped), host_policy.SWITCHED_OFF)
        self.assertIsNone(host_policy.refusal("https://aws.amazon.com/blogs/robotics/feed/", shipped))
        # the shipped feeds and the signal APIs are not in the way
        self.assertFalse({"api.github.com", "github.com", "huggingface.co", "arxiv.org"} & blocked)

    def test_a_switched_off_feed_is_not_blocked_by_host(self):
        self.assertIsNone(host_policy.refusal("https://github.com/huggingface/lerobot/releases.atom"))
        self.assertIsNone(host_policy.refusal("https://api.github.com/repos/huggingface/lerobot/releases?per_page=1"))

    def test_a_host_something_switched_on_uses_is_not_blocked(self):
        path = sources_file(self.folder, extra={"feeds": [
            {"id": "same-host", "url": "https://www.pi.website/feed.xml"},
            {"id": "aws", "url": "https://a.example/feed.xml"}]})
        self.use(path)
        self.assertEqual(host_policy.load(), frozenset({"physicalintelligence.company"}))
        self.assertIsNone(host_policy.refusal("https://www.pi.website/feed.xml"))
        self.assertEqual(host_policy.refusal("https://physicalintelligence.company/"), host_policy.SWITCHED_OFF)

    def test_switching_the_entry_back_on_unblocks_it(self):
        self.use(sources_file(self.folder, pi_on=True))
        self.assertEqual(host_policy.load(), frozenset())
        self.assertIsNone(host_policy.refusal(PI_PAGE))

    def test_a_mistyped_switch_still_blocks(self):
        for value in ("false", 0, None, "no"):
            with self.subTest(value):
                path = Path(self.folder) / "typo.json"
                path.write_text(json.dumps({"page_watch": [{"id": "x", "url": "https://x.example/p", "enabled": value}]}))
                self.assertEqual(host_policy.load(path), frozenset({"x.example"}))

    def test_an_unreadable_policy_refuses_everything(self):
        path = Path(self.folder) / "sources.json"
        for broken in ("{broken", "[]", json.dumps({"feeds": "x"}), json.dumps({"page_watch": {"a": 1}}), ""):
            with self.subTest(broken):
                path.write_text(broken, encoding="utf-8")
                self.assertEqual(host_policy.refusal("https://a.example/feed.xml"), host_policy.UNREADABLE)
        path.unlink()
        self.assertEqual(host_policy.refusal("https://a.example/feed.xml"), host_policy.UNREADABLE)

    def test_a_url_whose_two_readings_disagree_or_that_has_no_host_is_refused(self):
        for url in ("https://a.example\\@www.pi.website/x", "not a url", "https:///x", "https://", None, 5, "//a.example/x"):
            with self.subTest(url):
                self.assertIn(host_policy.refusal(url), (host_policy.UNPARSEABLE, host_policy.SWITCHED_OFF))

    def test_the_policy_is_reread_when_the_file_changes(self):
        path = Path(self.folder) / "sources.json"
        self.assertIsNotNone(host_policy.refusal(PI_PAGE))
        path.write_text(json.dumps({"feeds": [], "page_watch": []}) + " " * 40, encoding="utf-8")
        self.assertIsNone(host_policy.refusal(PI_PAGE))


class HttpGetTests(PolicyCase):
    def test_a_blocked_url_is_not_requested_and_reports_no_answer(self):
        with self.network({}) as transport:
            for url in (PI_PAGE, "https://pi.website/robots.txt", "https://PHYSICALINTELLIGENCE.COMPANY./"):
                report = {}
                self.assertIsNone(fetch.http_get(url, report=report), url)
                self.assertIsNone(report["status"])
        self.assertEqual(transport.requests, [])

    def test_an_unreadable_policy_sends_nothing(self):
        (Path(self.folder) / "sources.json").write_text("{broken")
        with self.network({("a.example", "/feed.xml"): text("x")}) as transport:
            self.assertIsNone(fetch.http_get("https://a.example/feed.xml"))
        self.assertEqual(transport.requests, [])

    def test_an_allowed_url_and_an_allowed_redirect_still_work(self):
        routes = {("a.example", "/feed.xml"): redirect("https://b.example/feed.xml"),
                  ("b.example", "/feed.xml"): redirect("/final.xml"),
                  ("b.example", "/final.xml"): text("<feed/>", "application/xml")}
        with self.network(routes) as transport:
            report = {}
            self.assertEqual(fetch.http_get("https://a.example/feed.xml", report=report), b"<feed/>")
        self.assertEqual(report["final_url"], "https://b.example/final.xml")
        self.assertEqual(transport.hosts(), ["a.example", "b.example", "b.example"])

    def test_a_redirect_to_a_blocked_host_is_not_sent_and_is_not_retried(self):
        routes = {("a.example", "/feed.xml"): redirect("https://www.pi.website/feed.xml")}
        with self.network(routes) as transport:
            report = {}
            self.assertIsNone(fetch.http_get("https://a.example/feed.xml", retries=3, report=report))
        self.assertEqual(transport.requests, [("a.example", "/feed.xml")])      # one request, to the host that redirected
        self.assertIsNone(report["status"])

    def test_every_hop_of_a_chain_is_checked_not_only_the_first(self):
        routes = {("a.example", "/f"): redirect("https://b.example/f"),
                  ("b.example", "/f"): redirect("https://c.example/f", 301),
                  ("c.example", "/f"): redirect("https://www.pi.website/f", 307),
                  ("www.pi.website", "/f"): text("pi")}
        with self.network(routes) as transport:
            self.assertIsNone(fetch.http_get("https://a.example/f"))
        self.assertEqual(transport.hosts(), ["a.example", "b.example", "c.example"])
        self.assertEqual(transport.asked_host("pi.website"), [])

    def test_every_spelling_of_the_blocked_host_in_a_location_is_refused(self):
        targets = ("https://WWW.PI.WEBSITE./x", "//pi.website/x", "https://user:pw@api.pi.website:443/x",
                   "https://physicalintelligence.company/", "https://pi.website:8443/x", "https://www.pi.website\\.a.example/",
                   "http://www.pi.website/x")
        for target in targets:
            with self.subTest(target):
                routes = {("a.example", "/f"): redirect(target)}
                with self.network(routes) as transport:
                    self.assertIsNone(fetch.http_get("https://a.example/f", retries=1))
                self.assertEqual(transport.asked_host("pi.website"), [])
                self.assertEqual(transport.asked_host("physicalintelligence.company"), [])
                self.assertEqual(transport.hosts(), ["a.example"])

    def test_switching_the_entry_back_on_lets_the_redirect_through(self):
        self.use(sources_file(self.folder, pi_on=True))
        routes = {("a.example", "/f"): redirect("https://www.pi.website/f"), ("www.pi.website", "/f"): text("pi")}
        with self.network(routes) as transport:
            self.assertEqual(fetch.http_get("https://a.example/f"), b"pi")
        self.assertEqual(transport.hosts(), ["a.example", "www.pi.website"])


class ConsumerTests(PolicyCase):
    """The real readers, with the redirect an unrelated host might answer."""

    def config(self, **extra) -> Config:
        sources = {"feeds": [{"id": "aws", "url": "https://a.example/feed.xml", "publisher": "A", "evidence": "O"}],
                   "filters": {"max_entries_per_feed": 10}, **extra}
        return Config(sources=sources, taxonomy={"anchors": ["robot"]}, glossary={}, baseline={})

    def test_feed_whose_answer_redirects_to_a_blocked_host(self):
        routes = {("a.example", "/feed.xml"): redirect("https://www.pi.website/feed.xml"),
                  ("www.pi.website", "/feed.xml"): reply(200, "OK", {"Content-Type": "application/rss+xml"}, RSS)}
        with self.network(routes) as transport:
            items, failed = fetch.fetch_feeds(self.config())
        self.assertEqual((items, failed), ([], ["aws"]))
        self.assertEqual(transport.asked_host("pi.website"), [])

    def test_the_same_feed_is_read_when_the_redirect_goes_somewhere_else(self):
        routes = {("a.example", "/feed.xml"): redirect("https://b.example/feed.xml"),
                  ("b.example", "/feed.xml"): reply(200, "OK", {"Content-Type": "application/rss+xml"}, RSS)}
        with self.network(routes) as transport:
            items, failed = fetch.fetch_feeds(self.config())
        self.assertEqual((len(items), failed), (1, []))
        self.assertEqual(transport.hosts(), ["a.example", "b.example"])

    def test_robots_txt_that_redirects_to_a_blocked_host(self):
        routes = {("a.example", "/robots.txt"): redirect("https://www.pi.website/robots.txt"),
                  ("www.pi.website", "/robots.txt"): ROBOTS_ALLOW,
                  ("a.example", "/feed.xml"): reply(200, "OK", {"Content-Type": "application/rss+xml"}, RSS)}
        skipped, reasons = [], {}
        with self.network(routes) as transport:
            items, failed = fetch.fetch_feeds(self.config(), robots=fetch.RobotsCache(), skipped=skipped, reasons=reasons)
        self.assertEqual((items, failed), ([], []))
        self.assertEqual((skipped, reasons), (["aws"], {"aws": fetch.UNREACHABLE}))     # not read, and not "failed"
        self.assertEqual(transport.asked_host("pi.website"), [])
        self.assertNotIn(("a.example", "/feed.xml"), transport.requests)

    def test_robots_txt_and_feed_that_do_not_redirect_are_read_as_before(self):
        routes = {("a.example", "/robots.txt"): ROBOTS_ALLOW,
                  ("a.example", "/feed.xml"): reply(200, "OK", {"Content-Type": "application/rss+xml"}, RSS)}
        with self.network(routes):
            items, failed = fetch.fetch_feeds(self.config(), robots=fetch.RobotsCache())
        self.assertEqual((len(items), failed), (1, []))

    def test_an_attention_api_that_redirects_to_a_blocked_host(self):
        routes = {("huggingface.co", "/api/daily_papers"): redirect("https://www.pi.website/api/daily_papers"),
                  ("www.pi.website", "/api/daily_papers"): text("[]", "application/json")}
        config = self.config(heat={"enabled": True, "hn_queries": [], "github_repos": []})
        with self.network(routes) as transport:
            signals = heat.fetch_signals(config, date(2026, 11, 16))
        self.assertEqual(signals.failed, ["hf_daily_papers"])
        self.assertEqual(transport.asked_host("pi.website"), [])

    def test_the_attention_api_is_read_when_it_does_not_redirect(self):
        routes = {("huggingface.co", "/api/daily_papers"): text('[{"paper": {"id": "2611.00001", "upvotes": 7}}]',
                                                                   "application/json")}
        config = self.config(heat={"enabled": True, "hn_queries": [], "github_repos": []})
        with self.network(routes):
            signals = heat.fetch_signals(config, date(2026, 11, 16))
        self.assertEqual((signals.failed, signals.upvotes), ([], {"2611.00001": 7}))

    def test_the_arxiv_readers_use_the_same_guard(self):
        routes = {("export.arxiv.org", "/api/query"): redirect("https://pi.website/q"),
                  ("rss.arxiv.org", "/rss/cs.RO"): redirect("https://physicalintelligence.company/rss"),
                  ("pi.website", "/q"): text("x"), ("physicalintelligence.company", "/rss"): text("x")}
        with self.network(routes) as transport:
            self.assertIsNone(fetch.http_get(f"{fetch.ARXIV_ENDPOINT}?search_query=x", retries=1))
            self.assertIsNone(fetch.http_get(f"{fetch.ARXIV_RSS}/cs.RO", retries=1))
        self.assertEqual(transport.hosts(), ["export.arxiv.org", "rss.arxiv.org"])


if __name__ == "__main__":
    unittest.main()
