"""The validator the nightly job hands back to the drafting agent.

Each problem must name the item and say what to change, because the message goes to
a model as its only instruction for the fix. No test here touches the network.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pairadar import notes  # noqa: E402

DAY = "2026-09-23"
PAPER = "https://arxiv.org/abs/2609.11111"
ROBOT = "https://spectrum.ieee.org/blood-draw-robot-vitestro-aletta"
LINE = {"zh": "值得跟进。", "en": "Worth following.", "ja": "追う価値がある。"}


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/"data"/"notes").mkdir(parents=True)
        (self.root/"radar"/"daily").mkdir(parents=True)
        picked = [{"url": PAPER, "title": "A paper", "source_id": "arxiv"},
                  {"url": ROBOT, "title": "Blood-draw robot", "source_id": "ieee-spectrum"},
                  {"url": "https://example.org/curated", "title": "Curated", "source_id": "baseline"}]
        (self.root/"radar"/"latest.json").write_text(json.dumps({"date": DAY, "picked": picked}))
        for lang in ("zh", "en", "ja"):
            (self.root/"radar"/"daily"/f"{DAY}.{lang}.md").write_text("Success rose to `87%` over 1,200 draws.")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, document) -> None:
        text = document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
        notes.notes_path(DAY, self.root).write_text(text, encoding="utf-8")

    def document(self, **entries) -> dict:
        return {"schema": "pairadar-notes-1", "date": DAY, "author": {"agent": "radar-analyst"},
                "notes": entries or {PAPER: LINE, ROBOT: LINE}}

    def test_a_clean_draft_passes(self) -> None:
        self.write(self.document())
        self.assertEqual(notes.check(DAY, self.root), ([], []))

    def test_an_invented_figure_names_the_item_the_language_and_the_figure(self) -> None:
        # 2026-09-23: the draft cited 1,600 and 94.5, and the night was lost.
        self.write(self.document(**{PAPER: LINE, ROBOT: dict(LINE, ja="1,600 回で 94.5% に達した。")}))
        problems, _ = notes.check(DAY, self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn(f"{ROBOT} [ja]: cites 1,600, 94.5", problems[0])
        self.write(self.document(**{PAPER: LINE, ROBOT: dict(LINE, en="87% over 1,200 draws.")}))
        self.assertEqual(notes.check(DAY, self.root)[0], [], "figures the page shows are fine")

    def test_broken_json_says_where(self) -> None:
        self.write('{"schema": "pairadar-notes-1",\n "notes": {,}}')
        problems, _ = notes.check(DAY, self.root)
        self.assertRegex(problems[0], r"not valid JSON: .*line 2 column \d+")

    def test_a_key_that_matches_no_pick_is_a_problem(self) -> None:
        # A note under the wrong key renders nowhere, silently.
        self.write(self.document(**{PAPER: LINE, "https://example.org/other": LINE}))
        problems, _ = notes.check(DAY, self.root)
        self.assertEqual(problems, [f"https://example.org/other: matches no item picked on {DAY}; "
                                    "key each note by its item's url"])

    def test_keys_compare_as_the_renderer_compares_them(self) -> None:
        self.write(self.document(**{"http://arxiv.org/abs/2609.11111v2": LINE, ROBOT + "/": LINE}))
        self.assertEqual(notes.check(DAY, self.root), ([], []))

    def test_a_missing_language_is_a_problem_and_a_missing_item_is_advice(self) -> None:
        self.write(self.document(**{PAPER: {"zh": "只有中文。", "en": "", "ja": ""}}))
        problems, advice = notes.check(DAY, self.root)
        self.assertEqual(problems, [f"{PAPER}: no en, ja line; write all three languages or drop the item"])
        self.assertEqual(advice, [f"{ROBOT}: no note for “Blood-draw robot”"], "the baseline needs no note")

    def test_the_header_is_checked(self) -> None:
        self.write(dict(self.document(), schema="notes-2", date="2026-09-22"))
        problems, _ = notes.check(DAY, self.root)
        self.assertEqual(problems, ['"schema" must be "pairadar-notes-1"', f'"date" must be "{DAY}"'])

    def test_no_file_is_a_problem(self) -> None:
        self.assertEqual(notes.check(DAY, self.root)[0], [f"data/notes/{DAY}.json was not written"])


class StampTest(unittest.TestCase):
    def test_the_models_that_ran_replace_the_agents_own_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"data"/"notes").mkdir(parents=True)
            path = notes.notes_path(DAY, root)
            path.write_text(json.dumps({"schema": "pairadar-notes-1", "date": DAY, "notes": {PAPER: LINE},
                                        "author": {"agent": "radar-analyst", "model": "claude-opus-4.6"}}))
            notes.stamp(DAY, "claude-opus-5", "radar-reviewer", "claude-sonnet-5", root=root)
            document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["author"], {"agent": "radar-analyst", "model": "claude-opus-5",
                                              "kind": "llm-draft", "cli": "kiro-cli"})
        self.assertEqual(document["review"], {"agent": "radar-reviewer", "model": "claude-sonnet-5",
                                              "verdict": "APPROVE"})
        self.assertEqual(document["notes"], {PAPER: LINE})


if __name__ == "__main__":
    unittest.main()
