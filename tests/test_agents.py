"""Unit tests for _has_signal, TestRunner._extract_test_command, _resolve_claude,
sidecar session I/O (_read_sessions/_write_session), and kill_active_child."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from orchestrator import claude_cli, process_control, state
from orchestrator.agents import (
    Implementer,
    PlanReviewer,
    PlannerReviewer,
    TestRunner,
    _load_prompt,
)
from orchestrator.claude_cli import ClaudeAgentBackend, _classify_result
from orchestrator.process_control import kill_active_child
from orchestrator.errors import EscalationError, HaltError, MissingArtifactError, RateLimitError
from orchestrator.protocol import (
    _escalation_excerpt,
    _has_escalation,
    _has_signal,
    _read_sessions,
    _write_session,
    _write_sessions,
)
from orchestrator.ports import AgentRole, RoleSetting
from tests.fakes import FakeAgentBackend, silent_backend, target_path, writing_backend


# ---------------------------------------------------------------------------
# Task 1: match within the last-5-line window
# ---------------------------------------------------------------------------


def test_signal_on_last_line():
    """Signal is the exact last line — most common case."""
    text = "some preamble\nmore text\nREVIEW_PASS"
    assert _has_signal(text, "REVIEW_PASS") is True


def test_signal_on_line_3_of_5():
    """Signal is on line 3 of 5: still inside the last-5 window."""
    lines = ["line1", "line2", "REVIEW_PASS", "line4", "line5"]
    assert _has_signal("\n".join(lines), "REVIEW_PASS") is True


def test_signal_on_line_6_of_10():
    """Signal is the first line inside the last-5 window of a 10-line text."""
    # Lines 1-5 (indices 0-4) are outside [-5:]; line 6 (index 5) is first inside.
    lines = [
        "line1", "line2", "line3", "line4", "line5",
        "REVIEW_PASS",  # index 5 — first line of [-5:]
        "line7", "line8", "line9", "line10",
    ]
    assert _has_signal("\n".join(lines), "REVIEW_PASS") is True


def test_signal_is_plan_review_pass():
    """Function is signal-agnostic: PLAN_REVIEW_PASS works the same way."""
    text = "planning notes\nPLAN_REVIEW_PASS"
    assert _has_signal(text, "PLAN_REVIEW_PASS") is True


# ---------------------------------------------------------------------------
# Task 2: window exclusion and exact-match rejection
# ---------------------------------------------------------------------------


def test_signal_on_line_5_of_10_excluded():
    """Signal is on line 5 (index 4) of a 10-line text — just outside [-5:]."""
    lines = [
        "line1", "line2", "line3", "line4",
        "REVIEW_PASS",  # index 4 — excluded by [-5:]
        "line6", "line7", "line8", "line9", "line10",
    ]
    assert _has_signal("\n".join(lines), "REVIEW_PASS") is False


def test_signal_as_substring_rejected():
    """Signal embedded in a longer line must NOT match (strip+equality check)."""
    text = "some text\nno REVIEW_PASS here\nmore text"
    assert _has_signal(text, "REVIEW_PASS") is False


def test_signal_with_surrounding_whitespace():
    """A line with leading/trailing whitespace still matches after strip()."""
    text = "preamble\n  REVIEW_PASS  \ntrailer"
    assert _has_signal(text, "REVIEW_PASS") is True


def test_empty_text_returns_false():
    """Empty string has no lines; any() over an empty iterable is False."""
    assert _has_signal("", "REVIEW_PASS") is False


# ---------------------------------------------------------------------------
# Task 1: backtick-wrapped command
# ---------------------------------------------------------------------------


def test_extract_test_command_backtick_wrapped(tmp_path):
    """Should return the command without backticks when the command line is wrapped in single backticks."""
    p = tmp_path / "plan.md"
    p.write_text("## Test Command\n`uv run pytest tests/ -v`")
    assert TestRunner._extract_test_command(p) == "uv run pytest tests/ -v"


# ---------------------------------------------------------------------------
# Task 2: bare command without backticks
# ---------------------------------------------------------------------------


def test_extract_test_command_bare(tmp_path):
    """Should return the command string as-is when the command line has no backticks."""
    p = tmp_path / "plan.md"
    p.write_text("## Test Command\nuv run pytest tests/ -v")
    assert TestRunner._extract_test_command(p) == "uv run pytest tests/ -v"


# ---------------------------------------------------------------------------
# Task 3: command after intervening blank lines
# ---------------------------------------------------------------------------


def test_extract_test_command_blank_lines(tmp_path):
    """Should return the first non-empty non-heading line when blank lines follow the heading."""
    p = tmp_path / "plan.md"
    p.write_text("## Test Command\n\n\nuv run pytest -v")
    assert TestRunner._extract_test_command(p) == "uv run pytest -v"


# ---------------------------------------------------------------------------
# Task 4: heading absent
# ---------------------------------------------------------------------------


def test_extract_test_command_no_heading(tmp_path):
    """Should return None when the plan has no Test Command heading."""
    p = tmp_path / "plan.md"
    p.write_text("# Some Plan\n## Other Section\nrun this")
    assert TestRunner._extract_test_command(p) is None


# ---------------------------------------------------------------------------
# Task 5: empty section before next heading
# ---------------------------------------------------------------------------


def test_extract_test_command_empty_section(tmp_path):
    """Should return None when the Test Command section is blank up to the next heading."""
    p = tmp_path / "plan.md"
    p.write_text("## Test Command\n\n## Next Section\nuv run pytest")
    assert TestRunner._extract_test_command(p) is None


# ---------------------------------------------------------------------------
# --- _resolve_claude ---
# ---------------------------------------------------------------------------


def _make_claude_bin(node_dir: Path) -> Path:
    """Create `node_dir/bin/claude` as an empty file and return its path.

    Content is irrelevant — `_resolve_claude` only calls `.exists()` on it.
    """
    bin_dir = node_dir / "bin"
    bin_dir.mkdir(parents=True)
    claude_bin = bin_dir / "claude"
    claude_bin.write_text("")
    return claude_bin


# ---------------------------------------------------------------------------
# Task 2: PATH-hit tests
# ---------------------------------------------------------------------------


def test_resolve_claude_path_hit(monkeypatch):
    """Returns the PATH-resolved path directly when shutil.which finds it."""
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert claude_cli._resolve_claude() == "/usr/local/bin/claude"


def test_resolve_claude_path_hit_short_circuits_nvm(monkeypatch, tmp_path):
    """A PATH hit short-circuits the nvm fallback — no FileNotFoundError even
    though the patched home has no `.nvm` at all."""
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli.Path, "home", lambda: tmp_path / "nonexistent_home")
    assert claude_cli._resolve_claude() == "/usr/local/bin/claude"


# ---------------------------------------------------------------------------
# Task 3: not-found tests -> FileNotFoundError with install message
# ---------------------------------------------------------------------------


def test_resolve_claude_no_nvm_dir_raises(monkeypatch, tmp_path):
    """Raises when the fake home exists but has no `.nvm` inside it at all."""
    fakehome = tmp_path / "fakehome"
    fakehome.mkdir()
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli.Path, "home", lambda: fakehome)
    with pytest.raises(FileNotFoundError, match="npm install -g @anthropic-ai/claude-code"):
        claude_cli._resolve_claude()


def test_resolve_claude_empty_node_versions_dir_raises(monkeypatch, tmp_path):
    """Raises when `.nvm/versions/node/` exists but is empty."""
    fakehome = tmp_path / "fakehome"
    (fakehome / ".nvm" / "versions" / "node").mkdir(parents=True)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli.Path, "home", lambda: fakehome)
    with pytest.raises(FileNotFoundError, match="npm install -g @anthropic-ai/claude-code"):
        claude_cli._resolve_claude()


def test_resolve_claude_no_bin_claude_in_any_version_raises(monkeypatch, tmp_path):
    """Raises when version dirs exist but none has a `bin/claude` file."""
    fakehome = tmp_path / "fakehome"
    node_dir = fakehome / ".nvm" / "versions" / "node"
    (node_dir / "v18.0.0").mkdir(parents=True)
    (node_dir / "v20.1.0").mkdir(parents=True)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli.Path, "home", lambda: fakehome)
    with pytest.raises(FileNotFoundError, match="npm install -g @anthropic-ai/claude-code"):
        claude_cli._resolve_claude()


# ---------------------------------------------------------------------------
# Task 4: nvm candidate selection -- multi-candidate, partial-install,
# non-dir tolerance
# ---------------------------------------------------------------------------


def test_resolve_claude_multi_candidate_returns_highest(monkeypatch, tmp_path):
    """When multiple version dirs each have `bin/claude`, and the sort happens
    to agree with semver order, the highest-sorting one is returned."""
    fakehome = tmp_path / "fakehome"
    node_dir = fakehome / ".nvm" / "versions" / "node"
    _make_claude_bin(node_dir / "v18.0.0")
    v20 = _make_claude_bin(node_dir / "v20.1.0")
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli.Path, "home", lambda: fakehome)
    assert claude_cli._resolve_claude() == str(v20)


def test_resolve_claude_partial_install_skipped(monkeypatch, tmp_path):
    """A version dir with no `bin/claude` is skipped in favor of the next
    candidate that has one, with no raise."""
    fakehome = tmp_path / "fakehome"
    node_dir = fakehome / ".nvm" / "versions" / "node"
    (node_dir / "v20.1.0").mkdir(parents=True)
    v18 = _make_claude_bin(node_dir / "v18.0.0")
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli.Path, "home", lambda: fakehome)
    assert claude_cli._resolve_claude() == str(v18)


def test_resolve_claude_non_dir_entry_tolerated(monkeypatch, tmp_path):
    """A stray non-version entry (unparseable name, sorts last) is
    tolerated without a crash, and the valid version dir is still
    returned."""
    fakehome = tmp_path / "fakehome"
    node_dir = fakehome / ".nvm" / "versions" / "node"
    node_dir.mkdir(parents=True)
    (node_dir / "zzstray").write_text("")  # unparseable name, sorts last / is skipped
    v18 = _make_claude_bin(node_dir / "v18.0.0")
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli.Path, "home", lambda: fakehome)
    assert claude_cli._resolve_claude() == str(v18)


# ---------------------------------------------------------------------------
# _resolve_claude: semver-aware ordering of nvm version directories
# ---------------------------------------------------------------------------


def test_resolve_claude_semver_ordering_picks_true_latest(monkeypatch, tmp_path):
    """The true-latest version wins over a lexicographically larger name:
    v20.11.0 beats v9.0.0. A plain name sort would pick v9.0.0, so this pins
    that the nvm directory pick compares version numbers, not strings."""
    fakehome = tmp_path / "fakehome"
    node_dir = fakehome / ".nvm" / "versions" / "node"
    _make_claude_bin(node_dir / "v9.0.0")
    v20 = _make_claude_bin(node_dir / "v20.11.0")
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli.Path, "home", lambda: fakehome)
    assert claude_cli._resolve_claude() == str(v20)


# ---------------------------------------------------------------------------
# Task 2: _sorted_nvm_node_dirs -- direct unit cases on the pure helper
# ---------------------------------------------------------------------------


def test_sorted_nvm_node_dirs_orders_by_semver_not_lexicographic():
    """Single- vs double-digit majors order semantically: v20 and v10 both
    outrank v9, which a plain lexicographic sort would get wrong."""
    dirs = [Path("v9.0.0"), Path("v20.11.0"), Path("v10.0.0")]
    result = claude_cli._sorted_nvm_node_dirs(dirs)
    assert [d.name for d in result] == ["v20.11.0", "v10.0.0", "v9.0.0"]


def test_sorted_nvm_node_dirs_unparseable_sorts_last():
    """Unparseable names (no leading decimal segment) sort after every
    parseable version, ordered among themselves by string name."""
    dirs = [Path("v18.20.4"), Path("system"), Path("v20.1.0")]
    result = claude_cli._sorted_nvm_node_dirs(dirs)
    assert [d.name for d in result] == ["v20.1.0", "v18.20.4", "system"]


def test_sorted_nvm_node_dirs_empty_list_never_raises():
    """An empty input list returns an empty list without raising."""
    assert claude_cli._sorted_nvm_node_dirs([]) == []


def test_run_usage_probe_launches_resolved_cli(monkeypatch):
    """The probe must launch the same resolved binary as the agents, because a probe that launches the wrong binary fails silently."""
    monkeypatch.setattr(claude_cli, "_resolve_claude", lambda: "/marker/bin/claude")
    seen = {}

    class _Result:
        stdout = "Current session: 12%"

    def fake(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake)

    assert claude_cli._run_usage_probe() == "Current session: 12%"
    assert seen["args"][0] == ["/marker/bin/claude", "/usage"]
    assert seen["kwargs"]["timeout"] == 30


# ---------------------------------------------------------------------------
# --- _read_sessions ---
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _read_sessions reads and validates the sidecar
# ---------------------------------------------------------------------------


def test_read_sessions_missing_file_returns_empty_dict(tmp_path):
    """Should return {} when the sidecar file does not exist."""
    plan_path = tmp_path / "01-foo-plan.md"
    assert _read_sessions(plan_path) == {}


def test_read_sessions_valid_json_roundtrips(tmp_path):
    """Should return the parsed dict when the sidecar contains valid JSON."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    sidecar.write_text(json.dumps({"planner": "sid-123", "step": "planned"}))
    assert _read_sessions(plan_path) == {"planner": "sid-123", "step": "planned"}


