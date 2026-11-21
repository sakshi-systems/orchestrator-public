"""What each model role actually sends the backend: the composed instructions on initial and
continuing calls, and the re-review assignment's current-file obligations. Offline — a fake
backend records every call and writes the assigned artifacts."""

from pathlib import Path

import pytest

from orchestrator import agents
from orchestrator.agents import CodeReviewer, Implementer, PlanReviewer, Planner, _load_prompt
from orchestrator.implementer_result import WorkingTreeResult
from orchestrator.ports import AgentRole, IterationState
from orchestrator.protocol import _read_sessions
from tests.fakes import BackendCall, FakeAgentBackend

_PROMPT_NAMES = ("execution", "planner", "test-planner", "reviewer", "implementer", "escalation")



_ITERATION = IterationState(base="0" * 40, commit="1" * 40, ref="refs/orchestrator/01-slug")

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

@pytest.fixture(autouse=True)
def _factory_dir(project_dir):
    (project_dir / ".ai-factory").mkdir(exist_ok=True)



def _assert_composition(instructions: str, expected: list[str]) -> None:
    """The execution canary once, then exactly the expected role canaries once each, in order."""
    assert instructions.count(_canary("execution")) == 1
    for name in _PROMPT_NAMES:
        wanted = name == "execution" or name in expected
        assert instructions.count(_canary(name)) == (1 if wanted else 0), name
    positions = [instructions.index(_canary(name)) for name in ["execution", *expected]]
    assert positions == sorted(set(positions))


def _artifact_backend() -> FakeAgentBackend:
    """A fake that answers the reviewers and writes the artifact each other
    assignment names, as the real roles' agents do."""

    def on_call(call: BackendCall) -> str | None:
        if call.role is AgentRole.IMPLEMENTER:
            return None
        if call.role is AgentRole.PLAN_REVIEWER:
            return "ok\n\nPLAN_REVIEW_PASS"
        if call.role is AgentRole.CODE_REVIEWER:
            return "ok\n\nREVIEW_PASS"
        return "# Plan"

    return FakeAgentBackend(on_call=on_call)


def test_planner_carries_planner_composition_across_plan_and_revision(
    canary_prompts, project_dir, tmp_path
):
    backend = _artifact_backend()
    role = Planner(project_dir, backend)
    plan = project_dir / ".ai-factory" / "plan.md"
    plan_review = project_dir / ".ai-factory" / "plan-review-1.md"
    plan_review.write_text("findings")

    role.plan("Title", "Description", plan)
    role.plan("Title", "Description", plan, plan_review_path=plan_review)

    assert len(backend.calls) == 2
    for call in backend.calls:
        assert call.role is AgentRole.PLANNER
        assert call.cwd == str(project_dir)
        _assert_composition(call.instructions, ["planner", "escalation"])
    assert [c.session_id for c in backend.calls] == [None, backend.returned[0]]

    plan_call, revision_call = backend.calls
    assert "**Title**" in plan_call.prompt and "Description" in plan_call.prompt
    assert str(plan) not in plan_call.prompt
    assert "## Plan\n\n# Plan" in revision_call.prompt
    assert "## Plan review\n\nfindings" in revision_call.prompt
    assert str(plan) not in revision_call.prompt and str(plan_review) not in revision_call.prompt


def test_code_reviewer_carries_reviewer_composition_across_review_and_re_review(
    canary_prompts, project_dir, tmp_path
):
    backend = _artifact_backend()
    role = CodeReviewer(project_dir, backend)
    plan = project_dir / ".ai-factory" / "plan.md"
    plan.write_text("# Plan")
    review_1 = project_dir / ".ai-factory" / "x-review-1.md"
    review_2 = project_dir / ".ai-factory" / "x-review-2.md"

    role.review(plan, review_1, iteration_state=_ITERATION)
    role.review(plan, review_2, prev_review_path=review_1, iteration_state=_ITERATION)

    assert len(backend.calls) == 2
    for call in backend.calls:
        assert call.role is AgentRole.CODE_REVIEWER
        assert call.cwd == str(project_dir)
        _assert_composition(call.instructions, ["reviewer", "escalation"])
    assert [c.session_id for c in backend.calls] == [None, backend.returned[0]]
    assert str(review_1) not in backend.calls[0].prompt and str(review_2) not in backend.calls[0].prompt
    assert str(review_2) not in backend.calls[1].prompt and str(review_1) not in backend.calls[1].prompt
    assert "## The previous review" in backend.calls[1].prompt


