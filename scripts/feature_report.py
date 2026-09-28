"""Local delivery/announcement health and measured GitHub download counts."""
from __future__ import annotations
import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import time

try:
    from .agent_pipeline import write_json
    from .feature_policy import PROJECTS
    from .feature_outcomes import deferral_details
except ImportError:
    from agent_pipeline import write_json
    from feature_policy import PROJECTS
    from feature_outcomes import deferral_details


def report(state: Path, day: str, *, metrics: bool = True) -> dict:
    start = date.fromisoformat(day)
    dates = [(start - timedelta(days=n)).isoformat() for n in range(7)]
    records = []
    for name in PROJECTS:
        outcomes = []
        for current in dates:
            path = state / current / name / "result.json"
            if path.exists():
                item = json.loads(path.read_text())
                outcome = {"day": current, "status": item["status"],
                           "feature_id": item.get("feature_id"), "error": item.get("error"),
                           "reason": item.get("reason"), "reason_code": item.get("reason_code")}
                if item["status"] == "deferred":
                    retired = state / "tasks" / name / str(item.get("feature_id")) / "retired-task.json"
                    if retired.exists():
                        outcome.update(deferral_details(json.loads(retired.read_text()), current))
                        # Legacy receipts called planning exhaustion implementation failure.
                        if (outcome["failed_phase"] == "plan" and outcome["planning_attempts"] >= 9
                                and str(item.get("reason", "")).startswith("Nine implementation attempts failed")):
                            outcome["reason"] = "Planning budget exhausted; no implementation or verified release."
                            outcome["reason_code"] = "plan-budget-exhausted"
                outcomes.append(outcome)
        active = state / "tasks" / name / "active.json"
        item = json.loads(active.read_text()) if active.exists() else {}
        records.append({"repo": name, "outcomes": outcomes, "active_phase": item.get("phase"),
                        "active_feature": item.get("id"), "feedback": item.get("feedback")})
    announcements = []
    items = [json.loads(p.read_text()) for p in (state / "outbox").glob("*.json")]
    measured, deadline = set(), time.monotonic() + 45
    for item in sorted(items, key=lambda x: x["event"]["released_at"], reverse=True):
        event = item["event"]
        row = {"id": event["id"], "status": item["status"], "channels": item.get("channels", {}),
               "last_error": item.get("last_error")}
        if (metrics and item["status"] == "announced" and event["repo"] not in measured
                and time.monotonic() < deadline):
            measured.add(event["repo"])
            try:
                tag = event["release_url"].rsplit("/", 1)[1]
                response = subprocess.run(
                    ["gh", "api", f"repos/{event['repo']}/releases/tags/{tag}"],
                    capture_output=True, text=True, check=True,
                    timeout=min(8, max(0.1, deadline - time.monotonic())))
                release = json.loads(response.stdout)
                row["github_asset_downloads"] = {
                    a["name"]: a["download_count"] for a in release["assets"]}
            except Exception as error:
                row["metrics_error"] = str(error)
        announcements.append(row)
    result = {"date": day, "checked_at": datetime.now(timezone.utc).isoformat(),
              "projects": records, "announcements": announcements,
              "announcement_errors": [
                  {"file": str(p), "error": json.loads(p.read_text())["error"]}
                  for p in (state / "announcement-errors").glob("*.json")],
              "measurement_scope": "GitHub asset downloads only; no inferred reach or conversions"}
    write_json(state / "reports" / (day + ".json"), result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/ai-feature-agent")
    parser.add_argument("--no-metrics", action="store_true")
    args = parser.parse_args()
    result = report(args.state, args.date, metrics=not args.no_metrics)
    print(json.dumps({"report": str(args.state / "reports" / (args.date + ".json")),
                      "projects": len(result["projects"]), "announcements": len(result["announcements"])}))
