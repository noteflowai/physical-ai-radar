"""A struggling source says how long it has been dark, not only how often."""
import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from pairadar import health

TODAY = date(2026, 9, 27)


def _run(day: int, failed, fallback=None):
    entry = {"date": f"2026-09-{day:02d}", "count": 8, "failed": list(failed)}
    if fallback is not None:
        entry["fallback"] = list(fallback)
    return entry


# `x` answered on the 22nd and has been dark on every run since.
CONTINUOUS = [_run(22, []), *[_run(day, ["x"]) for day in (23, 24, 25, 26, 27)]]

# `x` failed, answered once, then failed the two latest runs: three failures, flapping.
FLAPPING = [_run(24, ["x"]), _run(25, []), _run(26, ["x"]), _run(27, ["x"])]

# Same shape, but the answering day was served by the fallback endpoint.
FALLBACK_DAY = [_run(24, ["x"]), _run(25, [], fallback=["x"]), _run(26, ["x"]), _run(27, ["x"])]


class StreakReportTest(unittest.TestCase):
    def _only(self, verdict):
        self.assertEqual(len(verdict["struggling"]), 1, verdict)
        return verdict["struggling"][0]

    def test_every_struggling_entry_carries_a_streak_and_a_start_date(self) -> None:
        for runs in (CONTINUOUS, FLAPPING, FALLBACK_DAY):
            with self.subTest(runs=runs):
                verdict = health.report(runs, TODAY)
                dates = {entry["date"] for entry in runs}
                for entry in verdict["struggling"]:
                    self.assertIsInstance(entry["streak_days"], int)
                    self.assertGreaterEqual(entry["streak_days"], 1)
                    self.assertIn(entry["failing_since"], dates)
                    date.fromisoformat(entry["failing_since"])

    def test_a_continuously_dark_source_reports_the_first_day_of_the_outage(self) -> None:
        entry = self._only(health.report(CONTINUOUS, TODAY))
        self.assertEqual(entry["source"], "x")
        self.assertEqual(entry["failed_days"], 5)
        self.assertEqual(entry["streak_days"], 5)
        self.assertEqual(entry["failing_since"], "2026-09-23")

    def test_a_flapping_source_reports_only_its_current_streak(self) -> None:
        entry = self._only(health.report(FLAPPING, TODAY))
        self.assertEqual(entry["failed_days"], 3, "the history is still counted")
        self.assertEqual(entry["streak_days"], 2)
        self.assertEqual(entry["failing_since"], "2026-09-26")

    def test_a_fallback_served_day_ends_the_streak(self) -> None:
        verdict = health.report(FALLBACK_DAY, TODAY)
        entry = self._only(verdict)
        self.assertEqual(verdict["fallbacks"], {"x": 1})
        self.assertEqual(entry["failed_days"], 3)
        self.assertEqual(entry["streak_days"], 2)
        self.assertEqual(entry["failing_since"], "2026-09-26")

    def test_run_order_in_the_log_does_not_change_the_streak(self) -> None:
        shuffled = [CONTINUOUS[3], CONTINUOUS[0], CONTINUOUS[5], CONTINUOUS[1], CONTINUOUS[4], CONTINUOUS[2]]
        self.assertEqual(health.report(shuffled, TODAY), health.report(CONTINUOUS, TODAY))

    def test_the_streak_never_reaches_past_the_window(self) -> None:
        runs = [_run(day, ["x"]) for day in (22, 23, 24, 25, 26, 27)]
        narrow = self._only(health.report(runs, TODAY, window=3))
        self.assertEqual(narrow["failed_days"], 3)
        self.assertEqual(narrow["streak_days"], 3)
        self.assertEqual(narrow["failing_since"], "2026-09-25")
        wide = self._only(health.report(runs, TODAY))
        self.assertEqual(wide["streak_days"], 6)
        self.assertEqual(wide["failing_since"], "2026-09-22")

    def test_a_recovered_source_is_still_not_struggling(self) -> None:
        recovered = [*CONTINUOUS, _run(28, [])]
        verdict = health.report(recovered, date(2026, 9, 28))
        self.assertEqual(verdict["struggling"], [])
        self.assertEqual(verdict["failures"], {"x": 5})
        self.assertEqual(health.streak(recovered, date(2026, 9, 28), health.WINDOW, "x"), (0, None))

    def test_existing_report_fields_keep_their_names_and_values(self) -> None:
        verdict = health.report(FALLBACK_DAY, TODAY)
        self.assertEqual(verdict["reference"], "2026-09-27")
        self.assertEqual(verdict["window_days"], health.WINDOW)
        self.assertEqual(verdict["threshold_days"], health.THRESHOLD)
        self.assertEqual(verdict["runs_with_health"], 4)
        self.assertEqual(verdict["failures"], {"x": 3})
        self.assertEqual(verdict["fallbacks"], {"x": 1})
        self.assertEqual([(e["source"], e["failed_days"]) for e in verdict["struggling"]],
                         health.struggling(FALLBACK_DAY, TODAY))
        self.assertEqual(set(verdict), {"reference", "window_days", "threshold_days",
                                        "runs_with_health", "failures", "fallbacks", "struggling"})

    def test_worst_first_ranking_is_kept_with_the_new_fields(self) -> None:
        runs = [_run(day, ["x", "y"] if day >= 25 else ["x"]) for day in (23, 24, 25, 26, 27)]
        verdict = health.report(runs, TODAY)
        self.assertEqual([e["source"] for e in verdict["struggling"]], ["x", "y"])
        self.assertEqual([e["streak_days"] for e in verdict["struggling"]], [5, 3])
        self.assertEqual([e["failing_since"] for e in verdict["struggling"]],
                         ["2026-09-23", "2026-09-25"])


class StreakCliTest(unittest.TestCase):
    def test_cli_prints_the_streak_and_keeps_its_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            path.write_text(json.dumps({"runs": CONTINUOUS}), encoding="utf-8")
            common = ["--history", str(path), "--date", "2026-09-27"]

            def verdict(argv):
                # Captured so a synthetic outage never lands in a scheduled job's log.
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = health.main(argv)
                return code, json.loads(out.getvalue())

            code, printed = verdict(common)
            self.assertEqual(code, 0, "the daily run must stay green")
            self.assertEqual(printed["struggling"], [
                {"source": "x", "failed_days": 5, "streak_days": 5, "failing_since": "2026-09-23"}])

            code, printed = verdict([*common, "--fail-on-struggling"])
            self.assertEqual(code, 1)
            self.assertEqual(printed["struggling"][0]["streak_days"], 5)

            code, printed = verdict([*common, "--fail-on-struggling", "--threshold", "9"])
            self.assertEqual(code, 0)
            self.assertEqual(printed["struggling"], [])

            code, printed = verdict([*common, "--window", "3"])
            self.assertEqual(code, 0)
            self.assertEqual(printed["struggling"], [
                {"source": "x", "failed_days": 3, "streak_days": 3, "failing_since": "2026-09-25"}])


if __name__ == "__main__":
    unittest.main()
