"""Scoring keyword lane classification against a labeled file (pairadar.lanes --labels).

SYNTHETIC FIXTURES: every labeled case below is built by the test from keywords of the
lanes in data/taxonomy.json, and its label is chosen by the test. The cases exercise the
report and its error handling; they say nothing about how well the taxonomy files real
radar items.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from pairadar import lanes
from pairadar.config import Item, load_config
from pairadar.distill import classify

NONSENSE = "Qzxv wlorp"
GROUPS = ("all", "confident", "flagged")


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = lanes.main(argv)
    return code, out.getvalue(), err.getvalue()


def text_of(entry: dict) -> str:
    return f"{entry['title']}. {entry.get('summary', '')}"


class LabeledAgreementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.lane_ids = [lane["id"] for lane in self.config.lanes]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def confident_text(self, skip: set) -> tuple[str, str]:
        """A run of one lane's own keywords that it wins clearly, with no flag."""
        for lane in self.config.lanes:
            if lane["id"] in skip:
                continue
            keywords = lane["keywords"]
            for start in range(len(keywords)):
                text = ", ".join(keywords[start:start + 3])
                best, runner_up = lanes.ranked_lanes(f"{text}. ", self.config)[:2]
                clear = best[2] - runner_up[2] > lanes.MARGIN or runner_up[1] == 0
                if best[0] == lane["id"] and best[1] >= 2 and clear:
                    return lane["id"], text
        self.fail("no lane in the taxonomy wins its own keywords clearly")

    def write(self, payload, name: str = "my-labels.json") -> Path:
        path = self.dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def build_cases(self):
        lane_a, text_a = self.confident_text(set())
        lane_b, text_b = self.confident_text({lane_a})
        zero = lanes.ranked_lanes(f"{NONSENSE}. ", self.config)
        self.assertTrue(all(hits == 0 for _, hits, _ in zero), "the nonsense title must match no keyword")
        fallback = zero[0][0]
        wrong = next(lane_id for lane_id in self.lane_ids if lane_id != fallback)
        items = [
            {"id": "c1", "title": text_a, "lane": lane_a},                       # confident, agrees
            {"id": "c2", "title": NONSENSE, "summary": text_b, "lane": lane_b},  # confident, agrees
            {"title": text_a, "lane": lane_b},                                   # confident misfile, no id
            {"id": "t1", "title": NONSENSE, "lane": wrong},                      # thin misfile
            {"id": "t2", "title": NONSENSE, "summary": "", "lane": fallback},    # thin, agrees
        ]
        for entry in items[:3]:
            best, runner_up = lanes.ranked_lanes(text_of(entry), self.config)[:2]
            self.assertGreaterEqual(best[1], 2)
            self.assertTrue(best[2] - runner_up[2] > lanes.MARGIN or runner_up[1] == 0)
        return items, lane_a, lane_b, fallback, wrong

    def test_agreement_is_split_into_confident_and_flagged(self) -> None:
        items, _, _, _, _ = self.build_cases()
        path = self.write({"items": items})
        before = path.read_bytes()
        code, out, err = run(["--labels", str(path)])
        self.assertEqual((code, err), (0, ""))
        verdict = json.loads(out)
        self.assertEqual(verdict["labels"], "my-labels.json")
        self.assertEqual(verdict["labeled"], 5)
        self.assertEqual(verdict["margin"], lanes.MARGIN)
        self.assertEqual(verdict["agreement"], {
            "all": {"n": 5, "agree": 3, "rate": 0.6},
            "confident": {"n": 3, "agree": 2, "rate": 0.667},
            "flagged": {"n": 2, "agree": 1, "rate": 0.5},
        })
        self.assertNotIn(self.tmp.name, out)
        self.assertNotIn(str(path.resolve()), out)
        self.assertEqual(path.read_bytes(), before, "the label file must never be written")

    def test_disagreements_name_the_misfiles_the_flags_missed(self) -> None:
        items, lane_a, lane_b, fallback, wrong = self.build_cases()
        code, out, _ = run(["--labels", str(self.write({"items": items}))])
        self.assertEqual(code, 0)
        missed, thin = json.loads(out)["disagreements"]
        keys = {"id", "title", "label", "predicted", "reasons", "best", "runner_up"}
        self.assertEqual(set(missed), keys)
        self.assertEqual(set(thin), keys)
        self.assertIsNone(missed["id"])
        self.assertEqual((missed["title"], missed["label"], missed["predicted"]), (items[2]["title"], lane_b, lane_a))
        self.assertEqual(missed["reasons"], [])
        self.assertEqual((thin["id"], thin["label"], thin["predicted"]), ("t1", wrong, fallback))
        self.assertIn("thin", thin["reasons"])
        for finding, source in ((missed, items[2]), (thin, items[3])):
            self.assertEqual(finding["predicted"], classify(text_of(source), self.config)[0])
            self.assertEqual(finding["best"]["lane"], finding["predicted"])
            self.assertEqual(set(finding["best"]), {"lane", "hits", "score"})
            self.assertEqual(set(finding["runner_up"]), {"lane", "hits", "score"})
        self.assertEqual(thin["best"]["hits"], 0)

    def test_an_empty_label_file_reports_no_rates(self) -> None:
        path = self.write({"items": []}, "empty.json")
        code, out, err = run(["--labels", str(path), "--margin", "0.5"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {
            "labels": "empty.json", "labeled": 0, "margin": 0.5,
            "agreement": {group: {"n": 0, "agree": 0, "rate": None} for group in GROUPS},
            "disagreements": [],
        })


class WithoutLabelsTest(unittest.TestCase):
    def test_the_published_report_is_unchanged(self) -> None:
        expected = lanes.report(lanes.published_picks(lanes.LATEST_PATH), load_config(), lanes.MARGIN)
        code, out, err = run([])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, json.dumps(expected, ensure_ascii=False, indent=2) + "\n")

    def test_a_snapshot_with_a_thin_pick_still_fails_when_asked(self) -> None:
        pick = {"id": "x", "title": NONSENSE, "url": "https://example.org/x"}
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "latest.json"
            snapshot.write_text(json.dumps({"picked": [pick]}), encoding="utf-8")
            code, out, err = run(["--latest", str(snapshot), "--fail-on-suspicious"])
        self.assertEqual((code, err), (1, ""))
        verdict = json.loads(out)
        self.assertEqual(verdict, lanes.report([Item.from_dict(pick)], load_config()))
        [finding] = verdict["suspicious"]
        self.assertEqual(set(finding), {"id", "title", "url", "lane", "reasons", "best", "runner_up"})
        self.assertIn("thin", finding["reasons"])


class LabelErrorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lane_ids = [lane["id"] for lane in load_config().lanes]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.valid = {"id": "ok", "title": NONSENSE, "lane": self.lane_ids[0]}

    def assertRefused(self, argv: list[str], fragment: str) -> None:
        code, out, err = run(argv)
        self.assertEqual(code, 2)
        self.assertEqual(out, "", "no partial report may be printed")
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("lanes: "), lines[0])
        self.assertIn(fragment, lines[0])

    def refuse(self, content, fragment: str) -> None:
        path = self.dir / "labels.json"
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content), encoding="utf-8")
        self.assertRefused(["--labels", str(path)], fragment)

    def test_unreadable_files(self) -> None:
        self.assertRefused(["--labels", str(self.dir / "nope.json")], "cannot read")
        self.refuse("{", "is not valid JSON")
        self.refuse(b"\xff\xfe{}", "is not UTF-8")

    def test_a_document_without_an_items_list(self) -> None:
        self.refuse({"entries": []}, '"items" list')
        self.refuse([], '"items" list')
        self.refuse({"items": {}}, '"items" list')

    def test_bad_entries_name_their_index(self) -> None:
        self.refuse({"items": [self.valid, 3]}, "item 2 is not an object")
        self.refuse({"items": [{"lane": self.lane_ids[0]}]}, "item 1: title must be a non-empty string")
        self.refuse({"items": [{"title": "", "lane": self.lane_ids[0]}]}, "item 1: title must be a non-empty string")
        self.refuse({"items": [self.valid, {"title": "x", "lane": "  "}]}, "item 2: lane must be a non-empty string")
        self.refuse({"items": [{"title": "x", "lane": 5}]}, "item 1: lane must be a non-empty string")
        self.refuse({"items": [dict(self.valid, id=7)]}, "item 1: id must be a string")
        self.refuse({"items": [dict(self.valid, summary=None)]}, "item 1: summary must be a string")
        self.refuse({"items": [dict(self.valid, summary=["a"])]}, "item 1: summary must be a string")

    def test_an_unknown_lane_lists_the_valid_ones(self) -> None:
        self.refuse({"items": [dict(self.valid, lane="nope")]},
                    'item 1: unknown lane "nope"; valid lanes: ' + ", ".join(self.lane_ids))

    def test_labels_combine_only_with_margin(self) -> None:
        path = self.dir / "labels.json"
        path.write_text(json.dumps({"items": [self.valid]}), encoding="utf-8")
        self.assertRefused(["--labels", str(path), "--fail-on-suspicious"],
                           "lanes: --labels cannot be combined with --fail-on-suspicious")
        self.assertRefused(["--labels", str(path), "--latest", str(path)],
                           "lanes: --labels cannot be combined with --latest")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