def _halt_lines(exc_info):
    return str(exc_info.value).splitlines()


def test_read_sessions_malformed_json_halts_and_keeps_evidence(tmp_path):
    """Should halt, not return {}, when the sidecar holds malformed JSON — and leave
    the sidecar bytes untouched so the evidence survives."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    original = b"{not valid json"
    sidecar.write_bytes(original)
    with pytest.raises(HaltError) as exc_info:
        _read_sessions(plan_path)
    lines = _halt_lines(exc_info)
    assert lines[0] == "Sidecar is not valid JSON"
    assert str(sidecar) in lines[1:]
    assert sidecar.read_bytes() == original


@pytest.mark.parametrize("content", ["[]", '"text"', "null", "42"])
def test_read_sessions_non_object_json_halts(tmp_path, content):
    """Should halt when the sidecar parses as JSON that is not an object."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    sidecar.write_bytes(content.encode())
    with pytest.raises(HaltError) as exc_info:
        _read_sessions(plan_path)
    assert _halt_lines(exc_info)[0] == "Sidecar JSON is not an object"
    assert sidecar.read_bytes() == content.encode()


def test_read_sessions_invalid_utf8_halts(tmp_path):
    """Should halt when the sidecar bytes are not valid UTF-8."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    sidecar.write_bytes(b'{"step": "\xff"}')
    with pytest.raises(HaltError) as exc_info:
        _read_sessions(plan_path)
    assert _halt_lines(exc_info)[0] == "Sidecar is not valid UTF-8 text"


def test_read_sessions_unreadable_sidecar_halts(tmp_path):
    """Should halt, not return {}, when the sidecar path exists but cannot be read."""
    plan_path = tmp_path / "01-foo-plan.md"
    plan_path.with_suffix(".json").mkdir()
    with pytest.raises(HaltError) as exc_info:
        _read_sessions(plan_path)
    assert _halt_lines(exc_info)[0] == "Sidecar could not be read"


def test_read_sessions_read_failure_is_not_absence(tmp_path, monkeypatch):
    """Should halt when the read itself fails, even if exists() reports False —
    an injected read failure is not mistaken for an absent sidecar."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    sidecar.write_text(json.dumps({"step": "planned"}))
    real_exists = Path.exists
    real_read_bytes = Path.read_bytes

    def fake_exists(self, *args, **kwargs):
        if self == sidecar:
            return False
        return real_exists(self, *args, **kwargs)

    def fake_read_bytes(self):
        if self == sidecar:
            raise PermissionError("denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    with pytest.raises(HaltError):
        _read_sessions(plan_path)


def test_read_sessions_preserves_unknown_nested_values(tmp_path):
    """Should return unknown keys and nested JSON values unchanged."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    record = {
        "step": "planned:1",
        "extra": {"nested": [1, {"a": None}], "n": 3.5, "flag": True},
    }
    sidecar.write_text(json.dumps(record))
    assert _read_sessions(plan_path) == record


def test_read_sessions_derives_sidecar_path_via_with_suffix(tmp_path):
    """Should derive the sidecar path via .with_suffix('.json'), not by appending
    onto the plan file's own suffix."""
    plan_path = tmp_path / "03-bar-plan.md"
    sidecar = tmp_path / "03-bar-plan.json"
    sidecar.write_text(json.dumps({"step": "planned"}))
    assert _read_sessions(plan_path) == {"step": "planned"}


# ---------------------------------------------------------------------------
# --- _write_session ---
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _write_session create, merge, and overwrite
# ---------------------------------------------------------------------------


def test_write_session_creates_fresh_sidecar_with_only_new_key(tmp_path):
    """Should create a fresh sidecar containing only the new key when none exists yet."""
    plan_path = tmp_path / "01-foo-plan.md"
    _write_session(plan_path, "planner", "sid-abc")
    sidecar = tmp_path / "01-foo-plan.json"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text()) == {"planner": "sid-abc"}


