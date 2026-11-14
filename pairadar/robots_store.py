"""A small private record of robots.txt answers, kept between runs so a host is asked less often.

Why: from 2026-11-12 `www.pi.website` answered the robots.txt request of every run with 429. The radar asked for it three
times a day (the 07:40 round and the two night rounds), each time from nothing. This keeps two kinds of fact, and nothing
else:

- a robots.txt that WAS read (a 200, or a 404/410 meaning "none"), for at most 24 hours (RFC 9309, section 2.4: a crawler
  should not use a cached copy for longer). Within that time the file is not asked for again;
- a 429 together with the wait it asked for (`Retry-After`, capped at a day; six hours when it gave none). Until then the
  host is not asked again, and the source stays "unreadable" exactly as it was when it was asked.

What it never does: an answer that could not be read is never replaced by an older one. A cached file is used only while it
is fresh, before any request, never after a failed one; an expired entry is ignored, not used as a fallback. Nothing here
can make a refused page readable. If the file is missing, unreadable, from the wrong user, too loose in its mode, has a
repeated key or any field that is not exactly what this module writes, the whole file is ignored and the host is asked,
which is the behaviour without this module.

Only a production round writes (`writable`); `--out` runs and previews read and never touch the state directory.
The file holds the text of robots.txt (public) and times; no URL, no page text, no credential.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = "pairadar-robots-state-v1"
FILE = "robots-state.json"
MAX_AGE = timedelta(hours=24)
DEFAULT_BACKOFF = timedelta(hours=6)
MAX_BACKOFF = timedelta(hours=24)
MIN_BACKOFF = timedelta(minutes=1)
SKEW = timedelta(minutes=5)
MAX_ORIGINS = 16
MAX_BODY_CHARS = 512_000
MAX_FILE_BYTES = 8_000_000
BACKOFF_REASONS = frozenset({"http-429"})
_ORIGIN = re.compile(r"^https://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


def _time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("time")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("time zone")
    return moment.astimezone(timezone.utc)


def _strict(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate key")
        return dict(items)

    def refuse(token: str) -> None:
        raise ValueError(token)
    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=refuse)


def _validate(entry: object) -> None:
    if not isinstance(entry, dict) or entry.get("kind") not in ("rules", "none", "backoff"):
        raise ValueError("entry")
    kind = entry["kind"]
    if kind == "rules":
        if set(entry) != {"kind", "fetched_at", "body"} or not isinstance(entry["body"], str) \
                or len(entry["body"]) > MAX_BODY_CHARS:
            raise ValueError("rules entry")
        _time(entry["fetched_at"])
    elif kind == "none":
        if set(entry) != {"kind", "fetched_at"}:
            raise ValueError("none entry")
        _time(entry["fetched_at"])
    else:
        if set(entry) != {"kind", "reason", "recorded_at", "until"} or entry["reason"] not in BACKOFF_REASONS:
            raise ValueError("backoff entry")
        recorded, until = _time(entry["recorded_at"]), _time(entry["until"])
        if not recorded < until <= recorded + MAX_BACKOFF:
            raise ValueError("backoff span")


class RobotsStore:
    """lookup(origin) -> ("rules", text | None) | ("backoff", reason) | None; record_rules; record_backoff."""

    def __init__(self, directory: Path, *, writable: bool,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.directory = Path(directory)
        self.writable = writable
        self._clock = clock
        self._origins: dict[str, dict[str, Any]] | None = None

    # --- reading ---------------------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self.directory / FILE

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._origins is not None:
            return self._origins
        self._origins = {}
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return self._origins                                       # no file, a symlink, unreadable: ask the host
        try:
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 \
                        or info.st_size > MAX_FILE_BYTES:
                    return self._origins
                record = _strict(stream.read(MAX_FILE_BYTES + 1))
            if not isinstance(record, dict) or set(record) != {"schema", "origins"} or record["schema"] != SCHEMA \
                    or not isinstance(record["origins"], dict) or len(record["origins"]) > MAX_ORIGINS:
                return self._origins
            for origin, entry in record["origins"].items():
                if not isinstance(origin, str) or not _ORIGIN.fullmatch(origin):
                    return self._origins
                _validate(entry)
            self._origins = dict(record["origins"])
        except (OSError, ValueError, UnicodeDecodeError):
            self._origins = {}                                         # one bad field spoils the whole file
        return self._origins

    def lookup(self, origin: str) -> tuple[str, Any] | None:
        entry = self._load().get(origin)
        if entry is None:
            return None
        now = self._clock()
        try:
            if entry["kind"] == "backoff":
                recorded, until = _time(entry["recorded_at"]), _time(entry["until"])
                if recorded - SKEW <= now < until:
                    return "backoff", entry["reason"]
                return None
            fetched = _time(entry["fetched_at"])
        except ValueError:
            return None
        if not fetched - SKEW <= now < fetched + MAX_AGE:               # expired, or from the future: never used
            return None
        return "rules", (entry["body"] if entry["kind"] == "rules" else None)

    # --- writing ---------------------------------------------------------------------------------
    def record_rules(self, origin: str, body: str | None) -> None:
        """A robots.txt that was read (its text) or is absent for good (None, from a 404 or 410)."""
        stamp = self._clock().astimezone(timezone.utc).isoformat()
        entry = {"kind": "none", "fetched_at": stamp} if body is None \
            else {"kind": "rules", "fetched_at": stamp, "body": body}
        self._put(origin, entry)

    def record_backoff(self, origin: str, reason: str, retry_after: int | None) -> None:
        """The host asked to be left alone (429): until `Retry-After`, or DEFAULT_BACKOFF when it said nothing."""
        if reason not in BACKOFF_REASONS:
            return
        wait = DEFAULT_BACKOFF if retry_after is None else timedelta(seconds=retry_after)
        wait = max(MIN_BACKOFF, min(wait, MAX_BACKOFF))
        now = self._clock().astimezone(timezone.utc)
        self._put(origin, {"kind": "backoff", "reason": reason, "recorded_at": now.isoformat(),
                           "until": (now + wait).isoformat()})

    def _put(self, origin: str, entry: dict[str, Any]) -> None:
        if not self.writable or not _ORIGIN.fullmatch(origin):
            return
        try:
            _validate(entry)
            origins = dict(self._load())
            origins[origin] = entry
            if len(origins) > MAX_ORIGINS:
                oldest = sorted(origins, key=lambda key: _time(origins[key].get("fetched_at") or origins[key]["recorded_at"]))
                for key in oldest[:len(origins) - MAX_ORIGINS]:
                    del origins[key]
            self._write({"schema": SCHEMA, "origins": origins})
            self._origins = origins
        except (OSError, ValueError, KeyError):
            print("[fetch] robots state not written")                    # a convenience: its loss changes nothing else

    def _write(self, record: dict[str, Any]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("robots state directory must be private and owned")
        descriptor, name = tempfile.mkstemp(prefix=".robots-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True, indent=1)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(name)
