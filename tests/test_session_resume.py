"""Behavioral evidence for runtime-owned records and continuation through the shared flow."""

import io
import json
import os
import subprocess
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.errors import HaltError
from orchestrator.protocol import _read_sessions, _write_artifact, _write_session, _write_sessions
from orchestrator import pipeline
from orchestrator.agents import Planner, SpecLayer, _load_prompt
from orchestrator.config import OrchestratorConfig, role_runtimes
from orchestrator.errors import EscalationError
from orchestrator.layout import _artifact_subdir, _tests_sibling
from orchestrator.implementer_result import WorkingTreeResult
from orchestrator.pipeline import IMPLEMENT_MODE, TEST_MODE, _artifact_dir, _next_number, process_task
from orchestrator.ports import AgentRole, Repository
from orchestrator.roadmap import parse_roadmap
from tests.fakes import FakeAgentBackend


INVALID_FIELDS = [
    ("runtime", value) for value in (
        None, "", "CLAUDE", " codex", "other", 1, [],
        {"planner": {"runtime": "claude"}, "architect": {"runtime": "claude"}},
        {"planner": "claude"},
        {"planner": {}},
        {"planner": {"runtime": "other"}},
        {"planner": {"runtime": 1}},
        {"planner": {"runtime": "hermes"}},
        {"planner": {"runtime": "hermes", "instance": 5}},
        {"planner": {"runtime": "hermes", "instance": ""}},
        {"planner": {"runtime": "head"}},
        {"planner": {"runtime": "head", "head": {}}},
        {"planner": {"runtime": "head", "head": {"repository": "/r", "owner": "m"}}},
        {"planner": {"runtime": "head", "head": {"repository": "", "owner": "m", "folder": "f"}}},
        {"planner": {"runtime": "head", "head": {"repository": "/r", "owner": 3, "folder": "f"}}},
    )
] + [
    (key, value)
    for key in ("planner", "implementer", "code-reviewer", "plan-reviewer-2")
    for value in (None, "", " \t\n", 0, {}, [])
] + [
    ("elapsed", value) for value in (None, 0, "", "-1", "+1", "1.0", " 1", "١", "²")
] + [("step", value) for value in (None, 0, [], {})]


@pytest.mark.parametrize("key,value", INVALID_FIELDS)
def test_invalid_known_values_preserve_read_and_merge_evidence(tmp_path, monkeypatch, key, value):
    plan = tmp_path / "01-work.md"
    sidecar = plan.with_suffix(".json")
    original = json.dumps({key: value, "optional": {"nested": [1, None]}}).encode()
    sidecar.write_bytes(original)
    effects = []
    monkeypatch.setattr("orchestrator.protocol.os.replace", lambda *args: effects.append(args))
    monkeypatch.setattr(Path, "write_text", lambda path, *args, **kwargs: effects.append(("write", path)))
    with pytest.raises(HaltError, match="Sidecar has an invalid field value") as caught:
        _read_sessions(plan)
    assert str(sidecar) in str(caught.value)
    assert key in str(caught.value)
    for update in (("step", "done"), (key, "valid")):
        with pytest.raises(HaltError):
            _write_session(plan, *update)
        assert sidecar.read_bytes() == original
        assert not sidecar.with_suffix(".json.tmp").exists()
    with pytest.raises(HaltError):
        _write_sessions(plan, {key: "valid", "step": "done", "runtime": "claude"})
    assert sidecar.read_bytes() == original
    assert effects == []


@pytest.mark.parametrize("key,value", INVALID_FIELDS)
def test_invalid_update_never_reaches_temp_or_replace(tmp_path, monkeypatch, key, value):
    plan = tmp_path / "01-work.md"
    sidecar = plan.with_suffix(".json")
    original = b'{"step":"planned:1", "optional":[null, {"a":1}]}'
    sidecar.write_bytes(original)
    effects = []
    monkeypatch.setattr("orchestrator.protocol.os.replace", lambda *args: effects.append(args))
    monkeypatch.setattr(Path, "write_text", lambda path, *args, **kwargs: effects.append(("write", path)))
    with pytest.raises(HaltError):
        _write_session(plan, key, value)
    with pytest.raises(HaltError):
        _write_sessions(plan, {"runtime": "claude", "step": "planning:1", key: value})
    assert sidecar.read_bytes() == original
    assert not sidecar.with_suffix(".json.tmp").exists()
    assert effects == []


@pytest.mark.parametrize("elapsed", ["0", "00", "123"])
@pytest.mark.parametrize("owner", [
    "codex",
    {role.value: {"runtime": "codex"} for role in AgentRole},
    {"planner": {"runtime": "codex"}, "code_reviewer": {"runtime": "codex"}},
    {
        "planner": {"runtime": "hermes", "instance": "main"},
        "code_reviewer": {"runtime": "hermes", "instance": "second"},
    },
])
def test_valid_record_keeps_opaque_identities_and_optional_json(tmp_path, elapsed, owner):
    plan = tmp_path / "01-work.md"
    data = {
        "runtime": owner, "planner": "  opaque planner\n", "implementer": "not-a-uuid",
        "code-reviewer": " opaque reviewer ", "plan-reviewer-2": " audit ", "elapsed": elapsed,
        "step": "unknown boundary",
        "optional": {"nested": [None, 1, True]},
    }
    plan.with_suffix(".json").write_text(json.dumps(data))
    assert _read_sessions(plan) == data
    _write_session(plan, "step", "done")
    assert _read_sessions(plan) == {**data, "step": "done"}
    assert not plan.with_suffix(".json.tmp").exists()