def test_write_session_merges_without_clobbering_other_keys(tmp_path):
    """Should merge the new key into an existing sidecar without clobbering other keys."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    sidecar.write_text(json.dumps({"planner": "sid-abc", "step": "planned"}))
    _write_session(plan_path, "implementer", "sid-xyz")
    assert json.loads(sidecar.read_text()) == {
        "planner": "sid-abc",
        "step": "planned",
        "implementer": "sid-xyz",
    }


def test_write_session_overwrites_existing_key(tmp_path):
    """Should overwrite the value of an existing key when writing the same key again."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    sidecar.write_text(json.dumps({"planner": "sid-old"}))
    _write_session(plan_path, "planner", "sid-new")
    assert json.loads(sidecar.read_text()) == {"planner": "sid-new"}


# ---------------------------------------------------------------------------
# _write_session evidence preservation and atomic write
# ---------------------------------------------------------------------------


@pytest.fixture
def replace_spy(monkeypatch):
    """Record every os.replace call made by the protocol module, delegating to the real one."""
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("orchestrator.protocol.os.replace", spy)
    return calls


def test_write_session_corruption_preserves_original_evidence(tmp_path, replace_spy):
    """Should halt and leave a corrupted sidecar byte-identical when written to.

    This is the data-loss silent-failure surface: a corrupted sidecar must not
    lose the session identities it held on the next write. The write halts
    instead of replacing the original with a one-key record.
    """
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    original = b"{not valid json"
    sidecar.write_bytes(original)
    with pytest.raises(HaltError):
        _write_session(plan_path, "step", "planned")
    assert sidecar.read_bytes() == original
    assert replace_spy == []
    assert not plan_path.with_suffix(".json.tmp").exists()


