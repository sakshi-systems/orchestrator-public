"""Unit tests for _parse_pct, _validate_sidecar_step, _detect_task_step, and _detect_test_task_step."""

import dataclasses
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pathlib import Path

from orchestrator import claude_cli as claude_module
from orchestrator import codex_cli as codex_module
from orchestrator import errors as errors_module
from orchestrator import layout as layout_module
from orchestrator import main as main_module
from orchestrator import pipeline as pipeline_module
from orchestrator import usage as usage_module
from orchestrator.agents import HEAD_VERIFY, REVIEW_VERIFY, TEST_RUN_VERIFY
from orchestrator.errors import EscalationError, HaltError, PipelineStopError, RateLimitError
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.layout import _artifact_subdir, _resolve_roadmap_relpath, _tests_sibling
from orchestrator.notify import Outcome
from orchestrator.protocol import _read_sessions
from orchestrator.pipeline import (
    IMPLEMENT_MODE,
    TEST_MODE,
    _artifact_dir,
    _next_number,
    process_task,
)
from orchestrator.resume import (
    _detect_step,
    _validate_sidecar_step,
)
from orchestrator.usage import _check_usage_limits, _parse_pct
from orchestrator.agents import _load_prompt
from orchestrator.ports import AgentRole, RoleSetting
from orchestrator.roadmap import mark_done
from tests.fakes import FakeAgentBackend, target_path
from tests.test_config import _write_config, _write_override

# ---------------------------------------------------------------------------
# Helpers / constants shared across _validate_sidecar_step tests
# ---------------------------------------------------------------------------

SEQ = "01"
SLUG = "slug"
FAIL_PREFIX = "review_failed:"
FAIL_SUFFIX = "-review-{n}.md"


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Return (plan_reviews_dir, artifact_dir), both created."""
    plan_reviews_dir = tmp_path / "plan-reviews"
    artifact_dir = tmp_path / "reviews"
    plan_reviews_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    return plan_reviews_dir, artifact_dir


def _call(step_value: str, plan_reviews_dir: Path, artifact_dir: Path) -> str:
    return _validate_sidecar_step(
        step_value, SEQ, SLUG, plan_reviews_dir, artifact_dir, FAIL_PREFIX, FAIL_SUFFIX
    )


# ---------------------------------------------------------------------------
# Task 1: Pass-through values that need no artifact
# ---------------------------------------------------------------------------


def test_validate_empty_string_returns_empty(tmp_path):
    """Should return '' when step_value is '' (empty input short-circuits)."""
    prd, art = _dirs(tmp_path)
    assert _call("", prd, art) == ""


def test_validate_planned_n_returns_planned_n(tmp_path):
    """Should return 'planned:2' when step_value is 'planned:2'."""
    prd, art = _dirs(tmp_path)
    assert _call("planned:2", prd, art) == "planned:2"


def test_validate_implemented_n_returns_implemented_n(tmp_path):
    """Should return 'implemented:2' when step_value is 'implemented:2'."""
    prd, art = _dirs(tmp_path)
    assert _call("implemented:2", prd, art) == "implemented:2"


def test_validate_planned_malformed_n_halts(tmp_path):
    """Should halt when step_value is 'planned:abc' (a malformed counter never clears the marker)."""
    prd, art = _dirs(tmp_path)
    with pytest.raises(HaltError):
        _call("planned:abc", prd, art)


def test_validate_implemented_malformed_n_halts(tmp_path):
    """Should halt when step_value is 'implemented:abc' (a malformed counter never clears the marker)."""
    prd, art = _dirs(tmp_path)
    with pytest.raises(HaltError):
        _call("implemented:abc", prd, art)


def test_validate_unknown_value_passthrough(tmp_path):
    """Should return 'some_unknown_value' when step_value is unrecognized (pass-through to plan-review detection)."""
    prd, art = _dirs(tmp_path)
    assert _call("some_unknown_value", prd, art) == "some_unknown_value"


# ---------------------------------------------------------------------------
# Task 2: plan_review_failed:N gated on the plan-review file
# ---------------------------------------------------------------------------


def test_validate_plan_review_failed_file_present(tmp_path):
    """Should return 'plan_review_failed:2' when plan-reviews/01-slug-plan-review-2.md exists."""
    prd, art = _dirs(tmp_path)
    (prd / "01-slug-plan-review-2.md").write_text("some review content")
    assert _call("plan_review_failed:2", prd, art) == "plan_review_failed:2"


def test_validate_plan_review_failed_file_missing(tmp_path):
    """Should return '' when step_value is 'plan_review_failed:2' but the plan-review file is missing."""
    prd, art = _dirs(tmp_path)
    assert _call("plan_review_failed:2", prd, art) == ""


def test_validate_plan_review_failed_malformed_n_halts(tmp_path):
    """Should halt when step_value is 'plan_review_failed:abc' (a malformed counter never clears the marker)."""
    prd, art = _dirs(tmp_path)
    with pytest.raises(HaltError):
        _call("plan_review_failed:abc", prd, art)


MALFORMED_COUNTERS = ["", "0", "-1", "+1", " 1", "1_0", "1:2", "\u00b2", "\u0663"]


@pytest.mark.parametrize("prefix", ["planned:", "plan_review_failed:", "review_failed:"])
@pytest.mark.parametrize("suffix", MALFORMED_COUNTERS)
def test_validate_malformed_counters_halt(tmp_path, prefix, suffix):
    """A recognized marker with a non-positive, non-ASCII-digit or compound counter halts."""
    prd, art = _dirs(tmp_path)
    with pytest.raises(HaltError, match="malformed attempt counter"):
        _call(prefix + suffix, prd, art)


@pytest.mark.parametrize("suffix", MALFORMED_COUNTERS)
def test_detect_malformed_counter_halts_instead_of_falling_back(tmp_path, suffix):
    """A malformed counter on a recognized marker halts even with artifacts that would let inference proceed."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "planned:" + suffix}))
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text("Fine\n\nPLAN_REVIEW_PASS")
    with pytest.raises(HaltError, match="malformed attempt counter"):
        _detect_task_step(DMS_SEQ, DMS_SLUG, plan_path, prd, rv)


# ---------------------------------------------------------------------------
# Task 3: plan_reviewed requires a passing plan-review file
# ---------------------------------------------------------------------------


def test_validate_plan_reviewed_with_passing_file(tmp_path):
    """Should return 'plan_reviewed' when a plan-review file content ends with PLAN_REVIEW_PASS."""
    prd, art = _dirs(tmp_path)
    (prd / "01-slug-plan-review-1.md").write_text("Some review notes\n\nPLAN_REVIEW_PASS")
    assert _call("plan_reviewed", prd, art) == "plan_reviewed"


def test_validate_plan_reviewed_no_files(tmp_path):
    """Should return '' when step_value is 'plan_reviewed' but no plan-review files exist."""
    prd, art = _dirs(tmp_path)
    assert _call("plan_reviewed", prd, art) == ""


# ---------------------------------------------------------------------------
# Task 4: review_failed:N gated on the review artifact file
# ---------------------------------------------------------------------------


def test_validate_review_failed_file_present(tmp_path):
    """Should return 'review_failed:1' when reviews/01-slug-review-1.md exists."""
    prd, art = _dirs(tmp_path)
    (art / "01-slug-review-1.md").write_text("review content")
    assert _call("review_failed:1", prd, art) == "review_failed:1"


def test_validate_review_failed_file_missing(tmp_path):
    """Should return '' when step_value is 'review_failed:1' but the review file is missing."""
    prd, art = _dirs(tmp_path)
    assert _call("review_failed:1", prd, art) == ""


# ---------------------------------------------------------------------------
# Task 5: escalated is always valid — a terminal, unindexed step
# ---------------------------------------------------------------------------


def test_validate_escalated_always_valid(tmp_path):
    """Should return 'escalated' as-is, with no artifact to check against disk."""
    prd, art = _dirs(tmp_path)
    assert _call("escalated", prd, art) == "escalated"


# Production patterns verbatim from usage.py
SESSION_PATTERN = r"Current session:\s+(\d+(?:\.\d+)?)%"
WEEKLY_PATTERN = r"Current week \(all models\):\s+(\d+(?:\.\d+)?)%"


# ---------------------------------------------------------------------------
# Task 1: Successful matches return the captured percentage as a float
# ---------------------------------------------------------------------------


def test_session_pattern_integer_percentage():
    """Should return 85.0 when session pattern matches 'Current session: 85%'."""
    result = _parse_pct("Current session: 85%", SESSION_PATTERN)
    assert result == 85.0


def test_weekly_pattern_decimal_percentage():
    """Should return 42.5 when weekly pattern matches 'Current week (all models): 42.5%'."""
    result = _parse_pct("Current week (all models): 42.5%", WEEKLY_PATTERN)
    assert result == 42.5


def test_session_pattern_decimal_percentage():
    """Should return 12.3 when session pattern matches a decimal 'Current session: 12.3%'."""
    result = _parse_pct("Current session: 12.3%", SESSION_PATTERN)
    assert result == 12.3


def test_successful_match_returns_float_instance():
    """Should return a float instance (not a string) on a successful match."""
    result = _parse_pct("Current session: 50%", SESSION_PATTERN)
    assert isinstance(result, float)


# ---------------------------------------------------------------------------
# Task 2: Non-matching input returns None
# ---------------------------------------------------------------------------


def test_no_match_unrelated_text():
    """Should return None when session pattern finds no match in unrelated text."""
    result = _parse_pct("No usage data available.", SESSION_PATTERN)
    assert result is None


def test_no_match_unrelated_digits():
    """Should return None when text contains unrelated digits 'used 5 tokens' and does not match the session pattern."""
    result = _parse_pct("used 5 tokens today", SESSION_PATTERN)
    assert result is None


# ---------------------------------------------------------------------------
# Task 3: re.search scans the whole string, returning the first match anywhere
# ---------------------------------------------------------------------------


def test_multiline_returns_first_match():
    """Should return the first matching percentage when given a multi-line output string."""
    text = (
        "Claude Code usage\n"
        "Some other metric: 99\n"
        "Current session: 70%\n"
        "Current week (all models): 30%\n"
    )
    result = _parse_pct(text, SESSION_PATTERN)
    assert result == 70.0


# ---------------------------------------------------------------------------
# _detect_task_step tests
# ---------------------------------------------------------------------------

DMS_SEQ = "01"
DMS_SLUG = "slug"
DMS_MAX_ITERATIONS = 3


def _detect_task_step(
    seq: str, slug: str,
    plan_path: Path, plan_reviews_dir: Path, reviews_dir: Path,
    max_iterations: int = DMS_MAX_ITERATIONS,
) -> tuple[str, int, Path]:
    """Detect where a previous implement run stopped: `_detect_step` with `REVIEW_VERIFY`."""
    return _detect_step(
        seq, slug, plan_path, plan_reviews_dir, reviews_dir,
        REVIEW_VERIFY, max_iterations=max_iterations, runtime="claude",
    )


def _detect_test_task_step(
    seq: str, slug: str,
    plan_path: Path, plan_reviews_dir: Path, test_runs_dir: Path,
    max_iterations: int = DMS_MAX_ITERATIONS,
) -> tuple[str, int, Path]:
    """Detect where a previous test run stopped: `_detect_step` with `TEST_RUN_VERIFY`."""
    return _detect_step(
        seq, slug, plan_path, plan_reviews_dir, test_runs_dir,
        TEST_RUN_VERIFY, max_iterations=max_iterations, runtime="claude",
    )


def _dms_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build .ai-factory/{plans,plan-reviews,reviews} and return (plan_reviews_dir, reviews_dir, plan_path).

    plan_path points to plans/01-slug.md (not created — caller decides whether to write it).
    """
    plans_dir = tmp_path / ".ai-factory" / "plans"
    plan_reviews_dir = tmp_path / ".ai-factory" / "plan-reviews"
    reviews_dir = tmp_path / ".ai-factory" / "reviews"
    plans_dir.mkdir(parents=True)
    plan_reviews_dir.mkdir(parents=True)
    reviews_dir.mkdir(parents=True)
    plan_path = plans_dir / f"{DMS_SEQ}-{DMS_SLUG}.md"
    return plan_reviews_dir, reviews_dir, plan_path


# ---------------------------------------------------------------------------
# Task 1: Fresh start and sidecar-driven steps (no git repo needed)
# ---------------------------------------------------------------------------


def test_detect_task_step_no_plan_file_returns_plan(tmp_path):
    """Should return ("plan", 1, plan_path) when the plan file does not exist."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    # plan_path intentionally not created on disk
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "plan"
    assert counter == 1
    assert returned_path == plan_path


def test_detect_task_step_sidecar_planned_1_returns_plan_review_1(tmp_path):
    """Should return ("plan_review", 1, plan_path) when sidecar step is "planned:1"."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "planned:1"}))
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "plan_review"
    assert counter == 1
    assert returned_path == plan_path


def test_detect_task_step_sidecar_planned_2_returns_plan_review_2(tmp_path):
    """Should return ("plan_review", 2, plan_path) when sidecar step is "planned:2"."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "planned:2"}))
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "plan_review"
    assert counter == 2
    assert returned_path == plan_path


def test_detect_task_step_sidecar_implemented_1_returns_review_1(tmp_path):
    """Should return ("review", 1, plan_path) when sidecar step is "implemented:1"."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "implemented:1"}))
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "review"
    assert counter == 1
    assert returned_path == plan_path


def test_detect_task_step_sidecar_implemented_3_returns_review_3(tmp_path):
    """Should return ("review", 3, plan_path) when sidecar step is "implemented:3"."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "implemented:3"}))
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "review"
    assert counter == 3
    assert returned_path == plan_path


def test_detect_task_step_sidecar_review_failed_returns_implement(tmp_path):
    """Should return ("implement", 2, plan_path) when sidecar step is "review_failed:1" and reviews/01-slug-review-1.md is present."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "review_failed:1"}))
    (rv / f"{DMS_SEQ}-{DMS_SLUG}-review-1.md").write_text("review content")
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "implement"
    assert counter == 2
    assert returned_path == plan_path


def test_detect_task_step_sidecar_plan_review_failed_returns_plan(tmp_path):
    """Should return ("plan", 3, plan_path) when sidecar step is "plan_review_failed:2" and plan-reviews/01-slug-plan-review-2.md is present."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "plan_review_failed:2"}))
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-2.md").write_text("review content")
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "plan"
    assert counter == 3
    assert returned_path == plan_path


def test_detect_task_step_sidecar_escalated_returns_escalated(tmp_path):
    """Should return ("escalated", 0, plan_path) when sidecar step is "escalated" — a
    terminal, unindexed step that never falls through to plan-review detection."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "escalated"}))
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "escalated"
    assert counter == 0
    assert returned_path == plan_path


def test_detect_task_step_sidecar_plan_reviewed_returns_implement(tmp_path):
    """Should return ("implement", 1, plan_path) when sidecar step is "plan_reviewed" and a passing plan-review file is present."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "plan_reviewed"}))
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text(
        "Review notes\n\nPLAN_REVIEW_PASS"
    )
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "implement"
    assert counter == 1
    assert returned_path == plan_path


# ---------------------------------------------------------------------------
# Fall-through to plan-review detection: latest plan-review not passing
# ---------------------------------------------------------------------------


def test_detect_task_step_plan_review_not_passing_returns_plan(tmp_path):
    """Should return ("plan", 2, plan_path) when no sidecar step and the latest plan-review does not end with PLAN_REVIEW_PASS."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text("Review notes, not passing")
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "plan"
    assert counter == 2
    assert returned_path == plan_path


# ---------------------------------------------------------------------------
# Fall-through to plan-review detection: latest plan-review passing
# ---------------------------------------------------------------------------


def test_detect_task_step_passing_review_without_sidecar_step_returns_implement(tmp_path):
    """Should return ("implement", 1, plan_path) when no sidecar step and both a passing plan-review and a passing review sit on disk.
    Reading a verify output as completion would close a task whose implementation was never made."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text("Review notes\n\nPLAN_REVIEW_PASS")
    (rv / f"{DMS_SEQ}-{DMS_SLUG}-review-1.md").write_text("Review notes\n\nREVIEW_PASS")
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert (step, counter, returned_path) == ("implement", 1, plan_path)


