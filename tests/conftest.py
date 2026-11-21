import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import courier as courier_module
from orchestrator.git_runner import REPOSITORY_LOCATION_VARIABLES


def pytest_sessionfinish(session, exitstatus):
    # NO_TESTS_COLLECTED (5) is the success condition for the empty scaffold.
    # Once real tests exist they collect normally and this never fires.
    if exitstatus == 5:
        session.exitstatus = 0


@pytest.fixture(autouse=True)
def _no_repository_location_variables(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits a repository-location variable: a pytest run
    started by a git hook would otherwise aim every git call, ``git_in``
    and the direct ``subprocess.run`` calls alike, at the hook's
    repository and damage it.  A test that sets one of these names
    itself does so after this fixture ran, in its own body, and keeps
    its effect."""
    for name in REPOSITORY_LOCATION_VARIABLES:
        monkeypatch.delenv(name, raising=False)


class bounded_sleep:
    """A sleep stand-in that fails the test after a fixed number of calls, so a
    wait that never ends fails instead of hanging. Every test that runs a
    HeadAgentBackend's wait gives the backend one of these."""

    def __init__(self, limit: int = 5) -> None:
        self.limit = limit
        self.calls = 0

    def __call__(self, seconds: float) -> None:
        self.calls += 1
        if self.calls > self.limit:
            raise AssertionError("the wait never found its answer")


@pytest.fixture(autouse=True)
def _exchanges_nowhere(tmp_path, monkeypatch):
    """No test touches the real exchange root: the placement is aimed at a
    directory of the test's own."""
    from orchestrator import exchange as exchange_module
    monkeypatch.setattr(exchange_module, "_ROOT", tmp_path / "test-exchanges")


@pytest.fixture(autouse=True)
def _courier_sends_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches a real codex or claude: the courier's subprocess is a
    stand-in whose run raises, keeping the module's constants and exception
    types.  A test that sends without its own stand-in fails instead of
    calling the executable; a test that sets its own replaces it."""
    def _refuse(*args, **kwargs):
        raise AssertionError("a test sent without its own subprocess stand-in")

    monkeypatch.setattr(courier_module, "subprocess", SimpleNamespace(
        run=_refuse,
        DEVNULL=subprocess.DEVNULL,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
        SubprocessError=subprocess.SubprocessError,
        TimeoutExpired=subprocess.TimeoutExpired,
        CompletedProcess=subprocess.CompletedProcess,
    ))


@pytest.fixture(autouse=True)
def _no_git_configuration_channels(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits a git configuration channel: the product's
    ``run_git`` keeps ``GIT_CONFIG``, ``GIT_CONFIG_PARAMETERS``,
    ``GIT_CONFIG_COUNT`` and the numbered key and value names in the
    environment it hands git, so a shell that carries one — a
    ``core.hooksPath`` among them — would silently reconfigure every
    git the tests run.  A test that sets a channel itself does so
    after this fixture ran, in its own body, and keeps its effect."""
    for name in ("GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    numbered = [name for name in os.environ
                if name.startswith("GIT_CONFIG_KEY_")
                or name.startswith("GIT_CONFIG_VALUE_")]
    for name in numbered:
        monkeypatch.delenv(name, raising=False)


@dataclass(frozen=True)
class GitStand:
    """A local bare origin and its clones.

    ``origin`` holds ``main`` with ``base_commit`` and, after it,
    ``main_commit``, and ``refs/pull/1/head`` at ``head_commit``, a
    child of ``base_commit`` and not of ``main_commit``.  ``seed`` is a
    clone to build more history with, ``checkout`` a clone that has
    fetched the pull request's head and serves as a repository's
    checkout.
    """

    origin: Path
    seed: Path
    checkout: Path
    base_commit: str
    head_commit: str
    main_commit: str


def git_environment() -> dict[str, str]:
    """The host environment with git's configuration files silenced, so
    the tests read no config of the machine they run on."""
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _commit_environment() -> dict[str, str]:
    env = git_environment()
    env.update(GIT_AUTHOR_NAME="Sakshi Test",
               GIT_AUTHOR_EMAIL="test@example.invalid",
               GIT_COMMITTER_NAME="Sakshi Test",
               GIT_COMMITTER_EMAIL="test@example.invalid")
    return env


def git_in(cwd: Path, *arguments: str, input: str | None = None) -> str:
    """Run git synchronously and return its standard output; used to
    build the history the tests lay worktrees from, plumbing calls
    reading their input from the given text."""
    result = subprocess.run(["git", *arguments], cwd=str(cwd),
                            env=_commit_environment(),
                            capture_output=True, text=True, input=input)
    if result.returncode != 0:
        raise AssertionError(f"git {arguments[:2]}: {result.stderr}")
    return result.stdout


@pytest.fixture
def git_stand(monkeypatch: pytest.MonkeyPatch,
              tmp_path: Path) -> GitStand:
    """The hermetic git environment set for the test, a bare origin with
    a pull request's head beside a main that has moved on, and clones
    of it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git_in(tmp_path, "clone", str(origin), str(seed))
    (seed / "file.txt").write_text("the first content\n", encoding="utf-8")
    run_sh = seed / "run.sh"
    run_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    run_sh.chmod(0o755)
    sub = seed / "sub"
    sub.mkdir()
    (sub / "more.txt").write_text("more\n", encoding="utf-8")
    git_in(seed, "add", "-A")
    git_in(seed, "commit", "-m", "the base of the pull request")
    base_commit = git_in(seed, "rev-parse", "HEAD").strip()
    git_in(seed, "push", str(origin), f"{base_commit}:refs/heads/main")
    git_in(seed, "checkout", "-b", "pr")
    (seed / "file.txt").write_text("the pull request's content\n",
                                   encoding="utf-8")
    git_in(seed, "add", "-A")
    git_in(seed, "commit", "-m", "the pull request's head")
    head_commit = git_in(seed, "rev-parse", "HEAD").strip()
    git_in(seed, "push", str(origin), f"{head_commit}:refs/pull/1/head")
    git_in(seed, "checkout", "main")
    (seed / "main-only.txt").write_text("only on main\n", encoding="utf-8")
    git_in(seed, "add", "-A")
    git_in(seed, "commit", "-m", "main has moved on")
    main_commit = git_in(seed, "rev-parse", "HEAD").strip()
    git_in(seed, "push", str(origin), f"{main_commit}:refs/heads/main")
    checkout = tmp_path / "checkout"
    git_in(tmp_path, "clone", str(origin), str(checkout))
    git_in(checkout, "fetch", "--no-tags", "--quiet", "origin",
           "refs/pull/1/head")
    return GitStand(origin=origin, seed=seed, checkout=checkout,
                    base_commit=base_commit, head_commit=head_commit,
                    main_commit=main_commit)
