"""Named-roadmap resolution at assembly: the roadmap in play, its test sibling, and the per-roadmap artifact subdirectory."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import OrchestratorConfig
from .errors import HaltError


_USER_SLUG_SCRIPT = Path.home() / ".claude" / "skills" / "roadmap-engine" / "scripts" / "user-slug.sh"


def _external_evidence_path(project_dir: Path, candidate: Path) -> Path:
    """Resolve an evidence destination outside the target's Git worktree."""
    try:
        target = project_dir.resolve()
        destination = candidate.resolve()
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
            capture_output=True,
        )
        root_bytes = result.stdout.removesuffix(b"\n")
        if result.returncode != 0 or not root_bytes:
            raise ValueError(f"Cannot resolve target Git worktree: {target}")
        root = Path(os.fsdecode(root_bytes)).resolve()
        if destination.is_relative_to(root):
            raise ValueError(f"Evidence path must be outside target Git worktree {root}: {destination}")
        return destination
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
        raise ValueError(f"Invalid external evidence path: {e}") from e


def _git_config_value(project_dir: Path, key: str) -> str | None:
    """Read a single git config value in project_dir; None on any failure or empty value."""
    result = subprocess.run(
        ["git", "config", key], cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _resolve_roadmap_relpath(config: OrchestratorConfig, project_dir: Path) -> str:
    """Three explicit states keyed on config.roadmap_path: absent, 'my', or an explicit path."""
    if config.roadmap_path is None:
        return "ROADMAP.md"

    if config.roadmap_path == "my":
        email = _git_config_value(project_dir, "user.email")
        name = _git_config_value(project_dir, "user.name")
        if not _USER_SLUG_SCRIPT.exists():
            raise HaltError(f"User-slug script not found\n{_USER_SLUG_SCRIPT}")
        try:
            result = subprocess.run(
                [str(_USER_SLUG_SCRIPT)], cwd=project_dir, capture_output=True, text=True,
            )
        except OSError as e:
            raise HaltError(f"User-slug script failed\n{e}") from e
        if result.returncode == 0:
            slug = result.stdout.strip()
        elif result.returncode == 2:
            raise HaltError(
                "roadmap_path is 'my' but no git identity is set — "
                "set git user.email or user.name, or use an explicit roadmap_path."
            )
        else:
            raise HaltError(f"User-slug script failed\n{result.stderr.strip()}")
        relpath = f"roadmaps/{slug}.md"
        roadmap_file = project_dir / ".ai-factory" / relpath
        if not roadmap_file.exists():
            raise HaltError(
                f"Named roadmap not found\n"
                f".ai-factory/{relpath}"
            )

        lines = roadmap_file.read_text().splitlines()
        first_line = lines[0] if lines else ""
        identity = email or name
        expected_owner = f"> Owner: {identity}"
        if first_line.strip() != expected_owner:
            raise HaltError(
                f"Named roadmap owner line does not match the current git identity\n"
                f"{relpath}: {first_line!r} vs. expected {expected_owner!r}"
            )
        return relpath

    return config.roadmap_path


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
