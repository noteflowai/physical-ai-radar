"""Preserve the approved contract while fitting planning reviews into the CLI budget."""
import copy
import hashlib
import json


def acceptance_paths(task_id: str, runners: list[str]) -> dict[str, str]:
    key = hashlib.sha256(task_id.encode()).hexdigest()[:12]
    suffixes = {"unittest": ".py", "pytest": ".py", "node": ".test.cjs",
                "vitest": ".test.ts", "php": ".php"}
    return {runner: "tests/test_feature_" + key + suffixes[runner] for runner in runners}


# Background fields that bounded_prompt() drops, in this order, when a prompt is over budget.
# recent_commits is the smallest (about 8 lines) and goes last. The planner is told this same
# tuple, so what it is told the reviewer may not see cannot drift from what is dropped.
DROP_ORDER = ("readmes", "tree", "research", "issues", "recent_commits")

# Source files above this size reach a reviewer as a signature index only. text_file() in
# develop_repos.py uses the same value as its default limit.
SIGNATURE_ONLY_BYTES = 24000
MAX_INSPECT_RANGES = 5

# Rules the controller itself applies. The planner prompt and both planning reviews carry
# these exact strings, so a reviewer does not treat a quoted rule as an invented one.
CONTROLLER_RULES = (
    "A stale roadmap forbids new features.",
    "Pure documentation may be identified for maintainer follow-up; this code delivery lane "
    "requires behavioral acceptance.",
)


def controller_rules_for_reviewer() -> str:
    """The controller rules as a reviewer sees them, marked as supplied by the controller."""
    return ("Controller-supplied rules (the controller enforces these; they are not claims "
            "by the plan and are not invented): " + " ".join(CONTROLLER_RULES) + " ")


def reviewer_visibility_for_planner() -> str:
    """What the reviewer can and cannot see, so the planner does not cite unseen material as fact."""
    return (
        "The independent reviewer does not see everything you see. When a prompt is over its "
        "size budget the reviewer's copy drops these background fields in this order: "
        + ", ".join(DROP_ORDER) + "; the names of dropped fields are listed in "
        "context.omitted_background. A claim that rests on one of those fields cannot be checked "
        "by the reviewer, so label it unverified in the plan instead of asserting it. "
        "The reviewer sees a source file of more than " + str(SIGNATURE_ONLY_BYTES) + " bytes "
        "only as a signature index, and otherwise sees only the plan's own inspect_ranges "
        "(at most " + str(MAX_INSPECT_RANGES) + " ranges) and the excerpts you inspected. "
        "If a requested range would be cut by the size budget the controller returns that as an "
        "error for you to correct; request a narrower range. ")


def plan_review_prompt(instruction: str, plan: dict, context: dict, budget: int = 100000) -> str:
    """Trim duplicate background before source excerpts; never rewrite the feature plan."""
    return bounded_prompt(instruction, {"plan": plan}, context, budget)


def bounded_prompt(instruction: str, required: dict, context: dict, budget: int = 100000) -> str:
    """Keep the complete proposal/correction contract ahead of duplicate background."""
    payload = {**required, "context": copy.deepcopy(context)}
    ctx = payload["context"]

    def render():
        return instruction + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # The reviewer still has the plan's source paths and actual inspected implementation.
    # Large README bodies/tree listings often duplicate those excerpts.
    for field in DROP_ORDER:
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