def test_write_session_non_object_preserves_original(tmp_path, replace_spy):
    """Should halt and keep the original bytes when the sidecar is not a JSON object."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    original = b"[1, 2]"
    sidecar.write_bytes(original)
    with pytest.raises(HaltError):
        _write_session(plan_path, "step", "planned")
    assert sidecar.read_bytes() == original
    assert replace_spy == []
    assert not plan_path.with_suffix(".json.tmp").exists()


def test_write_session_read_failure_preserves_original(tmp_path, monkeypatch, replace_spy):
    """Should halt and keep the original bytes when the existing sidecar cannot be read."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    original = json.dumps({"planner": "sid-old"}).encode()
    sidecar.write_bytes(original)
    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self):
        if self == sidecar:
            raise PermissionError("denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    with pytest.raises(HaltError):
        _write_session(plan_path, "step", "planned")
    monkeypatch.undo()
    assert sidecar.read_bytes() == original
    assert replace_spy == []
    assert not plan_path.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("batch", [False, True])
def test_write_session_replace_failure_halts_and_keeps_original(tmp_path, monkeypatch, batch):
    """Should halt, keep the original sidecar and leave no temp file when the replace fails."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    original = json.dumps({"planner": "sid-old"}).encode()
    sidecar.write_bytes(original)

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("orchestrator.protocol.os.replace", failing_replace)
    with pytest.raises(HaltError) as exc_info:
        if batch:
            _write_sessions(plan_path, {"runtime": "codex", "step": "planning:2", "planner": "sid-new"})
        else:
            _write_session(plan_path, "planner", "sid-new")
    assert _halt_lines(exc_info)[0] == "Sidecar could not be updated"
    assert sidecar.read_bytes() == original
    assert not plan_path.with_suffix(".json.tmp").exists()


def test_batch_replacement_contains_complete_boundary_and_preserves_metadata(tmp_path, monkeypatch):
    plan = tmp_path / "01-work.md"
    sidecar = plan.with_suffix(".json")
    record = {"planner": "old", "implementer": "impl", "plan-reviewer-1": "audit",
              "elapsed": "007", "optional": {"nested": [None, True, {"number": 2.5}]}}
    sidecar.write_text(json.dumps(record))
    updates = {"runtime": "codex", "step": "planning:2", "planner": " opaque new "}
    real_replace = os.replace
    replacements = []

    def inspect_replace(src, dst):
        assert Path(src) == plan.with_suffix(".json.tmp")
        assert Path(dst) == sidecar
        assert json.loads(sidecar.read_text()) == record
        replacements.append(json.loads(Path(src).read_text()))
        real_replace(src, dst)

    monkeypatch.setattr("orchestrator.protocol.os.replace", inspect_replace)
    _write_sessions(plan, updates)
    assert replacements == [{**record, **updates}]
    assert _read_sessions(plan) == replacements[0]
    assert not plan.with_suffix(".json.tmp").exists()


def test_write_session_retains_unknown_nested_values(tmp_path, replace_spy):
    """Should change only the requested key, retaining unknown keys and nested values."""
    plan_path = tmp_path / "01-foo-plan.md"
    sidecar = tmp_path / "01-foo-plan.json"
    record = {
        "planner": "sid-old",
        "step": "planned:1",
        "extra": {"nested": [1, {"a": None}], "n": 3.5, "flag": True},
    }
    sidecar.write_text(json.dumps(record))
    _write_session(plan_path, "planner", "sid-new")
    assert json.loads(sidecar.read_text()) == {**record, "planner": "sid-new"}
    assert [dst for _, dst in replace_spy] == [sidecar]


def test_write_session_leaves_no_tmp_file_behind(tmp_path):
    """Should write via a temp file and atomically replace it, leaving no .tmp file
    behind after a successful write."""
    plan_path = tmp_path / "01-foo-plan.md"
    _write_session(plan_path, "planner", "sid-abc")
    tmp_sidecar = plan_path.with_suffix(".json.tmp")
    sidecar = plan_path.with_suffix(".json")
    assert not tmp_sidecar.exists()
    assert sidecar.exists()


def test_write_session_content_is_rereadable_via_read_sessions(tmp_path):
    """Should produce sidecar content that is re-readable via _read_sessions —
    the two functions agree on path derivation and format."""
    plan_path = tmp_path / "01-foo-plan.md"
    _write_session(plan_path, "planner", "sid-abc")
    _write_session(plan_path, "implementer", "sid-xyz")
    assert _read_sessions(plan_path) == {"planner": "sid-abc", "implementer": "sid-xyz"}


# ---------------------------------------------------------------------------
# --- kill_active_child ---
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_active_proc():
    """Reset the shared state.active_proc global to None before and after each
    test — it's a module-level global, not per-instance, so leftover state would
    silently corrupt unrelated tests."""
    state.active_proc = None
    try:
        yield
    finally:
        state.active_proc = None


# ---------------------------------------------------------------------------
# Task 4: kill_active_child no-op branches
# ---------------------------------------------------------------------------


def test_kill_active_child_noop_when_already_none(clean_active_proc):
    """Should be a no-op and leave state.active_proc as None when it is already None."""
    state.active_proc = None
    kill_active_child()
    assert state.active_proc is None


def test_kill_active_child_clears_already_exited_process(clean_active_proc):
    """Should clear state.active_proc without signalling when the tracked process
    has already exited (covers the `proc.poll() is not None` short-circuit —
    os.killpg is not reached)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    state.active_proc = proc
    kill_active_child()
    assert state.active_proc is None


# ---------------------------------------------------------------------------
# Task 5: kill_active_child kills a live process group
# ---------------------------------------------------------------------------


def test_kill_active_child_kills_live_process_group(clean_active_proc):
    """Should kill the live process group and clear state.active_proc when the
    process is still running."""
    proc = subprocess.Popen(["sleep", "5"], preexec_fn=os.setsid)
    state.active_proc = proc
    try:
        kill_active_child()
        deadline = time.monotonic() + 1.0
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None
        assert state.active_proc is None
    finally:
        proc.wait()


# ---------------------------------------------------------------------------
# Task 6: kill_active_child killpg-failure fallback
# ---------------------------------------------------------------------------


class _FakeProc:
    """Minimal stand-in for subprocess.Popen exposing only what kill_active_child
    touches: .poll(), .pid, and .kill()."""

    def __init__(self):
        self.pid = 99999
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


def test_kill_active_child_falls_back_to_kill_on_killpg_failure(monkeypatch, clean_active_proc):
    """Should fall back to proc.kill() and still clear state.active_proc when
    os.killpg raises ProcessLookupError."""

    def raise_process_lookup_error(pgid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(process_control.os, "killpg", raise_process_lookup_error)
    fake = _FakeProc()
    state.active_proc = fake
    kill_active_child()
    assert fake.killed is True
    assert state.active_proc is None


# ---------------------------------------------------------------------------
# --- _classify_result ---
# ---------------------------------------------------------------------------
#
# The decision table: which terminal action a finished CLI invocation maps to.
# ---------------------------------------------------------------------------


def test_classify_result_network_death_retry_left():
    """Row 2: no result event, nonzero returncode, retries remain -> retry."""
    assert _classify_result({}, "", 1, False, 1, 3) == "retry"


def test_classify_result_network_death_exhausted():
    """Row 3: no result event, nonzero returncode, retries exhausted -> network_halt."""
    assert _classify_result({}, "", 1, False, 3, 3) == "network_halt"


def test_classify_result_overloaded_retry_left():
    """Row 1: overloaded/529 signal in result_text, retries remain -> retry."""
    assert _classify_result({"result": "overloaded"}, "overloaded", 0, False, 1, 3) == "retry"


def test_classify_result_real_cli_error():
    """Row 5: nonzero returncode with no rate-limit signal -> error."""
    assert _classify_result({"result": "boom"}, "boom", 1, False, 3, 3) == "error"


def test_classify_result_rate_limit_via_returncode():
    """Row 4: nonzero returncode with a rate-limit signal -> ratelimit."""
    assert _classify_result({"result": "You hit your limit"}, "You hit your limit", 1, False, 3, 3) == "ratelimit"


def test_classify_result_clean_success():
    """Row 8: zero returncode, no error -> ok."""
    assert _classify_result({"result": "done", "is_error": False}, "done", 0, False, 1, 3) == "ok"


def test_classify_result_transport_fault_retry_left():
    """A result-bearing transport error retries while attempts remain."""
    assert _classify_result({"result": "API Error: Connection closed mid-response."}, "API Error: Connection closed mid-response.", 1, False, 1, 3) == "retry"


def test_classify_result_transport_fault_exhausted():
    """A result-bearing transport error halts once attempts are spent."""
    assert _classify_result({"result": "API Error: Connection closed mid-response."}, "API Error: Connection closed mid-response.", 1, False, 3, 3) == "network_halt"


def test_classify_result_markerless_nonzero_still_errors():
    """A nonzero exit with no transport marker keeps classifying as a task-level error."""
    assert _classify_result({"result": "boom"}, "boom", 1, False, 1, 3) == "error"


def test_classify_result_ratelimit_text_still_ratelimit():
    """Rate-limit text is unaffected by the new transport branches."""
    assert _classify_result({"result": "You hit your limit"}, "You hit your limit", 1, False, 1, 3) == "ratelimit"


def test_classify_result_transport_marker_in_successful_result_is_inert():
    """A transport marker inside a successful result text is not a transport fault."""
    assert _classify_result({"result": "handles connection reset", "is_error": False}, "handles connection reset", 0, False, 1, 3) == "ok"


# ---------------------------------------------------------------------------
# --- _run_claude rate-limit halt ---
# ---------------------------------------------------------------------------


class _FakeRateLimitProc:
    """Minimal stand-in for subprocess.Popen exposing only what _run_claude touches
    for a single-attempt rate-limit halt: .stdout, .wait(), and .returncode."""

    def __init__(self):
        self.stdout = ['{"session_id": "s1", "result": "You hit your limit, resets at 5pm"}\n']
        self.returncode = 1

    def wait(self):
        pass


def test_run_claude_ratelimit_halt_has_fixed_first_line(monkeypatch, tmp_path, clean_active_proc):
    """RateLimitError's message opens with the fixed clause; the agent's own result
    text follows on a later line rather than being the whole message."""
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    fake = _FakeRateLimitProc()
    monkeypatch.setattr(claude_cli.subprocess, "Popen", lambda *a, **kw: fake)

    with pytest.raises(RateLimitError) as exc:
        claude_cli._run_claude("prompt", cwd=str(tmp_path))

    assert str(exc.value).splitlines()[0] == "Agent reported a rate limit"
    assert "You hit your limit, resets at 5pm" in str(exc.value).split("\n", 1)[1]


class _FakeOkProc:
    """Minimal stand-in for subprocess.Popen exposing only what _run_claude touches
    for a single successful attempt: .stdout, .wait(), and .returncode."""

    def __init__(self):
        self.stdout = ['{"session_id": "s1", "result": "ok"}\n']
        self.returncode = 0

    def wait(self):
        pass


def test_run_claude_launch_denies_work_outliving_the_turn(monkeypatch, tmp_path, clean_active_proc):
    """Every launched command carries a deny list for backgroundable tools and an
    environment variable disabling background tasks, so an agent can't leave work
    running past the end of its own turn."""
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    fake = _FakeOkProc()
    recorded: dict = {}

    def _fake_popen(*args, **kwargs):
        recorded["cmd"] = args[0]
        recorded["env"] = kwargs.get("env")
        return fake

    monkeypatch.setattr(claude_cli.subprocess, "Popen", _fake_popen)

    claude_cli._run_claude("prompt", cwd=str(tmp_path))

    assert recorded["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    cmd = recorded["cmd"]
    assert cmd[cmd.index("--disallowedTools") + 1] == "Monitor,ScheduleWakeup,CronCreate"


# ---------------------------------------------------------------------------
# --- EscalationError / _has_escalation / _escalation_excerpt ---
# ---------------------------------------------------------------------------


def test_escalation_error_is_not_a_halt_error():
    """EscalationError must be a direct Exception subclass, a sibling of PipelineStopError,
    never moved under HaltError by analogy with RateLimitError/NetworkError."""
    assert not issubclass(EscalationError, HaltError)


def test_has_escalation_on_last_line():
    """The ESCALATION marker is detected via the same last-5-line exact-match rule as
    REVIEW_PASS/PLAN_REVIEW_PASS."""
    text = "some findings\n\n## Escalation\nDecide A or B.\n\nESCALATION"
    assert _has_escalation(text) is True


def test_has_escalation_absent():
    """No marker present -> False."""
    assert _has_escalation("some findings\n\nREVIEW_PASS") is False


def test_escalation_excerpt_extracts_first_line_under_heading():
    """Should return the first non-empty, non-heading line under `## Escalation`."""
    text = "notes\n\n## Escalation\nThe missing decision is X.\nOption A\nOption B\n\nESCALATION"
    assert _escalation_excerpt(text) == "The missing decision is X."


def test_escalation_excerpt_absent_section_returns_empty():
    """Should return "" when there is no `## Escalation` section."""
    assert _escalation_excerpt("just some notes\n\nREVIEW_PASS") == ""


# ---------------------------------------------------------------------------
# --- ESCALATION detection wired into the four agent methods ---
# ---------------------------------------------------------------------------


ESCALATION_ARTIFACT = "Some notes\n\n## Escalation\nMissing decision: pick A or B.\n\nESCALATION"


def test_plan_escalation_raises_and_writes_sidecar(tmp_path):
    """plan() raises EscalationError and the sidecar ends with step=escalated plus a
    non-empty escalation summary when the written plan artifact ends with ESCALATION."""
    plan_path = tmp_path / "01-slug.md"
    backend = writing_backend(plan_path, ESCALATION_ARTIFACT)
    pr = PlannerReviewer(tmp_path, backend)

    with pytest.raises(EscalationError):
        pr.plan("Task title", "Task description", plan_path)

    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions["step"] == "escalated"
    assert sessions["escalation"]


def test_review_escalation_raises_before_review_pass_check(tmp_path):
    """review() raises EscalationError and writes the sidecar when the review artifact ends
    with ESCALATION, without falling through to the REVIEW_PASS check."""
    plan_path = tmp_path / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / "01-slug-review-1.md"
    backend = writing_backend(review_path, ESCALATION_ARTIFACT)
    pr = PlannerReviewer(tmp_path, backend)

    with pytest.raises(EscalationError):
        pr.review(plan_path, review_path)

    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions["step"] == "escalated"
    assert sessions["escalation"]


def test_review_plan_escalation_raises_and_writes_sidecar(tmp_path):
    """review_plan() raises EscalationError and writes the sidecar when the plan-review
    artifact ends with ESCALATION, without falling through to the PLAN_REVIEW_PASS check."""
    plan_path = tmp_path / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / "01-slug-plan-review-1.md"
    backend = writing_backend(review_path, ESCALATION_ARTIFACT)
    reviewer = PlanReviewer(tmp_path, backend)

    with pytest.raises(EscalationError):
        reviewer.review_plan(plan_path, review_path, 1)

    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions["step"] == "escalated"
    assert sessions["escalation"]


def test_implement_escalation_raises_and_writes_sidecar(tmp_path):
    """implement() raises EscalationError and writes the sidecar when the plan artifact it
    owns ends with ESCALATION."""
    plan_path = tmp_path / "01-slug.md"
    backend = writing_backend(plan_path, ESCALATION_ARTIFACT)
    impl = Implementer(tmp_path, backend)

    with pytest.raises(EscalationError):
        impl.implement(plan_path)

    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions["step"] == "escalated"
    assert sessions["escalation"]


# ---------------------------------------------------------------------------
# Regression: REVIEW_PASS/PLAN_REVIEW_PASS with no ESCALATION behaves as before
# ---------------------------------------------------------------------------


def test_review_regression_no_escalation_returns_bool(tmp_path):
    """An artifact ending with REVIEW_PASS and no ESCALATION line returns True, with no
    raise and no 'escalated' sidecar."""
    plan_path = tmp_path / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / "01-slug-review-1.md"
    backend = writing_backend(review_path, "All good\n\nREVIEW_PASS")
    pr = PlannerReviewer(tmp_path, backend)

    assert pr.review(plan_path, review_path) is True

    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions.get("step") != "escalated"
    assert "escalation" not in sessions


def test_review_plan_regression_no_escalation_returns_bool(tmp_path):
    """An artifact ending with PLAN_REVIEW_PASS and no ESCALATION line returns True, with
    no raise. The sidecar holds only the reviewer's session id, with no 'step' and no
    'escalation'."""
    plan_path = tmp_path / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / "01-slug-plan-review-1.md"
    backend = writing_backend(review_path, "All good\n\nPLAN_REVIEW_PASS")
    reviewer = PlanReviewer(tmp_path, backend)

    assert reviewer.review_plan(plan_path, review_path, 1) is True

    assert json.loads(plan_path.with_suffix(".json").read_text()) == {"plan-reviewer-1": "sid-1"}


# ---------------------------------------------------------------------------
# A review that was never written halts
# ---------------------------------------------------------------------------


def test_review_missing_file_raises_missing_artifact_error(tmp_path):
    """PlannerReviewer.review raises MissingArtifactError, not a False verdict, when the
    agent ends its turn without writing the review file. The session write still happens;
    no 'step' is written for this halt."""
    plan_path = tmp_path / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / "01-slug-review-1.md"
    backend = silent_backend()
    pr = PlannerReviewer(tmp_path, backend)

    with pytest.raises(MissingArtifactError) as exc_info:
        pr.review(plan_path, review_path)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Agent ended without writing its review"
    assert lines[1] == str(review_path)

    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions.get("planner") == "sid-1"
    assert "step" not in sessions


def test_review_plan_missing_file_raises_missing_artifact_error(tmp_path):
    """PlanReviewer.review_plan raises MissingArtifactError, not a False verdict, when the
    agent ends its turn without writing the review file. The session id is written even
    though no review exists, and no 'step' is written."""
    plan_path = tmp_path / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / "01-slug-plan-review-1.md"
    backend = silent_backend()
    reviewer = PlanReviewer(tmp_path, backend)

    with pytest.raises(MissingArtifactError) as exc_info:
        reviewer.review_plan(plan_path, review_path, 1)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Agent ended without writing its review"
    assert lines[1] == str(review_path)

    assert json.loads(plan_path.with_suffix(".json").read_text()) == {"plan-reviewer-1": "sid-1"}


# ---------------------------------------------------------------------------
# Roles reach the runtime only through the injected backend
# ---------------------------------------------------------------------------


def _artifact_writing_backend(plan_review_text: str = "ok\n\nPLAN_REVIEW_PASS") -> FakeAgentBackend:
    """A fake that writes whatever artifact the prompt names, shaped by the role."""

    def _write(call):
        path = target_path(call.prompt)
        if call.role is AgentRole.PLANNER_REVIEWER and "review" not in path.name:
            path.write_text("# Plan")
        elif call.role is AgentRole.PLANNER_REVIEWER:
            path.write_text("All good\n\nREVIEW_PASS")
        elif call.role is AgentRole.PLAN_REVIEWER:
            path.write_text(plan_review_text)

    return FakeAgentBackend(on_call=_write)


def test_planner_reviewer_passes_complete_instructions_and_continues_session(tmp_path):
    """Every PlannerReviewer call carries the complete planner + reviewer + escalation
    instructions, resumed ones included; the session id handed in is the one returned last."""
    backend = _artifact_writing_backend()
    pr = PlannerReviewer(tmp_path, backend)
    plan_path = tmp_path / "01-slug.md"
    review_1 = tmp_path / "01-slug-review-1.md"
    review_2 = tmp_path / "01-slug-review-2.md"

    pr.plan("Title", "Description", plan_path)
    assert pr.review(plan_path, review_1) is True
    assert pr.review(plan_path, review_2, prev_review_path=review_1) is True

    assert len(backend.calls) == 3
    for call in backend.calls:
        assert call.role is AgentRole.PLANNER_REVIEWER
        assert call.instructions == pr.system_prompt
        assert call.cwd == str(tmp_path)
        for name in ("planner", "reviewer", "escalation"):
            assert _load_prompt(name) in call.instructions
    assert backend.calls[0].session_id is None
    assert backend.calls[1].session_id == backend.returned[0]
    assert backend.calls[2].session_id == backend.returned[1]


def test_planner_reviewer_test_planner_prompt_reaches_instructions(tmp_path):
    backend = _artifact_writing_backend()
    pr = PlannerReviewer(tmp_path, backend, planner_prompt_name="test-planner")

    pr.plan("Title", "Description", tmp_path / "01-slug.md")

    assert _load_prompt("test-planner") in backend.calls[0].instructions


def test_implementer_fixes_continue_session_with_complete_instructions(tmp_path):
    backend = FakeAgentBackend()
    impl = Implementer(tmp_path, backend)
    plan_path = tmp_path / "01-slug.md"
    feedback = tmp_path / "01-slug-review-1.md"

    impl.implement(plan_path)
    impl.implement(plan_path, feedback_path=feedback)

    first, second = backend.calls
    for call in (first, second):
        assert call.role is AgentRole.IMPLEMENTER
        assert call.instructions == impl.system_prompt
        assert _load_prompt("implementer") in call.instructions
        assert _load_prompt("escalation") in call.instructions
    assert first.session_id is None
    assert second.session_id == backend.returned[0]
    assert str(feedback) in second.prompt
    assert "Review feedback" in second.prompt


def test_implementer_continuing_session_without_feedback_sends_plan_prompt(tmp_path):
    backend = FakeAgentBackend()
    impl = Implementer(tmp_path, backend)
    plan_path = tmp_path / "01-slug.md"
    roadmap_path = tmp_path / "roadmap.md"

    impl.implement(plan_path, roadmap_path=roadmap_path, line_number=4)
    impl.implement(plan_path, feedback_path=None, roadmap_path=roadmap_path, line_number=4)

    first, second = backend.calls
    assert second.session_id == backend.returned[0]
    assert second.prompt == (
        f"Roadmap: {roadmap_path} (line 5)\n"
        f"Implement the plan at: {plan_path}"
    )
    assert "Review feedback" not in second.prompt


def test_implementer_fresh_session_with_feedback_path_sends_plan_prompt(tmp_path):
    backend = FakeAgentBackend()
    impl = Implementer(tmp_path, backend)
    plan_path = tmp_path / "01-slug.md"
    roadmap_path = tmp_path / "roadmap.md"
    feedback = tmp_path / "01-slug-review-1.md"

    impl.implement(plan_path, feedback_path=feedback, roadmap_path=roadmap_path, line_number=4)

    call = backend.calls[0]
    assert call.session_id is None
    assert call.prompt == (
        f"Roadmap: {roadmap_path} (line 5)\n"
        f"Implement the plan at: {plan_path}"
    )
    assert "Review feedback" not in call.prompt


def test_plan_review_attempts_inherit_no_session(tmp_path):
    backend = _artifact_writing_backend()
    plan_path = tmp_path / "01-slug.md"
    plan_path.write_text("# Plan")

    for attempt in (1, 2):
        reviewer = PlanReviewer(tmp_path, backend)
        review_path = tmp_path / f"01-slug-plan-review-{attempt}.md"
        assert reviewer.review_plan(plan_path, review_path, attempt) is True

    assert len(backend.calls) == 2
    for call in backend.calls:
        assert call.session_id is None
        assert call.role is AgentRole.PLAN_REVIEWER
        assert call.cwd == str(tmp_path)
        assert _load_prompt("reviewer") in call.instructions
        assert _load_prompt("escalation") in call.instructions
    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions == {
        "plan-reviewer-1": backend.returned[0],
        "plan-reviewer-2": backend.returned[1],
    }
    assert backend.returned[0] != backend.returned[1]


_EXPECTED_POLICY = {
    AgentRole.PLANNER_REVIEWER: ("opus", "high", ["Read", "Write", "Glob", "Grep", "Bash"]),
    AgentRole.PLAN_REVIEWER: ("opus", "high", ["Read", "Write", "Glob", "Grep", "Bash"]),
    AgentRole.IMPLEMENTER: ("sonnet", "high", ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]),
}


@pytest.mark.parametrize("role", list(AgentRole))
def test_claude_backend_policy_and_instruction_delivery(monkeypatch, role):
    """Default settings and tools follow the role; only fresh sessions receive instructions."""
    seen: list[dict] = []

    def _recorder(**kwargs):
        seen.append(kwargs)
        return ("out", "sid-x")

    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli, "_run_claude", _recorder)
    model, effort, tools = _EXPECTED_POLICY[role]

    assert ClaudeAgentBackend().run(role=role, prompt="p", cwd="c", instructions="FULL") == ("out", "sid-x")
    assert ClaudeAgentBackend().run(role=role, prompt="p", cwd="c", instructions="FULL", session_id="s1") == ("out", "sid-x")

    fresh, resumed = seen
    assert fresh == dict(prompt="p", cwd="c", system_prompt="FULL", session_id=None,
                         allowed_tools=tools, model=model, effort=effort)
    assert resumed == dict(prompt="p", cwd="c", system_prompt=None, session_id="s1",
                           allowed_tools=tools, model=model, effort=effort)


@pytest.mark.parametrize("role", list(AgentRole))
@pytest.mark.parametrize(
    "override",
    [
        {"model": " custom-model "},
        {"effort": " custom-effort "},
        {"model": " custom-model ", "effort": " custom-effort "},
    ],
)
def test_claude_backend_overrides_preserve_tools_and_instruction_delivery(monkeypatch, role, override):
    seen: list[dict] = []

    def _recorder(**kwargs):
        seen.append(kwargs)
        return ("out", "sid-x")

    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli, "_run_claude", _recorder)
    backend = ClaudeAgentBackend({role.value: override})
    model, effort, tools = _EXPECTED_POLICY[role]
    model = override.get("model", model)
    effort = override.get("effort", effort)
    expected = {
        key: RoleSetting(default_model, default_effort)
        for key, (default_model, default_effort, _) in _EXPECTED_POLICY.items()
    }
    expected[role] = RoleSetting(model, effort)
    assert backend.role_settings == expected
    assert list(backend.role_settings) == list(AgentRole)

    assert backend.run(role=role, prompt="p", cwd="c", instructions="FULL") == ("out", "sid-x")
    assert backend.run(role=role, prompt="p", cwd="c", instructions="FULL", session_id="sid-x") == ("out", "sid-x")

    fresh, resumed = seen
    assert fresh == dict(prompt="p", cwd="c", system_prompt="FULL", session_id=None,
                         allowed_tools=tools, model=model, effort=effort)
    assert resumed == dict(prompt="p", cwd="c", system_prompt=None, session_id="sid-x",
                           allowed_tools=tools, model=model, effort=effort)
    assert fresh["allowed_tools"] is not resumed["allowed_tools"]


