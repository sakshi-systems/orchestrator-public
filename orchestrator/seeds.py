"""Sakshi lays its roles' seeds itself when it prepares its
environment: the roles only read them.

A seed that names a source is cloned over git into its empty path, or,
when a copy is already there, pointed at its source and then fetched
and reset to the source's head, once per distinct directory and always
through git_runner, so a seed changes for the roles only when Sakshi
restarts, never while a review runs. A laid seed's directory ends
0o755 while its files take run_git's umask.
"""

from __future__ import annotations

from pathlib import Path

from .git_runner import GitError, run_git
from .sakshi_settings import SakshiSettings

_TIMEOUT = 300.0


async def lay_seeds(settings: SakshiSettings) -> None:
    """Lay every seed a role or a musafir gives a source for, once per
    distinct directory.

    A directory that is absent or empty is cloned from its source; a
    directory holding `.git` is pointed at its source and then fetched
    and reset to the source's head; anything else stops Sakshi naming
    the path. A seed that cannot be laid stops Sakshi naming the path
    and git's text. A laid seed's directory ends 0o755 while its files
    take run_git's umask.
    """
    seen: set[Path] = set()
    for repository in settings.repositories.values():
        team = repository.team
        roles = list(team.roles.values())
        if team.musafir is not None:
            roles.append(team.musafir)
        for role in roles:
            if role.source is None:
                continue
            identity = role.seed_identity()
            if identity in seen:
                continue
            seen.add(identity)
            await _lay_one(identity, role.source)


async def _lay_one(seed: Path, source: str) -> None:
    try:
        if (seed / ".git").exists():
            await _git(["remote", "set-url", "origin", "--", source], seed)
            await _git(["fetch", "--quiet", "origin", "HEAD"], seed)
            await _git(["reset", "--hard", "--quiet", "FETCH_HEAD"], seed)
        elif seed.exists() and (not seed.is_dir()
                                or any(seed.iterdir())):
            raise SystemExit(
                f"Seed {seed} is neither empty nor a repository")
        else:
            await _git(["clone", "--quiet", "--", source, str(seed)],
                       Path("/"))
    except GitError as e:
        raise SystemExit(f"Seed {seed} cannot be laid: {e.text}") from None
    try:
        seed.chmod(0o755)
    except OSError as e:
        raise SystemExit(
            f"Seed {seed} cannot be made readable by the role "
            f"users: {e}") from None


async def _git(arguments: list[str], cwd: Path) -> str:
    return await run_git(arguments, cwd=cwd, timeout=_TIMEOUT)
