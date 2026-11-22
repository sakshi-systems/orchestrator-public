"""Unit tests for the user-slug derivation: the family's rule, pinned by value —
a wrong derivation would name another file with no signal."""

import subprocess

import pytest

from orchestrator import user_slug as user_slug_module
from orchestrator.user_slug import USER_SLUG_MISSING, user_slug


def _identity(monkeypatch, email=None, name=None):
    """Patch the git config reads with one run_git_sync stand-in answering
    user.email and user.name as git would (exit 1 for a key that is not set)."""
    def _config(arguments, *, cwd, timeout=None):
        key = arguments[-1]
        value = email if key == "user.email" else name if key == "user.name" else None
        if value is None:
            return subprocess.CompletedProcess(["git", *arguments], 1, "", "")
        return subprocess.CompletedProcess(["git", *arguments], 0, value, "")

    monkeypatch.setattr(user_slug_module, "run_git_sync", _config)


@pytest.mark.parametrize(
    ("email", "name", "expected"),
    [
        # Upper case in the local part is lowered; the domain never enters the slug.
        ("John.Doe@Example.COM", None, "john-doe"),
        # A `+` and dots in the local part are separators, like every character outside a-z0-9.
        ("john.doe+orchestrator@example.com", None, "john-doe-orchestrator"),
        # Runs of separators collapse to one `-`.
        ("john...doe__x@example.com", None, "john-doe-x"),
        # Leading and trailing separators are stripped.
        ("._john.doe_.@example.com", None, "john-doe"),
        # An email that yields a slug wins; the name is never asked.
        ("jane.doe@example.com", "John Roe", "jane-doe"),
        # An email whose local part yields nothing falls back to the name.
        ("@example.com", "Jane Roe", "jane-roe"),
        ("!!!@example.com", "Jane Roe", "jane-roe"),
        # A name with spaces: the whole name, each space one separator in the run.
        (None, "Alice van der Berg", "alice-van-der-berg"),
        # İ (U+0130) is lowered never — it is outside ASCII A-Z, so it joins a
        # separator run; Unicode case mapping would read it as `i` plus a dot.
        ("İstanbul@example.com", None, "stanbul"),
        # The Kelvin sign (U+212A) is outside ASCII A-Z and becomes a separator;
        # Unicode case mapping would read it as `k`.
        (None, "\u212aelvin", "elvin"),
        # An accented letter is outside a-z0-9 and becomes part of a separator run.
        ("josé@example.com", None, "jos"),
        # Neither setting present: no slug.
        (None, None, None),
    ],
)
def test_user_slug_pins_the_rule_by_value(monkeypatch, tmp_path, email, name, expected):
    _identity(monkeypatch, email=email, name=name)
    assert user_slug(tmp_path) == expected


def test_the_no_slug_text_is_one_fixed_first_line_and_a_line_naming_both_settings():
    lines = USER_SLUG_MISSING.splitlines()
    assert len(lines) == 2
    assert lines[0] == "No git identity yields a user slug"
    assert "user.email" in lines[1]
    assert "user.name" in lines[1]
