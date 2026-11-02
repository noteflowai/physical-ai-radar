"""A robots.txt matcher for the static fetcher, following RFC 9309 for the one product token the radar sends.

Python's `urllib.robotparser` takes the first matching rule in file order, so `Allow: /` before `Disallow: /private`
lets `/private` through, and it reads only the first group that names an agent. This module does what the RFC says:

- groups: consecutive `User-agent` lines share the rules that follow; a `User-agent` line after rules starts a new
  group; every group that names our product token (case-insensitive) is merged; when none does, every `*` group is
  merged; with neither, everything is allowed. Rules before the first `User-agent` line are ignored, other directives
  (`Sitemap`, `Crawl-delay`, ...) are ignored and do not end a group.
- rules: `Allow` and `Disallow` paths with `*` (any run of characters) and a final `$` (end of the path). An empty
  `Disallow:` or `Allow:` is no rule. The path matched is the request path plus `?query`, after percent-encoding is
  normalised (unreserved characters decoded, the rest upper-cased, non-ASCII as UTF-8 octets).
- decision: the matching rule with the most octets wins; on a tie `Allow` wins; no matching rule allows.

What it will not guess: a file that cannot be read completely (invalid UTF-8, more than MAX_RULES rules, a rule longer
than MAX_PATTERN) raises RobotsUnsupported, and the caller treats the robots.txt as unreadable, which allows nothing.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

MAX_RULES = 5_000
MAX_PATTERN = 2_000
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")
_LINE_END = re.compile(r"\r\n|\r|\n")


class RobotsUnsupported(Exception):
    """The file cannot be judged completely; nothing may be inferred from it."""


def _normalise_once(path: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == "%" and i + 2 < len(path) and path[i + 1] in _HEX and path[i + 2] in _HEX:
            decoded = chr(int(path[i + 1:i + 3], 16))
            out.append(decoded if decoded in _UNRESERVED else "%" + path[i + 1:i + 3].upper())
            i += 3
        elif ord(ch) > 127:
            out.append("".join(f"%{byte:02X}" for byte in ch.encode("utf-8")))
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def normalise(path: str) -> str:
    """Percent-encoding in one canonical form: unreserved characters decoded, other escapes upper-cased, non-ASCII
    characters as UTF-8 octets. `*` and `$` pass through unchanged (an encoded `%2A` stays a literal asterisk).

    Decoding a character can complete an escape that was not one before (`%4%61` becomes `%4a`), so the pass is repeated
    until nothing changes; every change shortens the text, so this ends, and `normalise(normalise(x)) == normalise(x)`."""
    previous = None
    while path != previous:
        previous, path = path, _normalise_once(path)
    return path


def _match(pattern: str, target: str) -> bool:
    """Whether `pattern` (with `*` and a final `$`) matches the start of `target`. Leftmost search of each literal
    part, so the cost is bounded by len(target) * number of parts."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    parts = pattern.split("*")
    if len(parts) == 1:
        return target == pattern if anchored else target.startswith(pattern)
    if not target.startswith(parts[0]):
        return False
    position = len(parts[0])
    for part in parts[1:-1]:
        found = target.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    last = parts[-1]
    if anchored:
        return len(target) - len(last) >= position and target.endswith(last)
    return target.find(last, position) >= 0


@dataclass
class Rules:
    """The merged rules that apply to the radar: (is_allow, normalised pattern)."""

    rules: list[tuple[bool, str]] = field(default_factory=list)

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        target = normalise((parts.path or "/") + (("?" + parts.query) if parts.query else ""))
        best_length, best_allow = -1, True
        for is_allow, pattern in self.rules:
            if _match(pattern, target):
                length = len(pattern)
                if length > best_length or (length == best_length and is_allow):
                    best_length, best_allow = length, is_allow
        return best_allow


def parse(text: str, token: str) -> Rules:
    """The rules of `text` that apply to the product `token`. Raises RobotsUnsupported when the file cannot be judged
    completely."""
    token = token.lower()
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    current: tuple[list[str], list[tuple[bool, str]]] | None = None
    started = False
    total = 0
    for raw in _LINE_END.split(text.lstrip("\ufeff")):
        line = raw.split("#", 1)[0].strip()
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or started:
                current, started = ([], []), False
                groups.append(current)
            current[0].append(value.lower())
        elif key in ("allow", "disallow"):
            if current is None:
                continue
            started = True
            if not value:
                continue
            if len(value) > MAX_PATTERN:
                raise RobotsUnsupported("rule too long")
            total += 1
            if total > MAX_RULES:
                raise RobotsUnsupported("too many rules")
            current[1].append((key == "allow", normalise(value)))

    def ours(agent: str) -> bool:
        return agent == token or agent.startswith(token + "/")

    chosen = [group for group in groups if any(ours(agent) for agent in group[0])]
    if not chosen:
        chosen = [group for group in groups if "*" in group[0]]
    return Rules([rule for _, rules in chosen for rule in rules])
