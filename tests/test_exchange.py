"""The exchange placement and the answer command, against real files."""

import hashlib
import sys
from pathlib import Path

import pytest

from orchestrator import answer, exchange
from orchestrator.exchange import deposit, exchange_paths, remove_call, remove_task
from orchestrator.ports import AgentRole


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "exchanges"
    monkeypatch.setattr(exchange, "_ROOT", root)
    return root


def _project(tmp_path) -> Path:
    return tmp_path / "project"


def test_the_paths_sit_under_the_digest_of_the_resolved_project(tmp_path, root):
    project = _project(tmp_path)
    project.mkdir()
    resolved = project.resolve()

    paths = exchange_paths(project, AgentRole.CODE_REVIEWER, "reviews/john-doe/01-slug-review-1")

    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    assert paths.message == root / digest / "reviews/john-doe" / "code_reviewer-01-slug-review-1.message.md"
    assert paths.sent == root / digest / "reviews/john-doe" / "code_reviewer-01-slug-review-1.sent"
    assert paths.answer == root / digest / "reviews/john-doe" / "code_reviewer-01-slug-review-1.answer"


def test_a_deposit_appears_whole_and_the_first_answer_stands(tmp_path):
    directory = tmp_path / "deposits"
    directory.mkdir()
    answer_path = directory / "x.answer"

    deposit(answer_path, "the first answer\n")

    with pytest.raises(FileExistsError):
        deposit(answer_path, "the second answer\n")

    assert answer_path.read_text(encoding="utf-8") == "the first answer\n"
    assert not any(entry.name.startswith(".deposit-") for entry in directory.iterdir())


def test_a_failed_deposit_removes_its_temporary_file(tmp_path):
    directory = tmp_path / "deposits"
    directory.mkdir()
    answer_path = directory / "x.answer"
    deposit(answer_path, "the first answer\n")

    with pytest.raises(FileExistsError):
        deposit(answer_path, "the second answer\n")

    assert sorted(entry.name for entry in directory.iterdir()) == ["x.answer"]


def test_remove_call_removes_the_three_files_and_ignores_missing(tmp_path):
    directory = tmp_path / "calls"
    directory.mkdir()
    message = directory / "c.message.md"
    sent = directory / "c.sent"
    message.write_text("m", encoding="utf-8")

    remove_call(exchange.ExchangePaths(message=message, sent=sent, answer=directory / "c.answer"))

    assert not message.exists() and not sent.exists()


def _task_files(root, project, plan_name="01-slug", segment=None):
    """Lay the exchange files of one task: the planner's, the plan reviewer's
    and the reviewer's, under the project's digest."""
    digest = hashlib.sha256(str(project.resolve()).encode("utf-8")).hexdigest()
    laid = []
    base = Path(root, digest)
    for directory, role, name in (
        ("plans", "planner", f"{plan_name}.message.md"),
        ("plans", "planner", f"{plan_name}.sent"),
        ("plan-reviews", "plan_reviewer", f"{plan_name}-plan-review-1.message.md"),
        ("plan-reviews", "plan_reviewer", f"{plan_name}-plan-review-1.answer"),
        ("reviews", "code_reviewer", f"{plan_name}-review-1.message.md"),
        ("reviews", "code_reviewer", f"{plan_name}-review-1.sent"),
    ):
        path = base / directory
        if segment:
            path = path / segment
        path = path / f"{role}-{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        laid.append(path)
    return laid, base


def test_remove_task_removes_the_task_s_files_and_no_other_task_s(tmp_path, root):
    project = _project(tmp_path)
    project.mkdir()
    laid, base = _task_files(root, project)
    other = base / "plans" / "planner-07-other.message.md"
    other.write_text("x", encoding="utf-8")
    bystander = base / "reviews" / "code_reviewer-07-other.answer"
    bystander.parent.mkdir(parents=True, exist_ok=True)
    bystander.write_text("x", encoding="utf-8")
    plan = project / ".ai-factory" / "plans" / "01-slug.md"
    plan.parent.mkdir(parents=True)

    remove_task(project, plan)

    assert not any(path.exists() for path in laid)
    assert other.exists() and bystander.exists()


