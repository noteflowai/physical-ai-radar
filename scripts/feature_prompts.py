"""Preserve the approved contract while fitting planning reviews into the CLI budget."""
import copy
import hashlib
import json


def acceptance_paths(task_id: str, runners: list[str]) -> dict[str, str]:
    key = hashlib.sha256(task_id.encode()).hexdigest()[:12]
    suffixes = {"unittest": ".py", "pytest": ".py", "node": ".test.cjs",
                "vitest": ".test.ts", "php": ".php"}
    return {runner: "tests/test_feature_" + key + suffixes[runner] for runner in runners}


def plan_review_prompt(instruction: str, plan: dict, context: dict, budget: int = 100000) -> str:
    """Trim duplicate background before source excerpts; never rewrite the feature plan."""
    payload = {"plan": plan, "context": copy.deepcopy(context)}
    ctx = payload["context"]

    def render():
        return instruction + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # The reviewer still has the plan's source paths and actual inspected implementation.
    # Large README bodies/tree listings often duplicate those excerpts.
    for field in ("readmes", "tree", "recent_commits", "research", "issues"):
        text = render()
        if len(text.encode()) <= budget:
            return text
        if field in ctx:
            ctx.pop(field)
            ctx.setdefault("omitted_background", []).append(field)
    files = ctx.get("inspected_files", {})
    while files and len(render().encode()) > budget:
        files.pop(next(iter(files)))
    text = render()
    if context.get("inspected_files") and not files:
        raise ValueError("Inspected source cannot fit the plan review budget; narrow the feature")
    if len(text.encode()) > budget:
        raise ValueError("Feature plan and required review context exceed the prompt budget; narrow the feature")
    return text