def test_detect_task_step_plan_review_pass_with_stray_line_returns_implement(tmp_path):
    """Should return ("implement", 1, plan_path) when the plan-review carries PLAN_REVIEW_PASS as an exact line followed by a stray line.
    A resume that misreads a passing plan-review silently repeats finished work."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text(
        "Review notes\n\nPLAN_REVIEW_PASS\nstray trailing line\n"
    )
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, prd, rv
    )
    assert step == "implement"
    assert counter == 1
    assert returned_path == plan_path


# ---------------------------------------------------------------------------
# Task 3: Canonical slug/seq resolution on mismatch
# ---------------------------------------------------------------------------


def test_detect_task_step_canonical_path_resolution(tmp_path):
    """Should resolve canonical plan path to 01-slug.md and return ("plan_review", 1, that path) when caller passes seq="02" and plan_path=02-slug.md."""
    prd, rv, plan_path_01 = _dms_dirs(tmp_path)
    plan_path_01.write_text("# Plan content")
    # Only 01-slug.md exists; 02-slug.md does not
    plans_dir = plan_path_01.parent
    plan_path_02 = plans_dir / "02-slug.md"
    # No sidecar, no plan-review files → falls through to "plan_review"
    step, counter, returned_path = _detect_task_step(
        "02", DMS_SLUG, plan_path_02, prd, rv
    )
    assert step == "plan_review"
    assert counter == 1
    assert returned_path == plan_path_01


# ---------------------------------------------------------------------------
# Resume adoption gate: a plan whose sidecar says `done` is skipped;
# the lowest-numbered of the rest is adopted.
# ---------------------------------------------------------------------------


def test_detect_task_step_done_plan_is_skipped(tmp_path):
    """A plan whose sidecar says `done` must not be adopted, even when its plan review and review passed."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "done"}))
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text(
        "Review notes\n\nPLAN_REVIEW_PASS"
    )
    (rv / f"{DMS_SEQ}-{DMS_SLUG}-review-1.md").write_text("Review notes\n\nREVIEW_PASS")
    fresh_seq = "02"
    fresh_plan_path = plan_path.parent / f"{fresh_seq}-{DMS_SLUG}.md"
    step, counter, returned_path = _detect_task_step(
        fresh_seq, DMS_SLUG, fresh_plan_path, prd, rv
    )
    assert (step, counter, returned_path) == ("plan", 1, fresh_plan_path)


def test_detect_task_step_lowest_plan_without_done_is_adopted(tmp_path):
    """With a `done` plan below and two in-flight plans above, the lowest in-flight one is adopted."""
    prd, rv, plan_path_01 = _dms_dirs(tmp_path)
    plans_dir = plan_path_01.parent
    plan_path_02 = plans_dir / "02-slug.md"
    plan_path_03 = plans_dir / "03-slug.md"
    for path, step_value in (
        (plan_path_01, "done"), (plan_path_02, "planned:1"), (plan_path_03, "planned:1"),
    ):
        path.write_text("# Plan content")
        path.with_suffix(".json").write_text(json.dumps({"step": step_value}))
    step, counter, returned_path = _detect_task_step(
        "04", DMS_SLUG, plans_dir / "04-slug.md", prd, rv
    )
    assert (step, counter, returned_path) == ("plan_review", 1, plan_path_02)


# ---------------------------------------------------------------------------
# Attempt limits: explicit counters are checked against max_iterations
# ---------------------------------------------------------------------------


def _seed_sidecar(plan_path: Path, step: str) -> None:
    plan_path.write_text("# Plan")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": step}))


def test_detect_planned_leading_zero_read_by_value(tmp_path):
    prd, rv, plan_path = _dms_dirs(tmp_path)
    _seed_sidecar(plan_path, "planned:02")
    step, counter, _ = _detect_task_step(DMS_SEQ, DMS_SLUG, plan_path, prd, rv)
    assert (step, counter) == ("plan_review", 2)


def test_detect_planned_at_bound_resumes_plan_review(tmp_path):
    prd, rv, plan_path = _dms_dirs(tmp_path)
    _seed_sidecar(plan_path, "planned:3")
    step, counter, _ = _detect_task_step(DMS_SEQ, DMS_SLUG, plan_path, prd, rv)
    assert (step, counter) == ("plan_review", 3)


def test_detect_plan_review_failed_in_budget_resumes_revision(tmp_path):
    prd, rv, plan_path = _dms_dirs(tmp_path)
    _seed_sidecar(plan_path, "plan_review_failed:2")
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-2.md").write_text("Findings")
    step, counter, _ = _detect_task_step(DMS_SEQ, DMS_SLUG, plan_path, prd, rv)
    assert (step, counter) == ("plan", 3)


@pytest.mark.parametrize("step_value", ["planned:4", "implemented:4", "review_failed:3"])
def test_detect_explicit_counter_past_budget_halts(tmp_path, step_value):
    prd, rv, plan_path = _dms_dirs(tmp_path)
    _seed_sidecar(plan_path, step_value)
    (rv / f"{DMS_SEQ}-{DMS_SLUG}-review-3.md").write_text("Findings")
    with pytest.raises(HaltError, match="exceeds the attempt budget"):
        _detect_task_step(DMS_SEQ, DMS_SLUG, plan_path, prd, rv)


def test_detect_test_run_failed_at_bound_halts(tmp_path):
    prd, trd, plan_path = _dms_dirs(tmp_path)
    _seed_sidecar(plan_path, "test_run_failed:3")
    (trd / f"{DMS_SEQ}-{DMS_SLUG}-test-3.txt").write_text("FAIL")
    with pytest.raises(HaltError, match="exceeds the attempt budget"):
        _detect_test_task_step(DMS_SEQ, DMS_SLUG, plan_path, prd, trd)


def test_detect_plan_review_failed_at_bound_halts_even_when_review_missing(tmp_path):
    """The artifact fallback would return ('plan', 3); the explicit exhausted counter halts first."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    _seed_sidecar(plan_path, "plan_review_failed:3")
    for n in (1, 2):
        (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-{n}.md").write_text("Findings")
    with pytest.raises(HaltError, match="exceeds the attempt budget"):
        _detect_task_step(DMS_SEQ, DMS_SLUG, plan_path, prd, rv)


def test_detect_stale_in_budget_marker_still_falls_back_to_artifacts(tmp_path):
    prd, rv, plan_path = _dms_dirs(tmp_path)
    _seed_sidecar(plan_path, "plan_review_failed:2")
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text("Findings")
    step, counter, _ = _detect_task_step(DMS_SEQ, DMS_SLUG, plan_path, prd, rv)
    assert (step, counter) == ("plan", 2)


# ---------------------------------------------------------------------------
# _detect_test_task_step tests
# ---------------------------------------------------------------------------

DTMS_SEQ = "01"
DTMS_SLUG = "slug"


def _dtms_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build .ai-factory/{plans,plan-reviews,test-runs} and return (plan_reviews_dir, test_runs_dir, plan_path).

    plan_path points to plans/01-slug.md (not created — caller decides whether to write it).
    """
    plans_dir = tmp_path / ".ai-factory" / "plans"
    plan_reviews_dir = tmp_path / ".ai-factory" / "plan-reviews"
    test_runs_dir = tmp_path / ".ai-factory" / "test-runs"
    plans_dir.mkdir(parents=True)
    plan_reviews_dir.mkdir(parents=True)
    test_runs_dir.mkdir(parents=True)
    plan_path = plans_dir / f"{DTMS_SEQ}-{DTMS_SLUG}.md"
    return plan_reviews_dir, test_runs_dir, plan_path


# ---------------------------------------------------------------------------
# Task 1: Fresh start and sidecar-driven steps (no git repo needed)
# ---------------------------------------------------------------------------


def test_detect_test_task_step_no_plan_file_returns_plan(tmp_path):
    """Should return ("plan", 1, plan_path) when the plan file does not exist."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    # plan_path intentionally not created on disk
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert step == "plan"
    assert counter == 1
    assert returned_path == plan_path


def test_detect_test_task_step_sidecar_plan_reviewed_returns_implement(tmp_path):
    """Should return ("implement", 1, plan_path) when sidecar step is "plan_reviewed" and a passing plan-review file is present."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "plan_reviewed"}))
    (prd / f"{DTMS_SEQ}-{DTMS_SLUG}-plan-review-1.md").write_text(
        "Review notes\n\nPLAN_REVIEW_PASS"
    )
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert step == "implement"
    assert counter == 1
    assert returned_path == plan_path


def test_detect_test_task_step_sidecar_implemented_2_returns_test_run_2(tmp_path):
    """Should return ("test_run", 2, plan_path) when sidecar step is "implemented:2"."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "implemented:2"}))
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert step == "test_run"
    assert counter == 2
    assert returned_path == plan_path


def test_detect_test_task_step_sidecar_test_run_failed_returns_implement(tmp_path):
    """Should return ("implement", 2, plan_path) when sidecar step is "test_run_failed:1" and test-runs/01-slug-test-1.txt is present."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "test_run_failed:1"}))
    (trd / f"{DTMS_SEQ}-{DTMS_SLUG}-test-1.txt").write_text("test output")
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert step == "implement"
    assert counter == 2
    assert returned_path == plan_path


def test_detect_test_task_step_sidecar_planned_returns_plan_review(tmp_path):
    """Should return ("plan_review", 1, plan_path) when sidecar step is "planned"."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "planned"}))
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert step == "plan_review"
    assert counter == 1
    assert returned_path == plan_path


def test_detect_test_task_step_sidecar_plan_review_failed_returns_plan(tmp_path):
    """Should return ("plan", 3, plan_path) when sidecar step is "plan_review_failed:2" and plan-reviews/01-slug-plan-review-2.md is present."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "plan_review_failed:2"}))
    (prd / f"{DTMS_SEQ}-{DTMS_SLUG}-plan-review-2.md").write_text("review content")
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert step == "plan"
    assert counter == 3
    assert returned_path == plan_path


# ---------------------------------------------------------------------------
# Fall-through to plan-review detection
# ---------------------------------------------------------------------------


def test_detect_test_task_step_no_plan_review_files_returns_plan_review(tmp_path):
    """Should return ("plan_review", 1, plan_path) when no sidecar step and no plan-review files exist."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert step == "plan_review"
    assert counter == 1
    assert returned_path == plan_path


def test_detect_test_task_step_plan_review_not_passing_returns_plan(tmp_path):
    """Should return ("plan", 2, plan_path) when no sidecar step and the latest plan-review does not end with PLAN_REVIEW_PASS."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    (prd / f"{DTMS_SEQ}-{DTMS_SLUG}-plan-review-1.md").write_text("Review notes, not passing")
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert step == "plan"
    assert counter == 2
    assert returned_path == plan_path


def test_detect_test_task_step_passing_test_run_without_sidecar_step_returns_implement(tmp_path):
    """Should return ("implement", 1, plan_path) when no sidecar step and both a passing plan-review and a passing test run sit on disk.
    Reading a verify output as completion would close a task whose implementation was never made (a test run on an unimplemented tree can pass)."""
    prd, trd, plan_path = _dtms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    (prd / f"{DTMS_SEQ}-{DTMS_SLUG}-plan-review-1.md").write_text("Review notes\n\nPLAN_REVIEW_PASS")
    (trd / f"{DTMS_SEQ}-{DTMS_SLUG}-test-1.txt").write_text("Test output\n\nTEST_PASS")
    step, counter, returned_path = _detect_test_task_step(
        DTMS_SEQ, DTMS_SLUG, plan_path, prd, trd
    )
    assert (step, counter, returned_path) == ("implement", 1, plan_path)


# ---------------------------------------------------------------------------
# cli() exception-routing tests
# ---------------------------------------------------------------------------


def _cli_config() -> OrchestratorConfig:
    return OrchestratorConfig(
        max_iterations=3,
        usage_threshold_5h=90,
        usage_threshold_weekly=95,
        enable_phase_sessions=False,
    )


def _run_cli_with(monkeypatch, exc):
    """Patch load_config/run_implement/report/argv; return the list report() calls get recorded into."""
    recorded = []

    monkeypatch.setattr(main_module, "load_config", lambda project_dir=None, *, task_reviewers=None: _cli_config())

    def _raise_run_implement(project_dir, config):
        raise exc

    monkeypatch.setattr(main_module, "run_implement", _raise_run_implement)

    def _fake_report(config, outcome, project, detail, run_summary):
        recorded.append((outcome, detail))

    monkeypatch.setattr(main_module, "report", _fake_report)
    monkeypatch.setattr(sys, "argv", ["orchestrator", "implement", "."])
    return recorded


def test_cli_pipeline_stop_error_routes_to_unconverged(monkeypatch):
    """Should record Outcome.UNCONVERGED and exit via SystemExit when run_implement raises PipelineStopError."""
    recorded = _run_cli_with(monkeypatch, PipelineStopError("boom"))
    with pytest.raises(SystemExit):
        main_module.cli()
    assert recorded[-1][0] is Outcome.UNCONVERGED


def test_cli_rate_limit_error_routes_to_halted(monkeypatch):
    """Should record Outcome.HALTED when run_implement raises RateLimitError."""
    recorded = _run_cli_with(monkeypatch, RateLimitError("boom"))
    with pytest.raises(SystemExit):
        main_module.cli()
    assert recorded[-1][0] is Outcome.HALTED


def test_cli_generic_exception_routes_to_errored_and_reraises(monkeypatch):
    """Should record Outcome.ERRORED and re-raise a generic Exception."""
    recorded = _run_cli_with(monkeypatch, ValueError("boom"))
    with pytest.raises(ValueError):
        main_module.cli()
    assert recorded and recorded[-1][0] is Outcome.ERRORED


def test_cli_escalation_error_routes_to_escalated_with_no_detail(monkeypatch):
    """Should record Outcome.ESCALATED with detail=None when run_implement raises EscalationError —
    the path and model-prose excerpt the exception carries must not reach the alert."""
    recorded = _run_cli_with(monkeypatch, EscalationError("some/plan.md: a paragraph of model prose"))
    with pytest.raises(SystemExit):
        main_module.cli()
    assert recorded[-1] == (Outcome.ESCALATED, None)


# ---------------------------------------------------------------------------
# Source-level exception-type tests: both currently raise PipelineStopError,
# both should raise HaltError once task 05 lands.
# ---------------------------------------------------------------------------


def test_check_usage_limits_raises_halt_error_over_threshold(monkeypatch):
    """Should raise HaltError when session usage crosses usage_threshold_5h (red now — currently raises PipelineStopError)."""
    config = OrchestratorConfig(
        max_iterations=3,
        usage_threshold_5h=90,
        usage_threshold_weekly=95,
        enable_phase_sessions=False,
    )

    monkeypatch.setattr(usage_module, "_run_usage_probe", lambda: "Current session: 99%")

    with pytest.raises(Exception) as exc:
        _check_usage_limits(config)

    HaltError = getattr(errors_module, "HaltError", None)
    assert HaltError is not None and isinstance(exc.value, HaltError)


