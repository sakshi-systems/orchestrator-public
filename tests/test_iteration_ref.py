"""The iteration's state as a commit on the run's ref, against real git."""

import json
from pathlib import Path

import pytest

from orchestrator import git_runner
from orchestrator import pipeline as pipeline_module
from orchestrator.config import OrchestratorConfig
from orchestrator.errors import IterationCommitNotMadeError, IterationRefNotPushedError, PipelineStopError
from orchestrator.iteration_ref import (
    make_iteration_commit,
    push_iteration_ref,
    ref_of,
    remove_iteration_refs,
)
from orchestrator.pipeline import IMPLEMENT_MODE
from orchestrator.ports import IterationState
from tests.conftest import git_in
from tests.fakes import FakeAgentBackend
from orchestrator.implementer_result import WorkingTreeResult


def _repo(tmp_path):
    """A project repository at a base commit: tracked work, a tracked file that
    .gitignore matches, and a plan under .ai-factory."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "tracked.txt").write_text("base\n")
    (project / "ignored-tracked.txt").write_text("tracked though ignored\n")
    (project / ".gitignore").write_text("*.log\nignored-tracked.txt\n")
    factory = project / ".ai-factory"
    (factory / "plans").mkdir(parents=True)
    (factory / "plans" / "01-slug.md").write_text("# Plan\n")
    git_in(project, "init", "-q", "-b", "main", ".")
    git_in(project, "config", "user.email", "test@example.com")
    git_in(project, "config", "user.name", "Test")
    git_in(project, "add", "tracked.txt", ".gitignore", ".ai-factory/")
    git_in(project, "add", "-f", "ignored-tracked.txt")  # tracked from the start, though ignored after
    git_in(project, "commit", "-q", "-m", "the base")
    base = git_in(project, "rev-parse", "HEAD").strip()
    origin = project.parent / f"{project.name}-origin.git"
    git_in(project.parent, "init", "--bare", "-b", "main", str(origin))
    git_in(project, "remote", "add", "origin", str(origin))
    git_in(project, "push", "-q", "origin", "HEAD:refs/heads/main")
    return project, base, origin


def _paths_of(project, commit):
    tree = git_in(project, "rev-parse", f"{commit}^{{tree}}").strip()
    listed = git_in(project, "ls-tree", "-r", "--name-only", "-z", tree)
    return {path for path in listed.split("\0") if path}


def _parents_of(project, commit):
    return git_in(project, "rev-list", "--parents", "-n", "1", commit).split()


def _remote_refs(project, origin, *patterns):
    listed = git_in(project, "ls-remote", str(origin), *patterns) if patterns else git_in(project, "ls-remote", str(origin))
    return {line.split()[1] for line in listed.splitlines() if line.strip()}


def test_the_commit_s_parent_is_the_base_and_its_tree_holds_what_the_add_stages(tmp_path):
    project, base, origin = _repo(tmp_path)
    git_in(project, "rm", "-q", "tracked.txt")  # a deletion the add stages
    (project / "new.txt").write_text("an untracked file until the add\n")
    (project / "scratch.log").write_text("untracked and ignored, never staged\n")
    (project / ".ai-factory" / "plans" / "01-slug.json").write_text("{}")
    git_in(project, "add", "-A")

    state = make_iteration_commit(project, "01-slug", 1)

    assert state.base == base
    assert state.ref == "refs/orchestrator/01-slug"
    assert state.commit
    assert _parents_of(project, state.commit) == [state.commit, base]
    paths = _paths_of(project, state.commit)
    assert "new.txt" in paths  # the untracked file the add staged
    assert "ignored-tracked.txt" in paths  # tracked, though .gitignore matches it
    assert ".ai-factory/plans/01-slug.md" in paths  # the run's artifacts are held beside the code
    assert ".ai-factory/plans/01-slug.json" in paths
    assert "tracked.txt" not in paths  # the deletion the add staged
    assert "scratch.log" not in paths  # untracked and ignored, never staged
    assert git_in(project, "rev-parse", "HEAD").strip() == base  # HEAD does not move


def test_a_second_iteration_moves_the_ref_to_a_commit_whose_parent_is_still_the_base(tmp_path):
    project, base, origin = _repo(tmp_path)
    (project / "first.txt").write_text("one\n")
    git_in(project, "add", "-A")
    first = make_iteration_commit(project, "01-slug", 1)

    (project / "second.txt").write_text("two\n")
    git_in(project, "add", "-A")
    second = make_iteration_commit(project, "01-slug", 2)

    assert second.commit != first.commit
    assert _parents_of(project, second.commit) == [second.commit, base]
    assert git_in(project, "rev-parse", "refs/orchestrator/01-slug").strip() == second.commit
    paths = _paths_of(project, second.commit)
    assert "first.txt" in paths and "second.txt" in paths  # each holds the whole state since the base


def test_a_held_lock_on_the_ref_halts_the_call(tmp_path):
    project, _, origin = _repo(tmp_path)
    lock = project / ".git" / "refs" / "orchestrator" / "01-slug.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")

    with pytest.raises(IterationCommitNotMadeError) as caught:
        make_iteration_commit(project, "01-slug", 1)

    lines = str(caught.value).splitlines()
    assert lines[0] == "The iteration's commit could not be made"
    assert lines[1] == "refs/orchestrator/01-slug"
    assert lines[2]


class _TaskStub:
    slug = "slug"
    title = "Some task"
    description = "Some description"
    line_number = 0


class _RecordingVerifier:
    """Records verify calls and passes."""

    def __init__(self):
        self.calls = []

    def verify(self, plan_path, out_path, prev_out_path, implementer_answer_path, iteration_state):
        self.calls.append(("verify", plan_path, out_path, prev_out_path, implementer_answer_path, iteration_state))
        return True


def _flow_at_implemented(tmp_path, monkeypatch):
    """A project resumed at implemented:1, with the flow's side effects neutralized."""
    project, base, origin = _repo(tmp_path)
    monkeypatch.setattr(pipeline_module, "mark_done", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_module, "_git_commit", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_module, "report", lambda *a, **k: None)
    plan = project / ".ai-factory" / "plans" / "01-slug.md"
    plan.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": "implemented:1"}))
    (project / ".ai-factory" / "plan-reviews").mkdir()
    (project / ".ai-factory" / "plan-reviews" / "01-slug-plan-review-1.md").write_text("PLAN_REVIEW_PASS")
    (project / ".ai-factory" / "reviews").mkdir()
    config = OrchestratorConfig(max_iterations=3, usage_threshold_5h=90, usage_threshold_weekly=95, enable_phase_sessions=False)
    return project, plan, config, base


