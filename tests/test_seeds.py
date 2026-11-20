"""Sakshi lays the seeds it is given sources for: a clone into an
empty path, an update to the source's new head, one laying per path,
and the refusals — against a local bare origin."""

from __future__ import annotations

import asyncio
import os
import pathlib
import stat
from pathlib import Path

import pytest

from orchestrator import seeds
from orchestrator.sakshi_settings import (ReviewedRepository, ReviewTeam,
                                          RoleSettings, SakshiSettings)
from orchestrator.seeds import lay_seeds

from tests.conftest import GitStand, git_in


def _role(seed: Path, source: str | None = None) -> RoleSettings:
    return RoleSettings(seed=seed, user="review_01", model="claude-3",
                        effort="high", head="01", source=source)


def _settings(roles: list[RoleSettings],
              musafir: RoleSettings | None = None) -> SakshiSettings:
    team = ReviewTeam(memory=Path("/srv/memory"), deadline_seconds=30,
                      roles={f"role-{index}": role
                             for index, role in enumerate(roles)},
                      musafir=musafir)
    return SakshiSettings(
        workspace_root=Path("/srv/sakshi"),
        repositories={"owner/name": ReviewedRepository(
            checkout=Path("/srv/checkouts/name"), team=team)})


def test_a_seed_is_cloned_into_an_empty_directory(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    asyncio.run(scenario())
    assert (seed / ".git").exists()
    assert (seed / "file.txt").read_text(encoding="utf-8") == \
        "the first content\n"


def test_a_seed_absent_is_cloned_too(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    asyncio.run(scenario())
    assert (seed / "file.txt").exists()


def test_an_update_moves_a_seed_to_the_remotes_new_head(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))
        first = (seed / "file.txt").read_text(encoding="utf-8")
        git_in(git_stand.seed, "checkout", "main")
        (git_stand.seed / "file.txt").write_text("a second content\n",
                                                 encoding="utf-8")
        git_in(git_stand.seed, "add", "-A")
        git_in(git_stand.seed, "commit", "-m", "the seed moves on")
        git_in(git_stand.seed, "push", str(git_stand.origin), "main")
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))
        return first

    first = asyncio.run(scenario())
    assert first == "the first content\n"
    assert (seed / "file.txt").read_text(encoding="utf-8") == \
        "a second content\n"


def test_two_roles_on_one_seed_directory_lay_once(
        tmp_path: Path, git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()
    real = seeds._lay_one
    calls: list = []

    async def recording(seed_path: Path, source: str) -> None:
        calls.append((seed_path, source))
        await real(seed_path, source)

    monkeypatch.setattr(seeds, "_lay_one", recording)

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin)),
                                   _role(seed, str(git_stand.origin))]))

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0] == (seed.resolve(), str(git_stand.origin))
    assert (seed / "file.txt").exists()


