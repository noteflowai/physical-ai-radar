"""Claim-audit worksheet (RA-01): python3 -m pairadar.claims over a feed.json store.

Every store here is SYNTHETIC: labeled fixtures built with feeds.record in the real
radar/feed.json format, or hand-edited _radar dicts. They exercise the worksheet's
behavior; they say nothing about any real source's accuracy. No test touches the network.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import claims, feeds  # noqa: E402
from pairadar.config import Item, md_text, md_url  # noqa: E402
from pairadar.distill import number_context  # noqa: E402

PUBLISHED = "2026-03-12T01:40:00Z"
LEAD = "The policy reached 92% success on real robots."
FILLER = "The setup used a single arm and a wrist camera on a cluttered table. " * 5
ROW = re.compile(r"^\| \d+ \|")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")


def item(ident, title, summary, numbers, source_id="blog", evidence="R"):
    return Item(id=ident, title=title, url=f"https://example.org/{ident}", publisher="Example",
                source_id=source_id, evidence=evidence, published="2026-03-01",
                summary=summary, numbers=list(numbers))


def paper():
    """92% is in the excerpt; 61% and 73% sit in summary text one_liner cuts off."""
    return item("syn:a", "Synthetic grasping policy",
                LEAD + " " + FILLER + "Ablations cost 61% and 73% of the gain.", ["92%", "61%", "73%"])


def titled():
    return item("syn:b", "Humanoid hits 40 Hz whole-body control",
                "A new controller for humanoids is described.", ["40 Hz"], evidence="O")


def baseline():
    return item("syn:c", "EgoScale egocentric dataset", "Large egocentric set with 20,854 hours.",
                ["20,854 hours"], source_id="baseline", evidence="M")


def older():
    return item("syn:d", "Clutter grasping study", "Grasp success improved by 18% in clutter.", ["18%"])


def radar(*pairs):
    return [{"_radar": feeds.record(it, day, PUBLISHED)} for it, day in pairs]


def write(directory: Path, items, name="feed.json") -> Path:
    path = directory / name
    path.write_text(json.dumps({"version": feeds.JSON_FEED_VERSION, "items": items}), encoding="utf-8")
    return path


def run_main(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = claims.main([str(arg) for arg in args])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "pairadar.claims", *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True)


def table_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if ROW.match(line)]


class Temp(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def three_day(self) -> Path:
        return write(self.dir, radar((paper(), "2026-03-12"), (titled(), "2026-03-12"),
                                     (baseline(), "2026-03-11"), (older(), "2026-03-10")))


class WorksheetTest(Temp):
    def test_statuses_follow_the_stored_title_and_excerpt(self) -> None:
        entries, skipped = claims.read_store(self.three_day())
        self.assertEqual(skipped, 0)
        by_id = {entry["id"]: entry for entry in entries}
        self.assertNotIn("61%", by_id["syn:a"]["excerpt"])  # the fixture really truncates
        self.assertEqual(by_id["syn:c"]["excerpt"], "")
        rows = claims.audit(entries, 7)
        self.assertEqual([(row["day"], row["claim"], row["status"]) for row in rows], [
            ("2026-03-12", "92%", "quoted"), ("2026-03-12", "61%", "unlocated"),
            ("2026-03-12", "73%", "unlocated"), ("2026-03-12", "40 Hz", "title"),
            ("2026-03-11", "20,854 hours", "no-excerpt"), ("2026-03-10", "18%", "quoted")])
        for row in rows:
            stored = by_id[row["id"]]
            if row["status"] == "quoted":
                self.assertEqual(row["context"], number_context(row["claim"], stored["excerpt"]))
                # the clause is quoted from the stored excerpt, not paraphrased
                self.assertIn(row["context"], stored["excerpt"])
            elif row["status"] == "title":
                self.assertEqual(row["context"], number_context(row["claim"], stored["title"]))
                self.assertIn(row["context"], stored["title"])
            else:
                self.assertEqual(row["context"], "")
            if row["context"]:
                self.assertIn(row["claim"], row["context"])
        self.assertEqual([row["claim"] for row in claims.audit(entries, 1)][-1], "20,854 hours")

    def test_cli_sheet_is_deterministic_and_uses_the_newest_entry_date(self) -> None:
        path = self.three_day()
        first = run_cli("--store", path, "--days", 7, "--limit", 30)
        second = run_cli("--store", path, "--days", 7, "--limit", 30)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("reference day 2026-03-12; days 7; figures in window 6; rows shown 6; "
                      "skipped entries 0", first.stdout)
        self.assertEqual(len(table_rows(first.stdout)), 6)
        self.assertNotIn("Showing", first.stdout)
        self.assertEqual(run_main("--store", path, "--days", 7, "--limit", 30)[1], first.stdout)

    def test_truncation_keeps_whole_window_totals_and_a_working_rerun(self) -> None:
        path = write(self.dir, radar((paper(), "2026-03-12"), (titled(), "2026-03-12"),
                                     (baseline(), "2026-03-11")))
        code, out, err = run_main("--store", path, "--limit", 2)
        self.assertEqual(code, 0, err)
        self.assertIn("figures in window 5; rows shown 2;", out)
        self.assertIn("Status counts (whole window): quoted 1; title 1; no-excerpt 1; unlocated 2", out)
        self.assertIn("Showing 2 of 5 figures; 3 not shown (1 unlocated, 1 no-excerpt among them). "
                      "Rerun with --limit 5 to list all.", out)
        self.assertEqual(len(table_rows(out)), 2)
        suggested = re.search(r"Rerun with --limit (\d+)", out).group(1)
        code, out, err = run_main("--store", path, "--limit", suggested)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(table_rows(out)), 5)
        self.assertNotIn("Showing", out)
        self.assertEqual(run_main("--store", path, "--limit", 1000)[0], 0)

    def test_hostile_text_stays_inside_its_cell(self) -> None:
        title, url = "Arm ] ( < {{ | test", "https://example.org/a_(b)"
        entry = {"date": "2026-03-12", "id": "syn:md", "url": url, "title": title,
                 "evidence": "O|x\ny", "published": "2026-03-01",
                 "excerpt": "Success rose\nto 92% overall | beyond.", "numbers": ["92%"]}
        code, out, err = run_main("--store", write(self.dir, [{"_radar": entry}]))
        self.assertEqual(code, 0, err)
        rows = table_rows(out)
        self.assertEqual(len(rows), 1)
        header = next(line for line in out.splitlines() if line.startswith("| #"))
        width = len(UNESCAPED_PIPE.split(header))
        self.assertEqual(width, len(claims.COLUMNS) + 2)
        self.assertEqual(len(UNESCAPED_PIPE.split(rows[0])), width)
        self.assertIn("[" + md_text(title).replace("|", "\\|") + "](" + md_url(url) + ")", rows[0])
        self.assertNotIn("{{", out)
        self.assertEqual(claims.cell("a\nb|c\r\nd"), "a b\\|c d")
        raw = claims.audit([entry], 7)[0]
        self.assertEqual((raw["title"], raw["evidence"], raw["url"]), (title, "O|x\ny", url))
        self.assertEqual(raw["status"], "quoted")
        self.assertIn("92%", raw["context"])
        # number_context joins words with single spaces, so a clause taken from text
        # with a line break is found in the whitespace-normalised excerpt.
        self.assertIn(raw["context"], " ".join(entry["excerpt"].split()))

    def test_a_window_without_figures_says_so(self) -> None:
        quiet = item("syn:e", "A robot post", "No figures here.", [])
        code, out, err = run_main("--store", write(self.dir, radar((quiet, "2026-03-12"))))
        self.assertEqual(code, 0, err)
        self.assertIn("figures in window 0; rows shown 0;", out)
        self.assertIn(claims.EMPTY, out)
        self.assertEqual(table_rows(out), [])


class MalformedStoreTest(Temp):
    def test_malformed_items_are_skipped_and_counted(self) -> None:
        good = feeds.record(item("syn:f", "Policy study", LEAD, ["92%"]), "2026-03-12", PUBLISHED)
        null_title = {"date": "2026-03-11", "id": "syn:null", "title": None, "url": None,
                      "evidence": None, "excerpt": None, "numbers": ["40 Hz", 7]}
        items = [{"_radar": good}, {"_radar": null_title}, {"_radar": {"id": "n", "numbers": ["5%"]}},
                 {"_radar": {"date": "yesterday", "numbers": ["5%"]}},
                 {"_radar": {"date": "20260930", "numbers": ["5%"]}}, {"id": "plain"}, "junk"]
        path = write(self.dir, items)
        result = run_cli("--store", path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("reference day 2026-03-12;", result.stdout)
        self.assertIn("skipped entries 5", result.stdout)
        entries, skipped = claims.read_store(path)
        self.assertEqual(skipped, 5)
        rows = claims.audit(entries, 7)
        self.assertEqual([(row["claim"], row["status"]) for row in rows],
                         [("92%", "quoted"), ("40 Hz", "no-excerpt")])
        self.assertEqual((rows[1]["title"], rows[1]["url"]), ("", ""))
        self.assertEqual(len(table_rows(result.stdout)), 2)

    def test_store_problems_exit_2_with_one_actionable_line(self) -> None:
        version = {"version": "https://jsonfeed.org/version/1", "items": []}
        undated = {"version": feeds.JSON_FEED_VERSION, "items": [{"_radar": {"date": "yesterday"}}]}
        cases = {
            "absent.json": (None, ["store not found", "--store"]),
            "broken.json": ("{not json", ["invalid JSON"]),
            "list.json": ("[]", ["not a JSON Feed store"]),
            "version.json": (json.dumps(version), [feeds.JSON_FEED_VERSION, "https://jsonfeed.org/version/1\""]),
            "undated.json": (json.dumps(undated), ["no dated radar entries"]),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name=name):
                path = self.dir / name
                if text is not None:
                    path.write_text(text, encoding="utf-8")
                before = sorted(p.name for p in self.dir.iterdir())
                result = run_cli("--store", path)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                lines = result.stderr.strip().splitlines()
                self.assertEqual(len(lines), 1, result.stderr)
                self.assertTrue(lines[0].startswith("claims: "), lines[0])
                for fragment in expected:
                    self.assertIn(fragment, lines[0])
                self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)

    def test_out_of_range_arguments_are_usage_errors(self) -> None:
        path = self.three_day()
        for args in (["--days", "0"], ["--days", "31"], ["--limit", "0"], ["--days", "x"]):
            with self.subTest(args=args):
                code, out, err = run_main("--store", path, *args)
                self.assertEqual(code, 2)
                self.assertIn("usage:", err)
                self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