@pytest.mark.parametrize("agents", [None, {}])
def test_claude_backend_exposes_original_defaults(monkeypatch, agents):
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    expected = {
        role: RoleSetting(model, effort)
        for role, (model, effort, _) in _EXPECTED_POLICY.items()
    }

    assert ClaudeAgentBackend().role_settings == expected
    assert ClaudeAgentBackend(agents).role_settings == expected


def test_claude_backend_settings_are_read_only_and_detached_from_input(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli, "_run_claude", lambda **kw: seen.append(kw) or ("out", "sid"))
    agents = {"implementer": {"model": "custom", "effort": "custom-effort"}}
    backend = ClaudeAgentBackend(agents)
    expected = {
        role: RoleSetting(model, effort)
        for role, (model, effort, _) in _EXPECTED_POLICY.items()
    }
    expected[AgentRole.IMPLEMENTER] = RoleSetting("custom", "custom-effort")

    with pytest.raises(TypeError):
        backend.role_settings[AgentRole.IMPLEMENTER] = RoleSetting("other", "other")
    with pytest.raises(AttributeError):
        backend.role_settings = {}
    with pytest.raises(AttributeError):
        backend.role_settings[AgentRole.IMPLEMENTER].model = "other"

    agents["implementer"]["model"] = "later"
    agents["implementer"]["effort"] = "later-effort"
    agents["planner_reviewer"] = {"model": "later-planner"}
    assert backend.role_settings == expected
    for role in AgentRole:
        backend.run(role=role, prompt="p", cwd="c", instructions="FULL")

    for role, call in zip(AgentRole, seen):
        assert (call["model"], call["effort"]) == expected[role]
        assert call["allowed_tools"] == _EXPECTED_POLICY[role][2]