def test_process_task_escalated_sidecar_raises_without_constructing_agents(tmp_path, monkeypatch):
    """Should raise EscalationError immediately, before constructing any agent
    (PlannerReviewer/Implementer/PlanReviewer) and without calling mark_done/_git_commit,
    when the sidecar step is "escalated" — a resumed-but-unresolved escalation must not
    silently re-run past the decision it stopped for."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(
        json.dumps({"step": "escalated", "escalation": "planner: pick A or B (01-slug.md)"})
    )

    def _fail_if_constructed(*args, **kwargs):
        raise AssertionError("agent must not be constructed for an escalated task")

    monkeypatch.setattr(pipeline_module, "PlannerReviewer", _fail_if_constructed)
    monkeypatch.setattr(pipeline_module, "Implementer", _fail_if_constructed)
    monkeypatch.setattr(pipeline_module, "PlanReviewer", _fail_if_constructed)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("must not be called for an escalated task")

    monkeypatch.setattr(pipeline_module, "mark_done", _fail_if_called)
    monkeypatch.setattr(pipeline_module, "_git_commit", _fail_if_called)

    config = OrchestratorConfig(
        max_iterations=3,
        usage_threshold_5h=90,
        usage_threshold_weekly=95,
        enable_phase_sessions=False,
    )

    class _TaskStub:
        slug = DMS_SLUG
        title = "Some task"
        description = "Some description"
        line_number = 0

    with pytest.raises(EscalationError, match="pick A or B"):
        process_task(tmp_path, _TaskStub(), 1, config, backend=FakeAgentBackend())


def test_process_task_records_done_after_mark_and_before_commit(tmp_path, monkeypatch):
    """The sidecar reads `done` once the roadmap is marked and before the commit runs."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "implemented:1"}))
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text(
        "Review notes\n\nPLAN_REVIEW_PASS"
    )

    class _PlannerStub:
        def __init__(self, *args, **kwargs):
            self.session_id = None

        def verify(self, plan_path, out_path, prev_out_path):
            return True

    class _ImplementerStub:
        def __init__(self, *args, **kwargs):
            self.session_id = None

    monkeypatch.setattr(pipeline_module, "PlannerReviewer", _PlannerStub)
    monkeypatch.setattr(pipeline_module, "Implementer", _ImplementerStub)

    recorded: list[str | None] = []

    def _record(*args, **kwargs):
        recorded.append(_read_sessions(plan_path).get("step"))

    monkeypatch.setattr(pipeline_module, "mark_done", _record)
    monkeypatch.setattr(pipeline_module, "_git_commit", _record)
    monkeypatch.setattr(pipeline_module.state, "tasks_done", 0)

    config = OrchestratorConfig(
        max_iterations=3,
        usage_threshold_5h=90,
        usage_threshold_weekly=95,
        enable_phase_sessions=False,
    )

    class _TaskStub:
        slug = DMS_SLUG
        title = "Some task"
        description = "Some description"
        line_number = 0

    process_task(tmp_path, _TaskStub(), 1, config, backend=FakeAgentBackend())

    assert recorded == ["implemented:1", "done"]


def test_process_task_resume_past_max_iterations_raises_halt_error(tmp_path):
    """Resuming after an exhausted review cycle halts before any role runs and leaves the sidecar untouched."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    sidecar = plan_path.with_suffix(".json")
    sidecar.write_text(json.dumps({"step": "review_failed:3"}))
    (rv / f"{DMS_SEQ}-{DMS_SLUG}-review-3.md").write_text("review content")
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text(
        "Review notes\n\nPLAN_REVIEW_PASS"
    )
    before = sidecar.read_bytes()

    config = OrchestratorConfig(
        max_iterations=3,
        usage_threshold_5h=90,
        usage_threshold_weekly=95,
        enable_phase_sessions=False,
    )

    class _TaskStub:
        slug = DMS_SLUG
        title = "Some task"
        description = "Some description"
        line_number = 0

    backend = FakeAgentBackend()
    with pytest.raises(HaltError):
        process_task(tmp_path, _TaskStub(), 1, config, backend=backend)

    assert backend.calls == []
    assert sidecar.read_bytes() == before


# ---------------------------------------------------------------------------
# _resolve_roadmap_relpath: three-state resolution + owner-line gate
# ---------------------------------------------------------------------------


def _config_with_roadmap_path(roadmap_path):
    return OrchestratorConfig(
        max_iterations=3,
        usage_threshold_5h=90,
        usage_threshold_weekly=95,
        enable_phase_sessions=False,
        roadmap_path=roadmap_path,
    )


def _fake_git_config(email=None, name=None, slug=None, script_returncode=None, script_stderr=""):
    """Build a subprocess.run stand-in answering `git config user.email`/`user.name`
    and the user-slug script call (any command not starting with `git`).

    The script call returns `script_returncode` when set, else the slug with exit 0
    when `slug` is set, else exit 2 (no derivable slug)."""
    class _Result:
        def __init__(self, returncode, stdout, stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _run(cmd, cwd=None, capture_output=None, text=None):
        if cmd[0] != "git":
            if script_returncode is not None:
                return _Result(script_returncode, "", script_stderr)
            if slug is not None:
                return _Result(0, f"{slug}\n")
            return _Result(2, "", "user-slug.sh: neither user.email nor user.name yields a slug …")
        key = cmd[-1]
        value = email if key == "user.email" else name if key == "user.name" else None
        if value is None:
            return _Result(1, "")
        return _Result(0, value)

    return _run


def _install_slug_script(tmp_path, monkeypatch):
    """Point the slug-script path at a placeholder file under tmp_path (subprocess.run is patched)."""
    script = tmp_path / "slug-bin" / "user-slug.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(layout_module, "_USER_SLUG_SCRIPT", script)
    return script


def test_resolve_roadmap_relpath_absent_returns_default(tmp_path):
    """Should return 'ROADMAP.md' when roadmap_path is absent (byte-stable default)."""
    config = _config_with_roadmap_path(None)
    assert _resolve_roadmap_relpath(config, tmp_path) == "ROADMAP.md"


def test_resolve_roadmap_relpath_my_present_matching_owner(tmp_path, monkeypatch):
    """Should return the named relpath when the file exists and its owner line matches."""
    roadmaps_dir = tmp_path / ".ai-factory" / "roadmaps"
    roadmaps_dir.mkdir(parents=True)
    (roadmaps_dir / "john-doe.md").write_text("> Owner: john.doe@example.com\n\n- [ ] Task\n")
    _install_slug_script(tmp_path, monkeypatch)
    monkeypatch.setattr(layout_module.subprocess, "run", _fake_git_config(email="john.doe@example.com", slug="john-doe"))

    config = _config_with_roadmap_path("my")
    assert _resolve_roadmap_relpath(config, tmp_path) == "roadmaps/john-doe.md"


def test_resolve_roadmap_relpath_my_name_derived_owner_matches(tmp_path, monkeypatch):
    """Should match the owner line against `name` when git email is unset (name-fallback derivation)."""
    roadmaps_dir = tmp_path / ".ai-factory" / "roadmaps"
    roadmaps_dir.mkdir(parents=True)
    (roadmaps_dir / "alice.md").write_text("> Owner: Alice\n\n- [ ] Task\n")
    _install_slug_script(tmp_path, monkeypatch)
    monkeypatch.setattr(layout_module.subprocess, "run", _fake_git_config(email=None, name="Alice", slug="alice"))

    config = _config_with_roadmap_path("my")
    assert _resolve_roadmap_relpath(config, tmp_path) == "roadmaps/alice.md"


def test_resolve_roadmap_relpath_my_missing_file_raises_halt(tmp_path, monkeypatch):
    """Should raise HaltError with a fixed first line and the expected path on a later line
    when the personal roadmap file is missing."""
    _install_slug_script(tmp_path, monkeypatch)
    monkeypatch.setattr(layout_module.subprocess, "run", _fake_git_config(email="john.doe@example.com", slug="john-doe"))

    config = _config_with_roadmap_path("my")
    with pytest.raises(HaltError) as exc:
        _resolve_roadmap_relpath(config, tmp_path)

    assert str(exc.value).splitlines()[0] == "Named roadmap not found"
    assert "roadmaps/john-doe.md" in str(exc.value).split("\n", 1)[1]


def test_resolve_roadmap_relpath_my_owner_mismatch_raises_halt(tmp_path, monkeypatch):
    """Should raise HaltError when the owner line names a different identity."""
    roadmaps_dir = tmp_path / ".ai-factory" / "roadmaps"
    roadmaps_dir.mkdir(parents=True)
    (roadmaps_dir / "john-doe.md").write_text("> Owner: someone.else@example.org\n\n- [ ] Task\n")
    _install_slug_script(tmp_path, monkeypatch)
    monkeypatch.setattr(layout_module.subprocess, "run", _fake_git_config(email="john.doe@example.com", slug="john-doe"))

    config = _config_with_roadmap_path("my")
    with pytest.raises(HaltError):
        _resolve_roadmap_relpath(config, tmp_path)


def test_resolve_roadmap_relpath_my_owner_mismatch_halt_has_fixed_first_line(tmp_path, monkeypatch):
    """HaltError's message opens with the fixed clause alone; the path and both quoted
    identities move to a later line rather than sharing the first."""
    roadmaps_dir = tmp_path / ".ai-factory" / "roadmaps"
    roadmaps_dir.mkdir(parents=True)
    (roadmaps_dir / "john-doe.md").write_text("> Owner: someone.else@example.org\n\n- [ ] Task\n")
    _install_slug_script(tmp_path, monkeypatch)
    monkeypatch.setattr(layout_module.subprocess, "run", _fake_git_config(email="john.doe@example.com", slug="john-doe"))

    config = _config_with_roadmap_path("my")
    with pytest.raises(HaltError) as exc:
        _resolve_roadmap_relpath(config, tmp_path)

    lines = str(exc.value).splitlines()
    assert lines[0] == "Named roadmap owner line does not match the current git identity"
    remainder = str(exc.value).split("\n", 1)[1]
    assert "roadmaps/john-doe.md" in remainder
    assert "someone.else@example.org" in remainder
    assert "john.doe@example.com" in remainder


def test_resolve_roadmap_relpath_my_malformed_first_line_raises_halt(tmp_path, monkeypatch):
    """Should raise HaltError when the file's first line isn't a well-formed owner line."""
    roadmaps_dir = tmp_path / ".ai-factory" / "roadmaps"
    roadmaps_dir.mkdir(parents=True)
    (roadmaps_dir / "john-doe.md").write_text("# Not an owner line\n\n- [ ] Task\n")
    _install_slug_script(tmp_path, monkeypatch)
    monkeypatch.setattr(layout_module.subprocess, "run", _fake_git_config(email="john.doe@example.com", slug="john-doe"))

    config = _config_with_roadmap_path("my")
    with pytest.raises(HaltError):
        _resolve_roadmap_relpath(config, tmp_path)


def test_resolve_roadmap_relpath_my_script_exit_2_raises_identity_halt(tmp_path, monkeypatch):
    """Should raise the identity HaltError when the script reports no derivable slug (exit 2)."""
    _install_slug_script(tmp_path, monkeypatch)
    monkeypatch.setattr(layout_module.subprocess, "run", _fake_git_config(email=None, name=None))

    config = _config_with_roadmap_path("my")
    with pytest.raises(HaltError) as exc:
        _resolve_roadmap_relpath(config, tmp_path)

    assert str(exc.value).splitlines()[0].startswith("roadmap_path is 'my' but no git identity is set")


def test_resolve_roadmap_relpath_my_missing_script_raises_halt(tmp_path, monkeypatch):
    """Should halt with a fixed first line and the script path below when the script is not installed."""
    missing = tmp_path / "nowhere" / "user-slug.sh"
    monkeypatch.setattr(layout_module, "_USER_SLUG_SCRIPT", missing)
    monkeypatch.setattr(
        layout_module.subprocess, "run", _fake_git_config(email="john.doe@example.com", slug="john-doe"),
    )

    config = _config_with_roadmap_path("my")
    with pytest.raises(HaltError) as exc:
        _resolve_roadmap_relpath(config, tmp_path)

    assert str(exc.value).splitlines()[0] == "User-slug script not found"
    assert str(missing) in str(exc.value).split("\n", 1)[1]


def test_resolve_roadmap_relpath_my_script_failure_raises_halt(tmp_path, monkeypatch):
    """Should halt with a fixed first line and the script's stderr below on an exit other than 0 or 2."""
    _install_slug_script(tmp_path, monkeypatch)
    monkeypatch.setattr(
        layout_module.subprocess, "run",
        _fake_git_config(
            email="john.doe@example.com", script_returncode=1,
            script_stderr="user-slug.sh: git is not installed\n",
        ),
    )

    config = _config_with_roadmap_path("my")
    with pytest.raises(HaltError) as exc:
        _resolve_roadmap_relpath(config, tmp_path)

    assert str(exc.value).splitlines()[0] == "User-slug script failed"
    assert "user-slug.sh: git is not installed" in str(exc.value).split("\n", 1)[1]


def test_resolve_roadmap_relpath_my_script_not_executable_raises_halt(tmp_path, monkeypatch):
    """Should treat an installed script that cannot be executed as a script failure."""
    _install_slug_script(tmp_path, monkeypatch)
    git_only = _fake_git_config(email="john.doe@example.com")

    def _run(cmd, cwd=None, capture_output=None, text=None):
        if cmd[0] != "git":
            raise PermissionError("Permission denied")
        return git_only(cmd, cwd=cwd, capture_output=capture_output, text=text)

    monkeypatch.setattr(layout_module.subprocess, "run", _run)

    config = _config_with_roadmap_path("my")
    with pytest.raises(HaltError) as exc:
        _resolve_roadmap_relpath(config, tmp_path)

    assert str(exc.value).splitlines()[0] == "User-slug script failed"


def test_resolve_roadmap_relpath_explicit_returned_verbatim(tmp_path):
    """Should return an explicit value verbatim, with no owner check."""
    config = _config_with_roadmap_path("roadmaps/alice.md")
    assert _resolve_roadmap_relpath(config, tmp_path) == "roadmaps/alice.md"


# ---------------------------------------------------------------------------
# _tests_sibling: derive the test-roadmap sibling from the roadmap in play
# ---------------------------------------------------------------------------


def test_tests_sibling_default_pair_is_special_cased():
    """Should map 'ROADMAP.md' to 'ROADMAP_TESTS.md' (not '-tests' suffixing)."""
    assert _tests_sibling("ROADMAP.md") == "ROADMAP_TESTS.md"


def test_tests_sibling_named_roadmap_uses_suffix():
    """Should map a named roadmap to its '-tests' suffixed sibling in the same directory."""
    assert _tests_sibling("roadmaps/john-doe.md") == "roadmaps/john-doe-tests.md"


# ---------------------------------------------------------------------------
# _artifact_subdir: key artifact dirs by the roadmap file's stem; default pair stays flat
# ---------------------------------------------------------------------------


def test_artifact_subdir_default_roadmap_is_flat():
    """Should return None for the default 'ROADMAP.md' (byte-stable flat layout)."""
    assert _artifact_subdir("ROADMAP.md") is None


def test_artifact_subdir_default_tests_roadmap_is_flat():
    """Should return None for the default 'ROADMAP_TESTS.md' (byte-stable flat layout)."""
    assert _artifact_subdir("ROADMAP_TESTS.md") is None


def test_artifact_subdir_named_roadmap_uses_stem():
    """Should key a named roadmap's artifacts by its stem."""
    assert _artifact_subdir("roadmaps/john-doe.md") == "john-doe"


def test_artifact_subdir_named_tests_roadmap_uses_stem():
    """Should key a named test-roadmap sibling by its main roadmap's stem (one stem per roadmap pair)."""
    assert _artifact_subdir("roadmaps/john-doe-tests.md") == "john-doe"


def test_artifact_subdir_explicit_path_tests_sibling_uses_stem():
    """Should key an explicit, non-'roadmaps/' roadmap's test sibling by its main roadmap's stem."""
    assert _artifact_subdir("custom-tests.md") == "custom"


def test_artifact_subdir_track_file_uses_stem():
    """Should key any other roadmap file (e.g. a track file) by its stem."""
    assert _artifact_subdir("ROADMAP.watch.md") == "ROADMAP.watch"


# ---------------------------------------------------------------------------
# _artifact_dir: one derivation for every artifact directory; pure, creates nothing
# ---------------------------------------------------------------------------


def test_artifact_dir_flat_mode_has_no_subdir(tmp_path):
    """Should place the directory directly under .ai-factory when the mode has no subdirectory."""
    result = _artifact_dir(tmp_path, IMPLEMENT_MODE, "plans")
    assert result == tmp_path / ".ai-factory" / "plans"
    assert not result.exists()


def test_artifact_dir_named_mode_appends_subdir(tmp_path):
    """Should append the mode's artifact subdirectory when it has one."""
    mode = IMPLEMENT_MODE._replace(artifact_subdir="john-doe")
    assert _artifact_dir(tmp_path, mode, "plans") == tmp_path / ".ai-factory" / "plans" / "john-doe"


def test_artifact_dir_test_mode_verify_dirname(tmp_path):
    """Should derive the test-run output directory from the verify kind's dirname."""
    result = _artifact_dir(tmp_path, TEST_MODE, TEST_MODE.verify.output_dirname)
    assert result == tmp_path / ".ai-factory" / "test-runs"


# ---------------------------------------------------------------------------
# _detect_task_step over subdir'd artifact dirs — dispatch unchanged one level deeper
# ---------------------------------------------------------------------------


