"""A robots.txt matcher for the static fetcher, following RFC 9309 for the one product token the radar sends.

Python's `urllib.robotparser` takes the first matching rule in file order, so `Allow: /` before `Disallow: /private`
lets `/private` through, and it reads only the first group that names an agent. This module does what the RFC says:

- groups: consecutive `User-agent` lines share the rules that follow; a `User-agent` line after rules starts a new
  group; every group that names our product token (case-insensitive) is merged; when none does, every `*` group is
  merged; with neither, everything is allowed. Rules before the first `User-agent` line are ignored, other directives
  (`Sitemap`, `Crawl-delay`, ...) are ignored and do not end a group.
- rules: `Allow` and `Disallow` paths with `*` (any run of characters) and a final `$` (end of the path). An empty
  `Disallow:` or `Allow:` is no rule. The path matched is the request path plus `?query`, after percent-encoding is
  normalised (unreserved characters decoded, the rest upper-cased, non-ASCII as UTF-8 octets). A `%` that is not
  followed by two hex digits is invalid: a rule with one makes the whole file unreadable.
  `*` and a final `$` are syntax only in the rule; an encoded `%2A` or `%24` in a rule is data and equals a literal `*`
  or `$` in the URL (RFC 9309 Figure 6), and never acts as a wildcard.
- decision: the matching rule with the most octets wins (counted in the same data form as the match); on a tie `Allow`
  wins; no matching rule allows.

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


def normalise(path: str) -> str:
    """Percent-encoding in one canonical form: an escape of an unreserved character is decoded, any other escape is
    upper-cased, and non-ASCII characters become UTF-8 octets. Nothing else changes, so `*`, `$`, `/` and `?` stay as
    written. A `%` that does not start a two-digit escape is not valid percent-encoding and raises RobotsUnsupported:
    nothing may be inferred from it (a rule with one makes the file unreadable; a URL with one is refused before it gets
    here). With every `%` an escape, decoding a character can never complete an escape that was not one before, and
    `normalise(normalise(x)) == normalise(x)`."""
    out: list[str] = []
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == "%":
            if not (i + 2 < len(path) and path[i + 1] in _HEX and path[i + 2] in _HEX):
                raise RobotsUnsupported("invalid percent-encoding")
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


_ESCAPE = re.compile(r"%([0-9A-F]{2})")


def _unit(byte: int) -> str:
    """One private-use character per escaped octet. Normalised text is pure ASCII, so nothing collides, and a search
    over the result can never start in the middle of an escape (`A` must not be found inside `%2A`)."""
    return chr(0xE000 + byte)


def _data(text: str) -> str:
    """A normalised string as DATA for comparison, every escaped octet one character. In a rule, `*` and a final `$`
    are syntax, and an encoded `%2A` or `%24` is data; in a URL, a literal `*` or `$` is data too (RFC 9309, Figure 6).
    So in the data form both are the same octet, and a rule split on its syntax characters compares equal to the URL
    however either side wrote them. Call it on the literal parts of a rule, never on the rule before it is split."""
    return _ESCAPE.sub(lambda match: _unit(int(match.group(1), 16)), text.replace("*", "%2A").replace("$", "%24"))


def _match(pattern: str, data: str) -> bool:
    """Whether `pattern` (a normalised rule with `*` and a final `$` as syntax) matches the start of `data` (the URL's
    path in data form). Leftmost search of each literal part, so the cost is bounded by len(data) * number of parts."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    parts = [_data(part) for part in pattern.split("*")]
    target = data
    if len(parts) == 1:
        return target == parts[0] if anchored else target.startswith(parts[0])
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


def specificity(pattern: str) -> int:
    """How many octets a normalised rule fixes, counted in the same data form the match uses: every literal octet
    (an escape counts as one, however it was written, and `%24` equals a `$` inside the rule), each `*` as one and a
    final `$` as one. Priority and matching therefore cannot disagree about two spellings of one rule."""
    anchored = pattern.endswith("$")
    parts = (pattern[:-1] if anchored else pattern).split("*")
    return sum(len(_data(part)) for part in parts) + (len(parts) - 1) + (1 if anchored else 0)


@dataclass
class Rules:
    """The merged rules that apply to the radar: (is_allow, normalised pattern)."""

    rules: list[tuple[bool, str]] = field(default_factory=list)

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        target = _data(normalise((parts.path or "/") + (("?" + parts.query) if parts.query else "")))
        best_length, best_allow = -1, True
        for is_allow, pattern in self.rules:
            if _match(pattern, target):
                length = specificity(pattern)
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
