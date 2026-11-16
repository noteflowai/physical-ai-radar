"""Which hosts the radar must not request, decided before a request is sent.

A `page_watch` entry a person has switched off (`enabled` present and not true, with a `disabled_reason`) stops the radar
asking its host for anything: not the page, not robots.txt, not a feed or an API that happens to redirect there. Switching
the entry off removes the page from the plan, but every reader that follows redirects (`fetch.http_get` for feeds, robots.txt,
arXiv and the attention APIs) would still send a request to it if some other host answered with a redirect. This module is
the one place that says whether a URL may be requested; `fetch.http_get` asks it before the first request and before every
redirect hop, and `fetch.fetch_page_watch` before a page or its robots.txt is requested.

What is blocked: the host of each switched-off page_watch entry's `url` and each name in its `hosts`, and every subdomain of
them (`www.pi.website` and `api.pi.website` for `pi.website`; `notpi.website` and `pi.website.example` are other hosts).
A switched-off feed is not blocked by host: its reason is often about one path (github.com disallows `/*.atom$`, not the
host). A name is not blocked while anything that is switched on uses it or a parent or child domain of it (a feed on the
same host keeps working); the entry is then off, but its host is not.

The authority is `data/sources.json`, read here and nowhere cached beyond its modification time. If it cannot be read or
makes no sense, nothing is requested (an unknown policy is not an empty one). Names are compared lower-case, without a
trailing dot, in their IDNA (ASCII) form. This does not resolve DNS: an address literal or a different name for the same
server is not recognised, and a redirect to one is not stopped here.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import DATA_DIR

SOURCES_PATH = DATA_DIR / "sources.json"

UNREADABLE = "host-policy-unreadable"
SWITCHED_OFF = "host-switched-off"
UNPARSEABLE = "url-unparseable"

_CACHE: dict[str, Any] = {}


class HostPolicyUnreadable(Exception):
    """sources.json cannot be read or is not the shape the policy needs."""


def canonical_host(raw: Any) -> str | None:
    """A host name as the policy compares it, or None when it is not a plain DNS name."""
    if not isinstance(raw, str):
        return None
    name = raw.strip().rstrip(".").lower()
    if not name or any(char in name for char in " /\\@:?#[]%\x00\t\r\n"):
        return None
    try:
        ascii_name = name.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return ascii_name.lower().rstrip(".") or None


def _url_host(url: Any) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        return canonical_host(urllib.parse.urlsplit(url).hostname)
    except ValueError:
        return None


def _entry_hosts(entry: dict[str, Any], standards: dict[Any, Any]) -> set[str]:
    url = entry.get("url")
    if not url and entry.get("standards_watch") in standards:
        url = (standards[entry["standards_watch"]] or {}).get("url")
    found = {_url_host(url)}
    aliases = entry.get("hosts")
    if isinstance(aliases, list):
        found.update(canonical_host(alias) for alias in aliases)
    return {host for host in found if host}


def _related(a: str, b: str) -> bool:
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def blocked_hosts(sources: Any) -> frozenset[str]:
    """The names blocked by `sources` (the parsed data/sources.json). Raises HostPolicyUnreadable on a shape it cannot read."""
    if not isinstance(sources, dict):
        raise HostPolicyUnreadable("sources.json is not an object")
    groups = {}
    for key in ("feeds", "page_watch", "standards_watch"):
        value = sources.get(key, [])
        if not isinstance(value, list):
            raise HostPolicyUnreadable(f"{key} is not a list")
        groups[key] = [entry for entry in value if isinstance(entry, dict)]
    standards = {watch.get("id"): watch for watch in groups["standards_watch"]}
    off: set[str] = set()
    on: set[str] = set()
    for entry in groups["page_watch"]:
        if "enabled" in entry and entry["enabled"] is not True:
            off |= _entry_hosts(entry, standards)
        else:
            on |= _entry_hosts(entry, standards)
    for feed in groups["feeds"]:
        if feed.get("enabled", True) is True:
            on |= _entry_hosts(feed, standards)
    return frozenset(host for host in off if not any(_related(host, other) for other in on))


def load(path: Path | None = None) -> frozenset[str]:
    """The blocked names from data/sources.json. Re-read when the file changes; raises HostPolicyUnreadable."""
    path = path or SOURCES_PATH
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if _CACHE.get("key") == key:
            return _CACHE["blocked"]
        blocked = blocked_hosts(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        _CACHE.clear()
        raise HostPolicyUnreadable(f"{path.name}: {error}") from error
    except HostPolicyUnreadable:
        _CACHE.clear()
        raise
    _CACHE.update(key=key, blocked=blocked)
    return blocked


def is_blocked_name(host: str, blocked: frozenset[str]) -> bool:
    return any(host == name or host.endswith("." + name) for name in blocked)


def refusal(url: Any, path: Path | None = None, blocked: frozenset[str] | None = None) -> str | None:
    """Why `url` must not be requested, or None when it may be.

    `blocked` is a policy already read (the caller holds the configuration it is running with); without it the policy is
    read from data/sources.json. The host is read the two ways urllib reads it (`urlsplit` and the `Request` it would
    send), and the answer is "refuse" if either is blocked or if the two do not agree: the check and the connection must
    name the same host.
    """
    if blocked is None:
        try:
            blocked = load(path)
        except HostPolicyUnreadable:
            return UNREADABLE
    if not isinstance(url, str):
        return UNPARSEABLE
    try:
        from_split = canonical_host(urllib.parse.urlsplit(url).hostname)
        # the host urllib would connect to: `Request.host` is "name:port", read without the port or userinfo
        from_request = canonical_host(urllib.parse.urlsplit("//" + urllib.request.Request(url).host).hostname)
    except ValueError:
        return UNPARSEABLE
    if not from_split or not from_request or from_split != from_request:
        return UNPARSEABLE
    return SWITCHED_OFF if is_blocked_name(from_split, blocked) else None