def test_a_halt_of_the_commit_leaves_implemented_recorded_and_calls_no_verify(tmp_path, monkeypatch):
    project, plan, config, _ = _flow_at_implemented(tmp_path, monkeypatch)
    verifier = _RecordingVerifier()
    mode = IMPLEMENT_MODE._replace(verify=IMPLEMENT_MODE.verify._replace(make_verifier=lambda reviewer, project_dir: verifier))
    lock = project / ".git" / "refs" / "orchestrator" / "01-slug.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")

    with pytest.raises(IterationCommitNotMadeError):
        pipeline_module.process_task(
            project, _TaskStub(), 1, config, mode,
            backend=FakeAgentBackend(), implementer_result=WorkingTreeResult(),
        )

    assert json.loads(plan.with_suffix(".json").read_text())["step"] == "implemented:1"
    assert verifier.calls == []


def test_a_resume_at_implemented_makes_the_commit_and_passes_the_state_to_verify(tmp_path, monkeypatch):
    project, plan, config, base = _flow_at_implemented(tmp_path, monkeypatch)
    verifier = _RecordingVerifier()
    mode = IMPLEMENT_MODE._replace(verify=IMPLEMENT_MODE.verify._replace(make_verifier=lambda reviewer, project_dir: verifier))

    pipeline_module.process_task(
        project, _TaskStub(), 1, config, mode,
        backend=FakeAgentBackend(), implementer_result=WorkingTreeResult(),
    )

    (kind, *call) = verifier.calls[-1]
    assert kind == "verify"
    state = call[-1]
    assert isinstance(state, IterationState)
    assert state.base == base
    assert state.ref == "refs/orchestrator/01-slug"
    assert _parents_of(project, state.commit) == [state.commit, base]
    # The task ended, so the ref is gone from the clone and from origin.
    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator/")
    assert listed.split() == []
    assert _remote_refs(project, _origin_of(project), "refs/orchestrator/*", "refs/heads/orchestrator/*") == set()