def test_claude_backend_policy_does_not_depend_on_instruction_text(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli, "_run_claude", lambda **kw: seen.append(kw) or ("out", "sid"))

    ClaudeAgentBackend().run(role=AgentRole.PLANNER_REVIEWER, prompt="p", cwd="c", instructions="SAME")
    ClaudeAgentBackend().run(role=AgentRole.IMPLEMENTER, prompt="p", cwd="c", instructions="SAME")

    assert seen[0]["model"] != seen[1]["model"]
    assert seen[0]["allowed_tools"] != seen[1]["allowed_tools"]


def test_claude_backend_propagates_invocation_faults(monkeypatch):
    def _boom(**kwargs):
        raise RateLimitError("limit")

    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli, "_run_claude", _boom)

    with pytest.raises(RateLimitError):
        ClaudeAgentBackend().run(role=AgentRole.IMPLEMENTER, prompt="p", cwd="c", instructions="I")


def test_claude_backend_preflight_resolves_only_when_unset(monkeypatch):
    """Construction resolves the executable once when unset, never runs a CLI, and propagates a missing binary."""
    def _no_run(*args, **kwargs):
        raise AssertionError("construction must not run the CLI")

    monkeypatch.setattr(subprocess, "Popen", _no_run)
    monkeypatch.setattr(subprocess, "run", _no_run)
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", None)
    resolves: list[int] = []
    monkeypatch.setattr(claude_cli, "_resolve_claude", lambda: resolves.append(1) or "/marker/bin/claude")

    ClaudeAgentBackend()
    assert claude_cli._CLAUDE_BIN == "/marker/bin/claude"
    assert resolves == [1]

    ClaudeAgentBackend()
    assert resolves == [1]

    def _missing():
        raise FileNotFoundError("claude CLI not found")

    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", None)
    monkeypatch.setattr(claude_cli, "_resolve_claude", _missing)
    with pytest.raises(FileNotFoundError):
        ClaudeAgentBackend()