def test_detect_task_step_subdird_dirs_dispatches_same_as_flat(tmp_path):
    """Should dispatch identically to the flat case when plans/plan-reviews/reviews are nested one level deeper under a per-roadmap subdir."""
    plans_dir = tmp_path / ".ai-factory" / "plans" / "john-doe"
    plan_reviews_dir = tmp_path / ".ai-factory" / "plan-reviews" / "john-doe"
    reviews_dir = tmp_path / ".ai-factory" / "reviews" / "john-doe"
    plans_dir.mkdir(parents=True)
    plan_reviews_dir.mkdir(parents=True)
    reviews_dir.mkdir(parents=True)
    plan_path = plans_dir / f"{DMS_SEQ}-{DMS_SLUG}.md"
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "implemented:1"}))
    step, counter, returned_path = _detect_task_step(
        DMS_SEQ, DMS_SLUG, plan_path, plan_reviews_dir, reviews_dir
    )
    assert step == "review"
    assert counter == 1
    assert returned_path == plan_path


# ---------------------------------------------------------------------------
# _next_number: max-based numbering over well-formed files
# ---------------------------------------------------------------------------


def test_next_number_empty_directory_returns_one(tmp_path):
    """Should return 1 when the directory is empty (the `if not existing: return 1` branch)."""
    assert _next_number(tmp_path) == 1


def test_next_number_single_well_formed_file_returns_one_past(tmp_path):
    """Should return one past the number when a single well-formed file exists."""
    (tmp_path / "03-x.md").write_text("")
    assert _next_number(tmp_path) == 4


def test_next_number_gap_returns_one_past_highest(tmp_path):
    """Should return one past the highest number, not the count, when files exist with a gap
    (pinning max+1 vs. a naive count+1 that would return 4)."""
    (tmp_path / "01-a.md").write_text("")
    (tmp_path / "02-b.md").write_text("")
    (tmp_path / "05-c.md").write_text("")
    assert _next_number(tmp_path) == 6


# ---------------------------------------------------------------------------
# _next_number: mixed digit / non-digit stems
# ---------------------------------------------------------------------------


def test_next_number_skips_later_sorting_non_digit_stem(tmp_path):
    """Should ignore a non-digit stem regardless of position and take the digit stem's value
    into the max: only "01-a.md" contributes a number, so the result is 1 + 1 == 2."""
    (tmp_path / "01-a.md").write_text("")
    (tmp_path / "zz-notes.md").write_text("")
    assert _next_number(tmp_path) == 2


def test_next_number_skips_earlier_sorting_non_digit_stem(tmp_path):
    """Should ignore a non-digit stem regardless of position and take the digit stem's value
    into the max: only "02-b.md" contributes a number, so the result is 2 + 1 == 3.
    Deliberate complement to the zz-notes case above — exercises the opposite placement of
    the stray non-digit file, confirming non-digit stems are excluded irrespective of order."""
    (tmp_path / "aa-notes.md").write_text("")
    (tmp_path / "02-b.md").write_text("")
    assert _next_number(tmp_path) == 3


def test_next_number_no_digit_stems_falls_back_to_count_plus_one(tmp_path):
    """Should fall back to count + 1 when no file has a digit-prefixed stem (the loop exhausts
    without returning, hitting the `len(existing)+1` fallback line)."""
    (tmp_path / "notes.md").write_text("")
    (tmp_path / "readme.md").write_text("")
    assert _next_number(tmp_path) == 3


# ---------------------------------------------------------------------------
# _next_number: double-digit rollover and the lexicographic-sort boundary
# ---------------------------------------------------------------------------


def test_next_number_rolls_from_single_to_double_digit(tmp_path):
    """Should roll from single- to double-digit numbering: the max over the parsed values
    [8, 9] yields 10, independent of any sort order or digit width."""
    (tmp_path / "08-a.md").write_text("")
    (tmp_path / "09-b.md").write_text("")
    assert _next_number(tmp_path) == 10


def test_next_number_sole_double_digit_entry(tmp_path):
    """Should return the correct next number when the sole entry is already a double-digit file
    (guards an off-by-one specific to multi-character digit prefixes)."""
    (tmp_path / "10-c.md").write_text("")
    assert _next_number(tmp_path) == 11


def test_next_number_mixed_width_resolves_numerically(tmp_path):
    """Should resolve a mixed-width set by numeric value, not string order: "9-a.md" and
    "10-b.md" yield max(9, 10) + 1 == 11, regardless of how the stems would sort as strings."""
    (tmp_path / "9-a.md").write_text("")
    (tmp_path / "10-b.md").write_text("")
    assert _next_number(tmp_path) == 11


def test_next_number_three_digit_width_boundary(tmp_path):
    """Should resolve past the two-to-three-digit width boundary numerically: "99-x.md" and
    "100-y.md" yield max(99, 100) + 1 == 101, not the already-used 100 that a lexicographic
    (string-sort) comparison would collide on."""
    (tmp_path / "99-x.md").write_text("")
    (tmp_path / "100-y.md").write_text("")
    assert _next_number(tmp_path) == 101


# ---------------------------------------------------------------------------
# process_task with the real roles and a fake backend
# ---------------------------------------------------------------------------


class _TaskStub:
    slug = DMS_SLUG
    title = "Some task"
    description = "Some description"
    line_number = 0


def _flow_config() -> OrchestratorConfig:
    return OrchestratorConfig(
        max_iterations=3,
        usage_threshold_5h=90,
        usage_threshold_weekly=95,
        enable_phase_sessions=False,
    )


def _neutralize_side_effects(monkeypatch):
    """Replace the roadmap mark, the commit, the report and the in-loop `git add`."""
    monkeypatch.setattr(pipeline_module, "mark_done", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_module, "_git_commit", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_module, "report", lambda *a, **k: None)
    monkeypatch.setattr(
        pipeline_module,
        "subprocess",
        SimpleNamespace(run=lambda *a, **k: subprocess.CompletedProcess(a, 0)),
    )
    monkeypatch.setattr(pipeline_module.state, "tasks_done", 0)


def _seed_review_failed(tmp_path: Path, sidecar: dict) -> tuple[Path, Path]:
    """Seed a task resumed after a failed review; return (plan_path, review_2_path)."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps(sidecar))
    (rv / f"{DMS_SEQ}-{DMS_SLUG}-review-1.md").write_text("Findings, no pass")
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text("Fine\n\nPLAN_REVIEW_PASS")
    return plan_path, rv / f"{DMS_SEQ}-{DMS_SLUG}-review-2.md"


def _review_writing_backend(review_2: Path) -> FakeAgentBackend:
    def _write(call):
        if call.role is AgentRole.PLANNER_REVIEWER:
            review_2.write_text("All good\n\nREVIEW_PASS")

    return FakeAgentBackend(on_call=_write)


def test_process_task_restores_saved_sessions_from_disk(tmp_path, monkeypatch):
    """A saved planner identity beats the phase identity; the implementer resumes its own."""
    plan_path, review_2 = _seed_review_failed(
        tmp_path, {"planner": "saved-planner", "implementer": "saved-impl", "step": "review_failed:1"}
    )
    _neutralize_side_effects(monkeypatch)
    backend = _review_writing_backend(review_2)

    process_task(tmp_path, _TaskStub(), 1, _flow_config(), phase_session_id="phase-sid", backend=backend)

    impl_call, review_call = backend.calls
    assert impl_call.role is AgentRole.IMPLEMENTER
    assert impl_call.session_id == "saved-impl"
    assert str(review_2.with_name(f"{DMS_SEQ}-{DMS_SLUG}-review-1.md")) in impl_call.prompt
    for name in ("implementer", "escalation"):
        assert _load_prompt(name) in impl_call.instructions
    assert impl_call.instructions.count(_load_prompt("execution")) == 1
    assert review_call.role is AgentRole.PLANNER_REVIEWER
    assert review_call.session_id == "saved-planner"
    for name in ("planner", "reviewer", "escalation"):
        assert _load_prompt(name) in review_call.instructions
    sessions = _read_sessions(plan_path)
    assert sessions["step"] == "done"
    assert sessions["planner"] == backend.returned[1]
    assert sessions["implementer"] == backend.returned[0]


def test_process_task_falls_back_to_phase_session_without_saved_planner(tmp_path, monkeypatch):
    plan_path, review_2 = _seed_review_failed(
        tmp_path, {"implementer": "saved-impl", "step": "review_failed:1"}
    )
    _neutralize_side_effects(monkeypatch)
    backend = _review_writing_backend(review_2)

    process_task(tmp_path, _TaskStub(), 1, _flow_config(), phase_session_id="phase-sid", backend=backend)

    impl_call, review_call = backend.calls
    assert impl_call.session_id == "saved-impl"
    assert review_call.session_id == "phase-sid"


def test_process_task_test_mode_verifies_by_shell_not_backend(tmp_path, monkeypatch):
    """Test mode makes exactly the planning, plan-review and implement backend calls; the test run is shell."""
    _neutralize_side_effects(monkeypatch)

    def _write(call):
        if call.role is AgentRole.PLANNER_REVIEWER:
            target_path(call.prompt).write_text("# Plan\n\n## Test Command\n`true`\n")
        elif call.role is AgentRole.PLAN_REVIEWER:
            target_path(call.prompt).write_text("Fine\n\nPLAN_REVIEW_PASS")

    backend = FakeAgentBackend(on_call=_write)

    process_task(tmp_path, _TaskStub(), 1, _flow_config(), TEST_MODE, backend=backend)

    assert [c.role for c in backend.calls] == [
        AgentRole.PLANNER_REVIEWER, AgentRole.PLAN_REVIEWER, AgentRole.IMPLEMENTER,
    ]
    assert _load_prompt("test-planner") in backend.calls[0].instructions
    test_run = _artifact_dir(tmp_path, TEST_MODE, "test-runs") / f"{DMS_SEQ}-{DMS_SLUG}-test-1.txt"
    assert test_run.read_text().rstrip().endswith("TEST_PASS")


def _write_loop_roadmaps(project_dir, content=""):
    factory_dir = project_dir / ".ai-factory"
    factory_dir.mkdir(exist_ok=True)
    for name in ("ROADMAP.md", "ROADMAP_TESTS.md"):
        (factory_dir / name).write_text(content)


def _unexpected_runtime(*args, **kwargs):
    pytest.fail("Unselected runtime was reached")


@pytest.mark.parametrize("loop_name, tests_roadmap", [("_implement_loop", False), ("_test_loop", True)])
@pytest.mark.parametrize("backend_name", [None, "claude", "codex"])
def test_loops_construct_one_backend_and_forward_it(tmp_path, monkeypatch, loop_name, tests_roadmap, backend_name):
    constructor_calls = []
    sentinel = FakeAgentBackend()
    selected = backend_name or "claude"

    def factory(agents=None):
        constructor_calls.append(sentinel)
        return sentinel

    captured = {}
    process_calls = []
    policy_calls = []
    make_calls = []
    real_make_runtime = main_module._make_runtime
    config = _config_with_roadmap_path(None)
    if backend_name is not None:
        config = dataclasses.replace(config, runtime=backend_name)
    monkeypatch.setattr(main_module, "ClaudeAgentBackend", factory if selected == "claude" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "CodexAgentBackend", factory if selected == "codex" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_usage_limits", (lambda c: policy_calls.append((c,))) if selected == "claude" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_codex_usage_limits", (lambda c, p: policy_calls.append((c, p))) if selected == "codex" else _unexpected_runtime)

    def make_runtime(c, p):
        make_calls.append((c, p))
        return real_make_runtime(c, p)

    def capture_loop(project_dir, roadmap_path, config, process_fn, mode, *, usage_check):
        captured.update(process_fn=process_fn, mode=mode, usage_check=usage_check)

    monkeypatch.setattr(main_module, "_make_runtime", make_runtime)
    monkeypatch.setattr(main_module, "_run_dynamic_loop", capture_loop)
    monkeypatch.setattr(main_module, "process_task", lambda *a, **k: process_calls.append((a, k)))
    _write_loop_roadmaps(tmp_path)

    getattr(main_module, loop_name)(tmp_path, config)
    assert policy_calls == []
    captured["usage_check"]()
    captured["process_fn"](_TaskStub(), 1, "sid")

    assert make_calls == [(config, tmp_path)]
    assert len(constructor_calls) == 1
    assert policy_calls == ([(config,)] if selected == "claude" else [(config, tmp_path)])
    (_, kwargs), = process_calls
    assert kwargs["backend"] is sentinel
    assert kwargs["phase_session_id"] == "sid"
    if tests_roadmap:
        assert captured["mode"].planner_prompt_name == "test-planner"
        assert captured["mode"].verify is TEST_RUN_VERIFY
    else:
        assert captured["mode"].verify is REVIEW_VERIFY


@pytest.mark.parametrize("loop_name", ["_implement_loop", "_test_loop"])
@pytest.mark.parametrize("selected", ["claude", "codex"])
@pytest.mark.parametrize("missing", [False, True])
def test_selected_constructor_preflights_without_probe(tmp_path, monkeypatch, capsys, loop_name, selected, missing):
    resolutions = []
    loops = []
    selected_module = claude_module if selected == "claude" else codex_module
    other_module = codex_module if selected == "claude" else claude_module
    monkeypatch.setattr(claude_module, "_CLAUDE_BIN", None)

    def resolve():
        resolutions.append(selected)
        if missing:
            raise RuntimeError("Selected executable missing")
        return "/offline/" + selected

    monkeypatch.setattr(selected_module, "_resolve_" + selected, resolve)
    other = "codex" if selected == "claude" else "claude"
    monkeypatch.setattr(other_module, "_resolve_" + other, _unexpected_runtime)
    monkeypatch.setattr(main_module, "CodexAgentBackend" if other == "codex" else "ClaudeAgentBackend", _unexpected_runtime)
    for module, name in ((main_module, "_check_usage_limits"), (main_module, "_check_codex_usage_limits"),
                         (usage_module, "_run_usage_probe"), (usage_module, "_read_rate_limits")):
        monkeypatch.setattr(module, name, _unexpected_runtime)
    monkeypatch.setattr(main_module, "_run_dynamic_loop", lambda *a, **k: loops.append(k))
    _write_loop_roadmaps(tmp_path)
    config = dataclasses.replace(_flow_config(), runtime=selected)

    if missing:
        with pytest.raises(RuntimeError, match="Selected executable missing"):
            getattr(main_module, loop_name)(tmp_path, config)
        assert loops == []
    else:
        getattr(main_module, loop_name)(tmp_path, config)
        assert len(loops) == 1
    assert resolutions == [selected]
    assert not (tmp_path / ".ai-factory" / "plans").exists()
    output = capsys.readouterr().out
    if missing:
        assert output == f"Agent runtime: {selected}\n"
    else:
        assert output.startswith(f"Agent runtime: {selected}\n")


@pytest.mark.parametrize("loop_name", ["_implement_loop", "_test_loop"])
@pytest.mark.parametrize("selected", ["claude", "codex"])
@pytest.mark.parametrize("overrides", [False, True])
def test_loops_apply_and_print_resolved_settings(tmp_path, monkeypatch, capsys, loop_name, selected, overrides):
    _write_loop_roadmaps(tmp_path)
    block = {
        "planner_reviewer": {"model": "operator-model"},
        "implementer": {"effort": "operator-effort"},
    } if overrides else {}
    config = dataclasses.replace(
        _flow_config(), runtime=selected, **{f"{selected}_agents": block},
    )
    selected_module = claude_module if selected == "claude" else codex_module
    selected_name = "ClaudeAgentBackend" if selected == "claude" else "CodexAgentBackend"
    other_name = "CodexAgentBackend" if selected == "claude" else "ClaudeAgentBackend"
    real_backend = getattr(main_module, selected_name)
    constructed = []
    loops = []
    monkeypatch.setattr(claude_module, "_CLAUDE_BIN", None)
    monkeypatch.setattr(selected_module, "_resolve_" + selected, lambda: "/offline/" + selected)
    other_module = codex_module if selected == "claude" else claude_module
    monkeypatch.setattr(other_module, "_resolve_codex" if selected == "claude" else "_resolve_claude", _unexpected_runtime)
    monkeypatch.setattr(main_module, other_name, _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_usage_limits", _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_codex_usage_limits", _unexpected_runtime)
    monkeypatch.setattr(main_module, "process_task", _unexpected_runtime)

    def construct(agents):
        constructed.append(agents)
        return real_backend(agents)

    monkeypatch.setattr(main_module, selected_name, construct)
    monkeypatch.setattr(main_module, "_run_dynamic_loop", lambda *a, **k: loops.append(k))

    getattr(main_module, loop_name)(tmp_path, config)

    assert len(constructed) == 1
    assert constructed[0] is block
    assert len(loops) == 1
    if selected == "claude":
        role_lines = [
            "  planner_reviewer: opus (high)",
            "  plan_reviewer: opus (high)",
            "  implementer: sonnet (high)",
        ]
        if overrides:
            role_lines[0] = "  planner_reviewer: operator-model (high)"
            role_lines[2] = "  implementer: sonnet (operator-effort)"
    else:
        role_lines = [
            "  planner_reviewer: gpt-6.1-sol (high)",
            "  plan_reviewer: gpt-6.1-sol (high)",
            "  implementer: gpt-6.1-sol (high)",
        ]
        if overrides:
            role_lines[0] = "  planner_reviewer: operator-model (high)"
            role_lines[2] = "  implementer: gpt-6.1-sol (operator-effort)"
    reviewer_line = "test run" if loop_name == "_test_loop" else "run"
    assert capsys.readouterr().out == "\n".join(
        [f"Agent runtime: {selected}", *role_lines, f"Task reviewer: {reviewer_line}", "Plan review iterations: 3"]
    ) + "\n"


# ---------------------------------------------------------------------------
# The reviewer choice at the composition root and the run's printed choices
# ---------------------------------------------------------------------------


def _capture_loop_mode(monkeypatch, project_dir):
    captured = {}
    monkeypatch.setattr(main_module, "_make_runtime", lambda c, p: (FakeAgentBackend(), lambda: None))
    monkeypatch.setattr(
        main_module, "_run_dynamic_loop",
        lambda project_dir, roadmap_path, config, process_fn, *, mode, usage_check: captured.update(mode=mode),
    )
    _write_loop_roadmaps(project_dir)
    return captured


@pytest.mark.parametrize("reviewer,kind", [("run", REVIEW_VERIFY), ("head", HEAD_VERIFY)])
def test_implement_loop_mode_carries_the_kind_the_setting_names(tmp_path, monkeypatch, reviewer, kind):
    captured = _capture_loop_mode(monkeypatch, tmp_path)
    config = dataclasses.replace(_flow_config(), task_reviewer=reviewer)

    main_module._implement_loop(tmp_path, config)

    assert captured["mode"].verify is kind


def test_implement_loop_reads_task_reviewer_from_the_overlay(tmp_path, monkeypatch):
    """The setting reaches the kind through the merge: an overlay 'head' selects the head's kind."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    captured = _capture_loop_mode(monkeypatch, project_dir)
    _write_config(tmp_path, monkeypatch)
    _write_override(project_dir, {"task_reviewer": "head"})
    config = load_config(project_dir, task_reviewers=main_module.TASK_REVIEWERS)

    main_module._implement_loop(project_dir, config)

    assert config.task_reviewer == "head"
    assert captured["mode"].verify is HEAD_VERIFY


