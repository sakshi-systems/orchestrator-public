"""The iteration's state as a commit on the run's ref, against real git."""

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from orchestrator import git_runner
from orchestrator import layout as layout_module
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
from orchestrator.implementer_result import WorkingTreeResult, ref_name_of


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
    """Origin's ref names, read by git's delimiters: a newline between
    records and a tab before the name, so a name holding a character Python
    treats as a line break or a space is read whole."""
    listed = git_in(project, "ls-remote", str(origin), *patterns) if patterns else git_in(project, "ls-remote", str(origin))
    names = set()
    for record in listed.split("\n"):
        if not record:
            continue
        _sha, tab, name = record.partition("\t")
        if tab:
            names.add(name)
    return names


def test_the_commit_s_parent_is_the_base_and_its_tree_holds_what_the_add_stages(tmp_path):
    project, base, origin = _repo(tmp_path)
    git_in(project, "rm", "-q", "tracked.txt")  # a deletion the add stages
    (project / "new.txt").write_text("an untracked file until the add\n")
    (project / "scratch.log").write_text("untracked and ignored, never staged\n")
    (project / ".ai-factory" / "plans" / "01-slug.json").write_text("{}")
    git_in(project, "add", "-A")

    state = make_iteration_commit(project, "01-slug", 1)

    assert state.base == base
    assert state.ref == "refs/orchestrator-iteration/01-slug"
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
    assert git_in(project, "rev-parse", "refs/orchestrator-iteration/01-slug").strip() == second.commit
    paths = _paths_of(project, second.commit)
    assert "first.txt" in paths and "second.txt" in paths  # each holds the whole state since the base


def test_a_named_roadmap_s_plan_carries_its_segment_in_the_ref(tmp_path):
    project, base, origin = _repo(tmp_path)
    (project / ".ai-factory" / "plans" / "john-doe").mkdir()
    plan = project / ".ai-factory" / "plans" / "john-doe" / "01-slug.md"
    plan.write_text("# Plan\n")
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")

    state = make_iteration_commit(project, ref_name_of(project, plan), 1)

    assert state.ref == "refs/orchestrator-iteration/john-doe/01-slug"
    assert git_in(project, "rev-parse", state.ref).strip() == state.commit


def test_the_branch_s_short_name_reaches_the_branch_and_the_iteration_s_the_iteration(tmp_path):
    """The two refs share no short name: with the iteration ref and the
    implementer's branch on different commits, each short name resolves to
    its own ref, so a command that names the branch by its short name gets
    the branch's commit, never the run's."""
    project, base, origin = _repo(tmp_path)
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, "01-slug", 1)
    push_iteration_ref(project, state)
    git_in(project, "push", "-q", "origin", f"{base}:refs/heads/orchestrator/01-slug")  # a Hermes task's branch
    git_in(project, "update-ref", "refs/heads/orchestrator/01-slug", base)  # its ref in the run's clone, on another commit
    assert state.commit != base

    assert git_in(project, "rev-parse", "orchestrator/01-slug").strip() == base
    assert git_in(project, "rev-parse", "orchestrator-iteration/01-slug").strip() == state.commit