def test_two_spellings_of_one_seed_directory_lay_once(
        tmp_path: Path, git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    seeds_dir = tmp_path / "seeds"
    (seeds_dir / "reviewer-1").mkdir(parents=True)
    real = seeds._lay_one
    calls: list = []

    async def recording(seed_path: Path, source: str) -> None:
        calls.append(seed_path)
        await real(seed_path, source)

    monkeypatch.setattr(seeds, "_lay_one", recording)

    async def scenario() -> None:
        await lay_seeds(_settings([
            _role(seeds_dir / "reviewer-1", str(git_stand.origin)),
            _role(seeds_dir / ".." / "seeds" / "reviewer-1",
                  str(git_stand.origin))]))

    asyncio.run(scenario())
    assert calls == [(seeds_dir / "reviewer-1").resolve()]
    assert (seeds_dir / "reviewer-1" / "file.txt").exists()


def test_an_alias_and_its_target_lay_once(
        tmp_path: Path, git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    real_dir = tmp_path / "real"
    (real_dir / "reviewer-1").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real_dir)
    real = seeds._lay_one
    calls: list = []

    async def recording(seed_path: Path, source: str) -> None:
        calls.append(seed_path)
        await real(seed_path, source)

    monkeypatch.setattr(seeds, "_lay_one", recording)

    async def scenario() -> None:
        await lay_seeds(_settings([
            _role(real_dir / "reviewer-1", str(git_stand.origin)),
            _role(alias / "reviewer-1", str(git_stand.origin))]))

    asyncio.run(scenario())
    assert calls == [(real_dir / "reviewer-1").resolve()]
    assert (real_dir / "reviewer-1" / "file.txt").exists()


def _other_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "other-origin.git"
    git_in(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "other-work"
    git_in(tmp_path, "clone", str(origin), str(work))
    (work / "other.txt").write_text("the other content\n", encoding="utf-8")
    git_in(work, "add", "-A")
    git_in(work, "commit", "-m", "an unrelated history")
    git_in(work, "push", str(origin), "main")
    return origin


def test_a_changed_source_moves_the_seed_to_the_new_origin(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()
    other = _other_origin(tmp_path)

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))
        await lay_seeds(_settings([_role(seed, str(other))]))

    asyncio.run(scenario())
    assert (seed / "other.txt").read_text(encoding="utf-8") == \
        "the other content\n"
    assert not (seed / "file.txt").exists()
    assert git_in(seed, "remote", "get-url", "origin").strip() == str(other)


def test_a_seed_with_no_origin_remote_stops_the_start(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()
    git_in(seed, "init", "-b", "main")

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    with pytest.raises(SystemExit) as raised:
        asyncio.run(scenario())
    assert str(seed) in str(raised.value)
    assert "No such remote" in str(raised.value)


def test_a_seed_with_absent_parents_is_cloned(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "a" / "b" / "reviewer-1"

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    asyncio.run(scenario())
    assert (seed / ".git").exists()
    assert (seed / "file.txt").exists()
    for created in (tmp_path / "a", tmp_path / "a" / "b", seed):
        assert stat.S_IMODE(created.stat().st_mode) == 0o755


def test_an_empty_seed_directory_mode_0700_ends_0755(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()
    seed.chmod(0o700)

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    asyncio.run(scenario())
    assert stat.S_IMODE(seed.stat().st_mode) == 0o755
    assert stat.S_IMODE((seed / "file.txt").stat().st_mode) == 0o644


def test_a_laid_seed_set_to_0700_ends_0755_after_a_refresh(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))
        seed.chmod(0o700)
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    asyncio.run(scenario())
    assert stat.S_IMODE(seed.stat().st_mode) == 0o755


def test_an_empty_seed_directory_mode_0777_ends_0755(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()
    seed.chmod(0o777)

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    asyncio.run(scenario())
    assert stat.S_IMODE(seed.stat().st_mode) == 0o755


def test_a_seed_directory_that_cannot_be_chmodded_stops_the_start(
        tmp_path: Path, git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()

    def raising(self: Path, mode: int) -> None:
        raise PermissionError(1, "not the owner")

    monkeypatch.setattr(pathlib.Path, "chmod", raising)

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    with pytest.raises(SystemExit) as raised:
        asyncio.run(scenario())
    assert str(seed) in str(raised.value)
    assert "cannot be made readable" in str(raised.value)


def test_a_populated_directory_with_no_git_is_refused(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()
    (seed / "stray.txt").write_text("not a repository\n", encoding="utf-8")

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    with pytest.raises(SystemExit) as raised:
        asyncio.run(scenario())
    assert str(seed) in str(raised.value)


def test_an_unreachable_source_stops_the_start_naming_the_path(
        tmp_path: Path) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()
    missing = tmp_path / "no-such-origin.git"

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(missing))]))

    with pytest.raises(SystemExit) as raised:
        asyncio.run(scenario())
    assert str(seed) in str(raised.value)
    assert "clone" in str(raised.value) or "repository" in str(raised.value)


def test_the_seed_is_readable_by_others_under_umask_077(
        tmp_path: Path, git_stand: GitStand) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, str(git_stand.origin))]))

    old = os.umask(0o077)
    try:
        asyncio.run(scenario())
    finally:
        os.umask(old)
    assert stat.S_IMODE(seed.stat().st_mode) == 0o755
    assert stat.S_IMODE((seed / "file.txt").stat().st_mode) == 0o644
    assert stat.S_IMODE((seed / ".git").stat().st_mode) == 0o755


def test_a_role_without_a_source_is_not_laid(
        tmp_path: Path, git_stand: GitStand,
        monkeypatch: pytest.MonkeyPatch) -> None:
    seed = tmp_path / "seeds" / "reviewer-1"
    seed.parent.mkdir()
    seed.mkdir()
    real = seeds.run_git
    calls: list = []

    async def counting(arguments, *, cwd, timeout):
        calls.append(arguments[0])
        return await real(arguments, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(seeds, "run_git", counting)

    async def scenario() -> None:
        await lay_seeds(_settings([_role(seed, None)]))

    asyncio.run(scenario())
    assert calls == []
    assert list(seed.iterdir()) == []