def test_test_loop_mode_carries_the_test_run_when_the_setting_says_head(tmp_path, monkeypatch):
    captured = _capture_loop_mode(monkeypatch, tmp_path)
    config = dataclasses.replace(_flow_config(), task_reviewer="head")

    main_module._test_loop(tmp_path, config)

    assert captured["mode"].verify is TEST_RUN_VERIFY


@pytest.mark.parametrize("loop_name,setting,reviewer_line", [
    ("_implement_loop", None, "run"),
    ("_implement_loop", "head", "head"),
    ("_test_loop", "head", "test run"),
])
@pytest.mark.parametrize("plan_review_iterations,expected_budget", [(None, 3), (0, 0), (5, 5)])
def test_loops_print_reviewer_and_budget_after_the_agent_lines(
    tmp_path, monkeypatch, capsys, loop_name, setting, reviewer_line, plan_review_iterations, expected_budget,
):
    _capture_loop_mode(monkeypatch, tmp_path)
    config = _flow_config()
    config.plan_review_iterations = plan_review_iterations
    if setting is not None:
        config.task_reviewer = setting

    getattr(main_module, loop_name)(tmp_path, config)

    assert capsys.readouterr().out == (
        f"Task reviewer: {reviewer_line}\nPlan review iterations: {expected_budget}\n"
    )


@pytest.mark.parametrize("value", ["universe", None, "", "RUN", "Head"])
def test_task_reviewer_outside_the_names_exits_naming_the_supplying_file(tmp_path, monkeypatch, value):
    base_path = _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    overlay_path = _write_override(project_dir, {"task_reviewer": value})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, task_reviewers=main_module.TASK_REVIEWERS)

    message = str(exc.value)
    assert "task_reviewer" in message
    assert repr(value) in message
    assert str(overlay_path) in message
    assert str(base_path) not in message


def test_task_reviewer_outside_names_in_base_exits_naming_base(tmp_path, monkeypatch):
    base_path = _write_config(tmp_path, monkeypatch, {"task_reviewer": "universe"})
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    overlay_path = _write_override(project_dir, {"max_iterations": 4})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, task_reviewers=main_module.TASK_REVIEWERS)

    message = str(exc.value)
    assert "task_reviewer" in message
    assert "universe" in message
    assert str(base_path) in message
    assert str(overlay_path) not in message


def test_absent_task_reviewer_in_base_and_overlay_reads_run(tmp_path, monkeypatch):
    """A project that sets nothing runs with the run's reviewer: the absent default is 'run'."""
    _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_override(project_dir, {"max_iterations": 4})

    config = load_config(project_dir, task_reviewers=main_module.TASK_REVIEWERS)

    assert config.task_reviewer == "run"


@pytest.mark.parametrize("value", [["run"], {"reviewer": "run"}])
def test_non_string_task_reviewer_exits_with_the_diagnostic(tmp_path, monkeypatch, value):
    """A list or object value is a diagnostic like any other bad value, not a traceback."""
    base_path = _write_config(tmp_path, monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    overlay_path = _write_override(project_dir, {"task_reviewer": value})

    with pytest.raises(SystemExit) as exc:
        load_config(project_dir, task_reviewers=main_module.TASK_REVIEWERS)

    message = str(exc.value)
    assert "task_reviewer" in message
    assert repr(value) in message
    assert str(overlay_path) in message
    assert str(base_path) not in message


@pytest.mark.parametrize("selected", ["claude", "codex"])
def test_runtime_reads_only_selected_block_and_prints_backend_mapping(tmp_path, monkeypatch, capsys, selected):
    block = {"planner_reviewer": {"model": "raw-model", "effort": "raw-effort"}}

    class SelectedConfig:
        runtime = selected

        @property
        def claude_agents(self):
            assert selected == "claude", "Unselected Claude block was read"
            return block

        @property
        def codex_agents(self):
            assert selected == "codex", "Unselected Codex block was read"
            return block

    backend = FakeAgentBackend()
    backend.role_settings = {
        AgentRole.IMPLEMENTER: RoleSetting("resolved-builder", "builder-effort"),
        AgentRole.PLANNER_REVIEWER: RoleSetting("resolved-planner", "planner-effort"),
        AgentRole.PLAN_REVIEWER: RoleSetting("resolved-reviewer", "reviewer-effort"),
    }
    constructed = []

    def construct(agents):
        constructed.append(agents)
        return backend

    monkeypatch.setattr(main_module, "ClaudeAgentBackend", construct if selected == "claude" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "CodexAgentBackend", construct if selected == "codex" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_usage_limits", _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_codex_usage_limits", _unexpected_runtime)

    result, usage_check = main_module._make_runtime(SelectedConfig(), tmp_path)

    assert result is backend
    assert len(constructed) == 1
    assert constructed[0] is block
    assert callable(usage_check)
    assert capsys.readouterr().out == (
        f"Agent runtime: {selected}\n"
        "  implementer: resolved-builder (builder-effort)\n"
        "  planner_reviewer: resolved-planner (planner-effort)\n"
        "  plan_reviewer: resolved-reviewer (reviewer-effort)\n"
    )


@pytest.mark.parametrize("loop_name", ["_implement_loop", "_test_loop"])
@pytest.mark.parametrize("selected", ["claude", "codex"])
def test_mode_assembly_binds_merged_config(tmp_path, monkeypatch, loop_name, selected):
    _write_loop_roadmaps(tmp_path)
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps({
        "max_iterations": 3, "enable_phase_sessions": False,
        "runtime": "claude" if selected == "codex" else "codex",
        "usage_threshold_5h": 5, "usage_threshold_weekly": 7,
    }))
    (tmp_path / ".ai-factory" / "orchestrator.json").write_text(json.dumps({
        "runtime": selected, "usage_threshold_5h": 61, "usage_threshold_weekly": 72,
    }))
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(base_path))
    config = load_config(tmp_path, task_reviewers=main_module.TASK_REVIEWERS)
    config.roadmap_path = None
    calls = []
    monkeypatch.setattr(main_module, "ClaudeAgentBackend", (lambda agents=None: FakeAgentBackend()) if selected == "claude" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "CodexAgentBackend", (lambda agents=None: FakeAgentBackend()) if selected == "codex" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_usage_limits", (lambda c: calls.append((c, None))) if selected == "claude" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_codex_usage_limits", (lambda c, p: calls.append((c, p))) if selected == "codex" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "_run_dynamic_loop", lambda *a, usage_check, **k: usage_check())

    getattr(main_module, loop_name)(tmp_path, config)

    assert calls == [(config, tmp_path if selected == "codex" else None)]
    assert calls[0][0] is config
    assert config.runtime == selected
    assert (config.usage_threshold_5h, config.usage_threshold_weekly) == (61, 72)


@pytest.mark.parametrize("loop_name", ["_implement_loop", "_test_loop"])
@pytest.mark.parametrize("selected", ["claude", "codex"])
@pytest.mark.parametrize("queue", ["pending", "halt", "unavailable", "empty", "completed"])
def test_real_loop_dispatches_selected_policy_before_each_task(tmp_path, monkeypatch, capsys, loop_name, selected, queue):
    events = []
    sentinel = FakeAgentBackend()
    content = "" if queue == "empty" else "## Work\n- [ ] **First** — do first\n- [ ] **Second** — do second\n"
    if queue == "completed":
        content = content.replace("[ ]", "[x]")
    _write_loop_roadmaps(tmp_path, content)
    monkeypatch.setattr(pipeline_module.state, "stop_requested", False)
    monkeypatch.setattr(pipeline_module, "report", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_module, "_run_summary", lambda: "offline")
    config = dataclasses.replace(_flow_config(), runtime=selected)

    def construct(agents=None):
        events.append("constructor")
        return sentinel

    monkeypatch.setattr(main_module, "ClaudeAgentBackend", construct if selected == "claude" else _unexpected_runtime)
    monkeypatch.setattr(main_module, "CodexAgentBackend", construct if selected == "codex" else _unexpected_runtime)
    monkeypatch.setattr(claude_module, "_resolve_claude", _unexpected_runtime)
    monkeypatch.setattr(codex_module, "_resolve_codex", _unexpected_runtime)
    monkeypatch.setattr(main_module, "_check_codex_usage_limits" if selected == "claude" else "_check_usage_limits", _unexpected_runtime)

    def acquire(*args):
        assert args == ((str(tmp_path),) if selected == "codex" else ())
        events.append("quota")
        percentage = 95 if queue == "halt" and events.count("quota") == 2 else 3
        if selected == "codex":
            return {} if queue == "unavailable" else {"rateLimits": {"primary": _quota_window(percentage, 10080)}}
        return "unrecognized" if queue == "unavailable" else f"Current week (all models): {percentage}%"

    monkeypatch.setattr(usage_module, "_read_rate_limits", acquire if selected == "codex" else _unexpected_runtime)
    monkeypatch.setattr(usage_module, "_run_usage_probe", acquire if selected == "claude" else _unexpected_runtime)

    def process(project_dir, task, index, effective_config, mode, *, phase_session_id, backend):
        assert backend is sentinel
        assert effective_config is config
        events.append("process")
        mark_done(project_dir / ".ai-factory" / mode.roadmap_relpath, task)
        return None

    monkeypatch.setattr(main_module, "process_task", process)
    if queue == "halt":
        with pytest.raises(HaltError):
            getattr(main_module, loop_name)(tmp_path, config)
        assert events == ["constructor", "quota", "process", "quota"]
    else:
        getattr(main_module, loop_name)(tmp_path, config)
        assert events == (["constructor"] if queue in ("empty", "completed") else ["constructor", "quota", "process", "quota", "process"])
    output = capsys.readouterr().out
    if queue == "unavailable":
        warning = CODEX_UNAVAILABLE if selected == "codex" else "  [usage check: could not parse output, continuing]"
        assert output.count(warning) == 2
    elif queue == "halt":
        assert "unavailable" not in output


# ---------------------------------------------------------------------------
# Resume attempt limits through process_task
# ---------------------------------------------------------------------------


def _artifact_snapshot(tmp_path: Path) -> list[str]:
    return sorted(str(p) for p in (tmp_path / ".ai-factory").rglob("*") if p.is_file())


def _failing_plan_reviews(prd: Path, numbers) -> None:
    for n in numbers:
        (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-{n}.md").write_text("Findings, no pass")


@pytest.mark.parametrize("case", ["planned_4", "plan_review_failed_3_missing_review"])
def test_process_task_exhausted_resume_halts_without_effects(tmp_path, monkeypatch, case):
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    sidecar = plan_path.with_suffix(".json")
    if case == "planned_4":
        sidecar.write_text(json.dumps({"step": "planned:4"}))
    else:
        sidecar.write_text(json.dumps({"step": "plan_review_failed:3"}))
        _failing_plan_reviews(prd, (1, 2))
    before_bytes = sidecar.read_bytes()
    before_files = _artifact_snapshot(tmp_path)
    backend = FakeAgentBackend()

    with pytest.raises(HaltError):
        process_task(tmp_path, _TaskStub(), 1, _flow_config(), backend=backend)

    assert backend.calls == []
    assert sidecar.read_bytes() == before_bytes
    assert _artifact_snapshot(tmp_path) == before_files


def test_process_task_inferred_over_budget_halts_without_creating_sidecar(tmp_path, monkeypatch):
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    _failing_plan_reviews(prd, (1, 2, 3))
    before_files = _artifact_snapshot(tmp_path)
    backend = FakeAgentBackend()

    with pytest.raises(HaltError):
        process_task(tmp_path, _TaskStub(), 1, _flow_config(), backend=backend)

    assert backend.calls == []
    assert not plan_path.with_suffix(".json").exists()
    assert _artifact_snapshot(tmp_path) == before_files


def _passing_backend(plan_review_passes: bool = True, code_review_passes: bool = True) -> FakeAgentBackend:
    """Write each assigned artifact: plans as text, reviews with or without the PASS signal."""

    def _write(call):
        if call.role is AgentRole.IMPLEMENTER:
            return
        path = target_path(call.prompt)
        if call.role is AgentRole.PLAN_REVIEWER:
            path.write_text("Fine\n\nPLAN_REVIEW_PASS" if plan_review_passes else "Findings, no pass")
        elif "-review-" in path.name:
            path.write_text("All good\n\nREVIEW_PASS" if code_review_passes else "Findings, no pass")
        else:
            path.write_text("# Plan\n\n## Test Command\n`false`\n")

    return FakeAgentBackend(on_call=_write)


def test_process_task_planned_at_bound_runs_plan_review_at_that_attempt(tmp_path, monkeypatch):
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "planned:3"}))
    backend = _passing_backend()

    process_task(tmp_path, _TaskStub(), 1, _flow_config(), backend=backend)

    first = backend.calls[0]
    assert first.role is AgentRole.PLAN_REVIEWER
    assert f"{DMS_SEQ}-{DMS_SLUG}-plan-review-3.md" in first.prompt


