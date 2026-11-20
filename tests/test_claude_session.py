"""Where a Claude session was saved: the config directory, session ids and
the transcript's recorded working directory.

Every test runs against a temporary configuration directory through
CLAUDE_CONFIG_DIR; the real ~/.claude is never read and the claude CLI is
never invoked.
"""

import json
import os
from pathlib import Path

import pytest

from orchestrator.claude_cli import (
    claude_config_dir,
    is_session_id,
    session_start_directory,
)

SESSION_ID = "3f2b8a10-9c4d-4e5f-8a7b-1c2d3e4f5a6b"
OTHER_SESSION_ID = "7a1c9e20-5d3b-4f8a-b2c6-9e0d1a2b3c4f"


def _use_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config = tmp_path / "claude-config"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


def _write_transcript(config: Path, project_key: str, session_id: str,
                      lines: list[str]) -> Path:
    project = config / "projects" / project_key
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript


def test_session_id_canonical_is_true() -> None:
    assert SESSION_ID == "3f2b8a10-9c4d-4e5f-8a7b-1c2d3e4f5a6b"
    assert is_session_id(SESSION_ID) is True


@pytest.mark.parametrize(
    "value",
    [
        "3F2B8A10-9C4D-4E5F-8A7B-1C2D3E4F5A6B",  # uppercase
        "3f2b8a109c4d4e5f8a7b1c2d3e4f5a6b",  # hyphenless
        "*",
        "../x",
        "",
        "not-a-session-id",
    ],
)
def test_session_id_non_canonical_is_false(value: str) -> None:
    assert is_session_id(value) is False


def test_config_dir_env_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "somewhere"))
    assert claude_config_dir() == Path(str(tmp_path / "somewhere"))


def test_config_dir_unset_and_empty_read_as_home(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert claude_config_dir() == Path.home() / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    assert claude_config_dir() == Path.home() / ".claude"


def test_config_dir_read_at_call_time(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(first))
    assert claude_config_dir() == first
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(second))
    assert claude_config_dir() == second


def test_start_directory_reads_cwd_from_unrelated_project_dir(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    saved = tmp_path / "work" / "orchestrator"
    _write_transcript(
        config,
        "Users-jane-somewhere-else-entirely",
        SESSION_ID,
        # built with json.dumps, so a temporary root holding a quote, a
        # backslash or any other character JSON escapes changes nothing
        [json.dumps({"type": "user", "cwd": str(saved)}), '{"type": "result"}'],
    )
    assert session_start_directory(SESSION_ID) == saved


def test_start_directory_takes_the_first_cwd_entry(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    _write_transcript(
        config,
        "-home-jane-work",
        SESSION_ID,
        [
            "this line is not json",  # skipped: not JSON
            "[1, 2]",  # skipped: JSON but not an object
            '{"type": "session-owner"}',  # skipped: no cwd
            '{"type": "queue-operation", "cwd": null}',  # skipped: cwd not a string
            '{"type": "attachment", "cwd": ""}',  # skipped: cwd empty
            '{"type": "user", "cwd": "/first/dir"}',
            '{"type": "assistant", "cwd": "/later/dir"}',
        ],
    )
    assert session_start_directory(SESSION_ID) == Path("/first/dir")


def test_start_directory_cyrillic_cwd(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    saved = "/home/сакши/проекты/оркестратор"
    _write_transcript(
        config,
        "----------",
        SESSION_ID,
        ['{"type": "user", "cwd": "%s"}' % saved],
    )
    assert session_start_directory(SESSION_ID) == Path(saved)


def test_start_directory_cwd_longer_than_the_project_key_limit(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    saved = "/deep/" + ("section-" * 40) + "end"
    assert len(saved) > 200
    _write_transcript(
        config,
        "p" * 200 + "-0f0e0d0c",
        SESSION_ID,
        ['{"type": "user", "cwd": "%s"}' % saved],
    )
    assert session_start_directory(SESSION_ID) == Path(saved)


def test_start_directory_no_transcript_for_the_id_is_none(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    _write_transcript(
        config,
        "-home-jane-other",
        OTHER_SESSION_ID,
        ['{"type": "user", "cwd": "/other/session/dir"}'],
    )
    assert session_start_directory(SESSION_ID) is None


def test_start_directory_two_transcripts_is_none(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    _write_transcript(
        config,
        "-home-jane-one",
        SESSION_ID,
        ['{"type": "user", "cwd": "/first/project/dir"}'],
    )
    _write_transcript(
        config,
        "-home-jane-two",
        SESSION_ID,
        ['{"type": "user", "cwd": "/second/project/dir"}'],
    )
    assert session_start_directory(SESSION_ID) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_start_directory_unreadable_transcript_is_none(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    transcript = _write_transcript(
        config,
        "-home-jane-work",
        SESSION_ID,
        ['{"type": "user", "cwd": "/saved/dir"}'],
    )
    transcript.chmod(0o000)
    try:
        assert session_start_directory(SESSION_ID) is None
    finally:
        transcript.chmod(0o644)


def test_start_directory_transcript_without_cwd_is_none(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    _write_transcript(
        config,
        "-home-jane-work",
        SESSION_ID,
        ['{"type": "session-owner"}', '{"type": "queue-operation"}', "not json"],
    )
    assert session_start_directory(SESSION_ID) is None


def test_start_directory_rejects_ids_that_never_reach_a_glob(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    _write_transcript(
        config,
        "-home-jane-work",
        SESSION_ID,
        ['{"type": "user", "cwd": "/saved/dir"}'],
    )
    # A transcript exists, so "*" or "../x" reaching the glob would match it.
    assert session_start_directory("*") is None
    assert session_start_directory("../x") is None


def test_start_directory_ignores_siblings_named_after_the_id(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    transcript = _write_transcript(
        config,
        "-home-jane-work",
        SESSION_ID,
        ['{"type": "user", "cwd": "/saved/dir"}'],
    )
    # Claude's real layout: a directory named by the session id sits beside
    # the transcript, and stray files collect the same prefix.
    (transcript.parent / SESSION_ID).mkdir()
    (transcript.parent / f"{SESSION_ID}.jsonl.bak").write_text(
        '{"type": "user", "cwd": "/from/backup"}', encoding="utf-8")
    assert session_start_directory(SESSION_ID) == Path("/saved/dir")


def test_start_directory_undecodable_transcript_is_none(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config_dir(monkeypatch, tmp_path)
    transcript = _write_transcript(
        config,
        "-home-jane-work",
        SESSION_ID,
        ['{"type": "user", "cwd": "/after/the/bad/bytes"}'],
    )
    transcript.write_bytes(b"\xff\xfe not utf-8\n" + transcript.read_bytes())
    assert session_start_directory(SESSION_ID) is None
