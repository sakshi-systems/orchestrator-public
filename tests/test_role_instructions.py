"""What each model role actually sends the backend: the composed instructions on initial and
continuing calls, and the re-review assignment's current-file obligations. Offline — a fake
backend records every call and writes the assigned artifacts."""

from pathlib import Path

import pytest

from orchestrator import agents
from orchestrator.agents import Implementer, PlanReviewer, PlannerReviewer, _load_prompt
from orchestrator.ports import AgentRole
from orchestrator.protocol import _read_sessions
from tests.fakes import BackendCall, FakeAgentBackend, target_path

_PROMPT_NAMES = ("execution", "planner", "test-planner", "reviewer", "implementer", "escalation")


def _canary(name: str) -> str:
    return f"<<canary:{name}>>"


@pytest.fixture
def canary_prompts(tmp_path, monkeypatch):
    """Point the prompt loader at one distinguishable canary file per prompt name."""
    prompts_dir = tmp_path / "canary-prompts"
    prompts_dir.mkdir()
    for name in _PROMPT_NAMES:
        (prompts_dir / f"{name}.md").write_text(_canary(name))
    monkeypatch.setattr(agents, "PROMPTS_DIR", prompts_dir)


@pytest.fixture
def project_dir(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def _assert_composition(instructions: str, expected: list[str]) -> None:
    """The execution canary once, then exactly the expected role canaries once each, in order."""
    assert instructions.count(_canary("execution")) == 1
    for name in _PROMPT_NAMES:
        wanted = name == "execution" or name in expected
        assert instructions.count(_canary(name)) == (1 if wanted else 0), name
    positions = [instructions.index(_canary(name)) for name in ["execution", *expected]]
    assert positions == sorted(set(positions))


def _artifact_backend() -> FakeAgentBackend:
    """A fake that writes the artifact each assignment names, as the real roles' agents do."""

    def on_call(call: BackendCall) -> None:
        if call.role is AgentRole.IMPLEMENTER:
            return
        path = target_path(call.prompt)
        path.parent.mkdir(parents=True, exist_ok=True)
        if call.role is AgentRole.PLAN_REVIEWER:
            path.write_text("ok\n\nPLAN_REVIEW_PASS")
        elif "-review-" in path.name:
            path.write_text("ok\n\nREVIEW_PASS")
        else:
            path.write_text("# Plan")

    return FakeAgentBackend(on_call=on_call)


def test_planner_reviewer_carries_full_composition_across_plan_revision_review_and_re_review(
    canary_prompts, project_dir, tmp_path
):
    backend = _artifact_backend()
    role = PlannerReviewer(project_dir, backend)
    plan = tmp_path / "plan.md"
    plan_review = tmp_path / "plan-review-1.md"
    plan_review.write_text("findings")
    review_1 = tmp_path / "x-review-1.md"
    review_2 = tmp_path / "x-review-2.md"

    role.plan("Title", "Description", plan)
    role.plan("Title", "Description", plan, plan_review_path=plan_review)
    role.review(plan, review_1)
    role.review(plan, review_2, prev_review_path=review_1)

    assert len(backend.calls) == 4
    for call in backend.calls:
        assert call.role is AgentRole.PLANNER_REVIEWER
        assert call.cwd == str(project_dir)
        _assert_composition(call.instructions, ["planner", "reviewer", "escalation"])
    assert [c.session_id for c in backend.calls] == [None, *backend.returned[:3]]

    plan_call, revision_call, review_call, re_review_call = backend.calls
    assert str(plan) in plan_call.prompt
    assert str(plan) in revision_call.prompt and str(plan_review) in revision_call.prompt
    assert str(review_1) in review_call.prompt
    assert str(review_2) in re_review_call.prompt and str(review_1) in re_review_call.prompt


def test_test_planner_variant_replaces_planner_fragment_and_continues_session(
    canary_prompts, project_dir, tmp_path
):
    backend = _artifact_backend()
    role = PlannerReviewer(project_dir, backend, planner_prompt_name="test-planner")
    plan = tmp_path / "plan.md"
    review = tmp_path / "x-review-1.md"

    role.plan("Title", "Description", plan)
    role.review(plan, review)

    plan_call, review_call = backend.calls
    for call in backend.calls:
        _assert_composition(call.instructions, ["test-planner", "reviewer", "escalation"])
    assert plan_call.session_id is None
    assert review_call.session_id == backend.returned[0]


def test_plan_reviews_are_independent_fresh_sessions_with_reviewer_composition(
    canary_prompts, project_dir, tmp_path
):
    backend = _artifact_backend()
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan")

    for attempt in (1, 2):
        PlanReviewer(project_dir, backend).review_plan(plan, tmp_path / f"pr-{attempt}.md", attempt)

    assert len(backend.calls) == 2
    for call in backend.calls:
        assert call.role is AgentRole.PLAN_REVIEWER
        assert call.session_id is None
        assert call.cwd == str(project_dir)
        _assert_composition(call.instructions, ["reviewer", "escalation"])
    sessions = _read_sessions(plan)
    assert sessions["plan-reviewer-1"] == backend.returned[0]
    assert sessions["plan-reviewer-2"] == backend.returned[1]


def test_implementer_initial_call_and_fix_share_composition_and_continue_session(
    canary_prompts, project_dir, tmp_path
):
    backend = _artifact_backend()
    role = Implementer(project_dir, backend)
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan")
    feedback = tmp_path / "feedback.md"

    role.implement(plan)
    role.implement(plan, feedback_path=feedback)

    first, fix = backend.calls
    for call in backend.calls:
        assert call.role is AgentRole.IMPLEMENTER
        assert call.cwd == str(project_dir)
        _assert_composition(call.instructions, ["implementer", "escalation"])
    assert first.session_id is None
    assert fix.session_id == backend.returned[0]
    assert str(plan) in first.prompt
    assert str(feedback) in fix.prompt


def test_re_review_assignment_requires_current_file_evidence(project_dir, tmp_path):
    backend = _artifact_backend()
    role = PlannerReviewer(project_dir, backend)
    plan = tmp_path / "plan.md"
    review_1 = tmp_path / "x-review-1.md"
    review_2 = tmp_path / "x-review-2.md"

    role.review(plan, review_1)
    role.review(plan, review_2, prev_review_path=review_1)

    prompt = backend.calls[-1].prompt
    assert str(review_1) in prompt
    assert "as it is now" in prompt
    assert "read it in full" in prompt
    assert "quote the CURRENT content" in prompt
    assert "Fixed or Not fixed" in prompt
    assert "new issues" in prompt


def test_packaged_execution_home_reaches_every_role_before_its_own_fragment(project_dir, tmp_path):
    execution = _load_prompt("execution")
    assert execution.strip()
    backend = _artifact_backend()
    plan = tmp_path / "plan.md"
    review = tmp_path / "x-review-1.md"

    planner = PlannerReviewer(project_dir, backend)
    planner.plan("Title", "Description", plan)
    planner.review(plan, review)
    PlanReviewer(project_dir, backend).review_plan(plan, tmp_path / "pr-1.md", 1)
    Implementer(project_dir, backend).implement(plan)

    own_fragment = {
        0: "planner",
        1: "planner",
        2: "reviewer",
        3: "implementer",
    }
    assert len(backend.calls) == 4
    for index, call in enumerate(backend.calls):
        assert call.instructions.count(execution) == 1
        assert call.instructions.index(execution) < call.instructions.index(_load_prompt(own_fragment[index]))