def test_process_task_plan_review_failed_in_budget_revises_then_reviews_next_attempt(tmp_path, monkeypatch):
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "plan_review_failed:2"}))
    _failing_plan_reviews(prd, (1, 2))
    backend = _passing_backend()

    process_task(tmp_path, _TaskStub(), 1, _flow_config(), backend=backend)

    revision, review = backend.calls[0], backend.calls[1]
    assert revision.role is AgentRole.PLANNER_REVIEWER
    assert f"{DMS_SEQ}-{DMS_SLUG}-plan-review-2.md" in revision.prompt
    assert review.role is AgentRole.PLAN_REVIEWER
    assert f"{DMS_SEQ}-{DMS_SLUG}-plan-review-3.md" in review.prompt


def test_process_task_live_plan_review_exhaustion_is_a_work_failure(tmp_path, monkeypatch):
    _neutralize_side_effects(monkeypatch)
    backend = _passing_backend(plan_review_passes=False)

    with pytest.raises(PipelineStopError):
        process_task(tmp_path, _TaskStub(), 1, _flow_config(), backend=backend)

    assert [c.role for c in backend.calls] == [
        AgentRole.PLANNER_REVIEWER, AgentRole.PLAN_REVIEWER,
        AgentRole.PLANNER_REVIEWER, AgentRole.PLAN_REVIEWER,
        AgentRole.PLANNER_REVIEWER, AgentRole.PLAN_REVIEWER,
    ]


def test_process_task_live_code_review_exhaustion_is_a_work_failure(tmp_path, monkeypatch):
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "plan_reviewed"}))
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text("Fine\n\nPLAN_REVIEW_PASS")
    backend = _passing_backend(code_review_passes=False)

    with pytest.raises(PipelineStopError):
        process_task(tmp_path, _TaskStub(), 1, _flow_config(), backend=backend)

    assert _read_sessions(plan_path)["step"] == "review_failed:3"


def test_process_task_live_test_run_exhaustion_is_a_work_failure(tmp_path, monkeypatch):
    _neutralize_side_effects(monkeypatch)
    backend = _passing_backend()

    with pytest.raises(PipelineStopError):
        process_task(tmp_path, _TaskStub(), 1, _flow_config(), TEST_MODE, backend=backend)

    plan_path = _artifact_dir(tmp_path, TEST_MODE, "plans") / f"{DMS_SEQ}-{DMS_SLUG}.md"
    assert _read_sessions(plan_path)["step"] == "test_run_failed:3"


# ---------------------------------------------------------------------------
# The plan-review budget bounds the flow and resume
# ---------------------------------------------------------------------------


def test_process_task_budget_zero_goes_from_planner_to_implementer_without_plan_review(tmp_path, monkeypatch):
    """At a budget of 0 the planner's plan goes straight to the implementer: no review call, no plan-review file."""
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    backend = _passing_backend()
    config = _flow_config()
    config.plan_review_iterations = 0

    process_task(tmp_path, _TaskStub(), 1, config, backend=backend)

    assert [c.role for c in backend.calls] == [
        AgentRole.PLANNER_REVIEWER, AgentRole.IMPLEMENTER, AgentRole.PLANNER_REVIEWER,
    ]
    assert list(prd.glob(f"{DMS_SEQ}-{DMS_SLUG}-plan-review-*.md")) == []
    assert _read_sessions(plan_path)["step"] == "done"


def test_process_task_planned_1_at_budget_zero_resumes_into_implementer_without_halt(tmp_path, monkeypatch):
    """A record at planned:1 passes its check at a budget of 0 and meets the empty plan-review loop."""
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "planned:1"}))
    backend = _passing_backend()
    config = _flow_config()
    config.plan_review_iterations = 0

    process_task(tmp_path, _TaskStub(), 1, config, backend=backend)

    assert backend.calls[0].role is AgentRole.IMPLEMENTER
    assert AgentRole.PLAN_REVIEWER not in [c.role for c in backend.calls]
    assert list(prd.glob(f"{DMS_SEQ}-{DMS_SLUG}-plan-review-*.md")) == []
    assert _read_sessions(plan_path)["step"] == "done"


def test_process_task_budget_above_max_iterations_runs_that_many_reviews_before_the_stop(tmp_path, monkeypatch):
    """A budget above the iteration limit is not cut by a check: every review runs, then the plan stop fires."""
    _neutralize_side_effects(monkeypatch)
    backend = _passing_backend(plan_review_passes=False)
    config = _flow_config()
    config.plan_review_iterations = 5

    with pytest.raises(PipelineStopError):
        process_task(tmp_path, _TaskStub(), 1, config, backend=backend)

    reviews = [c for c in backend.calls if c.role is AgentRole.PLAN_REVIEWER]
    assert len(reviews) == 5
    assert f"{DMS_SEQ}-{DMS_SLUG}-plan-review-5.md" in reviews[-1].prompt


@pytest.mark.parametrize("raw,setting,label,attempt,named_setting,value", [
    ("planning:2", 0, "planning:2", 2, "plan_review_iterations", 0),
    ("plan_review_failed:1", 0, "plan", 2, "plan_review_iterations", 0),
    ("planned:2", 1, "planned:2", 2, "plan_review_iterations", 1),
    ("plan_review_failed:1", 1, "plan", 2, "plan_review_iterations", 1),
    ("plan_review_failed:2", 2, "plan", 3, "plan_review_iterations", 2),
    ("implemented:5", 2, "implemented:5", 5, "max_iterations", 3),
    ("planning:4", None, "planning:4", 4, "max_iterations", 3),
    ("planned:4", None, "planned:4", 4, "max_iterations", 3),
    ("plan_review_failed:3", None, "plan", 4, "max_iterations", 3),
    ("implemented:5", None, "implemented:5", 5, "max_iterations", 3),
])
def test_detect_halt_names_the_setting_that_gives_the_bound(tmp_path, raw, setting, label, attempt, named_setting, value):
    """The halt names the plan-review setting for the plan family and the iteration limit for the rest."""
    prd, rv, plan_path = _dms_dirs(tmp_path)
    _seed_sidecar(plan_path, raw)

    with pytest.raises(HaltError) as exc:
        _detect_step(
            DMS_SEQ, DMS_SLUG, plan_path, prd, rv,
            REVIEW_VERIFY, max_iterations=3, runtime="claude",
            plan_review_iterations=setting,
        )

    assert str(exc.value) == (
        f"Resume exceeds the attempt budget\n"
        f"{label} attempt {attempt} exceeds {named_setting} ({value})\n"
        f"Raise {named_setting} in orchestrator.json to continue."
    )


def test_process_task_resume_plan_review_failed_above_max_iterations_revises_within_budget(tmp_path, monkeypatch):
    """A failed review past the iteration limit but within the plan-review budget resumes its revision."""
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "plan_review_failed:4"}))
    _failing_plan_reviews(prd, (4,))
    backend = _passing_backend()
    config = _flow_config()
    config.max_iterations = 3
    config.plan_review_iterations = 6

    process_task(tmp_path, _TaskStub(), 1, config, backend=backend)

    revision, review = backend.calls[0], backend.calls[1]
    assert revision.role is AgentRole.PLANNER_REVIEWER
    assert f"{DMS_SEQ}-{DMS_SLUG}-plan-review-4.md" in revision.prompt
    assert review.role is AgentRole.PLAN_REVIEWER
    assert f"{DMS_SEQ}-{DMS_SLUG}-plan-review-5.md" in review.prompt
    assert _read_sessions(plan_path)["step"] == "done"


def test_process_task_resume_plan_review_failed_past_budget_halts_naming_the_budget_setting(tmp_path, monkeypatch):
    """A failed review past the plan-review budget but within the iteration limit halts naming the setting."""
    _neutralize_side_effects(monkeypatch)
    prd, rv, plan_path = _dms_dirs(tmp_path)
    plan_path.write_text("# Plan content")
    plan_path.with_suffix(".json").write_text(json.dumps({"step": "plan_review_failed:4"}))
    _failing_plan_reviews(prd, (4,))
    backend = FakeAgentBackend()
    config = _flow_config()
    config.max_iterations = 5
    config.plan_review_iterations = 2

    with pytest.raises(HaltError) as exc:
        process_task(tmp_path, _TaskStub(), 1, config, backend=backend)

    assert str(exc.value) == (
        "Resume exceeds the attempt budget\n"
        "plan_review_failed:4 attempt 4 exceeds plan_review_iterations (2)\n"
        "Raise plan_review_iterations in orchestrator.json to continue."
    )
    assert backend.calls == []


@pytest.mark.parametrize("plan_review_iterations", [1, 2])
def test_process_task_failing_reviews_keep_the_implement_cycle_at_max_iterations(
    tmp_path, monkeypatch, plan_review_iterations,
):
    """A non-zero plan-review budget never cuts the implement and verify cycle."""
    _neutralize_side_effects(monkeypatch)
    backend = _passing_backend(code_review_passes=False)
    config = _flow_config()
    config.plan_review_iterations = plan_review_iterations

    with pytest.raises(PipelineStopError):
        process_task(tmp_path, _TaskStub(), 1, config, backend=backend)

    assert [c.role for c in backend.calls].count(AgentRole.IMPLEMENTER) == config.max_iterations


@pytest.mark.parametrize("suffix", [".md", ".json"])
def test_next_number_reserves_both_extensions_and_ignores_temporary_files(tmp_path, suffix):
    (tmp_path / f"9-closed{suffix}").write_text('{"step":"done"}')
    (tmp_path / "10-pending.json.tmp").write_text("temporary")
    (tmp_path / "20-pending.md.tmp").write_text("temporary")
    assert _next_number(tmp_path) == 10


def test_next_number_fallback_counts_only_markdown(tmp_path):
    (tmp_path / "notes.md").write_text("notes")
    (tmp_path / "other.md").write_text("notes")
    (tmp_path / "optional.json").write_text("{}")
    (tmp_path / "99-pending.json.tmp").write_text("temporary")
    assert _next_number(tmp_path) == 3


def test_detect_exact_slug_ignores_broad_suffix_and_temp_decoys(tmp_path):
    prd, rv, plan = _dms_dirs(tmp_path)
    (plan.parent / f"1-other-{DMS_SLUG}.md").write_text("# Decoy")
    (plan.parent / f"2-{DMS_SLUG}.json.tmp").write_text("broken")
    assert _detect_task_step("01", DMS_SLUG, plan, prd, rv) == ("plan", 1, plan)


def test_detect_keeps_nonpadded_prefix_for_review_lookup(tmp_path):
    prd, rv, plan = _dms_dirs(tmp_path)
    chosen = plan.with_name(f"1-{DMS_SLUG}.md")
    chosen.write_text("# Plan")
    (prd / f"1-{DMS_SLUG}-plan-review-1.md").write_text("PLAN_REVIEW_PASS")
    (prd / f"01-{DMS_SLUG}-plan-review-1.md").write_text("findings")
    assert _detect_task_step("02", DMS_SLUG, plan, prd, rv) == ("implement", 1, chosen)


def test_detect_rejects_equal_unfinished_numeric_prefixes(tmp_path):
    prd, rv, plan = _dms_dirs(tmp_path)
    plan.write_text("# Plan")
    (plan.parent / f"1-{DMS_SLUG}.md").write_text("# Plan")
    with pytest.raises(HaltError, match="ambiguous"):
        _detect_task_step("02", DMS_SLUG, plan, prd, rv)


def test_detect_excludes_closed_json_before_numeric_collision_check(tmp_path):
    prd, rv, plan = _dms_dirs(tmp_path)
    plan.write_text("# Plan")
    (plan.parent / f"1-{DMS_SLUG}.json").write_text('{"step":"done"}')
    assert _detect_task_step("02", DMS_SLUG, plan, prd, rv) == ("plan_review", 1, plan)


def test_detect_validates_closed_candidate_before_exclusion(tmp_path):
    prd, rv, plan = _dms_dirs(tmp_path)
    plan.write_text("# Plan")
    (plan.parent / f"2-{DMS_SLUG}.json").write_text('{"step":"done", "planner":null}')
    with pytest.raises(HaltError, match="invalid field"):
        _detect_task_step("03", DMS_SLUG, plan, prd, rv)


@pytest.mark.parametrize("pending", ["planning:1", "planning:02", "planning:3"])
@pytest.mark.parametrize("has_plan", [False, True])
def test_detect_tagged_pending_is_authoritative(tmp_path, pending, has_plan):
    prd, rv, plan = _dms_dirs(tmp_path)
    if has_plan:
        plan.write_text("# Plausible plan")
    plan.with_suffix(".json").write_text(json.dumps({"runtime": "codex", "step": pending}))
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text("PLAN_REVIEW_PASS")
    result = _detect_step("02", DMS_SLUG, plan, prd, rv, REVIEW_VERIFY, max_iterations=3, runtime="codex")
    assert result == ("plan", int(pending.split(":")[1]), plan)


@pytest.mark.parametrize("pending", ["planning:", "planning:0", "planning:-1", "planning:+1", "planning:١", "planning:4"])
def test_detect_pending_counter_halts_before_fallback(tmp_path, pending):
    prd, rv, plan = _dms_dirs(tmp_path)
    plan.write_text("# Plausible plan")
    plan.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": pending}))
    (prd / f"{DMS_SEQ}-{DMS_SLUG}-plan-review-1.md").write_text("PLAN_REVIEW_PASS")
    with pytest.raises(HaltError):
        _detect_task_step(DMS_SEQ, DMS_SLUG, plan, prd, rv)


@pytest.mark.parametrize("step", [None, "", "unknown"])
def test_detect_tagged_missing_or_unknown_progress_halts(tmp_path, step):
    prd, rv, plan = _dms_dirs(tmp_path)
    plan.write_text("# Plausible plan")
    data = {"runtime": "claude"}
    if step is not None:
        data["step"] = step
    plan.with_suffix(".json").write_text(json.dumps(data))
    with pytest.raises(HaltError, match="no usable explicit step"):
        _detect_task_step(DMS_SEQ, DMS_SLUG, plan, prd, rv)


@pytest.mark.parametrize("step", ["planned:1", "plan_reviewed", "implemented:1", "review_failed:1", "plan_review_failed:1"])
def test_detect_completed_tagged_boundary_needs_markdown(tmp_path, step):
    prd, rv, plan = _dms_dirs(tmp_path)
    plan.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": step}))
    with pytest.raises(HaltError, match="no plan"):
        _detect_task_step(DMS_SEQ, DMS_SLUG, plan, prd, rv)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("step", ["done", "escalated"])
def test_detect_json_only_terminal_is_runtime_independent(tmp_path, runtime, step):
    prd, rv, plan = _dms_dirs(tmp_path)
    plan.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": step}))
    fresh = plan.with_name(f"02-{DMS_SLUG}.md")
    result = _detect_step("02", DMS_SLUG, fresh, prd, rv, REVIEW_VERIFY, max_iterations=3, runtime=runtime)
    assert result == (("plan", 1, fresh) if step == "done" else ("escalated", 0, plan))


@pytest.mark.parametrize("step", ["planning:2", "planned:1", "unknown", ""])
def test_detect_untagged_json_orphan_halts(tmp_path, step):
    prd, rv, plan = _dms_dirs(tmp_path)
    plan.with_suffix(".json").write_text(json.dumps({"step": step}))
    with pytest.raises(HaltError, match="no legacy plan"):
        _detect_task_step(DMS_SEQ, DMS_SLUG, plan, prd, rv)


@pytest.mark.parametrize("suffix", [".json", ".md"])
def test_flat_allocation_and_detection_ignore_named_segments_with_artifact_suffixes(tmp_path, suffix):
    prd, rv, plan = _dms_dirs(tmp_path)
    named_relpath = f"roadmaps/99-{DMS_SLUG}{suffix}.md"
    named_mode = IMPLEMENT_MODE._replace(artifact_subdir=_artifact_subdir(named_relpath))
    segment = _artifact_dir(tmp_path, named_mode, "plans")
    segment.mkdir()
    evidence = segment / f"01-{DMS_SLUG}.json"
    evidence.write_text('{"runtime":"codex", "step":"planning:2"}')
    original = evidence.read_bytes()
    assert segment.name == f"99-{DMS_SLUG}{suffix}"
    assert _next_number(plan.parent) == 1
    assert _detect_task_step(DMS_SEQ, DMS_SLUG, plan, prd, rv) == ("plan", 1, plan)
    assert evidence.read_bytes() == original