@pytest.fixture
def flow(tmp_path, monkeypatch):
    """Real task parsing and roles; external effects are isolated from the flow."""
    monkeypatch.setattr(pipeline, "_git_commit", lambda *args: None)
    monkeypatch.setattr(pipeline, "report", lambda *args: None)
    monkeypatch.setattr(pipeline.state, "tasks_done", 0)
    # The iteration's commit is real git: the flow's directory is a repository
    # of its own, with an identity set — so the in-loop `git add -A` runs there
    # too, against the real index.
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True)
    (tmp_path / ".gitkeep").write_text("")
    subprocess.run(["git", "add", ".gitkeep"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=str(tmp_path), check=True)

    # The run's ref pushes to a real origin: a bare repository beside the flow.
    origin = tmp_path.parent / f"{tmp_path.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], cwd=str(tmp_path), check=True)

    def prepare(*, runtime="claude", named=False, test_mode=False, prefix="1"):
        base = TEST_MODE if test_mode else IMPLEMENT_MODE
        roadmap_relpath = "roadmaps/developer.md" if named else "ROADMAP.md"
        if test_mode:
            roadmap_relpath = _tests_sibling(roadmap_relpath)
        mode = base._replace(roadmap_relpath=roadmap_relpath, artifact_subdir=_artifact_subdir(roadmap_relpath))
        roadmap = tmp_path / ".ai-factory" / roadmap_relpath
        roadmap.parent.mkdir(parents=True, exist_ok=True)
        roadmap.write_text("# Work\n\n- [ ] **Runtime task** — Complete task context\n")
        task = parse_roadmap(roadmap).tasks[0]
        plans = _artifact_dir(tmp_path, mode, "plans")
        prd = _artifact_dir(tmp_path, mode, "plan-reviews")
        output = _artifact_dir(tmp_path, mode, mode.verify.output_dirname)
        for directory in (plans, prd, output):
            directory.mkdir(parents=True, exist_ok=True)
        config = OrchestratorConfig(3, 90, 95, False, runtime=runtime)
        return SimpleNamespace(project=tmp_path, task=task, mode=mode, roadmap=roadmap,
                               plan=plans / f"{prefix}-{task.slug}.md", prd=prd, output=output,
                               config=config)

    return prepare


def _seed(flow, record=None, *, markdown=True):
    if markdown:
        flow.plan.write_text("# Plan\n\n## Test Command\n`true`\n")
    if record is not None:
        flow.plan.with_suffix(".json").write_text(json.dumps(record))


def _review(flow, number=1, *, passed=True, prefix=None):
    stem = flow.plan.stem if prefix is None else f"{prefix}-{flow.task.slug}"
    path = flow.prd / f"{stem}-plan-review-{number}.md"
    path.write_text("PLAN_REVIEW_PASS" if passed else "Actual findings")
    return path


def _backend(flow):
    def write(call):
        if call.role is AgentRole.IMPLEMENTER:
            return None
        if call.role is AgentRole.PLAN_REVIEWER:
            return "PLAN_REVIEW_PASS"
        if call.role is AgentRole.CODE_REVIEWER:
            return "REVIEW_PASS"
        return "# Plan\n\n## Test Command\n`true`\n"
    return FakeAgentBackend(on_call=write)


def _run(flow, backend, *, phase=None):
    return process_task(flow.project, flow.task, _next_number(flow.plan.parent), flow.config,
                        flow.mode, phase_session_id=phase, backend=backend, implementer_result=WorkingTreeResult(),
                        tree=(Repository(directory=flow.project, folder=None),))


def _snapshot(flow):
    return {str(p.relative_to(flow.project)): p.read_bytes()
            for p in flow.project.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(flow.project).parts}


def _assert_planner_assignment(call, flow):
    assert call.role is AgentRole.PLANNER
    assert flow.task.title in call.prompt
    assert flow.task.description in call.prompt
    assert f"Roadmap: {flow.roadmap.relative_to(flow.project).as_posix()} (line {flow.task.line_number + 1})" in call.prompt
    assert "Return the plan as your answer." in call.prompt
    assert str(flow.plan) not in call.prompt
    names = ("execution", flow.mode.planner_prompt_name, "escalation")
    fragments = [_load_prompt(name) for name in names]
    assert all(call.instructions.count(fragment) == 1 for fragment in fragments)
    positions = [call.instructions.index(fragment) for fragment in fragments]
    assert positions == sorted(positions)


@pytest.fixture
def replacements(monkeypatch):
    """Observe complete temporary records at the actual atomic replacement boundary."""
    records = []
    real_replace = os.replace

    def inspect(src, dst):
        record = json.loads(Path(src).read_text())
        if "runtime" in record:
            assert isinstance(record.get("step"), str) and record["step"]
        records.append((Path(dst), record))
        real_replace(src, dst)

    monkeypatch.setattr("orchestrator.protocol.os.replace", inspect)
    return records


@pytest.mark.parametrize("raw,test_mode", [
    (raw, test_mode)
    for raw in (None, "", "unknown boundary", "planned:01", "implemented:01", "review_failed:01")
    for test_mode in (False, True)
    if not (test_mode and raw == "implemented:01")
])
def test_legacy_adoption_exposes_complete_valid_boundary_before_first_role(flow, replacements, raw, test_mode):
    f = flow(test_mode=test_mode)
    if raw == "review_failed:01":
        raw = f.mode.verify.fail_tag + "01"
    record = {"implementer": "impl", "plan-reviewer-1": "audit", "elapsed": "007",
              "optional": {"nested": [None, True]}}
    if raw is not None:
        record["step"] = raw
    _seed(f, record)
    if raw and not raw.startswith("planned:"):
        _review(f)
    if raw and raw.startswith(f.mode.verify.fail_tag):
        (f.output / f"{f.plan.stem}{f.mode.verify.output_suffix.format(n=1)}").write_text("findings")
    expected = raw if raw and raw not in ("unknown boundary",) else "planned:1"
    if raw == "unknown boundary":
        expected = "plan_reviewed"

    def inspect(call):
        data = _read_sessions(f.plan)
        assert data == {**record, "runtime": role_runtimes("claude"), "step": expected}
        raise RuntimeError("observed first role")

    backend = FakeAgentBackend(on_call=inspect)
    with pytest.raises(RuntimeError, match="observed first role"):
        _run(f, backend)
    assert replacements[0] == (f.plan.with_suffix(".json"), {**record, "runtime": role_runtimes("claude"), "step": expected})
    assert len(replacements) == 1


@pytest.mark.parametrize("raw,review,expected", [
    (raw, review, expected)
    for raw in (None, "", "unknown boundary")
    for review, expected in ((None, "planned:1"), (True, "plan_reviewed"), (False, "planning:2"))
] + [
    ("plan_reviewed", None, "planned:1"),
    ("plan_review_failed:2", True, "plan_reviewed"),
    ("review_failed:1", False, "planning:2"),
    ("plan_reviewed", True, "plan_reviewed"),
    ("plan_review_failed:01", False, "planning:2"),
])
def test_legacy_inference_and_stale_boundaries_normalize_atomically_before_calls(flow, replacements, raw, review, expected):
    f = flow(named=True)
    record = {"planner": " saved planner ", "implementer": "unused impl", "elapsed": "007",
              "plan-reviewer-1": "audit", "optional": {"nested": [None, {"ok": True}]}}
    if raw is not None:
        record["step"] = raw
    _seed(f, record)
    if review is not None:
        _review(f, passed=review)

    def inspect(call):
        data = _read_sessions(f.plan)
        assert data == {**record, "runtime": role_runtimes("claude"), "step": expected}
        if expected.startswith("planning:"):
            _assert_planner_assignment(call, f)
            assert call.session_id == record["planner"]
        elif expected.startswith("planned:"):
            assert call.role is AgentRole.PLAN_REVIEWER and call.session_id is None
        else:
            assert call.role is AgentRole.IMPLEMENTER and call.session_id == record["implementer"]
        raise RuntimeError("observed normalization")

    with pytest.raises(RuntimeError, match="observed normalization"):
        _run(f, FakeAgentBackend(on_call=inspect), phase="competing phase")
    records = [data for _, data in replacements]
    retained = raw == "plan_review_failed:01"
    assert records[0] == {**record, "runtime": role_runtimes("claude"), "step": raw if retained else expected}
    assert records[-1]["step"] == expected
    assert all(data["optional"] == record["optional"] and data["elapsed"] == "007" for data in records)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("raw,review,expected", [
    ("plan_reviewed", None, "planned:1"),
    ("plan_review_failed:2", True, "plan_reviewed"),
    ("review_failed:1", False, "planning:2"),
])
def test_tagged_artifact_fallback_replaces_stale_boundary_before_role(flow, replacements, runtime, raw, review, expected):
    f = flow(runtime=runtime)
    record = {"runtime": runtime, "step": raw, "planner": "saved", "optional": {"n": [None]}}
    _seed(f, record)
    if review is not None:
        _review(f, passed=review)

    def inspect(call):
        assert _read_sessions(f.plan) == {**record, "runtime": role_runtimes(runtime), "step": expected}
        raise RuntimeError("observed tagged fallback")

    with pytest.raises(RuntimeError, match="observed tagged fallback"):
        _run(f, FakeAgentBackend(on_call=inspect))
    assert replacements == [(f.plan.with_suffix(".json"), {**record, "runtime": role_runtimes(runtime), "step": expected})]


@pytest.mark.parametrize("passed,role", [(True, AgentRole.IMPLEMENTER), (False, AgentRole.PLANNER)])
def test_resume_reads_a_non_ascii_plan_review_as_written(flow, monkeypatch, passed, role):
    """A resume signal read of a non-ASCII plan review decides the boundary under a default
    text encoding that is not UTF-8: a passing review resumes at the implementer with the
    plan's exact text in its message, a failing one revises the plan carrying the review's
    exact text."""
    f = flow()
    _seed(f, {"runtime": "claude", "step": "plan_reviewed", "planner": "saved", "implementer": "impl-saved"},
          markdown=False)
    _write_artifact(f.plan, "# План: работа ✓\n\n## Test Command\n`true`\n")
    review_text = "Проблемы — не хватает шага."
    if passed:
        review_text += "\n\nPLAN_REVIEW_PASS\n"
    _write_artifact(f.prd / f"1-{f.task.slug}-plan-review-1.md", review_text)

    def inspect(call):
        assert call.role is role
        if passed:
            assert call.session_id == "impl-saved"
            assert "## Plan\n\n# План: работа ✓" in call.prompt
        else:
            assert "## Plan review\n\nПроблемы — не хватает шага." in call.prompt
        raise RuntimeError("observed first role")

    monkeypatch.setattr(io, "text_encoding", lambda enc, *a, **k: "latin-1" if enc is None else enc)
    with pytest.raises(RuntimeError, match="observed first role"):
        _run(f, FakeAgentBackend(on_call=inspect))


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("raw,test_mode", [
    ("planned:01", False), ("plan_reviewed", True), ("implemented:01", False),
    ("review_failed:01", False), ("test_run_failed:01", True),
])
def test_valid_tagged_boundary_is_not_rewritten_before_first_role(flow, replacements, runtime, raw, test_mode):
    f = flow(runtime=runtime, test_mode=test_mode)
    record = {"runtime": runtime, "step": raw, "planner": "saved", "optional": [None, True]}
    _seed(f, record)
    _review(f)
    if raw.startswith(f.mode.verify.fail_tag):
        (f.output / f"{f.plan.stem}{f.mode.verify.output_suffix.format(n=1)}").write_text("Actual findings")

    def inspect(call):
        assert _read_sessions(f.plan) == record
        assert replacements == []
        raise RuntimeError("observed retained tagged boundary")

    with pytest.raises(RuntimeError, match="observed retained tagged boundary"):
        _run(f, FakeAgentBackend(on_call=inspect))


@pytest.mark.parametrize("record", [None, {}, {"step": "unknown string"}])
@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_legacy_markdown_adoption_is_claude_only(flow, runtime, record):
    f = flow(runtime=runtime)
    _seed(f, record)
    before = _snapshot(f)
    backend = _backend(f)
    if runtime == "codex":
        with pytest.raises(HaltError, match="runtime does not match"):
            _run(f, backend)
        assert backend.calls == []
        assert _snapshot(f) == before
    else:
        _run(f, backend)
        assert backend.calls[0].role is AgentRole.PLAN_REVIEWER
        assert backend.calls[0].session_id is None
        assert backend.calls[1].session_id is None
        assert backend.calls[2].session_id is None
        assert _read_sessions(f.plan)["step"] == "done"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("saved_planner,phase,expected", [
    ("  opaque planner\n", "phase", "  opaque planner\n"),
    (None, "phase", "phase"), (None, None, None),
])
@pytest.mark.parametrize("saved_impl", [None, " opaque implementer "])
def test_matching_owner_restores_only_continuing_ids_by_presence(flow, runtime, saved_planner, phase, expected, saved_impl):
    f = flow(runtime=runtime)
    record = {"runtime": runtime, "step": "review_failed:1", "plan-reviewer-1": "audit-only",
              "optional": {"nested": [True, None]}, "elapsed": "7"}
    if saved_planner is not None:
        record["planner"] = saved_planner
    if saved_impl is not None:
        record["implementer"] = saved_impl
    _seed(f, record)
    _review(f)
    (f.output / f"{f.plan.stem}-review-1.md").write_text("findings")
    backend = _backend(f)
    _run(f, backend, phase=phase)
    impl, review = backend.calls
    assert impl.role is AgentRole.IMPLEMENTER and impl.session_id == saved_impl
    assert review.role is AgentRole.CODE_REVIEWER and review.session_id is None
    assert all(call.session_id != "audit-only" for call in backend.calls)
    updated = _read_sessions(f.plan)
    assert updated["plan-reviewer-1"] == "audit-only"
    assert updated["optional"] == record["optional"]
    assert int(updated["elapsed"]) >= 7


@pytest.mark.parametrize("runtime,owner", [("codex", "claude"), ("claude", "codex"), ("codex", None)])
def test_owner_mismatch_precedes_restore_and_artifact_fallback(flow, runtime, owner):
    f = flow(runtime=runtime)
    record = {"step": "planned:1", "planner": "saved"}
    if owner is not None:
        record["runtime"] = owner
    _seed(f, record)
    _review(f)
    backend = _backend(f)
    before = _snapshot(f)
    with pytest.raises(HaltError, match="runtime does not match") as caught:
        _run(f, backend, phase="phase")
    lines = str(caught.value).splitlines()
    recorded = owner if owner is not None else "claude"
    assert lines[1] == str(f.plan.with_suffix(".json"))
    assert lines[2:] == [
        f"Role: {role}; recorded: {{'runtime': '{recorded}'}}; selected: {{'runtime': '{runtime}'}}"
        for role in ("planner", "plan_reviewer", "implementer", "code_reviewer")
    ]
    assert backend.calls == []
    assert _snapshot(f) == before


def test_a_role_absent_from_the_owner_record_takes_the_effective_entry(flow):
    """A record whose owner object leaves roles out resumes with no halt: each absent
    role reads as taking the run's runtime, and the run's writes keep the record valid."""
    f = flow()
    _seed(f, {"runtime": {"planner": {"runtime": "claude"}}, "step": "planned:1", "planner": "saved"})
    _review(f)
    backend = _backend(f)

    _run(f, backend)

    assert _read_sessions(f.plan)["step"] == "done"


def test_an_instance_mismatch_halts_naming_the_role(flow):
    """A role that would continue an identity under another Hermes instance halts
    before any call, the message naming the role and both instances."""
    f = flow()
    f.config.roles = {"planner": {"runtime": "hermes", "instance": "this-one"}}
    _seed(f, {"runtime": {"planner": {"runtime": "hermes", "instance": "other-one"}},
              "step": "planned:1", "planner": "saved"})
    _review(f)
    backend = _backend(f)
    before = _snapshot(f)

    with pytest.raises(HaltError, match="runtime does not match") as caught:
        _run(f, backend)

    message = str(caught.value)
    assert "Role: planner" in message
    assert "'instance': 'other-one'" in message and "'instance': 'this-one'" in message
    assert backend.calls == []
    assert _snapshot(f) == before


@pytest.mark.parametrize("key,value", INVALID_FIELDS)
def test_malformed_active_state_cannot_call_backend_or_rewrite(flow, key, value):
    f = flow()
    _seed(f, {"runtime": "claude", "step": "planned:1", key: value})
    _review(f)
    backend = _backend(f)
    before = _snapshot(f)
    with pytest.raises(HaltError):
        _run(f, backend, phase="phase-cannot-hide-invalid-planner")
    assert backend.calls == []
    assert _snapshot(f) == before


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("saved_id,phase", [(None, None), (" known opaque id ", "phase"), (None, "phase")])
@pytest.mark.parametrize("feedback", [False, True])
@pytest.mark.parametrize("markdown", [False, True])
def test_pending_round_two_replays_rooted_assignment_and_incoming_identity(flow, runtime, saved_id, phase, feedback, markdown):
    f = flow(runtime=runtime)
    data = {"runtime": runtime, "step": "planning:2", "plan-reviewer-1": "audit",
            "implementer": "saved implementer", "elapsed": "007", "optional": {"nested": [None, True]}}
    if saved_id is not None:
        data["planner"] = saved_id
    _seed(f, data, markdown=markdown)
    previous = _review(f) if feedback else f.prd / f"{f.plan.stem}-plan-review-1.md"
    # Plausible later artifacts cannot supersede pending planning.
    _review(f, 2)
    backend = _backend(f)
    delegate = backend.on_call

    def inspect(call):
        disk = _read_sessions(f.plan)
        for key in ("implementer", "optional", "plan-reviewer-1"):
            assert disk[key] == data[key]
        if call.role is AgentRole.PLAN_REVIEWER:
            assert disk["step"] == "planned:2"
            assert int(disk["elapsed"]) >= 7
            assert call.session_id is None
        elif call.role is AgentRole.PLANNER:
            expected_id = saved_id if saved_id is not None else phase
            assert disk["runtime"] == role_runtimes(runtime) and disk["step"] == "planning:2"
            assert disk.get("planner") == expected_id
            assert disk["elapsed"] == "007"
            assert call.session_id == expected_id
        return delegate(call)

    # Inspect planning and its immediate plan review before implementation changes identities.
    def inspect_first_calls(call):
        if len(backend.calls) <= 2:
            return inspect(call)
        return delegate(call)
    backend.on_call = inspect_first_calls
    _run(f, backend, phase=phase)
    planning, review, implement, verify = backend.calls
    _assert_planner_assignment(planning, f)
    assert planning.session_id == (saved_id if saved_id is not None else phase)
    if feedback:
        assert f"## Plan review\n\n{previous.read_text()}" in planning.prompt
    else:
        assert "## Plan review" not in planning.prompt
    assert ("## Plan review" in planning.prompt) is feedback
    assert ("has issues" in planning.prompt) is feedback
    assert review.role is AgentRole.PLAN_REVIEWER and review.session_id is None
    assert (f.prd / f"{f.plan.stem}-plan-review-2.md").read_text() == "PLAN_REVIEW_PASS"
    assert implement.session_id == "saved implementer"
    assert verify.role is AgentRole.CODE_REVIEWER and verify.session_id is None
    assert _read_sessions(f.plan)["step"] == "done"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("owner", [None, "claude", "codex"])
def test_json_only_done_history_is_excluded_and_next_call_uses_fresh_group(flow, runtime, owner):
    f = flow(runtime=runtime)
    data = {"step": "done", "planner": "closed-identity"}
    if owner is not None:
        data["runtime"] = owner
    _seed(f, data, markdown=False)
    original = f.plan.with_suffix(".json").read_bytes()
    backend = _backend(f)
    _run(f, backend)
    first = backend.calls[0]
    assert first.session_id is None
    assert f.plan.with_name(f"02-{f.task.slug}.md").exists()
    assert f.plan.with_suffix(".json").read_bytes() == original


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("owner", [None, "claude", "codex"])
def test_json_only_escalation_stops_without_restoring_ids(flow, runtime, owner):
    f = flow(runtime=runtime)
    data = {"step": "escalated", "planner": "saved", "escalation": "decision needed"}
    if owner is not None:
        data["runtime"] = owner
    _seed(f, data, markdown=False)
    before = _snapshot(f)
    backend = _backend(f)
    with pytest.raises(EscalationError, match="decision needed"):
        _run(f, backend, phase="phase")
    assert backend.calls == []
    assert _snapshot(f) == before


@pytest.mark.parametrize("step", ["done", "escalated"])
@pytest.mark.parametrize("bad", [{"runtime": "other"}, {"planner": None}, {"elapsed": "-1"}])
def test_invalid_terminal_metadata_is_never_excluded_or_treated_as_escalation(flow, step, bad):
    f = flow()
    _seed(f, {"step": step, **bad}, markdown=False)
    before = _snapshot(f)
    backend = _backend(f)
    with pytest.raises(HaltError):
        _run(f, backend)
    assert backend.calls == []
    assert _snapshot(f) == before


@pytest.mark.parametrize("record,markdown", [
    ({"runtime": "claude", "step": step}, False)
    for step in ("planned:1", "plan_reviewed", "implemented:1", "review_failed:1", "plan_review_failed:1")
] + [({"step": step}, False) for step in ("", "unknown", "planning:2", "planned:1")]
  + [({"runtime": "claude", **step}, markdown)
     for markdown in (False, True) for step in ({}, {"step": ""}, {"step": "unknown"})]
  + [({"runtime": "claude", "step": step}, markdown)
     for markdown in (False, True)
     for step in ("planning:", "planning:0", "planning:-1", "planning:١", "planning:4",
                  "planned:4", "plan_review_failed:3", "review_failed:3")])
def test_unusable_record_halts_without_calls_or_evidence_rewrite(flow, record, markdown):
    f = flow()
    _seed(f, record, markdown=markdown)
    backend = _backend(f)
    before = _snapshot(f)
    with pytest.raises(HaltError):
        _run(f, backend)
    assert backend.calls == []
    assert _snapshot(f) == before


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("test_mode", [False, True])
def test_flow_preserves_selected_prefix_for_every_artifact_and_isolates_segments(flow, named, test_mode):
    f = flow(named=named, test_mode=test_mode)
    _seed(f, {"runtime": "claude", "step": "planned:2", "planner": "selected-planner"})
    padded_plan = f.plan.with_name(f"01-{f.task.slug}.md")
    padded_plan.write_text("closed padded plan")
    padded_plan.with_suffix(".json").write_text('{"step":"done", "planner":"closed-padded"}')
    padded_review = _review(f, 2, prefix="01", passed=False)
    padded_output = f.output / f"01-{f.task.slug}{f.mode.verify.output_suffix.format(n=1)}"
    padded_output.write_text("unrelated padded output")
    # A broader slug and another segment neither poison selection nor reserve this directory's numbers.
    broad = f.plan.with_name(f"0-other-{f.task.slug}.json")
    broad.write_text("malformed broad-suffix decoy")
    other = f.project / ".ai-factory" / "plans" / "other-developer"
    other.mkdir()
    (other / f"99-{f.task.slug}.json").write_text("malformed other segment")
    assert _next_number(f.plan.parent) == 2
    decoys = {p: p.read_bytes() for p in (padded_plan, padded_plan.with_suffix(".json"),
                                         padded_review, padded_output, broad)}
    backend = _backend(f)
    _run(f, backend)
    assert (f.prd / f"{f.plan.stem}-plan-review-2.md").read_text() == "PLAN_REVIEW_PASS"
    assert backend.calls[0].session_id is None
    expected_output = f.output / f"{f.plan.stem}{f.mode.verify.output_suffix.format(n=1)}"
    assert expected_output.exists()
    if test_mode:
        assert expected_output.read_text().rstrip().endswith("TEST_PASS")
        assert [call.role for call in backend.calls] == [AgentRole.PLAN_REVIEWER, AgentRole.IMPLEMENTER]
    else:
        assert expected_output.read_text() == "REVIEW_PASS"
        assert backend.calls[-1].role is AgentRole.CODE_REVIEWER
        assert backend.calls[-1].session_id is None
    assert _read_sessions(f.plan)["step"] == "done"
    implementer_answers = _artifact_dir(f.project, f.mode, "reviews")
    assert (implementer_answers / f"{f.plan.stem}-implementer-1.md").read_text() == "fake output"
    assert {p: p.read_bytes() for p in decoys} == decoys
    assert not f.plan.with_name(f"02-{f.task.slug}.json").exists()
    main_roadmap = "roadmaps/developer.md" if named else "ROADMAP.md"
    assert _artifact_subdir(main_roadmap) == _artifact_subdir(_tests_sibling(main_roadmap)) == f.mode.artifact_subdir


def test_closed_unpadded_history_does_not_collide_with_padded_unfinished_flow(flow):
    f = flow(prefix="01")
    _seed(f, {"step": "planned:1"})
    closed = f.plan.with_name(f"1-{f.task.slug}.json")
    closed.write_text('{"runtime":"codex", "step":"done"}')
    original = closed.read_bytes()
    backend = _backend(f)
    _run(f, backend)
    assert (f.prd / f"01-{f.task.slug}-plan-review-1.md").read_text() == "PLAN_REVIEW_PASS"
    assert closed.read_bytes() == original
    assert _read_sessions(f.plan)["step"] == "done"


@pytest.mark.parametrize("json_only", [False, True])
def test_unfinished_numeric_collision_stops_flow_without_modifying_evidence(flow, json_only):
    f = flow()
    _seed(f, {"runtime": "claude", "step": "planning:1"}, markdown=not json_only)
    duplicate = f.plan.with_name(f"01-{f.task.slug}.md")
    duplicate.write_text("# Another unfinished group")
    backend = _backend(f)
    before = _snapshot(f)
    with pytest.raises(HaltError, match="ambiguous"):
        _run(f, backend)
    assert backend.calls == []
    assert _snapshot(f) == before


@pytest.mark.parametrize("revision", [False, True])
def test_planner_exception_does_not_close_the_task(flow, monkeypatch, revision):
    f = flow(prefix="01")
    if revision:
        _seed(f, {"runtime": "claude", "step": "planned:1", "planner": "saved"})

    def fail(call):
        if call.role is AgentRole.PLAN_REVIEWER:
            return "Findings"
        raise RuntimeError("planner interrupted")

    backend = FakeAgentBackend(on_call=fail)
    with pytest.raises(RuntimeError, match="planner interrupted"):
        _run(f, backend)
    assert _read_sessions(f.plan).get("step") != "done"
    assert _read_sessions(f.plan)["step"] == f"planning:{2 if revision else 1}"
    assert not parse_roadmap(f.roadmap).tasks[0].done


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("named,test_mode", [(False, False), (True, True)])
def test_every_planning_call_records_current_identity_and_success_advances_before_review(flow, replacements, runtime, named, test_mode):
    f = flow(runtime=runtime, named=named, test_mode=test_mode, prefix="01")
    delegate = _backend(f).on_call
    observed = []
    planning_calls = 0
    review_calls = 0

    def inspect(call):
        nonlocal planning_calls, review_calls
        if call.role is AgentRole.PLANNER:
            planning_calls += 1
            expected_id = " phase identity " if planning_calls == 1 else "planner-sid-1"
            assert call.session_id == expected_id
            data = _read_sessions(f.plan)
            assert data["runtime"] == role_runtimes(runtime)
            assert data["step"] == f"planning:{planning_calls}"
            assert data["planner"] == expected_id
            _assert_planner_assignment(call, f)
            assert ("## Plan review" in call.prompt) is (planning_calls == 2)
            observed.append(data.copy())
        elif call.role is AgentRole.PLAN_REVIEWER:
            review_calls += 1
            data = _read_sessions(f.plan)
            assert data["step"] == f"planned:{review_calls}"
            assert data["planner"] == f"planner-sid-{1 if review_calls == 1 else 3}"
            assert call.session_id is None
            if review_calls == 1:
                return "Revise the actual plan"
        return delegate(call)

    backend = FakeAgentBackend(on_call=inspect)
    _run(f, backend, phase=" phase identity ")
    assert planning_calls == review_calls == 2
    assert observed[1]["plan-reviewer-1"] == "plan_reviewer-sid-2"
    assert _read_sessions(f.plan)["step"] == "done"
    pending = [record for _, record in replacements if record["step"].startswith("planning:")]
    assert pending[0] == {"runtime": role_runtimes(runtime), "step": "planning:1", "planner": " phase identity "}
    assert any(record["step"] == "planning:2" and record["planner"] == "planner-sid-1" for record in pending)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("named,test_mode", [(False, False), (True, True)])
@pytest.mark.parametrize("start,phase", [("fresh", None), ("fresh", " phase only on first invocation "),
                                        ("resumed", "competing phase")])
@pytest.mark.parametrize("plausible", [False, True])
def test_failed_planning_restarts_exact_group_round_and_known_incoming_identity(
    flow, monkeypatch, replacements, runtime, named, test_mode, start, phase, plausible,
):
    f = flow(runtime=runtime, named=named, test_mode=test_mode, prefix="1" if start == "resumed" else "01")
    data = {}
    round_number = 1
    incoming = phase
    if start == "resumed":
        round_number = 2
        incoming = " saved task identity "
        data = {"runtime": runtime, "step": "plan_review_failed:1", "planner": incoming,
                "implementer": "saved impl", "plan-reviewer-1": "old audit", "elapsed": "017",
                "optional": {"nested": [None, {"number": 3}]}}
        _seed(f, data)
        _review(f, passed=False)
    effects = []
    real_mark_done = pipeline.mark_done
    def observe_done(*args):
        effects.append("done")
        real_mark_done(*args)
    monkeypatch.setattr(pipeline, "mark_done", observe_done)
    monkeypatch.setattr(pipeline, "_git_commit", lambda *args: effects.append("commit"))
    seen = []

    def fail(call):
        _assert_planner_assignment(call, f)
        assert call.session_id == incoming
        disk = _read_sessions(f.plan)
        expected = {**data, "runtime": role_runtimes(runtime), "step": f"planning:{round_number}"}
        if incoming is not None:
            expected["planner"] = incoming
        assert disk == expected
        seen.append(disk)
        assert ("## Plan review" in call.prompt) is (start == "resumed")
        if plausible:
            f.plan.write_text("# A plausible plan produced before the invocation fails\n")
        raise RuntimeError("planning did not return")

    failed_backend = FakeAgentBackend(on_call=fail)
    with pytest.raises(RuntimeError, match="planning did not return"):
        _run(f, failed_backend, phase=phase)
    assert effects == []
    assert len(failed_backend.calls) == 1
    assert not parse_roadmap(f.roadmap).tasks[0].done
    assert _read_sessions(f.plan) == seen[0]
    if start == "fresh" and not plausible:
        assert not f.plan.exists()
    assert _next_number(f.plan.parent) == 2
    failed_bytes = f.plan.with_suffix(".json").read_bytes()
    replay = _backend(f)
    delegate = replay.on_call

    def inspect_replay(call):
        if len(replay.calls) == 1:
            _assert_planner_assignment(call, f)
            assert call.session_id == incoming
            assert _read_sessions(f.plan) == seen[0]
            assert ("## Plan review" in call.prompt) is (start == "resumed")
        elif call.role is AgentRole.PLAN_REVIEWER:
            assert call.session_id is None
            assert _read_sessions(f.plan)["step"] == f"planned:{round_number}"
            assert _read_sessions(f.plan)["planner"] == "planner-sid-1"
        return delegate(call)

    replay.on_call = inspect_replay
    _run(f, replay)
    assert (f.prd / f"{f.plan.stem}-plan-review-{round_number}.md").read_text() == "PLAN_REVIEW_PASS"
    assert effects == ["done", "commit"]
    assert parse_roadmap(f.roadmap).tasks[0].done
    assert f.plan.with_suffix(".json").read_bytes() != failed_bytes
    assert not f.plan.with_name(f"02-{f.task.slug}.json").exists()
    final = _read_sessions(f.plan)
    assert final["step"] == "done"
    assert final["planner"] == "planner-sid-1"
    assert ("code-reviewer" in final) is (not test_mode)
    if data:
        assert final["optional"] == data["optional"]
        assert final["plan-reviewer-1"] == data["plan-reviewer-1"]
        assert int(final["elapsed"]) >= 17
    assert replacements[0][1]["step"] == f"planning:{round_number}"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("named,test_mode", [(False, False), (True, True)])
@pytest.mark.parametrize("initial_round", [1, 2])
@pytest.mark.parametrize("plausible", [False, True])
def test_failed_inline_revision_restarts_pending_round_with_latest_successful_identity(
    flow, monkeypatch, replacements, runtime, named, test_mode, initial_round, plausible,
):
    f = flow(runtime=runtime, named=named, test_mode=test_mode, prefix="01" if initial_round == 1 else "1")
    data = {}
    if initial_round == 2:
        data = {"runtime": runtime, "step": "plan_review_failed:1", "planner": "saved before planning",
                "implementer": "saved impl", "plan-reviewer-1": "original audit", "elapsed": "013",
                "optional": {"nested": [None, {"retained": True}]}}
        _seed(f, data)
        _review(f, passed=False)
    pending_round = initial_round + 1
    previous = f.prd / f"{f.plan.stem}-plan-review-{initial_round}.md"
    future = f.prd / f"{f.plan.stem}-plan-review-{pending_round}.md"
    effects = []
    monkeypatch.setattr(pipeline, "_git_commit", lambda *args: effects.append("commit"))
    planning_count = 0
    delegate = _backend(f).on_call
    pending_records = []

    def fail_revision(call):
        nonlocal planning_count
        disk = _read_sessions(f.plan)
        if call.role is AgentRole.PLAN_REVIEWER:
            assert disk["step"] == f"planned:{initial_round}"
            assert disk["planner"] == "planner-sid-1"
            assert call.session_id is None
            return "Actual findings that require revision"
        _assert_planner_assignment(call, f)
        planning_count += 1
        if planning_count == 1:
            assert call.session_id == ("saved before planning" if data else "first phase identity")
            assert disk["step"] == f"planning:{initial_round}"
            return delegate(call)
        assert call.session_id == "planner-sid-1"
        assert disk["planner"] == "planner-sid-1"
        assert disk["step"] == f"planning:{pending_round}"
        assert disk["runtime"] == role_runtimes(runtime)
        assert disk[f"plan-reviewer-{initial_round}"] == "plan_reviewer-sid-2"
        assert f"## Plan review\n\n{previous.read_text()}" in call.prompt
        if data:
            for key in ("optional", "implementer", "plan-reviewer-1"):
                assert disk[key] == data[key]
            assert int(disk["elapsed"]) >= 13
        pending_records.append(disk)
        if plausible:
            f.plan.write_text("# Plausible revised plan\n\n## Test Command\n`true`\n")
            future.write_text("PLAN_REVIEW_PASS")
        raise RuntimeError("revision did not return")

    first_backend = FakeAgentBackend(on_call=fail_revision)
    with pytest.raises(RuntimeError, match="revision did not return"):
        _run(f, first_backend, phase="first phase identity")
    assert planning_count == 2
    assert len(first_backend.calls) == 3
    assert effects == []
    assert not parse_roadmap(f.roadmap).tasks[0].done
    assert _read_sessions(f.plan) == pending_records[0]
    assert previous.read_text() == "Actual findings that require revision"
    assert _next_number(f.plan.parent) == 2
    replay = _backend(f)
    replay_delegate = replay.on_call
    replay.sid = "new successful replay identity"

    def inspect_replay(call):
        if len(replay.calls) == 1:
            _assert_planner_assignment(call, f)
            assert call.session_id == "planner-sid-1"
            assert _read_sessions(f.plan) == pending_records[0]
            assert f"## Plan review\n\n{previous.read_text()}" in call.prompt
            assert future.exists() is plausible
        elif call.role is AgentRole.PLAN_REVIEWER:
            assert call.session_id is None
            assert _read_sessions(f.plan)["step"] == f"planned:{pending_round}"
            assert _read_sessions(f.plan)["planner"] == "new successful replay identity"
        return replay_delegate(call)

    replay.on_call = inspect_replay
    _run(f, replay, phase="competing restart phase")
    assert future.read_text() == "PLAN_REVIEW_PASS"
    assert effects == ["commit"]
    assert parse_roadmap(f.roadmap).tasks[0].done
    assert _read_sessions(f.plan)["step"] == "done"
    assert _read_sessions(f.plan)["planner"] == "new successful replay identity"
    assert previous.read_text() == "Actual findings that require revision"
    assert not f.plan.with_name(f"02-{f.task.slug}.json").exists()
    assert any(record["step"] == f"planning:{pending_round}" and record["planner"] == "planner-sid-1"
               for _, record in replacements)


@pytest.mark.parametrize("feedback", [False, True])
def test_planning_assignment_includes_supplied_roadmap_without_display_line(flow, feedback):
    f = flow()
    review = _review(f) if feedback else f.prd / "missing-review.md"
    backend = FakeAgentBackend()
    Planner(f.project, backend).plan(f.task.title, f.task.description, f.plan,
                                     plan_review_path=review, roadmap_path=f.roadmap)
    prompt = backend.calls[0].prompt
    assert f"Roadmap: {f.roadmap.relative_to(f.project).as_posix()}\n" in prompt
    assert f.task.title in prompt and f.task.description in prompt
    assert "Return the plan as your answer." in prompt
    assert str(f.plan) not in prompt
    assert ("## Plan review" in prompt) is feedback


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_tagged_completed_missing_review_can_infer_in_budget_dispatch(flow, runtime):
    f = flow(runtime=runtime)
    _seed(f, {"runtime": runtime, "step": "plan_review_failed:1", "planner": "saved"})
    backend = _backend(f)
    _run(f, backend)
    # The failed review artifact is absent, so the existing plan gets its first review.
    assert backend.calls[0].role is AgentRole.PLAN_REVIEWER
    assert backend.calls[0].session_id is None
    assert (f.prd / f"{f.plan.stem}-plan-review-1.md").read_text() == "PLAN_REVIEW_PASS"
    assert backend.calls[-1].role is AgentRole.CODE_REVIEWER
    assert backend.calls[-1].session_id is None
    assert _read_sessions(f.plan)["planner"] == "saved"


def test_legacy_inferred_over_budget_stops_before_backend_and_sidecar_creation(flow):
    f = flow()
    _seed(f)
    for number in (1, 2, 3):
        _review(f, number, passed=False)
    backend = _backend(f)
    before = _snapshot(f)
    with pytest.raises(HaltError, match="attempt budget"):
        _run(f, backend)
    assert backend.calls == []
    assert _snapshot(f) == before
    assert not f.plan.with_suffix(".json").exists()


@pytest.mark.parametrize("suffix", [".json", ".md"])
@pytest.mark.parametrize("test_mode", [False, True])
def test_flat_fresh_flow_ignores_suffix_bearing_named_segments(flow, suffix, test_mode):
    f = flow(test_mode=test_mode)
    named_relpath = f"roadmaps/99-{f.task.slug}{suffix}.md"
    named_roadmap = f.project / ".ai-factory" / named_relpath
    named_roadmap.parent.mkdir(parents=True, exist_ok=True)
    named_roadmap.write_text("# Independent roadmap\n")
    named_mode = IMPLEMENT_MODE._replace(roadmap_relpath=named_relpath,
                                         artifact_subdir=_artifact_subdir(named_relpath))
    preserved = {}
    for dirname in ("plans", "plan-reviews", "reviews", "test-runs"):
        segment = _artifact_dir(f.project, named_mode, dirname)
        segment.mkdir(parents=True)
        assert segment.name == f"99-{f.task.slug}{suffix}"
        evidence = segment / "01-independent.json"
        evidence.write_text('{"step":"planning:2", "planner":"other-runtime-identity"}')
        preserved[evidence] = evidence.read_bytes()
    preserved[named_roadmap] = named_roadmap.read_bytes()
    assert _next_number(f.plan.parent) == 1
    backend = _backend(f)
    _run(f, backend)
    fresh = f.plan.with_name(f"01-{f.task.slug}.md")
    assert fresh.exists()
    assert backend.calls[0].session_id is None
    assert _read_sessions(fresh)["step"] == "done"
    assert {path: path.read_bytes() for path in preserved} == preserved
    assert not f.plan.with_name(f"100-{f.task.slug}.json").exists()


def test_real_selected_markdown_with_directory_sidecar_still_halts_and_preserves_evidence(flow):
    f = flow()
    _seed(f)
    sidecar = f.plan.with_suffix(".json")
    sidecar.mkdir()
    (sidecar / "evidence.txt").write_text("unreadable sidecar evidence")
    before = _snapshot(f)
    backend = _backend(f)
    with pytest.raises(HaltError, match="Sidecar could not be read"):
        _run(f, backend)
    assert backend.calls == []
    assert _snapshot(f) == before


def test_plan_review_budget_zero_flows_planner_to_implementer_without_review(flow):
    """With the plan-review budget at 0 the flow implements the plan with no review round and no plan-review file."""
    f = flow(prefix="01")
    f.config.plan_review_iterations = 0
    backend = _backend(f)

    _run(f, backend)

    assert [call.role for call in backend.calls] == [
        AgentRole.PLANNER, AgentRole.IMPLEMENTER, AgentRole.CODE_REVIEWER,
    ]
    assert list(f.prd.glob("*-plan-review-*.md")) == []
    assert _read_sessions(f.plan)["step"] == "done"


_HEAD_ENTRY = {"runtime": "head", "head": {
    "repository": "/repos/the-project", "owner": "max", "folder": "r-d",
}}


def test_a_head_entry_in_the_owner_record_is_accepted(flow):
    f = flow()
    f.config.roles = {
        "planner": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"])},
        "plan_reviewer": {"runtime": "claude"},
        "implementer": {"runtime": "claude"},
        "code_reviewer": {"runtime": "claude"},
    }
    from orchestrator.config import role_runtimes
    _seed(f, {"step": "planned:1", "planner": "saved",
              "runtime": role_runtimes(f.config.runtime, f.config.roles)})
    _review(f)

    _run(f, _backend(f))

    assert f.plan.with_suffix(".json").exists()  # no halt: the record's head entry reads


def test_a_resume_whose_role_s_head_address_differs_halts_naming_the_role(flow):
    f = flow()
    f.config.roles = {
        "planner": {"runtime": "head", "head": dict(_HEAD_ENTRY["head"])},
        "plan_reviewer": {"runtime": "claude"},
        "implementer": {"runtime": "claude"},
        "code_reviewer": {"runtime": "claude"},
    }
    recorded = dict(_HEAD_ENTRY)
    recorded["head"] = {"repository": "/repos/another", "owner": "max", "folder": "r-d"}
    _seed(f, {"step": "planned:1", "planner": "saved", "runtime": {"planner": recorded}})
    _review(f)
    before = _snapshot(f)

    with pytest.raises(HaltError, match="runtime does not match") as caught:
        _run(f, _backend(f))

    assert "planner" in str(caught.value)
    assert _snapshot(f) == before


# ---------------------------------------------------------------------------
# Planning off: the spec is the plan, the record passes planning:1 -> planned:1
# ---------------------------------------------------------------------------

_PLANNING_OFF_SPEC = "# The task's spec\n\nBuild it — über plain, trailing newline\n"
_PLANNING_OFF_ROLES = (AgentRole.IMPLEMENTER, AgentRole.CODE_REVIEWER)


def _planning_off_flow(flow, *, description=None, spec_text=_PLANNING_OFF_SPEC):
    """A flow whose mode and configuration are wired as the implement loop wires
    them under planning false, over a task whose description carries the tag."""
    f = flow(prefix="01")
    if spec_text is not None:
        spec = f.project / ".ai-factory" / "specs" / "01-thing.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(spec_text, encoding="utf-8")
    if description is None:
        description = "Do the work. Spec: `.ai-factory/specs/01-thing.md`."
    f.roadmap.write_text(f"# Work\n\n- [ ] **Runtime task** — {description}\n")
    task = parse_roadmap(f.roadmap).tasks[0]
    mode = f.mode._replace(
        make_planner=lambda project_dir, backend, prompt_name: SpecLayer(project_dir),
        roles=_PLANNING_OFF_ROLES,
    )
    config = dataclasses.replace(f.config, planning=False, plan_review_iterations=0)
    return SimpleNamespace(project=f.project, task=task, mode=mode, roadmap=f.roadmap,
                           plan=f.plan, prd=f.prd, output=f.output, config=config)


def test_a_fresh_task_under_planning_off_lays_the_spec_and_calls_two_roles(flow):
    """planning:1 and planned:1 are recorded, the spec is laid at the plan's path, no
    planner and no plan reviewer is called, no plan-review file exists, implementation
    starts at iteration 1 and no phase identity carries."""
    f = _planning_off_flow(flow)
    backend = _backend(f)
    records = []
    real_replace = os.replace

    def inspect(src, dst):
        records.append(json.loads(Path(src).read_text()))
        real_replace(src, dst)

    f.plan.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch_replace = pytest.MonkeyPatch()
    monkeypatch_replace.setattr("orchestrator.protocol.os.replace", inspect)
    try:
        result = _run(f, backend)
    finally:
        monkeypatch_replace.undo()

    assert result is None
    assert [call.role for call in backend.calls] == [AgentRole.IMPLEMENTER, AgentRole.CODE_REVIEWER]
    assert f.plan.read_bytes() == f"# Plan: Runtime task\n\n{_PLANNING_OFF_SPEC}".encode("utf-8")
    assert (f.output / f"{f.plan.stem}-review-1.md").exists()  # the reviewer's artifact
    assert list(f.prd.iterdir()) == []  # no plan-review file is written
    steps = [record["step"] for record in records if "step" in record]
    assert steps[:2] == ["planning:1", "planned:1"]
    final = _read_sessions(f.plan)
    assert final["step"] == "done"
    assert final["runtime"] == role_runtimes("claude", {}, _PLANNING_OFF_ROLES)
    assert "planner" not in final  # the laying holds no session


def test_a_crash_between_the_laying_and_the_record_repeats_the_laying(flow):
    """A record still at planning:1 lays the spec again, to the same bytes."""
    f = _planning_off_flow(flow)
    f.plan.parent.mkdir(parents=True, exist_ok=True)
    f.plan.write_text("stale bytes from the interrupted round")
    f.plan.with_suffix(".json").write_text(json.dumps({
        "runtime": role_runtimes("claude", {}, _PLANNING_OFF_ROLES),
        "step": "planning:1",
    }))
    backend = _backend(f)

    _run(f, backend)

    assert f.plan.read_bytes() == f"# Plan: Runtime task\n\n{_PLANNING_OFF_SPEC}".encode("utf-8")
    assert [call.role for call in backend.calls] == [AgentRole.IMPLEMENTER, AgentRole.CODE_REVIEWER]
    assert _read_sessions(f.plan)["step"] == "done"


def test_a_record_at_planned_one_resumes_into_implementation(flow):
    """planned:1 resumes into implementation at iteration 1; a record past its
    planning keeps its plan."""
    f = _planning_off_flow(flow)
    f.plan.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "# Plan: Runtime task\n\nthe spec as it was laid\n"
    f.plan.write_text(sentinel)
    f.plan.with_suffix(".json").write_text(json.dumps({
        "runtime": role_runtimes("claude", {}, _PLANNING_OFF_ROLES),
        "step": "planned:1",
    }))
    backend = _backend(f)

    _run(f, backend)

    assert f.plan.read_text() == sentinel
    assert [call.role for call in backend.calls] == [AgentRole.IMPLEMENTER, AgentRole.CODE_REVIEWER]
    assert "the spec as it was laid" in backend.calls[0].prompt  # implementation, iteration 1
    assert _read_sessions(f.plan)["step"] == "done"


def test_a_task_with_no_tag_halts_before_any_role_call(flow):
    """A description without the tag halts the run with the record at planning:1."""
    f = _planning_off_flow(flow, description="Do the work with no tag at all.", spec_text=None)
    backend = _backend(f)

    with pytest.raises(HaltError) as caught:
        _run(f, backend)

    first_line = str(caught.value).splitlines()[0]
    assert "Runtime task" in first_line
    assert "no spec" in first_line
    assert backend.calls == []
    assert _read_sessions(f.plan)["step"] == "planning:1"


@pytest.mark.parametrize("step,second_line", [
    ("planning:2", "planning:2 attempt 2 exceeds planning (false)"),
    ("planned:2", "planned:2 attempt 2 exceeds planning (false)"),
    ("plan_review_failed:1", "plan attempt 2 exceeds planning (false)"),
    ("plan_review_failed:2", "plan_review_failed:2 attempt 2 exceeds planning (false)"),
])
def test_a_planning_off_record_past_the_first_round_halts_naming_planning(flow, step, second_line):
    """No round past the first can continue under planning off: the halt names the
    planning setting, says how to continue, calls no role and writes nothing."""
    f = _planning_off_flow(flow)
    f.plan.parent.mkdir(parents=True, exist_ok=True)
    f.plan.write_text("# Plan: Runtime task\n\nwhatever the round left\n")
    f.plan.with_suffix(".json").write_text(json.dumps({
        "runtime": role_runtimes("claude", {}, _PLANNING_OFF_ROLES),
        "step": step,
    }))
    backend = _backend(f)
    before = _snapshot(f)

    with pytest.raises(HaltError) as caught:
        _run(f, backend)

    assert str(caught.value) == (
        "Resume exceeds the attempt budget\n"
        f"{second_line}\n"
        "Roll the record back to planning:1, which lays the spec, "
        "or set planning to true in the run settings file to continue."
    )
    assert backend.calls == []
    assert _snapshot(f) == before