def test_remove_task_under_a_segment_removes_only_that_segment_s_files(tmp_path, root):
    project = _project(tmp_path)
    project.mkdir()
    laid, base = _task_files(root, project, segment="john-doe")
    other_segment, _ = _task_files(root, project, plan_name="01-slug", segment="other")
    plan = project / ".ai-factory" / "plans" / "john-doe" / "01-slug.md"

    remove_task(project, plan)

    assert not any(path.exists() for path in laid)
    assert all(path.exists() for path in other_segment)


def test_remove_task_raises_nothing_for_a_missing_directory(tmp_path, root):
    project = _project(tmp_path)
    project.mkdir()
    plan = project / ".ai-factory" / "plans" / "01-slug.md"

    remove_task(project, plan)  # no exchange directory exists at all


# ---------------------------------------------------------------------------
# The answer command


def _cli(monkeypatch, capsys, argv, stdin):
    monkeypatch.setattr(sys, "argv", ["orchestrator-answer", *argv])
    monkeypatch.setattr(sys, "stdin", _Stdin(stdin))
    try:
        answer.cli()
    except SystemExit as exc:
        return exc.code
    raise AssertionError("cli returned without exiting")


class _Stdin:
    """A standard input stand-in that carries bytes: reading it as text fails
    the test, so the command must take its input as bytes and decode UTF-8
    itself."""

    class _Buffer:
        def __init__(self, raw):
            self._raw = raw

        def read(self):
            return self._raw

    def __init__(self, raw):
        self.buffer = self._Buffer(raw)

    def read(self):
        raise AssertionError("the command must read standard input as bytes")


@pytest.mark.parametrize("name,stdin,cause", [
    ("x.answer", b"   \n", "the answer is empty"),
    ("x.txt", b"the answer", "the deposit path must end in .answer"),
    ("absent/x.answer", b"the answer", "the deposit path's directory does not exist"),
    ("x.answer", b"the \xff\xfe answer", "the answer is not valid UTF-8"),
])
def test_every_refusal_writes_nothing_and_exits_one(tmp_path, monkeypatch, capsys, name, stdin, cause):
    """Every row's path sits under a directory that exists, except the row
    refused for a directory that does not, and each row asserts the one line
    the command printed names its own cause, so no row passes on another
    refusal."""
    directory = tmp_path / "deposits"
    directory.mkdir()
    answer_path = directory / name

    code = _cli(monkeypatch, capsys, [str(answer_path)], stdin)

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == cause
    if name.startswith("absent/"):
        assert not answer_path.parent.exists()
    else:
        assert list(directory.iterdir()) == []  # nothing was written there


def test_a_deposit_exits_zero_and_a_second_answer_leaves_the_first(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "deposits"
    directory.mkdir()
    answer_path = directory / "x.answer"

    assert _cli(monkeypatch, capsys, [str(answer_path)], b"the answer\n") == 0
    assert answer_path.read_text(encoding="utf-8") == "the answer\n"

    assert _cli(monkeypatch, capsys, [str(answer_path)], b"the second\n") == 1
    assert answer_path.read_text(encoding="utf-8") == "the answer\n"


def test_an_answer_holding_non_ascii_text_is_deposited_byte_for_byte(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "deposits"
    directory.mkdir()
    answer_path = directory / "x.answer"
    raw = "Réponse — révision, проверка, 検証\n".encode("utf-8")

    assert _cli(monkeypatch, capsys, [str(answer_path)], raw) == 0
    assert answer_path.read_bytes() == raw


def test_a_deposit_that_cannot_be_written_is_refused_in_one_line(tmp_path, monkeypatch, capsys):
    from orchestrator import answer

    def _refuse(path, text):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(answer, "deposit", _refuse)
    directory = tmp_path / "deposits"
    directory.mkdir()
    code = _cli(monkeypatch, capsys, [str(directory / "x.answer")], b"the answer")

    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1
    assert "Permission denied" in captured.err
