"""Named-roadmap resolution at assembly: the roadmap in play, its test sibling, and the per-roadmap artifact subdirectory."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import OrchestratorConfig
from .errors import HaltError
from .git_runner import run_git_sync
from .user_slug import USER_SLUG_MISSING, git_config_value, user_slug


def _external_evidence_path(project_dir: Path, candidate: Path) -> Path:
    """Resolve an evidence destination outside the target's Git worktree."""
    try:
        target = project_dir.resolve()
        destination = candidate.resolve()
        result = run_git_sync(["rev-parse", "--show-toplevel"], cwd=target)
        root_text = result.stdout.removesuffix("\n")
        if result.returncode != 0 or not root_text:
            raise ValueError(f"Cannot resolve target Git worktree: {target}")
        root = Path(root_text).resolve()
        if destination.is_relative_to(root):
            raise ValueError(f"Evidence path must be outside target Git worktree {root}: {destination}")
        return destination
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
        raise ValueError(f"Invalid external evidence path: {e}") from e


def _resolve_roadmap_relpath(config: OrchestratorConfig, project_dir: Path, roadmap: str | None = None) -> str:
    """Three explicit states keyed on the roadmap value: absent, 'my', or an
    explicit path.

    The value is `roadmap` — the ``--roadmap`` option's — where one is given,
    and the configured `roadmap_path` where none is, so a run of the project
    and a command over its data take the same roadmap.
    """
    value = roadmap if roadmap is not None else config.roadmap_path
    if value is None:
        return "ROADMAP.md"

    if value == "my":
        slug = user_slug(project_dir)
        if slug is None:
            raise HaltError(
                f"{USER_SLUG_MISSING}\n"
                f"or set roadmap_path to an explicit path."
            )
        relpath = f"roadmaps/{slug}.md"
        roadmap_file = project_dir / ".ai-factory" / relpath
        if not roadmap_file.exists():
            raise HaltError(
                f"Named roadmap not found\n"
                f".ai-factory/{relpath}"
            )

        lines = roadmap_file.read_text().splitlines()
        first_line = lines[0] if lines else ""
        identity = (
            git_config_value(project_dir, "user.email")
            or git_config_value(project_dir, "user.name")
        )
        expected_owner = f"> Owner: {identity}"
        if first_line.strip() != expected_owner:
            raise HaltError(
                f"Named roadmap owner line does not match the current git identity\n"
                f"{relpath}: {first_line!r} vs. expected {expected_owner!r}"
            )
        return relpath

    return value


def _tests_sibling(relpath: str) -> str:
    """Derive the test-roadmap sibling path from the roadmap in play."""
    if relpath == "ROADMAP.md":
        return "ROADMAP_TESTS.md"
    path = Path(relpath)
    return str(path.parent / f"{path.stem}-tests{path.suffix}")


def _artifact_subdir(relpath: str) -> str | None:
    """Key artifact dirs by the roadmap file's stem; the default pair stays flat (None)."""
    if relpath in ("ROADMAP.md", "ROADMAP_TESTS.md"):
        return None
    stem = Path(relpath).stem.removesuffix("-tests")
    return stem or Path(relpath).stem
