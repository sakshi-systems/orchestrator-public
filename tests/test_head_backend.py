"""The head's backend: a message, a marker and a wait, with stand-ins for the
send and the sleep — no real codex or claude is ever reached."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import exchange
from orchestrator.courier import SendResult
from orchestrator.errors import HeadNotReachedError
from orchestrator.exchange import answer_command, exchange_paths
from orchestrator.head_backend import HeadAgentBackend
from orchestrator.locator import HeadIdentity
from orchestrator.ports import AgentRole
from tests.conftest import bounded_sleep

IDENTITY = HeadIdentity(repository=Path("/repos/the-project"), owner="max", folder="r-d-head")
PROMPT = "Review the changes, please."
INSTRUCTIONS = "The reviewer's instructions, verbatim."
EXCHANGE = "reviews/john-doe/01-slug-review-1"


class Sender:
    """A stand-in courier send: records the call, answers scripted results."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, identity, text):
        self.calls.append((identity, text))
        return self.results.pop(0) if self.results else SendResult(sent=True, reason=None)


class Sleeper:
    """A stand-in sleep that deposits on its first call and fails the test
    after a fixed number of calls, so a wait that never ends fails, not hangs."""

    def __init__(self, answer_path, text, limit=5):
        self.answer_path = answer_path
        self.text = text
        self.limit = limit
        self.calls = 0
        self.observed = []
        self.observed_message = ""

    def __call__(self, seconds):
        self.calls += 1
        if self.calls == 1:
            self.observed.append(self.answer_path.with_suffix(".sent").exists())
            self.observed_message = self.answer_path.with_suffix(".message.md").read_text(encoding="utf-8")
            exchange.deposit(self.answer_path, self.text)
        elif self.calls > self.limit:
            raise AssertionError("the wait never found its answer")


def _backend(project, sender, sleeper=None):
    return HeadAgentBackend(
        HeadIdentity(repository=IDENTITY.repository, owner=IDENTITY.owner, folder=IDENTITY.folder),
        send=sender, sleep=sleeper if sleeper is not None else bounded_sleep(),
    )


def _run(backend, project, exchange_name=EXCHANGE):
    return backend.run(
        role=AgentRole.CODE_REVIEWER, prompt=PROMPT, cwd=str(project),
        instructions=INSTRUCTIONS, session_id=None, exchange=exchange_name,
    )


def _paths_of(project):
    return exchange_paths(project, AgentRole.CODE_REVIEWER, EXCHANGE)


