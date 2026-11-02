"""The static fetcher: URL and address policy, the pinned TLS connection, response handling and the deadline.

Policy tests need no network. Connection tests run a real TLS server on 127.0.0.1 and a real client handshake; only the
resolver and the address policy are replaced (the policy would, correctly, refuse loopback). What these tests show is the
behaviour of this module; they say nothing about the host's routing or egress rules.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pairadar import fetch, static_fetch  # noqa: E402
from pairadar.static_fetch import StaticFetchError, address_allowed, parse_url  # noqa: E402
from static_fixture import HOST, Reply, TlsTestCase  # noqa: E402

PAGE = "<html><head><title>News</title></head><body><a href='/news/a'>Alpha post here</a></body></html>"


def reason(call) -> str:
    try:
        call()
    except StaticFetchError as exc:
        return exc.reason
    return ""


class UrlTests(unittest.TestCase):
    def test_percent_signs_and_dot_segments_must_be_valid(self):
        """04 and 01: a % that starts no escape, and a path the server would resolve differently from the way robots.txt is
        judged, are refused before any request."""
        for url in ("https://good.test/%", "https://good.test/%4", "https://good.test/%zz", "https://good.test/%4%31",
                    "https://good.test/a%", "https://good.test/a?q=%", "https://good.test/a?q=%4", "https://good.test/a?q=%zz&r=1",
                    "https://good.test/%G1", "https://good.test/%1G", "https://good.test/100%",
                    "https://good.test/a/../private", "https://good.test/a/./b", "https://good.test/..", "https://good.test/.",
                    "https://good.test/a/..", "https://good.test/%2e%2e/x", "https://good.test/%2E./x", "https://good.test/.%2e/x",
                    "https://good.test/a/%2e/b", "https://good.test//private", "https://good.test/a//b", "https://good.test///",
                    # 01's R-D-b7b7-01: an encoded separator hides a dot segment from the check; no server's behaviour is assumed
                    "https://good.test/public%2F..%2Fprivate", "https://good.test/public%2f..%2fprivate", "https://good.test/public%2F%2e%2e%2Fprivate",
                    "https://good.test/public%2f%2E%2E%2fprivate", "https://good.test/public/..%2Fprivate", "https://good.test/public%2F../private",
                    "https://good.test/public%5C..%5Cprivate", "https://good.test/public%5c..%5cprivate", "https://good.test/public%5C",
                    "https://good.test/a%2Fb", "https://good.test/a%2fb", "https://good.test/%2F", "https://good.test/a%2F",
                    "https://good.test/public%252F..%252Fprivate", "https://good.test/public%252f..%252fprivate",
                    "https://good.test/%252e%252e/x", "https://good.test/%252E%252E/x", "https://good.test/public%255C..%255Cprivate",
                    "https://good.test/public%25252F..%25252Fprivate", "https://good.test/a%25252Fb", "https://good.test/%25%32%65%25%32%65/x",
                    "https://good.test/a%252Fb", "https://good.test/" + "%25" * 5 + "2F",
                    "https://good.test/a\\b",
                    # 01's R-D-155c-01 and -02: an escape that a decode could turn into another spelling of the same path, and
                    # path parameters; only unreserved characters and non-ASCII octets may be escaped
                    "https://good.test/%2570rivate", "https://good.test/%2570rivate", "https://good.test/%2550rivate",
                    "https://good.test/%252570rivate", "https://good.test/%25%37%30rivate", "https://good.test/%2541", "https://good.test/%25",
                    "https://good.test/100%25", "https://good.test/a%20b", "https://good.test/a%2Bb", "https://good.test/a%2525",
                    "https://good.test/a%3Fb", "https://good.test/a%23b", "https://good.test/a%2Ab", "https://good.test/a%24",
                    "https://good.test/a%00b", "https://good.test/a%0Ab", "https://good.test/a%7Fb", "https://good.test/a%22b",
                    "https://good.test/private;v=1", "https://good.test/private%3Bv=1", "https://good.test/private%3bv=1",
                    "https://good.test/a;b/c", "https://good.test/a/b;", "https://good.test/;", "https://good.test/a/..;/b",
                    "https://good.test/a?q=a%20b", "https://good.test/a?q=%2570", "https://good.test/a?q=%2F", "https://good.test/a?q=%25"):
            with self.subTest(url):
                self.assertEqual(reason(lambda: parse_url(url)), "url-invalid")
        for url, target in (("https://good.test/%70rivate", "/%70rivate"), ("https://good.test/%41%2e", "/%41%2e"),
                            ("https://good.test/caf%C3%A9", "/caf%C3%A9"), ("https://good.test/%E3%83%84", "/%E3%83%84"),
                            ("https://good.test/a%7Eb", "/a%7Eb"), ("https://good.test/a%2db%5F", "/a%2db%5F"),
                            ("https://good.test/a,b:c+d=e&f@g!h'i(j)k", "/a,b:c+d=e&f@g!h'i(j)k"), ("https://good.test/a$b*c", "/a$b*c"),
                            ("https://good.test/a?x=1;y=2", "/a?x=1;y=2"),
                            ("https://good.test/a?q=%7E", "/a?q=%7E"), ("https://good.test/news/", "/news/"),
                            ("https://good.test/a.b/c..d/.hidden", "/a.b/c..d/.hidden"), ("https://good.test/a...b", "/a...b"),
                            ("https://good.test/...", "/..."), ("https://good.test", "/"), ("https://good.test/", "/"),
                            ("https://good.test/a/?x=1", "/a/?x=1")):
            with self.subTest(url):
                self.assertEqual(parse_url(url).target, target)

    def test_accepts_plain_public_https_urls(self):
        for url, host, target in (("https://good.test/news", "good.test", "/news"),
                                  ("https://Good.Test", "good.test", "/"),
                                  ("https://good.test:443/a/b?x=1&y=2", "good.test", "/a/b?x=1&y=2"),
                                  ("https://www.xn--bcher-kva.example/p", "www.xn--bcher-kva.example", "/p")):
            with self.subTest(url):
                parsed = parse_url(url)
                self.assertEqual((parsed.host, parsed.target), (host, target))

    def test_refuses_everything_else(self):
        bad = ["http://good.test/", "ftp://good.test/", "good.test/news", "//good.test/", "https:///x", "",
               "https://user@good.test/", "https://user:pw@good.test/", "https://good.test:8443/", "https://good.test:80/",
               "https://good.test:/", "https://127.0.0.1/", "https://8.8.8.8/", "https://[::1]/", "https://[::ffff:8.8.8.8]/",
               "https://2130706433/", "https://0x7f.0.0.1/", "https://0177.0.0.1/", "https://localhost/", "https://intranet/",
               "https://good.test/a b", "https://good.test/a\tb", "https://good.test/a\nb", "https://good.test/a\x00b",
               "https://good.test/a\\b", "https://good.test/#frag", "https://good_test.com/", "https://good.test./",
               "https://good.test.:443/", "https://xn--/", "https://bücher.example/", "https://BÜCHER.example/",
               "https://-bad.test/",
               "https://bad-.test/", "https://good..test/", "https://münchen.de/", "https://good.test/é",
               "https://" + "a" * 64 + ".test/", "https://good.test/" + "x" * 500, None, 5, b"https://good.test/"]
        for url in bad:
            with self.subTest(url=url):
                self.assertEqual(reason(lambda: parse_url(url)), "url-invalid")


class AddressTests(unittest.TestCase):
    def test_public_ipv4_only(self):
        for ok in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "151.101.1.69"):
            with self.subTest(ok):
                self.assertTrue(address_allowed(ok))
        refused = [
            "127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.0.1", "169.254.169.254",
            "169.254.0.1", "100.64.0.1", "100.127.255.255", "0.0.0.0", "0.1.2.3", "255.255.255.255", "224.0.0.1",
            "239.255.255.255", "240.0.0.1", "192.0.0.1", "192.0.0.9", "192.0.2.1", "192.88.99.1", "198.18.0.1", "198.19.255.255",
            "198.51.100.1", "203.0.113.1",
            # IPv6 in every form is refused for now, whatever it embeds
            "::1", "::", "::ffff:8.8.8.8", "::ffff:169.254.169.254", "::ffff:127.0.0.1", "64:ff9b::808:808",
            "64:ff9b::a9fe:a9fe", "2002:0808:0808::1", "fd00::1", "fc00::1", "fe80::1", "ff02::1", "2001:db8::1",
            "2001:4860:4860::8888", "::8.8.8.8",
            # not a canonical dotted quad
            "08.8.8.8", "8.8.8", "8.8.8.8.8", "8.8.8.8 ", " 8.8.8.8", "8.8.8.8\n", "0x08080808", "134744072", "", "localhost"]
        for text in refused:
            with self.subTest(text):
                self.assertFalse(address_allowed(text))
        for value in (None, 5, b"8.8.8.8", ["8.8.8.8"]):
            self.assertFalse(address_allowed(value))

    def test_does_not_rest_on_is_global_alone(self):
        # the explicit blocks refuse addresses even where this Python's flag would not
        import ipaddress
        for text in ("192.0.0.9", "192.0.0.10", "192.88.99.1"):
            self.assertFalse(address_allowed(text), text)
        self.assertTrue(ipaddress.ip_address("::ffff:8.8.8.8").version == 6)      # why mapped addresses are refused by version

    def test_the_default_user_agent_is_the_radars(self):
        self.assertEqual(static_fetch.USER_AGENT, fetch.USER_AGENT)

    def test_the_production_tls_context_verifies_the_certificate_and_the_name(self):
        import ssl
        context = static_fetch.StaticFetcher()._context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(static_fetch.StaticFetcher()._port, 443)
        with patch.object(ssl.SSLContext, "set_alpn_protocols") as alpn:
            static_fetch.StaticFetcher()
        alpn.assert_called_once_with(["http/1.1"])                                 # HTTP/1.1 only

    def test_the_static_path_uses_no_proxy_aware_or_cookie_helper(self):
        import ast
        tree = ast.parse((ROOT / "pairadar" / "static_fetch.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        for forbidden in ("urllib.request", "requests", "urllib3", "http.cookiejar", "http.server", "socketserver", "subprocess"):
            self.assertNotIn(forbidden, imported)
        source = (ROOT / "pairadar" / "static_fetch.py").read_text(encoding="utf-8")
        for forbidden in ("getproxies", "ProxyHandler", "HTTPS_PROXY", "environ"):
            self.assertNotIn(forbidden, source)

    def test_every_special_purpose_ipv4_range_is_refused_without_is_global(self):
        from unittest.mock import PropertyMock
        samples = ["0.1.2.3", "10.1.1.1", "100.64.1.1", "100.127.255.254", "127.0.0.2", "169.254.1.1", "172.16.5.5",
                   "172.31.1.1", "192.0.0.5", "192.0.2.5", "192.88.99.5", "192.168.9.9", "198.18.5.5", "198.19.5.5",
                   "198.51.100.5", "203.0.113.5", "224.1.1.1", "239.9.9.9", "240.1.1.1", "255.255.255.254", "255.255.255.255"]
        with patch.object(ipaddress.IPv4Address, "is_global", new_callable=PropertyMock, return_value=True):
            for text in samples:
                with self.subTest(text):
                    self.assertFalse(address_allowed(text))
            self.assertTrue(address_allowed("8.8.8.8"))                           # the flag is really neutralised

    def test_ipv6_forms_that_embed_ipv4_are_refused_whole(self):
        for text in ("64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b:1::1", "64:ff9b:1:ffff::1", "::127.0.0.1", "::7f00:1",
                     "::ffff:0:7f00:1", "::ffff:7f00:1", "2002:7f00:1::1", "2002:a9fe:a9fe::", "2001:0:4136:e378:8000:63bf:3fff:fdd2",
                     "2001::1", "fe80::1%eth0", "fe80::1", "ff02::1", "::", "::1", "fc00::1", "fd12:3456::1"):
            with self.subTest(text):
                self.assertFalse(address_allowed(text))

class EdgeTests(TlsTestCase):
    """Header cap, failure details, stuck lookups and allowance; the server is only there for what needs a connection."""

    def get(self, fetcher, path="/news", **kw):
        return fetcher.get(f"https://{HOST}{path}", fetcher.budget(10), **kw)

    def test_a_response_with_a_huge_header_block_is_refused(self):
        self.server.serve("/p", Reply(200, PAGE, headers={"X-Pad": "a" * 20000}))
        self.assertEqual(reason(lambda: self.get(self.fetcher(), "/p")), "headers-too-large")
        many = {f"X-{i}": "b" * 200 for i in range(90)}                            # many modest headers, still over the cap
        self.server.serve("/q", Reply(200, PAGE, headers=many))
        self.assertEqual(reason(lambda: self.get(self.fetcher(), "/q")), "headers-too-large")
        self.server.serve("/ok", Reply(200, PAGE, headers={f"X-{i}": "c" * 40 for i in range(20)}))
        self.assertEqual(self.get(self.fetcher(), "/ok").body.decode(), PAGE)

    def test_failures_carry_the_http_status_and_a_redirect_only_its_target_name(self):
        cases = [("/a", Reply(403, "x"), "http-403", 403, ""),
                 ("/b", Reply(302, "", headers={"Location": "https://other.example/secret/path?token=abc#f"}), "redirect", 302, "other.example"),
                 ("/c", Reply(301, "", headers={"Location": "/relative/place?x=1"}), "redirect", 301, "relative"),
                 ("/d", Reply(302, "", headers={"Location": "http://169.254.169.254/latest/meta-data/"}), "redirect", 302, "invalid"),
                 ("/e", Reply(302, "", headers={"Location": "https://user:pw@evil.example:8443/x"}), "redirect", 302, "evil.example"),
                 ("/f", Reply(302, "", headers={}), "redirect", 302, "invalid")]
        for path, reply, expected, status, detail in cases:
            with self.subTest(path):
                self.server.serve(path, reply)
                with self.assertRaises(StaticFetchError) as caught:
                    self.get(self.fetcher(), path)
                self.assertEqual((caught.exception.reason, caught.exception.status, caught.exception.detail), (expected, status, detail))
                for leak in ("secret", "token", "pw", "8443", "meta-data", "relative"):
                    if leak != detail:
                        self.assertNotIn(leak, caught.exception.detail)

    def test_location_host_keeps_only_a_valid_domain_name(self):
        host = static_fetch.location_host
        self.assertEqual(host("https://Docs.Example.com/a?b#c"), "docs.example.com")
        for bad in ("https://127.0.0.1/", "https://[::1]/x", "https://2130706433/", "https://good_test.com/", "https://a/",
                    "https://" + "a" * 30 + "." + "b" * 60 + ".example/", "javascript:alert(1)", "https://exa mple.com/",
                    "https://example.com\r\nSet-Cookie: x", "https://x\x00.example/", "", None, 5, "https://-bad.example/"):
            with self.subTest(bad):
                self.assertEqual(host(bad), "invalid")
        self.assertEqual(host("//cdn.example.com/x"), "cdn.example.com")
        self.assertEqual(host("/x"), "relative")
        self.assertEqual(host("?x=1"), "relative")

    def test_a_stuck_lookup_is_cleaned_up_when_it_finishes_and_the_fetcher_works_again(self):
        release = threading.Event()
        calls = []

        def resolver(host):
            calls.append(host)
            if len(calls) <= static_fetch.MAX_STUCK_RESOLVERS:
                release.wait(30)
            return ["127.0.0.1"]

        fetcher = self.fetcher(resolver=resolver)
        self.server.serve("/news", Reply(200, PAGE))
        for _ in range(static_fetch.MAX_STUCK_RESOLVERS):
            self.assertEqual(reason(lambda: fetcher.get(f"https://{HOST}/news", fetcher.budget(0.5))), "budget")
        self.assertEqual(reason(lambda: fetcher.get(f"https://{HOST}/news", fetcher.budget(5))), "resolver-stuck")
        self.assertEqual(len(calls), static_fetch.MAX_STUCK_RESOLVERS)             # the refused request started no lookup
        release.set()
        for thread in list(fetcher._abandoned):
            thread.join(5)
        self.assertEqual(self.get(fetcher, "/news").body.decode(), PAGE)             # finished lookups are forgotten
        self.assertEqual(fetcher._abandoned, [])

    def test_a_process_with_stuck_lookups_still_exits(self):
        import subprocess
        code = (
            "import sys, threading, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pairadar import static_fetch as s\n"
            "never = threading.Event()\n"
            "f = s.StaticFetcher(resolver=lambda h: (never.wait(3600), ['8.8.8.8'])[1], min_interval=0)\n"
            "for _ in range(4):\n"
            "    try:\n"
            "        f.get('https://good.test/', f.budget(0.4))\n"
            "    except s.StaticFetchError as e:\n"
            "        print(e.reason)\n"
        )
        started = time.monotonic()
        done = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["budget", "budget", "resolver-stuck", "resolver-stuck"])
        self.assertLess(time.monotonic() - started, 15)                              # daemon lookups did not hold the interpreter

    def test_the_allowance_and_pacing_count_robots_and_pages_alike(self):
        self.server.serve("/robots.txt", Reply(200, "User-agent: *\nAllow: /\n", ctype="text/plain"))
        self.server.serve("/news", Reply(200, PAGE))
        fetcher = self.fetcher()
        budget = fetcher.budget(20)
        fetcher.get(f"https://{HOST}/robots.txt", budget, kinds=static_fetch.TEXT_TYPES)
        fetcher.get(f"https://{HOST}/news", budget)
        self.assertEqual(budget.requests_left, static_fetch.MAX_REQUESTS_PER_SOURCE - 2)


class ConnectionTests(TlsTestCase):
    def get(self, fetcher, path="/news", **kw):
        return fetcher.get(f"https://{HOST}{path}", fetcher.budget(10), **kw)

    def test_a_complete_200_is_a_page_and_the_request_is_bare(self):
        self.server.serve("/news", Reply(200, PAGE))
        page = self.get(self.fetcher())
        self.assertEqual((page.status, page.content_type, page.body.decode()), (200, "text/html", PAGE))
        (method, path, headers), = self.server.requests
        self.assertEqual((method, path), ("GET", "/news"))
        self.assertEqual(headers["host"], HOST)                                   # no port in Host
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(headers["connection"], "close")
        self.assertEqual(headers["user-agent"], static_fetch.USER_AGENT)
        for forbidden in ("cookie", "authorization", "proxy-authorization", "referer", "x-forwarded-for"):
            self.assertNotIn(forbidden, headers)
        self.assertEqual(self.server.sni, [HOST])                                 # SNI is the original name

    def test_the_query_is_sent_exactly(self):
        self.server.serve("/news", Reply(200, PAGE))
        self.get(self.fetcher(), "/news?a=1&b=%7Ex")
        self.assertEqual(self.server.paths(), ["/news?a=1&b=%7Ex"])

    def test_proxy_settings_in_the_environment_are_ignored(self):
        self.server.serve("/news", Reply(200, PAGE))
        env = {"HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
               "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
        with patch.dict(os.environ, env):
            self.get(self.fetcher())
        self.assertEqual(self.server.paths(), ["/news"])

    def test_chunked_bodies_are_read(self):
        self.server.serve("/news", Reply(200, PAGE, chunked=True))
        self.assertEqual(self.get(self.fetcher()).body.decode(), PAGE)

    def test_charset_is_honoured_and_never_raises(self):
        self.server.serve("/a", Reply(200, "caf\xe9".encode("latin-1"), ctype="text/html; charset=ISO-8859-1"))
        self.server.serve("/c", Reply(200, "<p>caf\u00e9</p>", ctype="text/html; charset=UTF-8"))
        self.server.serve("/d", Reply(200, "<p>plain</p>", ctype="text/html"))
        self.assertEqual(static_fetch.decode(self.get(self.fetcher(), "/a")), "caf\xe9")
        self.assertEqual(static_fetch.decode(self.get(self.fetcher(), "/c")), "<p>caf\u00e9</p>")
        self.assertEqual(static_fetch.decode(self.get(self.fetcher(), "/d")), "<p>plain</p>")
        # R-PW-05: Python knows codecs that rewrite bytes into markup; a page may only name a plain text encoding
        for name in ("utf-7", "unicode_escape", "punycode", "nonsense-9", "rot13", "utf-16", "hex", "idna", "base64"):
            with self.subTest(name):
                self.server.serve("/b", Reply(200, b"+ADw-script+AD4- broken", ctype=f"text/html; charset={name}"))
                self.assertEqual(reason(lambda: self.get(self.fetcher(), "/b")), "content-type")
        for name in sorted(static_fetch.ALLOWED_CHARSETS):
            codecs_name = name.replace("x-sjis", "shift_jis")
            import codecs
            codecs.lookup(codecs_name)                                                      # every allowed name is a real codec

    def test_only_a_complete_200_with_an_accepted_type_is_content(self):
        cases = {
            "redirect 301": (Reply(301, "", headers={"Location": "https://good.test/other"}), "redirect"),
            "redirect to the metadata address": (Reply(302, "", headers={"Location": "http://169.254.169.254/latest/"}), "redirect"),
            "redirect 307": (Reply(307, "", headers={"Location": "/x"}), "redirect"),
            "403": (Reply(403, "Access denied", ctype="text/html"), "http-403"),
            "404": (Reply(404, "no", ctype="text/html"), "http-404"),
            "410": (Reply(410, "gone", ctype="text/html"), "http-410"),
            "429": (Reply(429, "slow down"), "http-429"),
            "401": (Reply(401, "login"), "http-4xx"),
            "500": (Reply(500, "boom"), "http-5xx"),
            "503": (Reply(503, "later"), "http-5xx"),
            "202 challenge": (Reply(202, "<html>challenge</html>", headers={"x-amzn-waf-action": "challenge"}), "http-other"),
            "204": (Reply(204, ""), "http-other"),
            "206": (Reply(206, PAGE), "http-other"),
            "json": (Reply(200, "{}", ctype="application/json"), "content-type"),
            "no type": (Reply(200, PAGE, ctype=""), "content-type"),
            "pdf": (Reply(200, PAGE, ctype="application/pdf"), "content-type"),
            "gzip": (Reply(200, PAGE, headers={"Content-Encoding": "gzip"}), "content-encoding"),
            "br": (Reply(200, PAGE, headers={"Content-Encoding": "br"}), "content-encoding"),
            "truncated by length": (Reply(200, PAGE[:20], declare=len(PAGE)), "truncated"),
            "truncated chunked": (Reply(200, PAGE, chunked=True, stall_after=0, headers={}), "truncated"),
            "unframed": (Reply(200, PAGE, framing=False), "unframed"),
            "declared too large": (Reply(200, "x", declare=static_fetch.MAX_PAGE_BYTES + 1), "too-large"),
        }
        for name, (reply, expected) in cases.items():
            with self.subTest(name):
                self.server.serve("/p", reply)
                before = len(self.server.requests)
                got = reason(lambda: self.get(self.fetcher(), "/p"))
                self.assertEqual(got, expected)
                self.assertEqual(len(self.server.requests), before + 1)           # one request, no follow-up

    def test_a_body_over_the_limit_is_refused_whatever_it_declares(self):
        self.server.serve("/big", Reply(200, "x" * 5000, chunked=True))
        self.assertEqual(reason(lambda: self.get(self.fetcher(), "/big", max_bytes=1000)), "too-large")
        self.server.serve("/big2", Reply(200, "x" * 5000))
        self.assertEqual(reason(lambda: self.get(self.fetcher(), "/big2", max_bytes=1000)), "too-large")
        self.assertEqual(len(self.get(self.fetcher(), "/big2", max_bytes=5000).body), 5000)

    def test_text_plain_is_for_robots_only(self):
        self.server.serve("/robots.txt", Reply(200, "User-agent: *\nAllow: /\n", ctype="text/plain"))
        page = self.get(self.fetcher(), "/robots.txt", kinds=static_fetch.TEXT_TYPES)
        self.assertIn("Allow", page.body.decode())
        self.assertEqual(reason(lambda: self.get(self.fetcher(), "/robots.txt")), "content-type")

    def test_no_failure_carries_server_text_or_a_url(self):
        secret = "SECRET-BODY-TEXT"
        self.server.serve("/p", Reply(500, secret))
        try:
            self.get(self.fetcher(), "/p")
        except StaticFetchError as exc:
            self.assertNotIn(secret, str(exc) + repr(exc) + exc.reason)
            self.assertNotIn(HOST, str(exc) + repr(exc))
        else:
            self.fail("no error")

    # --- addresses and the pinned connection -------------------------------------------------------------
    def test_a_private_answer_is_refused_before_any_connection(self):
        for name, answer in {"loopback": ["127.0.0.1"], "metadata": ["169.254.169.254"], "private": ["10.1.2.3"],
                             "mapped": ["::ffff:8.8.8.8"], "nat64": ["64:ff9b::808:808"], "ula": ["fd00::1"],
                             "mixed, public first": ["8.8.8.8", "127.0.0.1"], "mixed, public last": ["127.0.0.1", "8.8.8.8"],
                             "mixed, mapped": ["8.8.8.8", "::ffff:8.8.8.8"]}.items():
            with self.subTest(name):
                fetcher = self.fetcher(resolver=lambda host, a=answer: a, policy=address_allowed)    # the real policy
                self.assertEqual(reason(lambda: self.get(fetcher)), "address-blocked")
        self.assertEqual(self.server.accepted, 0)                                  # not one connection was made

    def test_an_answer_with_no_ipv4_or_too_many_addresses_is_refused(self):
        self.assertEqual(reason(lambda: self.get(self.fetcher(resolver=lambda host: []))), "dns-no-ipv4")
        many = [f"8.8.{i}.8" for i in range(static_fetch.MAX_ADDRESSES + 1)]
        self.assertEqual(reason(lambda: self.get(self.fetcher(resolver=lambda host: many, policy=address_allowed))), "address-blocked")
        self.assertEqual(self.server.accepted, 0)

    def test_a_resolver_failure_is_dns_failed(self):
        def boom(host):
            raise socket.gaierror("secret detail 10.9.9.9")
        try:
            self.get(self.fetcher(resolver=boom))
        except StaticFetchError as exc:
            self.assertEqual(exc.reason, "dns-failed")
            self.assertNotIn("10.9.9.9", str(exc))

    def test_the_name_is_resolved_once_and_the_answer_is_the_connection(self):
        calls = []
        self.server.serve("/news", Reply(200, PAGE))
        # a name that answers a public address first and loopback second (rebinding) is never asked twice for one request
        fetcher = self.fetcher(resolver=lambda host: (calls.append(host), ["127.0.0.1"])[1])
        self.get(fetcher)
        self.assertEqual(calls, [HOST])

    def test_the_only_address_ever_connected_to_is_an_approved_one(self):
        connected: list[tuple] = []
        real = socket.socket

        class Spy(real):
            def connect(self, address):
                connected.append(address)
                return super().connect(address)

        self.server.serve("/news", Reply(200, PAGE))
        with patch.object(static_fetch.socket, "socket", Spy):
            for name, answer in {"metadata": ["169.254.169.254"], "private": ["10.1.2.3"], "mapped": ["::ffff:8.8.8.8"],
                                 "mixed": ["8.8.8.8", "169.254.169.254"]}.items():
                with self.subTest(name):
                    fetcher = self.fetcher(resolver=lambda host, a=answer: a, policy=address_allowed)
                    self.assertEqual(reason(lambda: self.get(fetcher)), "address-blocked")
            self.assertEqual(connected, [])                                        # not one connect() to any of them
            self.get(self.fetcher())
        self.assertEqual(connected, [("127.0.0.1", self.server.port)])             # the approved address, nothing else

    def test_the_connected_peer_must_be_the_approved_address(self):
        real = socket.socket

        class Lying(real):
            def getpeername(self):
                return ("10.0.0.1", 443)

        with patch.object(static_fetch.socket, "socket", Lying):
            self.assertEqual(reason(lambda: self.get(self.fetcher())), "peer-mismatch")
        self.assertEqual(self.server.requests, [])                                # no HTTP request was sent

    def test_a_certificate_for_another_name_is_refused_and_nothing_is_sent(self):
        # the server presents a certificate for good.test; the request is for a different name
        fetcher = self.fetcher()
        got = reason(lambda: fetcher.get("https://other.good.example/news", fetcher.budget(10)))
        self.assertEqual(got, "tls-failed")
        self.assertEqual(self.server.requests, [])

    def test_an_untrusted_certificate_is_refused(self):
        import ssl
        default = ssl.create_default_context()          # the system roots do not include the test certificate
        self.assertEqual(reason(lambda: self.get(self.fetcher(context=default))), "tls-failed")
        self.assertEqual(self.server.requests, [])

    def test_a_connection_refused_is_connect_failed(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        fetcher = self.fetcher()
        fetcher._port = port
        self.assertEqual(reason(lambda: self.get(fetcher)), "connect-failed")

    # --- allowance, pacing and the deadline ------------------------------------------------------------
    def test_a_source_has_a_request_allowance(self):
        self.server.serve("/news", Reply(200, PAGE))
        fetcher = self.fetcher()
        budget = fetcher.budget(20)
        for _ in range(static_fetch.MAX_REQUESTS_PER_SOURCE):
            fetcher.get(f"https://{HOST}/news", budget)
        before = self.server.accepted
        self.assertEqual(reason(lambda: fetcher.get(f"https://{HOST}/news", budget)), "request-limit")
        self.assertEqual(self.server.accepted, before)

    def test_requests_to_one_host_are_spaced(self):
        slept = []
        now = [100.0]
        fetcher = static_fetch.StaticFetcher(clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)),
                                             min_interval=2.0)
        budget = static_fetch.Budget(now[0] + 50, lambda: now[0])
        fetcher._pace("a.test", budget)
        self.assertEqual(slept, [])
        now[0] += 0.5
        fetcher._pace("a.test", budget)
        self.assertEqual(slept, [1.5])
        fetcher._pace("b.test", budget)                                           # another host is not held back
        self.assertEqual(slept, [1.5])

    def test_pacing_that_would_outlast_the_budget_is_refused(self):
        now = [100.0]
        fetcher = static_fetch.StaticFetcher(clock=lambda: now[0], sleep=lambda s: None, min_interval=2.0)
        budget = static_fetch.Budget(now[0] + 1.0, lambda: now[0])
        fetcher._pace("a.test", budget)
        self.assertEqual(reason(lambda: fetcher._pace("a.test", budget)), "budget")

    def test_a_resolver_that_never_returns_ends_at_the_deadline_and_is_bounded(self):
        release = threading.Event()
        fetcher = self.fetcher(resolver=lambda host: (release.wait(30), ["127.0.0.1"])[1])
        try:
            for _ in range(static_fetch.MAX_STUCK_RESOLVERS):
                started = time.monotonic()
                self.assertEqual(reason(lambda: fetcher.get(f"https://{HOST}/", fetcher.budget(0.6))), "budget")
                self.assertLess(time.monotonic() - started, 3)
            started = time.monotonic()
            self.assertEqual(reason(lambda: fetcher.get(f"https://{HOST}/", fetcher.budget(5))), "resolver-stuck")
            self.assertLess(time.monotonic() - started, 1)                         # refused at once, no new thread
            self.assertEqual(len([t for t in fetcher._abandoned if t.is_alive()]), static_fetch.MAX_STUCK_RESOLVERS)
        finally:
            release.set()
        self.assertEqual(self.server.accepted, 0)

    def hung(self, reply_or_mode, budget=1.0, path="/p"):
        started = time.monotonic()
        fetcher = self.fetcher()
        got = reason(lambda: fetcher.get(f"https://{HOST}{path}", fetcher.budget(budget)))
        return got, time.monotonic() - started

    def test_a_server_that_never_speaks_tls_ends_at_the_deadline(self):
        self.server.stall_tcp = True
        got, elapsed = self.hung(None)
        self.assertIn(got, ("budget", "timeout"))
        self.assertLess(elapsed, 4)

    def test_slow_headers_end_at_the_total_deadline(self):
        self.server.serve("/p", Reply(200, PAGE, stall_headers=True))              # one header byte every 0.25 s
        got, elapsed = self.hung(None, budget=1.5)
        self.assertEqual(got, "budget")
        self.assertLess(elapsed, 4)

    def test_a_slow_body_ends_at_the_total_deadline(self):
        self.server.serve("/p", Reply(200, "x" * 200, drip=0.2))                   # each read succeeds well inside a socket timeout
        got, elapsed = self.hung(None, budget=1.5)
        self.assertEqual(got, "budget")
        self.assertLess(elapsed, 4)

    def test_a_body_that_stops_mid_way_ends_at_the_deadline(self):
        self.server.serve("/p", Reply(200, "x" * 200, stall_after=10))
        got, elapsed = self.hung(None, budget=1.0)
        self.assertEqual(got, "budget")
        self.assertLess(elapsed, 4)

    def test_the_deadline_is_one_clock_for_every_phase(self):
        # a slow resolver spends the same budget the connection then has left
        fetcher = self.fetcher(resolver=lambda host: (time.sleep(0.8), ["127.0.0.1"])[1])
        self.server.serve("/p", Reply(200, "x" * 100, drip=0.1))
        started = time.monotonic()
        self.assertEqual(reason(lambda: fetcher.get(f"https://{HOST}/p", fetcher.budget(1.5))), "budget")
        self.assertLess(time.monotonic() - started, 4)

    def test_the_run_limit_caps_a_source_budget(self):
        fetcher = self.fetcher()
        now = time.monotonic()
        budget = fetcher.budget(30, limit=now + 2)
        self.assertLessEqual(budget.deadline, now + 2 + 0.5)
        expired = fetcher.budget(30, limit=now - 1)
        self.assertEqual(reason(lambda: fetcher.get(f"https://{HOST}/", expired)), "budget")
        self.assertEqual(self.server.accepted, 0)


if __name__ == "__main__":
    unittest.main()
