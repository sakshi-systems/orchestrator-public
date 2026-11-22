"""The user-slug derived once in the orchestrator: the rule that names a
person's roadmap and their run settings from the repository's git
identity, held in the orchestrator's own code so an installed copy needs
no skills package — the skills' user-slug.sh is the other implementation
of the same rule, and both follow it in lockstep.

The rule, docs/features/named-roadmaps.md § "What it is" its governing
text: the local part of git user.email before its first ``@``, then git
user.name where that yields nothing, lowercased in ASCII ``A``–``Z``
alone — never by Unicode case mapping, which reads the Kelvin sign as
``k`` and İ as an ``i`` plus a combining dot, where the rule reads both
as a separator — every run of characters outside ``a-z0-9`` one ``-``,
leading and trailing ``-`` stripped; what yields nothing after all that
is no slug at all.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .git_runner import run_git_sync


# The part of the no-slug halt text every caller shares: a fixed first
# line, then the line naming both settings. A caller whose fix is its
# own appends that caller's line after this text.
USER_SLUG_MISSING = (
    "No git identity yields a user slug\n"
    "set git user.email or user.name."
)


def git_config_value(project_dir: Path, key: str) -> str | None:
    """Read a single git config value in project_dir; None on any failure or empty value."""
    try:
        result = run_git_sync(["config", key], cwd=project_dir)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


_ASCII_LOWER = {code: code + 32 for code in range(ord("A"), ord("Z") + 1)}
_OUTSIDE_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    """The rule over one value: ASCII letters lowered, every run of
    characters outside a-z0-9 one `-`, leading and trailing `-`
    stripped."""
    lowered = value.translate(_ASCII_LOWER)
    return _OUTSIDE_SLUG_CHARS.sub("-", lowered).strip("-")


def user_slug(project_dir: Path) -> str | None:
    """Derive the user-slug from the git identity in project_dir: the
    local part of user.email before its first `@`, then user.name the
    same way where that yields nothing, then None."""
    email = git_config_value(project_dir, "user.email")
    slug = _slugify(email.split("@", 1)[0]) if email else ""
    if not slug:
        name = git_config_value(project_dir, "user.name")
        if name:
            slug = _slugify(name)
    return slug or None