# ---------------------------------------------------------------------------
# The ref is pushed before the verify, and removed when the task ends
# ---------------------------------------------------------------------------


class _SilentOrigin:
    """A local endpoint that accepts connections and never answers, as an
    origin that hangs every git call sent to it."""

    def __init__(self):
        import socket
        import threading

        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(5)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return

    def close(self):
        self.socket.close()


def test_the_push_puts_the_ref_on_origin_and_a_second_iteration_moves_it(tmp_path):
    project, base, origin = _repo(tmp_path)
    (project / "first.txt").write_text("one\n")
    git_in(project, "add", "-A")
    first = make_iteration_commit(project, "01-slug", 1)
    push_iteration_ref(project, first)

    assert _remote_refs(project, origin, "refs/orchestrator/*") == {first.ref}

    (project / "second.txt").write_text("two\n")
    git_in(project, "add", "-A")
    second = make_iteration_commit(project, "01-slug", 2)
    push_iteration_ref(project, second)

    assert _remote_refs(project, origin, "refs/orchestrator/*") == {second.ref}
    assert _parents_of(project, second.commit) == [second.commit, base]  # a sibling of the first, not a child


def test_a_repository_with_no_origin_halts_the_push(tmp_path):
    project, base, origin = _repo(tmp_path)
    git_in(project, "remote", "remove", "origin")
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, "01-slug", 1)

    with pytest.raises(IterationRefNotPushedError) as caught:
        push_iteration_ref(project, state)

    lines = str(caught.value).splitlines()
    assert lines[0] == "The iteration's ref could not be pushed"
    assert lines[1] == state.ref
    assert lines[2]


def test_a_push_halting_in_the_flow_leaves_implemented_recorded_and_runs_no_verify(tmp_path, monkeypatch):
    project, plan, config, base = _flow_at_implemented(tmp_path, monkeypatch)
    git_in(project, "remote", "remove", "origin")
    verifier = _RecordingVerifier()
    mode = IMPLEMENT_MODE._replace(verify=IMPLEMENT_MODE.verify._replace(make_verifier=lambda reviewer, project_dir: verifier))

    with pytest.raises(IterationRefNotPushedError):
        pipeline_module.process_task(
            project, _TaskStub(), 1, config, mode,
            backend=FakeAgentBackend(), implementer_result=WorkingTreeResult(),
        )

    assert json.loads(plan.with_suffix(".json").read_text())["step"] == "implemented:1"
    assert verifier.calls == []


def test_a_push_against_an_origin_that_never_answers_halts_with_the_bound_s_text(tmp_path, monkeypatch):
    project, base, origin = _repo(tmp_path)
    silent = _SilentOrigin()
    try:
        monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
        git_in(project, "remote", "set-url", "origin", f"http://127.0.0.1:{silent.port}/origin.git")
        (project / "work.txt").write_text("work\n")
        git_in(project, "add", "-A")
        state = make_iteration_commit(project, "01-slug", 1)

        with pytest.raises(IterationRefNotPushedError) as caught:
            push_iteration_ref(project, state)

        lines = str(caught.value).splitlines()
        assert lines[0] == "The iteration's ref could not be pushed"
        assert lines[1] == state.ref
        assert lines[2].startswith("timed out after")
    finally:
        silent.close()


def test_the_removal_takes_the_local_ref_the_remote_ref_and_the_task_s_branch(tmp_path, capsys):
    project, base, origin = _repo(tmp_path)
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, "01-slug", 1)
    push_iteration_ref(project, state)
    git_in(project, "push", "-q", "origin", f"{base}:refs/heads/orchestrator/01-slug")  # a Hermes task's branch
    assert _remote_refs(project, origin, "refs/orchestrator/*", "refs/heads/orchestrator/*") == {
        state.ref, "refs/heads/orchestrator/01-slug",
    }

    remove_iteration_refs(project, project / ".ai-factory" / "plans" / "01-slug.md")

    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator/")
    assert listed.split() == []
    assert _remote_refs(project, origin, "refs/orchestrator/*", "refs/heads/orchestrator/*") == set()
    assert capsys.readouterr().out == ""  # nothing failed, no line


def test_a_removal_with_no_branch_on_origin_removes_only_the_refs_silently(tmp_path, capsys):
    project, base, origin = _repo(tmp_path)
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, "01-slug", 1)
    push_iteration_ref(project, state)

    remove_iteration_refs(project, project / ".ai-factory" / "plans" / "01-slug.md")

    assert _remote_refs(project, origin, "refs/orchestrator/*", "refs/heads/orchestrator/*") == set()
    assert capsys.readouterr().out == ""