@pytest.mark.parametrize("suffix", [".json", ".md"])
def test_next_number_fallback_ignores_nonnumeric_artifact_segment_directories(tmp_path, suffix):
    named_mode = IMPLEMENT_MODE._replace(artifact_subdir=_artifact_subdir(f"roadmaps/notes{suffix}.md"))
    segment = _artifact_dir(tmp_path, named_mode, "plans")
    segment.mkdir(parents=True)
    (segment.parent / "local-notes.md").write_text("notes")
    assert _next_number(segment.parent) == 2


CODEX_UNAVAILABLE = "  [usage check: Codex quota precheck unavailable; continuing without quota protection]"


def _quota_window(percentage, duration):
    return {"usedPercent": percentage, "windowDurationMins": duration}


def _check_snapshot(snapshot, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(usage_module, "_read_rate_limits", lambda cwd: calls.append(cwd) or snapshot)
    usage_module._check_codex_usage_limits(_flow_config(), tmp_path)
    assert calls == [str(tmp_path)]


def test_codex_authoritative_bucket_wins_over_legacy_and_other_limits(tmp_path, monkeypatch, capsys):
    snapshot = {
        "rateLimitsByLimitId": {
            "codex": {"limitId": "codex", "primary": _quota_window(12, 300)},
            "codex_other": {"primary": _quota_window(99, 10080)},
        },
        "rateLimits": {"primary": _quota_window(99, 300)},
    }
    _check_snapshot(snapshot, tmp_path, monkeypatch)
    assert capsys.readouterr().out == "  [Codex usage: five-hour 12%]\n"


@pytest.mark.parametrize("buckets", [False, [], "codex", {}, {"codex_other": {}}, {"codex": None}, {"codex": []}, {"codex": 12}])
def test_codex_invalid_authoritative_map_never_falls_back(tmp_path, monkeypatch, capsys, buckets):
    snapshot = {"rateLimitsByLimitId": buckets, "rateLimits": {"primary": _quota_window(99, 10080)}}
    _check_snapshot(snapshot, tmp_path, monkeypatch)
    assert capsys.readouterr().out == CODEX_UNAVAILABLE + "\n"


@pytest.mark.parametrize("view", ["legacy", "null-map", "map"])
@pytest.mark.parametrize("identity", ["absent", None, "codex"])
def test_codex_accepts_compatible_bucket_identity(tmp_path, monkeypatch, capsys, view, identity):
    bucket = {"primary": _quota_window(0, 10080), "secondary": None}
    if identity != "absent":
        bucket["limitId"] = identity
    snapshot = {"rateLimitsByLimitId": {"codex": bucket}} if view == "map" else {"rateLimits": bucket}
    if view == "null-map":
        snapshot["rateLimitsByLimitId"] = None
    _check_snapshot(snapshot, tmp_path, monkeypatch)
    assert capsys.readouterr().out == "  [Codex usage: weekly 0%]\n"


@pytest.mark.parametrize("view", ["legacy", "map"])
@pytest.mark.parametrize("identity", ["codex_other", "Codex", " codex", "codex ", "", False, 0, ["codex"], {"id": "codex"}])
def test_codex_foreign_or_malformed_identity_is_unavailable(tmp_path, monkeypatch, capsys, view, identity):
    bucket = {"limitId": identity, "limitName": "codex", "primary": _quota_window(99, 10080)}
    snapshot = {"rateLimitsByLimitId": {"codex": bucket}} if view == "map" else {"rateLimits": bucket}
    _check_snapshot(snapshot, tmp_path, monkeypatch)
    assert capsys.readouterr().out == CODEX_UNAVAILABLE + "\n"


@pytest.mark.parametrize("primary,secondary,halt,expected", [
    (_quota_window(93, 10080), None, False, "weekly 93%"),
    (_quota_window(95, 10080), None, True, "weekly 95%"),
    (_quota_window(93, 10080), _quota_window(90, 300), True, "five-hour 90%"),
    (_quota_window(89, 300), _quota_window(94, 10080), False, "weekly 94%"),
    (_quota_window(90, 300), _quota_window(2, 300), True, "five-hour 90%"),
    (_quota_window(2, 300), _quota_window(90, 300), True, "five-hour 90%"),
    (_quota_window(100.5, 10080), None, True, "weekly 100%"),
    (_quota_window(89.999, 300), None, False, "five-hour 90%"),
    (_quota_window(94.999, 10080), None, False, "weekly 95%"),
])
def test_codex_duration_and_unrounded_threshold_decisions(tmp_path, monkeypatch, capsys, primary, secondary, halt, expected):
    snapshot = {"rateLimits": {"primary": primary, "secondary": secondary}}
    if halt:
        with pytest.raises(HaltError) as exc:
            _check_snapshot(snapshot, tmp_path, monkeypatch)
        assert str(exc.value).splitlines()[0] == "Codex usage threshold reached"
        assert "usage at" in str(exc.value).splitlines()[1]
    else:
        _check_snapshot(snapshot, tmp_path, monkeypatch)
    output = capsys.readouterr().out
    assert expected in output
    assert CODEX_UNAVAILABLE not in output
    if secondary is None:
        assert ("weekly" in output) != ("five-hour" in output)


@pytest.mark.parametrize("invalid", [
    None, [], "unknown", {},
    _quota_window(True, 300), _quota_window("99", 300),
    _quota_window(float("nan"), 300), _quota_window(float("inf"), 300),
    _quota_window(-1, 300), _quota_window(99, True), _quota_window(99, "300"),
    _quota_window(99, float("inf")), _quota_window(99, float("nan")),
    _quota_window(99, -300), _quota_window(99, 0), _quota_window(99, 60),
])
@pytest.mark.parametrize("valid_slot", ["primary", "secondary"])
def test_codex_bad_sibling_never_hides_supported_breach(tmp_path, monkeypatch, capsys, invalid, valid_slot):
    other_slot = "secondary" if valid_slot == "primary" else "primary"
    snapshot = {"rateLimits": {valid_slot: _quota_window(95, 10080), other_slot: invalid}}
    with pytest.raises(HaltError):
        _check_snapshot(snapshot, tmp_path, monkeypatch)
    assert capsys.readouterr().out == "  [Codex usage: weekly 95%]\n"


@pytest.mark.parametrize("snapshot", [
    None, [], {}, {"rateLimits": []}, {"rateLimits": {}},
    {"rateLimits": {"primary": _quota_window(99, 60)}},
    {"rateLimits": {"primary": _quota_window(False, 300)}},
    {"rateLimits": {"primary": _quota_window("99", 300)}},
    {"rateLimits": {"primary": _quota_window(float("nan"), 10080)}},
    {"rateLimits": {"primary": _quota_window(-0.1, 300)}},
    {"rateLimits": {"primary": None, "secondary": None}},
])
def test_codex_no_usable_observation_warns_without_invented_usage(tmp_path, monkeypatch, capsys, snapshot):
    _check_snapshot(snapshot, tmp_path, monkeypatch)
    assert capsys.readouterr().out == CODEX_UNAVAILABLE + "\n"


@pytest.mark.parametrize("error", [RuntimeError("private-error-payload"), TimeoutError("private-error-payload"),
                                        KeyboardInterrupt(), SystemExit(12)])
def test_codex_acquisition_fallback_preserves_interrupts(tmp_path, monkeypatch, capsys, error):
    def fail(cwd):
        assert cwd == str(tmp_path)
        raise error

    monkeypatch.setattr(usage_module, "_read_rate_limits", fail)
    if isinstance(error, Exception):
        usage_module._check_codex_usage_limits(_flow_config(), tmp_path)
        assert capsys.readouterr().out == CODEX_UNAVAILABLE + "\n"
    else:
        with pytest.raises(type(error)):
            usage_module._check_codex_usage_limits(_flow_config(), tmp_path)
        assert capsys.readouterr().out == ""


def test_codex_logs_only_observed_window_usage(tmp_path, monkeypatch, capsys):
    secret = "private-account-marker"
    snapshot = {"accountId": secret, "rateLimits": {
        "limitId": "codex", "limitName": secret, "credits": {"balance": secret},
        "primary": {**_quota_window(22.2, 10080), "resetsAt": secret},
        "secondary": None,
    }}
    _check_snapshot(snapshot, tmp_path, monkeypatch)
    output = capsys.readouterr().out
    assert output == "  [Codex usage: weekly 22%]\n"
    assert secret not in output
    assert "five-hour" not in output


@pytest.mark.parametrize("output,halt,expected", [
    ("Current session: 90%", True, "session 90%"),
    ("Current week (all models): 95%", True, "week 95%"),
    ("Current session: 89.999%", False, "session 90%"),
    ("Current week (all models): 94.999%", False, "week 95%"),
    ("Current session: 2%\\nCurrent week (all models): 95%", True, "week 95%"),
    ("Current session: 90%\\nCurrent week (all models): 3%", True, "session 90%"),
])
def test_claude_independent_unrounded_thresholds(monkeypatch, capsys, output, halt, expected):
    monkeypatch.setattr(usage_module, "_run_usage_probe", lambda: output)
    if halt:
        with pytest.raises(HaltError):
            _check_usage_limits(_flow_config())
    else:
        _check_usage_limits(_flow_config())
    log = capsys.readouterr().out
    assert expected in log
    assert "could not parse" not in log


@pytest.mark.parametrize("raises", [True, False])
def test_claude_unavailable_fallback_is_unchanged(monkeypatch, capsys, raises):
    def probe():
        if raises:
            raise RuntimeError("private probe error")
        return "unrecognized"

    monkeypatch.setattr(usage_module, "_run_usage_probe", probe)
    _check_usage_limits(_flow_config())
    assert capsys.readouterr().out == "  [usage check: could not parse output, continuing]\n"


# ---------------------------------------------------------------------------
# Correlated terminal completion proof
# ---------------------------------------------------------------------------

COMPLETION_ID = "abcdef01-2345-4678-9abc-def012345678"


@pytest.fixture
def completion_env(tmp_path, monkeypatch):
    import signal
    from orchestrator import state

    project = tmp_path / "target"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    events, reports = [], []
    env = SimpleNamespace(
        project=project, proof=tmp_path / "completion.json", events=events,
        reports=reports, config=dataclasses.replace(_flow_config(), roadmap_path=None),
        process=None,
    )
    for name, value in (
        ("queue_exhausted", False), ("stop_requested", False), ("run_started", None),
        ("tasks_done", 0), ("config", None), ("project_dir", None),
    ):
        monkeypatch.setattr(state, name, value)
    old_handler = signal.getsignal(signal.SIGINT)

    def without_sleep_prevention(fn, *args, **kwargs):
        fn(*args, **kwargs)
        return "offline elapsed"

    def constructor(agents=None):
        events.append("constructor")
        return FakeAgentBackend()

    def process(project_dir, task, index, config, mode, **kwargs):
        events.append("process")
        assert config is env.config
        if env.process is not None:
            return env.process(project_dir, task, mode)
        print("FAIL: diagnostic text has no completion authority")
        mark_done(project_dir / ".ai-factory" / mode.roadmap_relpath, task)

    monkeypatch.setattr(main_module, "_with_caffeinate", without_sleep_prevention)
    monkeypatch.setattr(main_module, "load_config", lambda project_dir=None, *, task_reviewers=None: env.config)
    monkeypatch.setattr(main_module, "ClaudeAgentBackend", constructor)
    monkeypatch.setattr(main_module, "CodexAgentBackend", constructor)
    monkeypatch.setattr(main_module, "_check_usage_limits", lambda *a: events.append("quota"))
    monkeypatch.setattr(main_module, "_check_codex_usage_limits", lambda *a: events.append("quota"))
    monkeypatch.setattr(claude_module, "_resolve_claude", _unexpected_runtime)
    monkeypatch.setattr(codex_module, "_resolve_codex", _unexpected_runtime)
    monkeypatch.setattr(usage_module, "_run_usage_probe", _unexpected_runtime)
    monkeypatch.setattr(usage_module, "_read_rate_limits", _unexpected_runtime)
    monkeypatch.setattr(main_module, "process_task", process)
    monkeypatch.setattr(pipeline_module, "report", lambda c, outcome, *a: reports.append(outcome))
    monkeypatch.setattr(main_module, "report", lambda c, outcome, *a: reports.append(outcome))
    try:
        yield env
    finally:
        signal.signal(signal.SIGINT, old_handler)


def _completion_roadmap(env, mode="implement", named=False, content=""):
    relpath = "roadmaps/developer.md" if named else "ROADMAP.md"
    env.config = dataclasses.replace(env.config, roadmap_path=relpath if named else None)
    if mode == "test":
        relpath = _tests_sibling(relpath)
    path = env.project / ".ai-factory" / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _completion_cli(env, monkeypatch, mode="implement", proof=None):
    monkeypatch.setattr(sys, "argv", [
        "orchestrator", mode, str(env.project),
        "--completion-file", str(proof or env.proof), "--invocation-id", COMPLETION_ID,
    ])
    return main_module.cli()


def _assert_completion(env, outcome, proof=None):
    path = proof or env.proof
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "invocation_id": COMPLETION_ID, "outcome": outcome,
    }
    assert not path.with_name(path.name + ".tmp").exists()


@pytest.mark.parametrize("mode", ["implement", "test"])
@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("queue", ["empty", "completed", "pending", "breakpoint"])
def test_cli_real_effective_queue_proves_exhaustion(completion_env, monkeypatch, capsys, mode, named, queue):
    env = completion_env
    content = "" if queue == "empty" else "- [x] **Finished** — already complete\n" if queue == "completed" else "- [ ] **First** — finish first\n"
    if queue == "pending":
        content += "- [ ] **Second** — finish second\n"
    if queue == "breakpoint":
        content += "---STOP---\n- [ ] **Later** — leave pending\n"
    roadmap = _completion_roadmap(env, mode, named, content)
    # A different queue remains pending; only the selected queue establishes proof.
    other = env.project / ".ai-factory" / ("ROADMAP.md" if named or mode == "test" else "ROADMAP_TESTS.md")
    other.write_text("- [ ] **Other queue** — leave pending\n")
    assert _completion_cli(env, monkeypatch, mode) is None
    assert main_module.state.queue_exhausted is True
    _assert_completion(env, "SUCCESS")
    count = 0 if queue in ("empty", "completed") else 2 if queue == "pending" else 1
    assert env.events == ["constructor"] + ["quota", "process"] * count
    assert env.reports == ([Outcome.RUN_DONE] if count else [])
    assert "[ ] **Other queue**" in other.read_text()
    if queue == "breakpoint":
        assert "[ ] **Later**" in roadmap.read_text()
    if count:
        assert "FAIL: diagnostic text" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["implement", "test"])
@pytest.mark.parametrize("finish_last", [False, True])
def test_cli_real_manual_stop_never_proves_exhaustion(completion_env, monkeypatch, capsys, mode, finish_last):
    env = completion_env
    content = "- [ ] **First** — finish first\n"
    if not finish_last:
        content += "- [ ] **Remaining** — leave pending\n"
    roadmap = _completion_roadmap(env, mode, content=content)

    def stop(project, task, effective_mode):
        mark_done(roadmap, task)
        main_module.state.stop_requested = True
        print("SUCCESS: misleading task console output")

    env.process = stop
    assert _completion_cli(env, monkeypatch, mode) is None
    assert main_module.state.queue_exhausted is False
    assert env.events == ["constructor", "quota", "process"]
    assert env.reports == [Outcome.MANUAL_STOP]
    _assert_completion(env, "FAIL")
    assert "SUCCESS: misleading" in capsys.readouterr().out
    assert ("[ ] **Remaining**" in roadmap.read_text()) is not finish_last


@pytest.mark.parametrize("mode", ["implement", "test"])
def test_direct_runs_clear_previous_exhaustion(completion_env, mode):
    env = completion_env
    roadmap = _completion_roadmap(env, mode)
    run = getattr(main_module, "run_" + mode)
    run(env.project, env.config)
    assert main_module.state.queue_exhausted is True
    roadmap.write_text("- [ ] **Pending** — leave pending\n")
    main_module.state.stop_requested = True
    run(env.project, env.config)
    assert main_module.state.queue_exhausted is False
    assert not env.proof.exists()