def test_a_call_without_an_exchange_raises_value_error(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    backend = _backend(project, Sender())

    with pytest.raises(ValueError, match="exchange"):
        backend.run(role=AgentRole.CODE_REVIEWER, prompt=PROMPT, cwd=str(project),
                    instructions=INSTRUCTIONS, session_id=None)


def test_the_message_holds_the_turn_the_instructions_the_assignment_and_the_command(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    sender = Sender()
    sleeper = Sleeper(_paths_of(project).answer, "the head's answer\n")
    backend = _backend(project, sender, sleeper)

    answer, session = _run(backend, project)

    assert answer == "the head's answer\n"
    assert session == "max/r-d-head"
    (identity, line), = sender.calls
    assert identity == IDENTITY
    paths = _paths_of(project)
    assert str(paths.message) in line  # one sentence carrying the message's absolute path
    message = sleeper.observed_message  # read while the call still stood
    assert str(project) in message  # the project's absolute path, cwd
    assert "code_reviewer" in message  # the role
    assert f"## Instructions\n\n{INSTRUCTIONS}" in message
    assert f"## Assignment\n\n{PROMPT}" in message
    how_to = message.split("## How to answer", 1)[1]
    assert str(answer_command()) in how_to
    assert str(paths.answer) in how_to


def test_a_space_in_each_path_does_not_split_the_command_line_the_head_runs(tmp_path, monkeypatch):
    """The message's command line, taken from between the backticks of
    `## How to answer` and run through `sh -c` as the head would run it,
    deposits the answer when the answer command's directory and the exchange
    root each hold a space: both paths reach the head quoted for the shell."""
    import shlex
    import subprocess

    from orchestrator import exchange as exchange_module
    from orchestrator import head_backend as head_backend_module

    bin_directory = tmp_path / "answer bin"
    bin_directory.mkdir()
    script = bin_directory / "orchestrator-answer"
    script.write_text(
        "#!/bin/sh\n"
        '[ "$#" -eq 1 ] || { echo "one deposit path, no more" >&2; exit 1; }\n'
        'cat > "$1"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(head_backend_module, "answer_command", lambda: script)
    monkeypatch.setattr(exchange_module, "_ROOT", tmp_path / "exchange root")

    project = tmp_path / "project"
    project.mkdir()
    paths = _paths_of(project)
    sender = Sender(results=[SendResult(sent=False, reason="no agent is reachable")])
    backend = _backend(project, sender)

    with pytest.raises(HeadNotReachedError):  # the send fails with the message standing
        _run(backend, project)

    assert str(paths.answer).startswith(str(tmp_path / "exchange root"))  # a space in the root
    message = paths.message.read_text(encoding="utf-8")
    how_to = message.split("## How to answer", 1)[1]
    command_line = how_to.split("`", 2)[1]
    assert command_line == f"{shlex.quote(str(script))} {shlex.quote(str(paths.answer))}"

    completed = subprocess.run(
        ["sh", "-c", command_line], input="the head's answer\n",
        text=True, capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert paths.answer.read_text(encoding="utf-8") == "the head's answer\n"


def test_a_send_writes_the_marker_and_a_non_send_raises_the_halt(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    paths = _paths_of(project)
    sender = Sender(results=[SendResult(sent=False, reason="no agent named 'max' is reachable")])
    backend = _backend(project, sender)

    with pytest.raises(HeadNotReachedError) as caught:
        _run(backend, project)

    lines = str(caught.value).splitlines()
    assert lines[0] == "A head could not be reached"
    assert lines[1] == "/repos/the-project max r-d-head"
    assert "no agent named 'max' is reachable" in str(caught.value)
    assert paths.message.exists()  # the message stays for the resume
    assert not paths.sent.exists()  # and no marker stands


def test_a_send_writes_the_sent_marker(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    paths = _paths_of(project)
    sender = Sender()
    sleeper = Sleeper(paths.answer, "the head's answer\n")
    backend = _backend(project, sender, sleeper)

    _run(backend, project)

    assert len(sender.calls) == 1  # one send per call
    assert sleeper.observed == [True]  # the marker stood when the wait began
    assert not paths.sent.exists()  # and the answer's return removed it


def test_a_deposit_already_made_is_returned_with_no_send(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    paths = _paths_of(project)
    paths.message.parent.mkdir(parents=True, exist_ok=True)
    exchange.deposit(paths.answer, "deposited before the call\n")
    sender = Sender()
    backend = _backend(project, sender)

    answer, session = _run(backend, project)

    assert answer == "deposited before the call\n"
    assert session == "max/r-d-head"
    assert sender.calls == []


def test_a_marker_without_a_deposit_sends_nothing_and_only_waits(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    paths = _paths_of(project)
    paths.message.parent.mkdir(parents=True, exist_ok=True)
    paths.message.write_text("an earlier attempt's message", encoding="utf-8")
    paths.sent.write_text("", encoding="utf-8")
    sender = Sender()
    sleeper = Sleeper(paths.answer, "deposited after the wait began\n")
    backend = _backend(project, sender, sleeper)

    answer, _ = _run(backend, project)

    assert answer == "deposited after the wait began\n"
    assert sender.calls == []


def test_after_an_answer_is_returned_none_of_the_call_s_files_remains(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    paths = _paths_of(project)

    for mode in ("wait", "already"):
        for path in (paths.message, paths.sent, paths.answer):
            path.unlink(missing_ok=True)
        paths.message.parent.mkdir(parents=True, exist_ok=True)
        if mode == "already":
            exchange.deposit(paths.answer, f"the {mode} answer\n")
            backend = _backend(project, Sender())
        else:
            backend = _backend(project, Sender(), Sleeper(paths.answer, f"the {mode} answer\n"))

        answer, _ = _run(backend, project)

        assert answer == f"the {mode} answer\n"
        assert not paths.message.exists()
        assert not paths.sent.exists()
        assert not paths.answer.exists()


# ---------------------------------------------------------------------------
# The task's start and end, through the whole flow


def _flow_project(tmp_path, monkeypatch):
    """A project repository with an origin, a roadmap of one task and the plan
    directories, ready for process_task."""
    from orchestrator import pipeline as pipeline_module
    from orchestrator.config import OrchestratorConfig
    from tests.conftest import bounded_sleep, git_in

    project = tmp_path / "project"
    (project / ".ai-factory" / "plans").mkdir(parents=True)
    (project / ".ai-factory" / "plan-reviews").mkdir(parents=True)
    (project / ".ai-factory" / "reviews").mkdir(parents=True)
    (project / ".ai-factory" / "ROADMAP.md").write_text(
        "# Work\n\n- [ ] **Some task** — Some description\n", encoding="utf-8",
    )
    git_in(project, "init", "-q", "-b", "main", ".")
    git_in(project, "config", "user.email", "test@example.com")
    git_in(project, "config", "user.name", "Test")
    git_in(project, "add", "-A")
    git_in(project, "commit", "-q", "-m", "the base")
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git_in(project, "remote", "add", "origin", str(origin))
    git_in(project, "push", "-q", "origin", "HEAD:refs/heads/main")
    monkeypatch.setattr(pipeline_module, "mark_done", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_module, "_git_commit", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_module, "report", lambda *a, **k: None)
    config = OrchestratorConfig(max_iterations=3, usage_threshold_5h=90, usage_threshold_weekly=95,
                                enable_phase_sessions=False)
    return project, config


def _routed(project, sender):
    from orchestrator.config import role_runtimes
    from orchestrator.role_backend import RoleRoutedBackend, backend_key
    from orchestrator.implementer_result import WorkingTreeResult
    from tests.fakes import FakeAgentBackend
    from orchestrator.courier import SendResult

    identity = HeadIdentity(repository=project, owner="max", folder="r-d")
    head = HeadAgentBackend(identity, send=sender, sleep=bounded_sleep())
    local = FakeAgentBackend(on_call=lambda call: {
        AgentRole.PLAN_REVIEWER: "Fine\n\nPLAN_REVIEW_PASS",
        AgentRole.IMPLEMENTER: "Did the work.",
    }.get(call.role, "fake output"))
    roles = {
        "planner": {"runtime": "head", "head": {
            "repository": str(project), "owner": "max", "folder": "r-d"}},
        "plan_reviewer": {"runtime": "claude"},
        "implementer": {"runtime": "claude"},
        "code_reviewer": {"runtime": "head", "head": {
            "repository": str(project), "owner": "max", "folder": "r-d"}},
    }
    return RoleRoutedBackend(
        {backend_key(entry): (head if entry["runtime"] == "head" else local) for entry in roles.values()},
        roles,
    )


class DepositingSender:
    """A stand-in send that deposits the scripted answer for the message it
    carries, so the head answers the moment it is reached."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    def __call__(self, identity, text):
        from orchestrator.courier import SendResult
        self.calls.append(text)
        message_path = Path(text.split("read ", 1)[1].split(" and", 1)[0])
        answer_path = message_path.with_name(message_path.name[: -len(".message.md")] + ".answer")
        if self.replies:
            answer = self.replies.pop(0)
        else:
            answer = "All good\n\nREVIEW_PASS"
        exchange.deposit(answer_path, answer)
        return SendResult(sent=True, reason=None)


class _TaskStub:
    slug = "some-task"
    title = "Some task"
    description = "Some description"
    line_number = 0


def _run_task(project, config, backend):
    from orchestrator.pipeline import process_task
    from orchestrator.implementer_result import WorkingTreeResult
    process_task(project, _TaskStub(), 1, config, backend=backend,
                 implementer_result=WorkingTreeResult())


def test_a_task_started_afresh_sends_its_messages_and_returns_no_leftover(tmp_path, monkeypatch):
    project, config = _flow_project(tmp_path, monkeypatch)
    plan_answer = exchange.exchange_paths(project, AgentRole.PLANNER, "plans/01-some-task")
    review_answer = exchange.exchange_paths(project, AgentRole.CODE_REVIEWER, "reviews/01-some-task-review-1")
    for paths, text in ((plan_answer, "an earlier attempt's plan"), (review_answer, "an earlier attempt's review")):
        paths.message.parent.mkdir(parents=True, exist_ok=True)
        exchange.deposit(paths.answer, text)
    sender = DepositingSender(replies=["# Plan\n\n## Test Command\n`true`\n"])

    _run_task(project, config, _routed(project, sender))

    plan = project / ".ai-factory" / "plans" / "01-some-task.md"
    assert plan.read_text(encoding="utf-8") == "# Plan\n\n## Test Command\n`true`\n"
    review = project / ".ai-factory" / "reviews" / "01-some-task-review-1.md"
    assert review.read_text(encoding="utf-8") == "All good\n\nREVIEW_PASS"
    # the task ended: none of the calls' files remains
    assert not plan_answer.message.exists() and not plan_answer.sent.exists() and not plan_answer.answer.exists()
    assert not review_answer.message.exists() and not review_answer.sent.exists() and not review_answer.answer.exists()


def test_a_task_that_stops_keeps_the_leftovers_of_its_waiting_call(tmp_path, monkeypatch):
    """The review's send fails, so the task halts with the message written and
    no marker standing: the call is still waiting, and only a task that ends
    (or starts afresh) removes its files."""
    from orchestrator.courier import SendResult
    project, config = _flow_project(tmp_path, monkeypatch)

    class Unreached:
        def __init__(self):
            self.calls = 0

        def __call__(self, identity, text):
            self.calls += 1
            if self.calls == 1:  # the plan reaches its head
                message_path = Path(text.split("read ", 1)[1].split(" and", 1)[0])
                answer_path = message_path.with_name(message_path.name[: -len(".message.md")] + ".answer")
                exchange.deposit(answer_path, "# Plan\n\n## Test Command\n`true`\n")
                return SendResult(sent=True, reason=None)
            return SendResult(sent=False, reason="no agent named 'max' is reachable")

    with pytest.raises(HeadNotReachedError):
        _run_task(project, config, _routed(project, Unreached()))

    paths = exchange.exchange_paths(project, AgentRole.CODE_REVIEWER, "reviews/01-some-task-review-1")
    assert paths.message.exists()  # kept: the task did not end
    assert not paths.sent.exists() and not paths.answer.exists()


def test_a_resume_with_a_record_returns_a_deposit_already_made(tmp_path, monkeypatch):
    project, config = _flow_project(tmp_path, monkeypatch)
    plan = project / ".ai-factory" / "plans" / "01-some-task.md"
    plan.write_text("# Plan\n\n## Test Command\n`true`\n", encoding="utf-8")
    config.roles = {
        "planner": {"runtime": "claude"}, "plan_reviewer": {"runtime": "claude"},
        "implementer": {"runtime": "claude"},
        "code_reviewer": {"runtime": "head", "head": {
            "repository": str(project), "owner": "max", "folder": "r-d"}},
    }
    plan.with_suffix(".json").write_text(json.dumps({
        "runtime": {"planner": {"runtime": "claude"}, "plan_reviewer": {"runtime": "claude"},
                    "implementer": {"runtime": "claude"},
                    "code_reviewer": {"runtime": "head", "head": {
                        "repository": str(project), "owner": "max", "folder": "r-d"}}},
        "step": "implemented:1",
    }), encoding="utf-8")
    (project / ".ai-factory" / "plan-reviews" / "01-some-task-plan-review-1.md").write_text(
        "PLAN_REVIEW_PASS", encoding="utf-8")
    (project / ".ai-factory" / "reviews" / "01-some-task-implementer-1.md").write_text(
        "Did the work.", encoding="utf-8")
    paths = exchange.exchange_paths(project, AgentRole.CODE_REVIEWER, "reviews/01-some-task-review-1")
    paths.message.parent.mkdir(parents=True, exist_ok=True)
    exchange.deposit(paths.answer, "REVIEW_PASS\n")  # a deposit the earlier attempt never read
    sender = DepositingSender(replies=[])

    _run_task(project, config, _routed(project, sender))

    assert sender.calls == []  # the deposit was returned with no send
    review = project / ".ai-factory" / "reviews" / "01-some-task-review-1.md"
    assert review.read_text(encoding="utf-8") == "REVIEW_PASS\n"
    assert not paths.answer.exists()  # and the call's files were removed after the answer


def test_the_task_end_sweep_removes_an_orphan_call_and_keeps_another_task_s(tmp_path, monkeypatch):
    """A task that stopped at its first review's send, leaving a record, is
    resumed; an orphan call's files under its plan stem, named for a review
    number no call takes, are swept when the task ends, and another task's
    files beside them stay."""
    from orchestrator.courier import SendResult
    from orchestrator.exchange import deposit
    project, config = _flow_project(tmp_path, monkeypatch)
    config.roles = {
        "planner": {"runtime": "claude"}, "plan_reviewer": {"runtime": "claude"},
        "implementer": {"runtime": "claude"},
        "code_reviewer": {"runtime": "head", "head": {
            "repository": str(project), "owner": "max", "folder": "r-d"}},
    }
    plan = project / ".ai-factory" / "plans" / "01-some-task.md"
    plan.write_text("# Plan\n\n## Test Command\n`true`\n", encoding="utf-8")
    plan.with_suffix(".json").write_text(json.dumps({
        "runtime": {"planner": {"runtime": "claude"}, "plan_reviewer": {"runtime": "claude"},
                    "implementer": {"runtime": "claude"},
                    "code_reviewer": {"runtime": "head", "head": {
                        "repository": str(project), "owner": "max", "folder": "r-d"}}},
        "step": "implemented:1",
    }), encoding="utf-8")
    (project / ".ai-factory" / "plan-reviews" / "01-some-task-plan-review-1.md").write_text(
        "PLAN_REVIEW_PASS", encoding="utf-8")
    (project / ".ai-factory" / "reviews" / "01-some-task-implementer-1.md").write_text(
        "Did the work.", encoding="utf-8")
    orphan = exchange.exchange_paths(project, AgentRole.CODE_REVIEWER, "reviews/01-some-task-review-9")
    other = exchange.exchange_paths(project, AgentRole.CODE_REVIEWER, "reviews/02-other-task-review-1")
    for paths in (orphan, other):
        paths.message.parent.mkdir(parents=True, exist_ok=True)
        paths.message.write_text("an earlier message", encoding="utf-8")
        paths.sent.write_text("", encoding="utf-8")
    paths = exchange.exchange_paths(project, AgentRole.CODE_REVIEWER, "reviews/01-some-task-review-1")
    paths.message.parent.mkdir(parents=True, exist_ok=True)
    deposit(paths.answer, "REVIEW_PASS\n")  # the review this resume does take

    class Reviewing:
        def __call__(self, identity, text):
            raise AssertionError("the review's deposit is already made; nothing is sent")

    _run_task(project, config, _routed(project, Reviewing()))

    assert not orphan.message.exists() and not orphan.sent.exists()  # swept at the task's end
    assert other.message.exists() and other.sent.exists()  # another task's files remain
