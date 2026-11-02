"""A strict, minimal HTTPS GET for page_watch sources that need no JavaScript (standard library only).

What it does, and nothing else: one `GET` of a public `https://<domain>/...` URL on port 443, over a connection pinned
to an address this module itself approved, with the certificate checked against the original domain name. There is no
proxy, no cookie, no credential, no redirect, no decompression and no JavaScript.

Order of every request: parse the URL strictly, resolve the name (IPv4 only, bounded in time), approve EVERY address in
the answer or refuse the whole answer, connect to an approved address, check the connected peer is that address, TLS with
SNI and verification against the original name, send the request, read at most `max_bytes`, all under one monotonic
deadline. A request that fails any step has no content: the caller gets a `StaticFetchError` with a fixed reason code and
never a partial body, a status page or an error text.

Reason codes, by who refused:
- blocked (our policy refused, nothing wrong with the server): url-invalid, address-blocked, redirect, content-type,
  content-encoding, too-large, unframed, request-limit
- unavailable (could not obtain the page): dns-failed, dns-no-ipv4, resolver-stuck, connect-failed, peer-mismatch,
  tls-failed, timeout, budget, truncated, protocol-error, http-403, http-404, http-410, http-429, http-4xx, http-5xx,
  http-other, challenge, empty-page, no-links, robots-unreadable

What this does not prove: that the host's network, routing or NAT cannot reach a forbidden address by another path.
The unit tests show this module's behaviour; the existing egress guard stays the defence in depth.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Iterable

USER_AGENT = "physical-ai-radar/1.0 (+https://github.com/noteflowai/physical-ai-radar)"
MAX_URL_CHARS = 500
MAX_PAGE_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 512_000
CONNECT_SECONDS = 10.0
SOURCE_SECONDS = 30.0
MIN_HOST_INTERVAL = 2.0
MAX_REQUESTS_PER_SOURCE = 4
MAX_ADDRESSES = 8
MAX_CONNECT_TRIES = 3
MAX_STUCK_RESOLVERS = 2
READ_CHUNK = 16_384
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
TEXT_TYPES = frozenset({"text/plain"})

BLOCKED = frozenset({"url-invalid", "address-blocked", "redirect", "content-type", "content-encoding", "too-large",
                     "unframed", "request-limit"})


class StaticFetchError(Exception):
    """A request that produced no content. `reason` is one fixed code; the message never carries a URL or server text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def blocked(self) -> bool:
        return self.reason in BLOCKED


# --- URL ------------------------------------------------------------------------------------------------

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class ParsedUrl:
    host: str
    target: str          # path and query as sent on the request line

    @property
    def origin(self) -> str:
        return f"https://{self.host}"