@pytest.mark.parametrize("mode", ["implement", "test"])
def test_cli_clears_previous_exhaustion_on_next_run(completion_env, monkeypatch, mode):
    env = completion_env
    roadmap = _completion_roadmap(env, mode)
    _completion_cli(env, monkeypatch, mode)
    _assert_completion(env, "SUCCESS")
    roadmap.write_text("- [ ] **Pending** — leave pending\n")
    main_module.state.stop_requested = True
    _completion_cli(env, monkeypatch, mode)
    _assert_completion(env, "FAIL")
    assert main_module.state.queue_exhausted is False


@pytest.mark.parametrize("failure", ["config", "parser"])
def test_cli_resets_exhaustion_before_startup_failure(completion_env, monkeypatch, failure):
    env = completion_env
    _completion_roadmap(env)
    _completion_cli(env, monkeypatch)
    _assert_completion(env, "SUCCESS")
    second_proof = env.proof.with_name("second.json")
    if failure == "config":
        error = SystemExit(9)

        def fail_config(project_dir=None, *, task_reviewers=None):
            assert main_module.state.queue_exhausted is False
            raise error

        monkeypatch.setattr(main_module, "load_config", fail_config)
        with pytest.raises(SystemExit) as caught:
            _completion_cli(env, monkeypatch, proof=second_proof)
        assert caught.value is error
        _assert_completion(env, "FAIL", second_proof)
    else:
        monkeypatch.setattr(sys, "argv", ["orchestrator", "unknown-command"])
        with pytest.raises(SystemExit) as caught:
            main_module.cli()
        assert caught.value.code == 2
        assert not second_proof.exists()
    assert main_module.state.queue_exhausted is False


def test_cli_normal_return_without_exhaustion_is_negative(completion_env, monkeypatch, capsys):
    env = completion_env
    monkeypatch.setattr(main_module, "run_implement", lambda *a: print("All tasks are done! SUCCESS"))
    assert _completion_cli(env, monkeypatch) is None
    assert "SUCCESS" in capsys.readouterr().out
    _assert_completion(env, "FAIL")


def test_completion_path_resolves_caller_relative_paths_and_target_subdirectory(completion_env, monkeypatch):
    env = completion_env
    env.project = env.project / "subdirectory"
    env.project.mkdir()
    _completion_roadmap(env)
    monkeypatch.chdir(env.project.parent.parent)
    monkeypatch.setattr(sys, "argv", [
        "orchestrator", "implement", "target/subdirectory",
        "--completion-file", "completion.json", "--invocation-id", COMPLETION_ID,
    ])
    main_module.cli()
    _assert_completion(env, "SUCCESS")


@pytest.mark.parametrize("candidate_kind", ["root", "inside", "root-alias", "file-alias"])
def test_completion_path_rejects_resolved_target_worktree(completion_env, monkeypatch, candidate_kind):
    env = completion_env
    root = env.project
    subdir = root / "subdirectory"
    subdir.mkdir()
    if candidate_kind == "root":
        candidate = root
    elif candidate_kind == "inside":
        candidate = root / "evidence.json"
    elif candidate_kind == "root-alias":
        alias = root.parent / "alias"
        alias.symlink_to(root, target_is_directory=True)
        candidate = alias / "nested" / "evidence.json"
    else:
        target_file = root / "evidence.json"
        target_file.write_text("existing evidence")
        candidate = root.parent / "file-alias.json"
        candidate.symlink_to(target_file)
    monkeypatch.setattr(main_module, "load_config", _unexpected_runtime)
    env.project = subdir
    with pytest.raises(SystemExit) as caught:
        _completion_cli(env, monkeypatch, proof=candidate)
    assert caught.value.code == 2
    assert env.events == []
    assert not env.proof.exists()


def test_completion_path_allows_destination_in_another_worktree(completion_env):
    env = completion_env
    other = env.project.parent / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
    candidate = other / "evidence.json"
    assert layout_module._external_evidence_path(env.project, candidate) == candidate.resolve()
    assert not candidate.exists()


@pytest.mark.parametrize("missing", [False, True])
def test_completion_path_requires_available_git_target(completion_env, monkeypatch, missing):
    env = completion_env
    env.project = env.project.parent / "not-git"
    if not missing:
        env.project.mkdir()
    monkeypatch.setattr(main_module, "load_config", _unexpected_runtime)
    with pytest.raises(SystemExit) as caught:
        _completion_cli(env, monkeypatch)
    assert caught.value.code == 2
    assert env.events == []
    assert not env.proof.exists()


@pytest.mark.parametrize("fault", ["exec", "empty", "failed", "resolve"])
def test_external_evidence_check_converts_boundary_faults_to_value_error(tmp_path, monkeypatch, fault):
    if fault == "resolve":
        def resolve(*a, **kw):
            raise RuntimeError("symlink loop")
        monkeypatch.setattr(Path, "resolve", resolve)
    else:
        def git(*a, **kw):
            if fault == "exec":
                raise FileNotFoundError("git unavailable")
            return SimpleNamespace(returncode=1 if fault == "failed" else 0, stdout=b"", stderr=b"")
        monkeypatch.setattr(layout_module.subprocess, "run", git)
    with pytest.raises(ValueError, match="Invalid external evidence path"):
        layout_module._external_evidence_path(tmp_path, tmp_path / "evidence")


@pytest.mark.parametrize("options", [
    ["--completion-file", "proof.json"],
    ["--completion-file", ""],
    ["--invocation-id", COMPLETION_ID],
    ["--invocation-id", ""],
    ["--completion-file", "proof.json", "--invocation-id", ""],
    ["--completion-file", "proof.json", "--invocation-id", COMPLETION_ID.upper()],
    ["--completion-file", "proof.json", "--invocation-id", COMPLETION_ID.replace("-", "")],
    ["--completion-file", "proof.json", "--invocation-id", "{" + COMPLETION_ID + "}"],
    ["--completion-file", "proof.json", "--invocation-id", " " + COMPLETION_ID],
    ["--completion-file", "proof.json", "--invocation-id", "invalid"],
])
@pytest.mark.parametrize("mode", ["implement", "test"])
def test_completion_arguments_reject_unpaired_or_noncanonical_identity(completion_env, monkeypatch, options, mode):
    env = completion_env
    monkeypatch.setattr(main_module, "load_config", _unexpected_runtime)
    monkeypatch.setattr(main_module, "_external_evidence_path", _unexpected_runtime)
    monkeypatch.setattr(sys, "argv", ["orchestrator", mode, str(env.project), *options])
    with pytest.raises(SystemExit) as caught:
        main_module.cli()
    assert caught.value.code == 2
    assert not env.proof.exists()
    assert env.events == []


@pytest.mark.parametrize("command", [None, "implement", "test"])
def test_completion_options_absent_preserve_default_and_explicit_runs(completion_env, monkeypatch, command):
    env = completion_env
    _completion_roadmap(env, command or "implement")
    monkeypatch.chdir(env.project)
    monkeypatch.setattr(sys, "argv", ["orchestrator"] + ([command] if command else []))
    monkeypatch.setattr(main_module, "_external_evidence_path", _unexpected_runtime)
    monkeypatch.setattr(main_module, "_write_completion_proof", _unexpected_runtime)
    assert main_module.cli() is None
    assert main_module.state.queue_exhausted is True
    assert env.events == ["constructor"]
    assert not env.proof.exists()


@pytest.mark.parametrize("mode", ["implement", "test"])
@pytest.mark.parametrize("error_type,outcome", [
    (PipelineStopError, Outcome.UNCONVERGED),
    (HaltError, Outcome.HALTED),
    (EscalationError, Outcome.ESCALATED),
])
def test_completion_handled_outcomes_retain_zero_exit_and_negative_proof(completion_env, monkeypatch, mode, error_type, outcome):
    env = completion_env
    roadmap = _completion_roadmap(env, mode, content="- [ ] **Pending** — remains unfinished\n")
    error = error_type("original reason")

    def fail(*a):
        raise error

    env.process = fail
    with pytest.raises(SystemExit) as caught:
        _completion_cli(env, monkeypatch, mode)
    assert caught.value.code == 0
    assert env.reports == [outcome]
    assert "[ ] **Pending**" in roadmap.read_text()
    _assert_completion(env, "FAIL")


@pytest.mark.parametrize("stage", ["config", "backend", "process", "finalization"])
@pytest.mark.parametrize("error_factory", [lambda: SystemExit(0), lambda: SystemExit(7),
                                          lambda: RuntimeError("original error"), KeyboardInterrupt, BaseException])
def test_completion_preserves_startup_and_escaping_errors(completion_env, monkeypatch, stage, error_factory):
    env = completion_env
    content = "- [ ] **Pending** — unfinished\n" if stage == "process" else ""
    _completion_roadmap(env, content=content)
    error = error_factory()

    def fail(*a, **kw):
        raise error

    if stage == "config":
        monkeypatch.setattr(main_module, "load_config", fail)
    elif stage == "backend":
        monkeypatch.setattr(main_module, "ClaudeAgentBackend", fail)
    elif stage == "process":
        env.process = fail
    else:
        def fail_after_loop(fn, *a, **kw):
            fn(*a, **kw)
            assert main_module.state.queue_exhausted is True
            raise error
        monkeypatch.setattr(main_module, "_with_caffeinate", fail_after_loop)
    with pytest.raises(type(error)) as caught:
        _completion_cli(env, monkeypatch)
    assert caught.value is error
    _assert_completion(env, "FAIL")
    assert env.reports == ([Outcome.ERRORED] if isinstance(error, Exception) and stage != "config" else [])


def test_completion_writer_publishes_exact_json_by_sibling_replace(completion_env, monkeypatch):
    env = completion_env
    _completion_roadmap(env)
    env.proof.write_text("previous record", encoding="utf-8")
    real_replace = main_module.os.replace
    observed = []

    def replace(source, destination):
        assert source == env.proof.with_name("completion.json.tmp")
        assert destination == env.proof
        assert destination.read_text(encoding="utf-8") == "previous record"
        observed.append(json.loads(source.read_text(encoding="utf-8")))
        real_replace(source, destination)

    monkeypatch.setattr(main_module.os, "replace", replace)
    _completion_cli(env, monkeypatch)
    assert observed == [{"invocation_id": COMPLETION_ID, "outcome": "SUCCESS"}]
    _assert_completion(env, "SUCCESS")


@pytest.mark.parametrize("scenario", ["exhausted", "handled", "escaping"])
@pytest.mark.parametrize("publication_fault", ["write", "replace"])
@pytest.mark.parametrize("existing_success", [False, True])
def test_completion_publication_failure_preserves_primary_outcome(completion_env, monkeypatch, capsys, scenario, publication_fault, existing_success):
    env = completion_env
    content = "" if scenario == "exhausted" else "- [ ] **Pending** — unfinished\n"
    _completion_roadmap(env, content=content)
    if existing_success:
        env.proof.write_text(json.dumps({"invocation_id": COMPLETION_ID, "outcome": "SUCCESS"}))
    primary = HaltError("original halt") if scenario == "handled" else RuntimeError("original error")
    if scenario != "exhausted":
        def fail(*a):
            raise primary
        env.process = fail
    temporary = env.proof.with_name(env.proof.name + ".tmp")
    observed = []
    real_write_text = Path.write_text

    if publication_fault == "write":
        def partial_write(path, data, *a, **kw):
            if path == temporary:
                real_write_text(path, "partial unpublished data", encoding="utf-8")
                raise OSError("injected write failure")
            return real_write_text(path, data, *a, **kw)
        monkeypatch.setattr(Path, "write_text", partial_write)
    else:
        def fail_replace(source, destination):
            assert source == temporary
            assert destination == env.proof
            observed.append(json.loads(source.read_text(encoding="utf-8")))
            raise OSError("injected replace failure")
        monkeypatch.setattr(main_module.os, "replace", fail_replace)

    if scenario == "exhausted":
        assert _completion_cli(env, monkeypatch) is None
        assert main_module.state.queue_exhausted is True
        assert env.reports == []
    elif scenario == "handled":
        with pytest.raises(SystemExit) as caught:
            _completion_cli(env, monkeypatch)
        assert caught.value.code == 0
        assert env.reports == [Outcome.HALTED]
    else:
        with pytest.raises(RuntimeError) as caught:
            _completion_cli(env, monkeypatch)
        assert caught.value is primary
        assert env.reports == [Outcome.ERRORED]
    if publication_fault == "replace":
        assert observed == [{"invocation_id": COMPLETION_ID, "outcome": "SUCCESS" if scenario == "exhausted" else "FAIL"}]
    assert "Could not publish completion proof:" in capsys.readouterr().err
    assert not env.proof.exists()
    assert not temporary.exists()


@pytest.mark.parametrize("scenario", ["exhausted", "handled", "escaping"])
@pytest.mark.parametrize("secondary_fault", ["diagnostic", "cleanup", "destination-cleanup"])
def test_completion_best_effort_diagnostics_and_cleanup_preserve_outcome(completion_env, monkeypatch, scenario, secondary_fault):
    env = completion_env
    _completion_roadmap(env, content="" if scenario == "exhausted" else "- [ ] **Pending** — unfinished\n")
    previous_proof = {"invocation_id": COMPLETION_ID, "outcome": "SUCCESS"}
    env.proof.write_text(json.dumps(previous_proof))
    primary = PipelineStopError("original stop") if scenario == "handled" else ValueError("original error")
    if scenario != "exhausted":
        def fail(*a):
            raise primary
        env.process = fail

    temporary = env.proof.with_name(env.proof.name + ".tmp")
    def fail_replace(*a):
        raise OSError("publication unavailable")
    monkeypatch.setattr(main_module.os, "replace", fail_replace)

    class BrokenStderr:
        def write(self, text):
            raise KeyboardInterrupt("diagnostic unavailable")

    cleanup_attempts = []
    real_unlink = Path.unlink
    def fail_cleanup(path, *a, **kw):
        if path == (env.proof if secondary_fault == "destination-cleanup" else temporary):
            cleanup_attempts.append(path)
            raise KeyboardInterrupt("cleanup unavailable")
        return real_unlink(path, *a, **kw)

    with monkeypatch.context() as patches:
        if secondary_fault == "diagnostic":
            patches.setattr(sys, "stderr", BrokenStderr())
        else:
            patches.setattr(Path, "unlink", fail_cleanup)
        if scenario == "exhausted":
            assert _completion_cli(env, patches) is None
        elif scenario == "handled":
            with pytest.raises(SystemExit) as caught:
                _completion_cli(env, patches)
            assert caught.value.code == 0
        else:
            with pytest.raises(ValueError) as caught:
                _completion_cli(env, patches)
            assert caught.value is primary
    if secondary_fault == "destination-cleanup":
        assert cleanup_attempts == [env.proof]
        assert json.loads(env.proof.read_text()) == previous_proof
    else:
        assert not env.proof.exists()
    if secondary_fault == "cleanup":
        assert cleanup_attempts == [temporary]
        assert temporary.exists()
    else:
        assert not temporary.exists()


def test_completion_missing_parent_preserves_exhausted_return(completion_env, monkeypatch, capsys):
    env = completion_env
    _completion_roadmap(env)
    env.proof = env.proof.parent / "unavailable" / "completion.json"
    assert _completion_cli(env, monkeypatch) is None
    assert main_module.state.queue_exhausted is True
    assert not env.proof.exists()
    assert "Could not publish completion proof:" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["implement", "test"])
def test_completion_initially_empty_queue_with_stop_is_negative(completion_env, monkeypatch, mode):
    env = completion_env
    _completion_roadmap(env, mode)
    main_module.state.stop_requested = True
    assert _completion_cli(env, monkeypatch, mode) is None
    assert main_module.state.queue_exhausted is False
    assert env.events == ["constructor"]
    _assert_completion(env, "FAIL")


@pytest.mark.parametrize("suffix", [" ", "\n", "\r", "\r\n"])
def test_external_evidence_check_preserves_worktree_path_characters(tmp_path, suffix):
    project = tmp_path / ("work tree" + suffix)
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    with pytest.raises(ValueError, match="outside target Git worktree"):
        layout_module._external_evidence_path(project, project / "evidence.json")
    external = tmp_path / "external.json"
    assert layout_module._external_evidence_path(project, external) == external.resolve()
