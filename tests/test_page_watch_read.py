"""PR216 follow-ups N1 and N2 (04's review of 8cfa9731; kiro-main's rulings 00-pr216-n1n2 and 00-pr216-n1-ruling).

N1: `page_watch_status` can never be "ok" now that no entry uses Firecrawl (ADR 027: it is about the Firecrawl channel).
The flat keys `page_watch_read_status`, `page_watch_read_count` and `page_watch_failed_count` in the private freshness
evidence say what the run actually read, whichever fetcher read it, and leave `status` as it was.
N2: an entry a person switched off is listed in the health report with its reason, so it does not go unseen.
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from pairadar import cli, fetch, health

KEYS = {"page_watch_read_status", "page_watch_read_count", "page_watch_failed_count"}


def static_run(outcomes: dict[str, str], *, answered: bool = False, unavailable: bool = False) -> dict:
    """The merged block `write_page_watch_evidence` reads, built by the real PageWatchResult from per-source outcomes."""
    result = fetch.PageWatchResult(enabled=True, unavailable=unavailable, firecrawl_answered=answered)
    result.planned = list(outcomes)
    for source, outcome in outcomes.items():
        if outcome == "unreached":
            continue                                   # no note: the run never got to it
        fetcher = "static" if source.startswith("s-") else "firecrawl"
        result.note(source, fetcher, outcome, "" if outcome in ("checked", "observed") else "page-failed")
        if outcome in ("observed",):
            continue
        result.attempted.append(source)
        if outcome == "checked":
            result.checked.append(source)
            if fetcher == "static":
                result.static_checked.append(source)
        elif outcome == "robots":
            result.robots_skipped.append(source)
        else:
            result.failed.append(source)
    return {**result.summary(), **result.detail()}


class PageWatchReadTest(unittest.TestCase):
    def read(self, block, error=False):
        """(status, read, failed, reason or None) from the flat keys."""
        out = cli.page_watch_read(block, error=error)
        self.assertTrue(KEYS <= set(out), out)
        self.assertLessEqual(set(out), KEYS | {"page_watch_read_reason"}, out)
        self.assertIn(out["page_watch_read_status"], ("ok", "partial", "failed"))
        return (out["page_watch_read_status"], out["page_watch_read_count"], out["page_watch_failed_count"],
                out.get("page_watch_read_reason"))

    def test_three_static_pages_read_is_ok_while_the_firecrawl_status_stays_skipped(self):
        block = static_run({"s-figure": "checked", "s-1x": "checked", "s-unitree": "checked"})
        self.assertEqual(cli.page_watch_status(block), "skipped")                    # ADR 027: Firecrawl never answered
        self.assertEqual(self.read(block), ("ok", 3, 0, None))

    def test_some_read_is_partial_and_counts_every_way_of_not_being_read(self):
        block = static_run({"s-a": "checked", "s-b": "unavailable", "s-c": "blocked", "s-d": "robots",
                            "f-e": "not-attempted", "f-f": "unreached"})
        self.assertEqual(self.read(block), ("partial", 1, 5, None))

    def test_none_read_is_failed(self):
        block = static_run({"s-a": "unavailable", "s-b": "blocked"})
        self.assertEqual(self.read(block), ("failed", 0, 2, None))

    def test_a_firecrawl_page_read_counts_like_a_static_one(self):
        block = static_run({"f-a": "checked", "s-b": "checked"}, answered=True)
        self.assertEqual(cli.page_watch_status(block), "ok")
        self.assertEqual(self.read(block), ("ok", 2, 0, None))

    def test_a_source_only_observed_makes_no_claim_either_way(self):
        self.assertEqual(self.read(static_run({"s-a": "checked", "s-b": "observed"})), ("ok", 1, 0, None))
        # nothing was read, so it is failed, never ok, and the reason says why
        self.assertEqual(self.read(static_run({"s-a": "observed", "s-b": "observed"})), ("failed", 0, 0, "observed-only"))

    def test_a_planned_entry_with_no_note_is_not_read(self):
        block = {"planned": ["s-a", "s-b"], "sources": {"s-a": {"outcome": "checked"}}, "checked": ["s-a"]}
        self.assertEqual(self.read(block), ("partial", 1, 1, None))

    def test_a_malformed_note_is_not_read_and_never_raises(self):
        block = {"planned": ["s-a", "s-b", "s-c"], "sources": {"s-a": "checked", "s-b": {"outcome": None}, "s-c": {}}}
        self.assertEqual(self.read(block), ("failed", 0, 3, None))

    def test_no_enabled_entry_or_no_block_is_failed_with_a_reason_never_ok(self):
        for block in (None, {}, {"enabled": False}, {"enabled": True, "planned": []}, "not a block", []):
            with self.subTest(block=block):
                self.assertEqual(self.read(block), ("failed", 0, 0, "no-enabled-entry"))

    def test_a_run_that_raised_is_failed_whatever_the_block_says(self):
        block = static_run({"s-a": "checked"})
        self.assertEqual(self.read(block, error=True), ("failed", 0, 0, "run-raised"))

    def test_without_a_static_source_the_counts_come_from_checked_failed_and_robots(self):
        block = {"enabled": True, "attempted": ["a", "b", "c"], "checked": ["a"], "failed": ["b"], "robots_skipped": ["c"]}
        self.assertEqual(self.read(block), ("partial", 1, 2, None))
        self.assertEqual(self.read({"enabled": True, "checked": ["a", "b"]}), ("ok", 2, 0, None))

    def test_a_firecrawl_channel_that_stopped_is_never_ok_and_says_so(self):
        # no static source, so `planned` is absent and the entries Firecrawl did not reach cannot be counted
        stopped_part_way = {"enabled": True, "unavailable": True, "attempted": ["a"], "checked": ["a"]}
        self.assertEqual(self.read(stopped_part_way), ("partial", 1, 0, "firecrawl-unavailable"))
        down = {"enabled": True, "unavailable": True}
        self.assertEqual(self.read(down), ("failed", 0, 0, "firecrawl-unavailable"))

    def test_the_status_is_one_of_the_three_values_00_ruled_for_every_shape_of_block(self):
        blocks = [None, {}, {"enabled": True}, {"enabled": True, "unavailable": True}, static_run({"s-a": "checked"}),
                  static_run({"s-a": "observed"}), static_run({"s-a": "blocked"}), {"planned": "x"}, {"sources": []}]
        for block in blocks:
            for error in (False, True):
                with self.subTest(block=block, error=error):
                    self.read(block, error=error)                                  # asserts the shape and the enum

    def test_the_flat_keys_are_in_the_evidence_record_and_status_is_unchanged(self):
        block = static_run({"s-figure": "checked", "s-1x": "unavailable"})
        fetch_dict = {"page_watch": {key: block[key] for key in ("enabled", "attempted", "checked", "failed",
                                                                 "robots_skipped", "unavailable", "items")}}
        detail = {key: block[key] for key in ("firecrawl_answered", "planned", "sources", "static_checked")}
        with tempfile.TemporaryDirectory() as folder:
            path = cli.write_page_watch_evidence(fetch_dict, "2026-11-16", Path(folder), detail=detail)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((record["page_watch_read_status"], record["page_watch_read_count"],
                              record["page_watch_failed_count"]), ("partial", 1, 1))
            self.assertNotIn("page_watch_read", record)                              # no nested object
            self.assertNotIn("page_watch_read_reason", record)
            self.assertEqual(record["status"], "skipped")
            self.assertEqual((record["sources_checked"], record["sources_failed"]), (1, ["s-1x"]))
            error_path = cli.write_page_watch_evidence(None, "2026-11-17", Path(folder), error=True)
            error_record = json.loads(error_path.read_text(encoding="utf-8"))
            self.assertEqual(error_record["status"], "error")
            self.assertEqual((error_record["page_watch_read_status"], error_record["page_watch_read_count"],
                              error_record["page_watch_failed_count"], error_record["page_watch_read_reason"]),
                             ("failed", 0, 0, "run-raised"))


class SwitchedOffTest(unittest.TestCase):
    SOURCES = {
        "feeds": [
            {"id": "on", "url": "https://example.org/a"},
            {"id": "on-true", "url": "https://example.org/b", "enabled": True},
            {"id": "off-feed", "url": "https://example.org/c", "enabled": False, "disabled_reason": "robots says no"},
            {"id": "typo", "url": "https://example.org/d", "enabled": "false", "disabled_reason": "mistyped switch"},
            {"id": "null", "url": "https://example.org/e", "enabled": None},
            {"id": "no-reason", "url": "https://example.org/f", "enabled": False},
            {"id": "odd-reason", "url": "https://example.org/g", "enabled": 0, "disabled_reason": 7},
        ],
        "page_watch": [
            {"id": "page-on", "url": "https://example.org/p"},
            {"id": "page-off", "enabled": False, "disabled_reason": "reads failed"},
        ],
    }

    def test_lists_every_entry_whose_switch_is_present_and_not_exactly_true_with_its_reason(self):
        self.assertEqual(health.switched_off(self.SOURCES), [
            {"id": "off-feed", "disabled_reason": "robots says no"},
            {"id": "typo", "disabled_reason": "mistyped switch"},
            {"id": "null", "disabled_reason": ""},
            {"id": "no-reason", "disabled_reason": ""},
            {"id": "odd-reason", "disabled_reason": ""},
            {"id": "page-off", "disabled_reason": "reads failed"},
        ])

    def test_an_entry_with_no_switch_or_a_true_one_is_not_listed(self):
        listed = {entry["id"] for entry in health.switched_off(self.SOURCES)}
        self.assertFalse(listed & {"on", "on-true", "page-on"})

    def test_it_agrees_with_what_the_fetcher_reads(self):
        listed = {entry["id"] for entry in health.switched_off(self.SOURCES)}
        read = {entry["id"] for entry in fetch.enabled_feeds(self.SOURCES)}
        self.assertEqual(listed & {feed["id"] for feed in self.SOURCES["feeds"]},
                         {feed["id"] for feed in self.SOURCES["feeds"]} - read)

    def test_garbage_gives_an_empty_list_and_never_raises(self):
        for sources in ({}, {"feeds": None}, {"feeds": "x", "page_watch": 3}, {"feeds": [None, 1, {"id": 5, "enabled": False}]},
                        [], "text"):
            with self.subTest(sources=sources):
                self.assertEqual(health.switched_off(sources), [])

    def test_the_report_carries_it_and_a_switched_off_entry_is_not_a_failure(self):
        verdict = health.report([], date(2026, 11, 16))
        self.assertIn("switched_off", verdict)
        self.assertEqual(verdict["struggling"], [])

    def test_the_shipped_configuration_lists_physical_intelligence_with_a_reason(self):
        listed = {entry["id"]: entry["disabled_reason"] for entry in health.switched_off()}
        self.assertIn("physical-intelligence", listed)
        self.assertTrue(all(isinstance(reason, str) and reason for reason in listed.values()), listed)


if __name__ == "__main__":
    unittest.main()