def parse_url(url: object) -> ParsedUrl:
    """A public https URL on port 443 with a domain-name host, or url-invalid.

    Refused: anything that is not a short ASCII string without whitespace, control characters or backslashes; user
    information; a fragment; an explicit port other than 443; IP literals in any notation (dotted, decimal, hex, IPv6
    brackets); a host with fewer than two labels or a numeric top-level label; a malformed label.
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS or not url.isascii():
        raise StaticFetchError("url-invalid")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F or ch == "\\" for ch in url):
        raise StaticFetchError("url-invalid")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise StaticFetchError("url-invalid") from None
    netloc = parts.netloc
    if parts.scheme != "https" or "@" in netloc or "[" in netloc or parts.fragment or "#" in url:
        raise StaticFetchError("url-invalid")
    if port not in (None, 443) or (netloc.count(":") and not netloc.endswith(":443")):
        raise StaticFetchError("url-invalid")
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    if len(labels) < 2 or any(not _LABEL.match(label) for label in labels):
        raise StaticFetchError("url-invalid")
    last = labels[-1]
    if last.isdigit() or last.startswith("0x"):
        raise StaticFetchError("url-invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise StaticFetchError("url-invalid")
    target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    return ParsedUrl(host, target)


# --- addresses ------------------------------------------------------------------------------------------

# Beyond `is_global`: ranges Python 3.12 may call global but nobody should reach from here, and the special-purpose
# blocks listed explicitly so the rule does not depend on one library flag.
_NEVER = tuple(ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32"))


def address_allowed(text: object) -> bool:
    """True only for a canonical public IPv4 address. IPv6 in any form (mapped, NAT64, ULA, ...) is refused for now."""
    if not isinstance(text, str):
        return False
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.version != 4 or str(ip) != text or not ip.is_global:
        return False
    return not any(ip in net for net in _NEVER)


def system_resolver(host: str) -> list[str]:
    """IPv4 answers only: AAAA records are never asked for."""
    return [info[4][0] for info in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)]


# --- one source's allowance -----------------------------------------------------------------------------

class Budget:
    """The monotonic deadline and the request allowance of one source, robots.txt included."""

    def __init__(self, deadline: float, clock: Callable[[], float] = time.monotonic,
                 max_requests: int = MAX_REQUESTS_PER_SOURCE) -> None:
        self.deadline = deadline
        self.clock = clock
        self.requests_left = max_requests

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def take(self) -> None:
        if self.remaining() <= 0:
            raise StaticFetchError("budget")
        if self.requests_left <= 0:
            raise StaticFetchError("request-limit")
        self.requests_left -= 1


@dataclass
class Page:
    status: int
    content_type: str
    body: bytes
    charset: str


# --- the fetcher ----------------------------------------------------------------------------------------

class StaticFetcher:
    """Not thread-safe; one per run. The constructor arguments exist for tests; production uses the defaults."""

    def __init__(self, *, resolver: Callable[[str], Iterable[str]] = system_resolver,
                 context: ssl.SSLContext | None = None, port: int = 443,
                 policy: Callable[[object], bool] = address_allowed,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 min_interval: float = MIN_HOST_INTERVAL) -> None:
        if context is None:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._resolver = resolver
        self._context = context
        self._port = port
        self._policy = policy
        self._clock = clock
        self._sleep = sleep
        self._min_interval = min_interval
        self._last: dict[str, float] = {}
        self._abandoned: list[threading.Thread] = []

    def budget(self, seconds: float = SOURCE_SECONDS, *, limit: float | None = None) -> Budget:
        """A source's budget: `seconds` from now, never beyond the absolute monotonic time `limit`."""
        deadline = self._clock() + seconds
        return Budget(min(deadline, limit) if limit is not None else deadline, self._clock)

    # --- steps -----------------------------------------------------------------------------------------
    def _pace(self, host: str, budget: Budget) -> None:
        last = self._last.get(host)
        now = self._clock()
        if last is not None and now - last < self._min_interval:
            wait = self._min_interval - (now - last)
            if wait >= budget.remaining():
                raise StaticFetchError("budget")
            self._sleep(wait)
        self._last[host] = self._clock()

    def _resolve(self, host: str, budget: Budget) -> list[str]:
        """The approved answer set. The lookup runs in a daemon thread that is abandoned at the deadline; at most
        MAX_STUCK_RESOLVERS abandoned lookups may be alive, after that this refuses to start another."""
        self._abandoned = [t for t in self._abandoned if t.is_alive()]
        if len(self._abandoned) >= MAX_STUCK_RESOLVERS:
            raise StaticFetchError("resolver-stuck")
        box: dict[str, object] = {}

        def work() -> None:
            try:
                box["answers"] = list(self._resolver(host))
            except Exception as exc:  # noqa: BLE001 -- any resolver failure is dns-failed
                box["error"] = type(exc).__name__

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(max(budget.remaining(), 0.0))
        if thread.is_alive():
            self._abandoned.append(thread)
            raise StaticFetchError("budget")
        if "error" in box:
            raise StaticFetchError("dns-failed")
        answers = list(dict.fromkeys(box.get("answers", [])))        # type: ignore[arg-type]
        if not answers:
            raise StaticFetchError("dns-no-ipv4")
        if len(answers) > MAX_ADDRESSES or not all(self._policy(address) for address in answers):
            raise StaticFetchError("address-blocked")
        return answers

    def _connect(self, host: str, addresses: list[str], budget: Budget) -> tuple[socket.socket, str]:
        for address in addresses[:MAX_CONNECT_TRIES]:
            remaining = budget.remaining()
            if remaining <= 0:
                raise StaticFetchError("budget")
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                raw.settimeout(min(CONNECT_SECONDS, remaining))
                raw.connect((address, self._port))
                peer = raw.getpeername()[0]
            except OSError:
                raw.close()
                continue
            if peer != address or not self._policy(peer):
                raw.close()
                raise StaticFetchError("peer-mismatch")
            return raw, address
        raise StaticFetchError("budget" if budget.remaining() <= 0 else "connect-failed")

    # --- the request -----------------------------------------------------------------------------------
    def get(self, url: str, budget: Budget, *, kinds: frozenset[str] = HTML_TYPES,
            max_bytes: int = MAX_PAGE_BYTES, accept: str = "text/html,application/xhtml+xml;q=0.9") -> Page:
        """The page at `url`, or StaticFetchError. Only a complete 200 response with an accepted type is a page."""
        parsed = parse_url(url)
        budget.take()
        self._pace(parsed.host, budget)
        addresses = self._resolve(parsed.host, budget)
        raw, _address = self._connect(parsed.host, addresses, budget)
        # the deadline is enforced from outside the blocking calls: at the deadline the connection is shut down, which
        # wakes a blocked handshake or read. The shutdown goes through a duplicate of the socket because wrap_socket
        # detaches the original object.
        try:
            guard = raw.dup()
        except OSError:
            raw.close()
            raise StaticFetchError("connect-failed") from None

        def expire() -> None:
            try:
                guard.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(max(budget.remaining(), 0.0), expire)
        timer.daemon = True
        timer.start()
        conn: http.client.HTTPSConnection | None = None
        try:
            try:
                raw.settimeout(max(budget.remaining(), 0.001))
                tls = self._context.wrap_socket(raw, server_hostname=parsed.host)
            except (socket.timeout, TimeoutError):
                raise StaticFetchError("budget" if budget.remaining() <= 0 else "timeout") from None
            except (ssl.SSLError, OSError):
                raise StaticFetchError("budget" if budget.remaining() <= 0 else "tls-failed") from None
            conn = http.client.HTTPSConnection(parsed.host, 443, context=self._context)
            conn.sock = tls
            try:
                tls.settimeout(max(budget.remaining(), 0.001))
                conn.request("GET", parsed.target, headers={
                    "User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "identity",
                    "Accept-Language": "en", "Connection": "close"})
                response = conn.getresponse()
                return self._read(response, tls, budget, kinds, max_bytes)
            except StaticFetchError:
                raise
            except (socket.timeout, TimeoutError):
                raise StaticFetchError("budget" if budget.remaining() <= 0 else "timeout") from None
            except (http.client.HTTPException, ValueError):
                raise StaticFetchError("budget" if budget.remaining() <= 0 else "protocol-error") from None
            except OSError:
                raise StaticFetchError("budget" if budget.remaining() <= 0 else "protocol-error") from None
        finally:
            timer.cancel()
            if conn is not None:
                conn.close()
            raw.close()
            guard.close()

    def _read(self, response: http.client.HTTPResponse, tls: socket.socket, budget: Budget,
              kinds: frozenset[str], max_bytes: int) -> Page:
        status = response.status
        if 300 <= status < 400:
            raise StaticFetchError("redirect")
        if status != 200:
            raise StaticFetchError({403: "http-403", 404: "http-404", 410: "http-410", 429: "http-429"}.get(
                status, "http-4xx" if 400 <= status < 500 else "http-5xx" if 500 <= status < 600 else "http-other"))
        ctype, _, params = (response.getheader("Content-Type") or "").partition(";")
        ctype = ctype.strip().lower()
        if ctype not in kinds:
            raise StaticFetchError("content-type")
        if (response.getheader("Content-Encoding") or "identity").strip().lower() not in ("", "identity"):
            raise StaticFetchError("content-encoding")
        length_header = response.getheader("Content-Length")
        length: int | None = None
        if length_header is not None:
            if not length_header.strip().isdigit():
                raise StaticFetchError("protocol-error")
            length = int(length_header.strip())
            if length > max_bytes:
                raise StaticFetchError("too-large")
        if length is None and not response.chunked:
            raise StaticFetchError("unframed")
        chunks: list[bytes] = []
        total = 0
        # http.client closes the connection itself once the body is complete, so the timeout is only set while it is open
        while not response.isclosed():
            if budget.remaining() <= 0:
                raise StaticFetchError("budget")
            try:
                tls.settimeout(max(budget.remaining(), 0.001))
                piece = response.read(READ_CHUNK)
            except http.client.IncompleteRead:
                raise StaticFetchError("budget" if budget.remaining() <= 0 else "truncated") from None
            if not piece:
                break
            total += len(piece)
            if total > max_bytes:
                raise StaticFetchError("too-large")
            chunks.append(piece)
        if length is not None and total != length:
            raise StaticFetchError("budget" if budget.remaining() <= 0 else "truncated")
        match = re.search(r"charset\s*=\s*\"?([A-Za-z0-9_.:-]+)", params)
        return Page(200, ctype, b"".join(chunks), match.group(1).lower() if match else "")


def decode(page: Page) -> str:
    """The body as text: the declared charset when Python knows it, else UTF-8; never raises."""
    for charset in (page.charset, "utf-8"):
        if not charset:
            continue
        try:
            return page.body.decode(charset, errors="replace")
        except LookupError:
            continue
    return page.body.decode("utf-8", errors="replace")