def test_a_refusing_origin_prints_the_one_line_removes_the_local_ref_and_returns(tmp_path, capsys):
    project, base, origin = _repo(tmp_path)
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, "01-slug", 1)
    push_iteration_ref(project, state)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        '  if [ "$new" = "0000000000000000000000000000000000000000" ]; then\n'
        "    exit 1\n"
        "  fi\n"
        "done\n"
        "exit 0\n"
    )
    hook.chmod(0o755)

    remove_iteration_refs(project, project / ".ai-factory" / "plans" / "01-slug.md")

    out = capsys.readouterr().out
    assert out == f">>> WARNING: the iteration's ref {state.ref} could not be removed, continuing anyway.\n"
    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator/")
    assert listed.split() == []  # the local ref went anyway


def test_an_origin_that_never_answers_prints_the_one_line(tmp_path, monkeypatch, capsys):
    project, base, origin = _repo(tmp_path)
    silent = _SilentOrigin()
    try:
        monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
        git_in(project, "remote", "set-url", "origin", f"http://127.0.0.1:{silent.port}/origin.git")
        (project / "work.txt").write_text("work\n")
        git_in(project, "add", "-A")
        state = make_iteration_commit(project, "01-slug", 1)

        remove_iteration_refs(project, project / ".ai-factory" / "plans" / "01-slug.md")

        out = capsys.readouterr().out
        assert out == f">>> WARNING: the iteration's ref {state.ref} could not be removed, continuing anyway.\n"
        listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator/")
        assert listed.split() == []  # the local ref went anyway
    finally:
        silent.close()


def test_a_local_removal_that_times_out_is_the_one_line_and_the_remote_still_runs(tmp_path, monkeypatch, capsys):
    """The local update-ref is held by a reference-transaction hook that never
    returns, so the call times out; the remote calls run against the bare
    origin, a repository of its own, and take the ref and the branch off it."""
    project, base, origin = _repo(tmp_path)
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, "01-slug", 1)
    push_iteration_ref(project, state)
    git_in(project, "push", "-q", "origin", f"{base}:refs/heads/orchestrator/01-slug")  # a Hermes task's branch
    assert _remote_refs(project, origin, "refs/orchestrator/*", "refs/heads/orchestrator/*") == {
        state.ref, "refs/heads/orchestrator/01-slug",
    }
    monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
    hook = project / ".git" / "hooks" / "reference-transaction"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do :; done\nsleep 30\n")
    hook.chmod(0o755)

    remove_iteration_refs(project, project / ".ai-factory" / "plans" / "01-slug.md")

    out = capsys.readouterr().out
    assert out == f">>> WARNING: the iteration's ref {state.ref} could not be removed, continuing anyway.\n"
    assert _remote_refs(project, origin, "refs/orchestrator/*", "refs/heads/orchestrator/*") == set()


def test_a_task_that_stops_before_it_ends_leaves_its_refs(tmp_path, monkeypatch):
    project, plan, config, base = _flow_at_implemented(tmp_path, monkeypatch)

    class _FailingVerifier:
        def verify(self, plan_path, out_path, prev_out_path, implementer_answer_path, iteration_state):
            out_path.write_text("The work failed the review.")
            return False

    mode = IMPLEMENT_MODE._replace(
        verify=IMPLEMENT_MODE.verify._replace(make_verifier=lambda reviewer, project_dir: _FailingVerifier()),
    )
    config = OrchestratorConfig(max_iterations=1, usage_threshold_5h=90, usage_threshold_weekly=95, enable_phase_sessions=False)

    with pytest.raises(PipelineStopError):
        pipeline_module.process_task(
            project, _TaskStub(), 1, config, mode,
            backend=FakeAgentBackend(), implementer_result=WorkingTreeResult(),
        )

    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator/")
    assert listed.split() == ["refs/orchestrator/01-slug"]  # the local ref stays
    assert _remote_refs(project, _origin_of(project), "refs/orchestrator/*") == {"refs/orchestrator/01-slug"}


def _origin_of(project):
    from pathlib import Path as _Path

    url = git_in(project, "remote", "get-url", "origin").strip()
    return _Path(url)