def test_test_planner_variant_replaces_planner_fragment_and_review_opens_its_own_session(
    canary_prompts, project_dir, tmp_path
):
    backend = _artifact_backend()
    planner = Planner(project_dir, backend, planner_prompt_name="test-planner")
    reviewer = CodeReviewer(project_dir, backend)
    plan = project_dir / ".ai-factory" / "plan.md"
    review = project_dir / ".ai-factory" / "x-review-1.md"

    planner.plan("Title", "Description", plan)
    reviewer.review(plan, review, iteration_state=_ITERATION)

    plan_call, review_call = backend.calls
    _assert_composition(plan_call.instructions, ["test-planner", "escalation"])
    _assert_composition(review_call.instructions, ["reviewer", "escalation"])
    assert plan_call.session_id is None
    assert review_call.session_id is None
    assert plan_call.role is AgentRole.PLANNER and review_call.role is AgentRole.CODE_REVIEWER


def test_plan_reviews_are_independent_fresh_sessions_with_reviewer_composition(
    canary_prompts, project_dir, tmp_path
):
    backend = _artifact_backend()
    plan = project_dir / ".ai-factory" / "plan.md"
    plan.write_text("# Plan")

    for attempt in (1, 2):
        PlanReviewer(project_dir, backend).review_plan(plan, project_dir / ".ai-factory" / f"pr-{attempt}.md", attempt)

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
    role = Implementer(project_dir, backend, result=WorkingTreeResult())
    plan = project_dir / ".ai-factory" / "plan.md"
    plan.write_text("# Plan")
    feedback = tmp_path / "feedback.md"
    feedback.write_text("address the findings")

    role.implement(plan, tmp_path / "impl-1.md")
    role.implement(plan, tmp_path / "impl-2.md", feedback_path=feedback)

    first, fix = backend.calls
    for call in backend.calls:
        assert call.role is AgentRole.IMPLEMENTER
        assert call.cwd == str(project_dir)
        _assert_composition(call.instructions, ["implementer", "escalation"])
    assert first.session_id is None
    assert fix.session_id == backend.returned[0]
    assert "## Plan\n\n# Plan" in first.prompt
    assert str(plan) not in first.prompt
    assert "## Review\n\naddress the findings" in fix.prompt
    assert str(feedback) not in fix.prompt


def test_re_review_assignment_requires_current_file_evidence(project_dir, tmp_path):
    backend = _artifact_backend()
    role = CodeReviewer(project_dir, backend)
    plan = project_dir / ".ai-factory" / "plan.md"
    plan.write_text("# Plan")
    review_1 = project_dir / ".ai-factory" / "x-review-1.md"
    review_2 = project_dir / ".ai-factory" / "x-review-2.md"

    role.review(plan, review_1, iteration_state=_ITERATION)
    role.review(plan, review_2, prev_review_path=review_1, iteration_state=_ITERATION)
    prompt = backend.calls[-1].prompt

    assert "as it is now" in prompt
    assert "read it in full" in prompt
    assert "quote the CURRENT content" in prompt
    assert "Fixed or Not fixed" in prompt
    assert "new issues" in prompt
    assert "## The previous review" in prompt
    assert str(review_1) not in prompt and str(review_2) not in prompt


def test_packaged_execution_home_reaches_every_role_before_its_own_fragment(project_dir, tmp_path):
    execution = _load_prompt("execution")
    assert execution.strip()
    backend = _artifact_backend()
    plan = project_dir / ".ai-factory" / "plan.md"
    review = project_dir / ".ai-factory" / "x-review-1.md"

    planner = Planner(project_dir, backend)
    planner.plan("Title", "Description", plan)
    CodeReviewer(project_dir, backend).review(plan, review, iteration_state=_ITERATION)
    PlanReviewer(project_dir, backend).review_plan(plan, project_dir / ".ai-factory" / "pr-1.md", 1)
    Implementer(project_dir, backend, result=WorkingTreeResult()).implement(plan, tmp_path / "impl-1.md")

    own_fragment = {
        0: "planner",
        1: "reviewer",
        2: "reviewer",
        3: "implementer",
    }
    assert len(backend.calls) == 4
    for index, call in enumerate(backend.calls):
        assert call.instructions.count(execution) == 1
        assert call.instructions.index(execution) < call.instructions.index(_load_prompt(own_fragment[index]))