def test_a_held_lock_on_the_ref_halts_the_call(tmp_path):
    project, _, origin = _repo(tmp_path)
    lock = project / ".git" / "refs" / "orchestrator-iteration" / "01-slug.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")

    with pytest.raises(IterationCommitNotMadeError) as caught:
        make_iteration_commit(project, "01-slug", 1)

    lines = str(caught.value).splitlines()
    assert lines[0] == "The iteration's commit could not be made"
    assert lines[1] == "refs/orchestrator-iteration/01-slug"
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
    lock = project / ".git" / "refs" / "orchestrator-iteration" / "01-slug.lock"
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
    assert state.ref == "refs/orchestrator-iteration/01-slug"
    assert _parents_of(project, state.commit) == [state.commit, base]
    # The task ended, so the ref is gone from the clone and from origin.
    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator-iteration/")
    assert listed.split() == []
    assert _remote_refs(project, _origin_of(project), "refs/orchestrator-iteration/*", "refs/heads/orchestrator/*") == set()


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

    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*") == {first.ref}

    (project / "second.txt").write_text("two\n")
    git_in(project, "add", "-A")
    second = make_iteration_commit(project, "01-slug", 2)
    push_iteration_ref(project, second)

    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*") == {second.ref}
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
    git_in(project, "update-ref", "refs/orchestrator-laid/01-slug", base)  # the run's own laid ref
    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/heads/orchestrator/*") == {
        state.ref, "refs/heads/orchestrator/01-slug",
    }

    remove_iteration_refs(project, project / ".ai-factory" / "plans" / "01-slug.md")

    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator-iteration/", "refs/orchestrator-laid/")
    assert listed.split() == []
    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/orchestrator-laid/*", "refs/heads/orchestrator/*") == set()
    assert capsys.readouterr().out == ""  # nothing failed, no line


def test_a_removal_with_no_branch_on_origin_removes_only_the_refs_silently(tmp_path, capsys):
    project, base, origin = _repo(tmp_path)
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, "01-slug", 1)
    push_iteration_ref(project, state)

    remove_iteration_refs(project, project / ".ai-factory" / "plans" / "01-slug.md")

    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/heads/orchestrator/*") == set()
    assert capsys.readouterr().out == ""


def test_a_ref_name_holding_a_unicode_line_boundary_is_removed_whole(tmp_path, capsys):
    """A named roadmap whose segment holds U+2028 reads as one record: the
    listing is split by git's newline and the name taken after the tab, so
    the ref and the branch come off origin and nothing is printed."""
    project, base, origin = _repo(tmp_path)
    (project / ".ai-factory" / "plans" / "a\u2028b").mkdir()
    plan = project / ".ai-factory" / "plans" / "a\u2028b" / "01-slug.md"
    plan.write_text("# Plan\n")
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, ref_name_of(project, plan), 1)
    push_iteration_ref(project, state)
    branch = f"refs/heads/orchestrator/a\u2028b/01-slug"
    git_in(project, "push", "-q", "origin", f"{base}:{branch}")  # a Hermes task's branch
    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/heads/orchestrator/*") == {
        state.ref, branch,
    }

    remove_iteration_refs(project, plan)

    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/orchestrator-laid/*", "refs/heads/orchestrator/*") == set()
    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator-iteration/", "refs/orchestrator-laid/")
    assert listed.split() == []
    assert capsys.readouterr().out == ""  # nothing failed, no line


def test_two_roadmaps_sharing_a_stem_never_share_a_ref_or_a_cleanup(tmp_path):
    project, base, origin = _repo(tmp_path)
    (project / ".ai-factory" / "plans" / "john-doe").mkdir()
    named = project / ".ai-factory" / "plans" / "john-doe" / "01-slug.md"
    named.write_text("# Plan\n")
    plain = project / ".ai-factory" / "plans" / "01-slug.md"

    (project / "plain.txt").write_text("plain\n")
    git_in(project, "add", "-A")
    plain_state = make_iteration_commit(project, ref_name_of(project, plain), 1)
    push_iteration_ref(project, plain_state)
    (project / "named.txt").write_text("named\n")
    git_in(project, "add", "-A")
    named_state = make_iteration_commit(project, ref_name_of(project, named), 1)
    push_iteration_ref(project, named_state)
    git_in(project, "push", "-q", "origin",
           f"{base}:refs/heads/orchestrator/01-slug",
           f"{base}:refs/heads/orchestrator/john-doe/01-slug")
    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/heads/orchestrator/*") == {
        plain_state.ref, named_state.ref,
        "refs/heads/orchestrator/01-slug", "refs/heads/orchestrator/john-doe/01-slug",
    }

    remove_iteration_refs(project, plain)  # the default pair's task ends

    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/heads/orchestrator/*") == {
        named_state.ref, "refs/heads/orchestrator/john-doe/01-slug",  # the named roadmap's own stay
    }
    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator-iteration/")
    assert listed.split() == [named_state.ref]  # and its local ref stays too


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
    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator-iteration/")
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
        listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator-iteration/")
        assert listed.split() == []  # the local ref went anyway
    finally:
        silent.close()


def _gone(pid: int) -> bool:
    """True when the pid names no live process; PermissionError counts
    as gone, being what a group of only zombies answers on macOS."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def _ended(pid: int, limit: float = 8.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.1)
    return _gone(pid)


def _hook_pid(pid_file: Path, limit: float = 5.0) -> int:
    """The pid the hook's shell wrote once it is running; the wait
    fails when nothing is written."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if pid_file.exists():
            text = pid_file.read_text(encoding="utf-8")
            pid = text.strip()
            if text.endswith("\n") and pid.isdecimal():
                return int(pid)
        time.sleep(0.05)
    raise AssertionError(f"no pid was written to {pid_file}")


def test_a_local_removal_that_times_out_is_the_one_line_and_the_remote_still_runs(tmp_path, monkeypatch, capsys):
    """The local update-ref is held by a reference-transaction hook that never
    returns, so the call times out; the hook's own shell is ended with the
    call, and the remote calls run against the bare origin, a repository of
    its own, and take the ref and the branch off it."""
    project, base, origin = _repo(tmp_path)
    (project / "work.txt").write_text("work\n")
    git_in(project, "add", "-A")
    state = make_iteration_commit(project, "01-slug", 1)
    push_iteration_ref(project, state)
    git_in(project, "push", "-q", "origin", f"{base}:refs/heads/orchestrator/01-slug")  # a Hermes task's branch
    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/heads/orchestrator/*") == {
        state.ref, "refs/heads/orchestrator/01-slug",
    }
    monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
    pid_file = tmp_path / "hook.pid"
    hook = project / ".git" / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\necho $$ > {shlex.quote(str(pid_file))}\nwhile read old new ref; do :; done\nsleep 30\n")
    hook.chmod(0o755)

    remove_iteration_refs(project, project / ".ai-factory" / "plans" / "01-slug.md")

    out = capsys.readouterr().out
    assert out == f">>> WARNING: the iteration's ref {state.ref} could not be removed, continuing anyway.\n"
    assert _remote_refs(project, origin, "refs/orchestrator-iteration/*", "refs/heads/orchestrator/*") == set()
    assert _ended(_hook_pid(pid_file))


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

    listed = git_in(project, "for-each-ref", "--format=%(refname)", "refs/orchestrator-iteration/")
    assert listed.split() == ["refs/orchestrator-iteration/01-slug"]  # the local ref stays
    assert _remote_refs(project, _origin_of(project), "refs/orchestrator-iteration/*") == {"refs/orchestrator-iteration/01-slug"}


def _origin_of(project):
    from pathlib import Path as _Path

    url = git_in(project, "remote", "get-url", "origin").strip()
    return _Path(url)


# ---------------------------------------------------------------------------
# The run's own git calls run through the scrubbed runner
# ---------------------------------------------------------------------------


def test_the_commit_runs_in_the_target_with_git_dir_aimed_elsewhere(tmp_path, monkeypatch):
    """A `GIT_DIR` naming another repository in the environment aims none of
    the task's calls there: the commit lands in the target and the other
    repository's HEAD does not move."""
    project, base, origin = _repo(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    git_in(other, "init", "-q", "-b", "main", ".")
    git_in(other, "config", "user.email", "other@example.com")
    git_in(other, "config", "user.name", "Other")
    (other / "seed.txt").write_text("seed\n")
    git_in(other, "add", "-A")
    git_in(other, "commit", "-q", "-m", "the other base")
    other_head = git_in(other, "rev-parse", "HEAD").strip()

    (project / "work.txt").write_text("work\n")
    with monkeypatch.context() as mp:
        mp.setenv("GIT_DIR", str(other / ".git"))
        pipeline_module._git_commit(project, "The task's title")

    assert git_in(project, "log", "-1", "--format=%s").strip() == "The task's title"
    assert "work.txt" in _paths_of(project, git_in(project, "rev-parse", "HEAD").strip())
    assert git_in(other, "rev-parse", "HEAD").strip() == other_head


def test_a_flow_with_git_index_file_outside_holds_the_implementer_s_file_in_the_iteration(tmp_path, monkeypatch):
    """The loop's staging is scrubbed alike: with `GIT_INDEX_FILE` pointing at
    a path outside the repository, the iteration's commit still holds a file
    the implementer's turn wrote, and the outside index is never laid."""
    project, plan, config, base = _flow_at_implemented(tmp_path, monkeypatch)
    (project / "implementer.txt").write_text("written by the implementer's turn\n")
    verifier = _RecordingVerifier()
    mode = IMPLEMENT_MODE._replace(
        verify=IMPLEMENT_MODE.verify._replace(make_verifier=lambda reviewer, project_dir: verifier),
    )
    outside = tmp_path / "outside" / "staging.index"
    outside.parent.mkdir()

    with monkeypatch.context() as mp:
        mp.setenv("GIT_INDEX_FILE", str(outside))
        pipeline_module.process_task(
            project, _TaskStub(), 1, config, mode,
            backend=FakeAgentBackend(), implementer_result=WorkingTreeResult(),
        )

    state = verifier.calls[-1][-1]
    assert "implementer.txt" in _paths_of(project, state.commit)
    assert not outside.exists()


def test_a_held_index_lock_prints_git_s_text_and_raises_the_called_process_error(tmp_path, capsys):
    """A failed `add` fails as `check=True` failed: git's own standard error
    reaches the console, then the completed process raises its error with the
    command and the non-zero status."""
    project, base, origin = _repo(tmp_path)
    (project / "work.txt").write_text("work\n")
    lock = project / ".git" / "index.lock"
    lock.write_text("")

    with pytest.raises(subprocess.CalledProcessError) as caught:
        pipeline_module._git_commit(project, "The task's title")

    assert caught.value.returncode != 0
    assert caught.value.cmd[0] == "git"
    assert "index.lock" in capsys.readouterr().err


def test_a_commit_hook_past_the_bound_still_ends_in_the_commit(tmp_path, monkeypatch):
    """The task's commit takes no bound: its duration is the target project's
    own hooks, so a commit hook that outruns the module's bound still ends in
    the commit."""
    project, base, origin = _repo(tmp_path)
    monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nsleep 1.2\n")
    hook.chmod(0o755)
    (project / "work.txt").write_text("work\n")

    pipeline_module._git_commit(project, "The task's title")

    assert git_in(project, "log", "-1", "--format=%s").strip() == "The task's title"
    assert git_in(project, "rev-parse", "HEAD").strip() != base


def test_the_commit_waits_for_git_alone_not_a_hook_s_background_job(tmp_path):
    """The commit reads none of git's output, so it leaves the two streams
    inherited and waits on git alone: a background job the pre-commit hook
    starts and leaves behind holds no pipe the commit waits for."""
    project, base, origin = _repo(tmp_path)
    pid_file = tmp_path / "background.pid"
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {shlex.quote(str(pid_file))}\nexit 0\n")
    hook.chmod(0o755)
    (project / "work.txt").write_text("work\n")

    started = time.monotonic()
    try:
        pipeline_module._git_commit(project, "The task's title")
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 9)
            except ProcessLookupError:
                pass
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert git_in(project, "log", "-1", "--format=%s").strip() == "The task's title"
    assert git_in(project, "rev-parse", "HEAD").strip() != base


def test_a_push_an_origin_refuses_prints_the_one_warning_and_raises_nothing(tmp_path, capsys):
    project, base, origin = _repo(tmp_path)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (project / "work.txt").write_text("work\n")

    pipeline_module._git_commit(project, "The task's title")

    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == ">>> WARNING: git push failed, continuing anyway."
    assert git_in(project, "rev-parse", "HEAD").strip() != base  # the commit stands


def test_a_push_an_origin_refuses_with_undecodable_bytes_prints_the_one_warning_and_raises_nothing(
        tmp_path, monkeypatch, capsys):
    """A refusal whose diagnostic holds a byte the locale cannot decode is
    still the one warning: the push's text never raises at the decode and
    the commit stands."""
    project, base, origin = _repo(tmp_path)
    monkeypatch.setattr(git_runner.locale, "getpreferredencoding",
                        lambda *args, **kwargs: "ascii")
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nprintf '\\351\\n'\nexit 1\n")
    hook.chmod(0o755)
    (project / "work.txt").write_text("work\n")

    pipeline_module._git_commit(project, "The task's title")

    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == ">>> WARNING: git push failed, continuing anyway."
    assert git_in(project, "rev-parse", "HEAD").strip() != base  # the commit stands


def test_a_push_against_an_origin_that_never_answers_prints_the_one_warning_and_raises_nothing(tmp_path, monkeypatch, capsys):
    project, base, origin = _repo(tmp_path)
    silent = _SilentOrigin()
    try:
        monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
        git_in(project, "remote", "set-url", "origin", f"http://127.0.0.1:{silent.port}/origin.git")
        (project / "work.txt").write_text("work\n")

        pipeline_module._git_commit(project, "The task's title")

        lines = capsys.readouterr().out.splitlines()
        assert lines[-1] == ">>> WARNING: git push failed, continuing anyway."
        assert git_in(project, "rev-parse", "HEAD").strip() != base  # the commit stands
    finally:
        silent.close()


def test_the_assembly_s_reads_run_in_the_target_with_git_dir_aimed_elsewhere(tmp_path, monkeypatch):
    """A `GIT_DIR` naming a second repository whose identity differs aims
    neither read there: the config read answers the target's value, and a
    candidate inside the target's worktree reads as inside it."""
    project, base, origin = _repo(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    git_in(other, "init", "-q", "-b", "main", ".")
    git_in(other, "config", "user.email", "other@example.com")
    git_in(other, "config", "user.name", "Other")
    (other / "seed.txt").write_text("seed\n")
    git_in(other, "add", "-A")
    git_in(other, "commit", "-q", "-m", "the other base")

    with monkeypatch.context() as mp:
        mp.setenv("GIT_DIR", str(other / ".git"))
        assert layout_module._git_config_value(project, "user.email") == "test@example.com"
        with pytest.raises(ValueError, match="outside target Git worktree"):
            layout_module._external_evidence_path(project, project / "evidence.json")
