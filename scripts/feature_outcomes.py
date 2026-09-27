"""Distinguish retryable work, exhausted budgets and verified delivery."""
from datetime import date, timedelta

DEFERRED_EXIT = 20
TERMINAL_BATCH = {"complete", "complete-with-deferrals"}


def outcome_exit(results: list[dict], *, plan_only: bool = False) -> int:
    successes = {"published", "planned"} if plan_only else {"published"}
    statuses = {item.get("status") for item in results}
    if statuses - successes - {"deferred"}:
        return 1
    return DEFERRED_EXIT if "deferred" in statuses else 0


def deferral_details(task: dict, day: str) -> dict:
    phase = task.get("phase", "unknown")
    planning, implementation = task.get("planning_attempts", 0), task.get("attempts", 0)
    count = (planning if phase == "plan" else implementation if phase == "implement"
             else task.get("phase_attempts", {}).get(phase, 0))
    return {
        "failed_phase": phase, "planning_attempts": planning,
        "implementation_attempts": implementation, "phase_attempts": count,
        "reason_code": phase + "-budget-exhausted",
        "last_feedback": task.get("feedback"),
        "retry_after": (date.fromisoformat(day) + timedelta(days=1)).isoformat(),
    }
