"""The run's tree found by one rule: the project's repository and, where
`.ai-factory` resolves into a different git toplevel (a link into another
repository, or a repository of its own), that repository, of which the run
takes only the folder `.ai-factory` resolves to.

The rule, docs/reference/target-project.md § "Git repository" its governing
text: the other repository enters the tree only where ``git rev-parse
--show-toplevel`` in the project's directory and in ``<project>/.ai-factory``
both exit 0 and name toplevels that differ through ``Path.resolve()`` — git
prints a physical path, so the comparison is by resolved path, never by the
strings, which read one repository reached through a symlinked ancestor as
two. The staging of ``roadmap-prune`` — ``add -A`` where the toplevels are
equal, ``add -A .`` where they differ, taking every change within the folder
`.ai-factory` resolves to and excluding the rest of its owning repository —
is the other implementation of the same rule, and both follow it in
lockstep.
"""

from __future__ import annotations

from pathlib import Path

from .git_runner import run_git_sync
from .ports import Repository


def _toplevel(cwd: Path) -> Path | None:
    """The toplevel of the repository `cwd` lies in, resolved; None where
    `cwd` lies in no repository, where the call cannot run — a directory
    that does not exist among them — or where git exits non-zero."""
    try:
        result = run_git_sync(["rev-parse", "--show-toplevel"], cwd=cwd)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.removesuffix("\n")
    if not text:
        return None
    return Path(text).resolve()


def _folder(cwd: Path) -> str | None:
    """The `git rev-parse --show-prefix` text of `cwd`, its trailing slash
    included and empty at a toplevel of its own; None where the call cannot
    run or git exits non-zero."""
    try:
        result = run_git_sync(["rev-parse", "--show-prefix"], cwd=cwd)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.removesuffix("\n")


def find_run_tree(project_dir: Path) -> tuple[Repository, ...]:
    """Find the run's tree from the tree on disk: the project's repository
    first, and — where `git rev-parse --show-toplevel` in the project's
    directory and in `<project>/.ai-factory` both exit 0 and the two
    toplevels, each through `Path.resolve()`, differ — the repository
    `.ai-factory` leads to after it, of which the run takes only the folder
    `.ai-factory` resolves to.

    A non-zero exit of either call, an `OSError` (a `.ai-factory` that does
    not exist among them) and equal toplevels leave the project's repository
    alone; a call that passes its bound surfaces as the
    `subprocess.TimeoutExpired` it is.
    """
    project = Repository(directory=project_dir, folder=None)
    factory = project_dir / ".ai-factory"
    project_root = _toplevel(project_dir)
    if project_root is None:
        return (project,)
    other_root = _toplevel(factory)
    if other_root is None or other_root == project_root:
        return (project,)
    folder = _folder(factory)
    if folder is None:
        return (project,)
    return (project, Repository(directory=factory, folder=folder))
