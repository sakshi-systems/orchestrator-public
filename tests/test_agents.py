"""Unit tests for the orchestrator's agent layer: the helpers and decision table
its agents run on, the backends and roles behind them, and the session and
child-process machinery they share."""

import io
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from orchestrator import claude_cli, process_control, state
from orchestrator.implementer_result import WorkingTreeResult
from orchestrator.agents import (
    CodeReviewer,
    Implementer,
    PlanReviewer,
    Planner,
    SpecLayer,
    TestRunner,
    _load_prompt,
)
from orchestrator.claude_cli import ClaudeAgentBackend, _classify_result
from orchestrator.process_control import kill_active_child
from orchestrator.errors import EmptyAnswerError, EscalationError, HaltError, RateLimitError
from orchestrator.protocol import (
    _escalation_excerpt,
    _has_escalation,
    _has_signal,
    _read_artifact,
    _read_sessions,
    _write_artifact,
    _write_session,
    _write_sessions,
)
from orchestrator.ports import AgentRole, IterationState, RoleSetting
from tests.fakes import FakeAgentBackend

_ITERATION = IterationState(base="0" * 40, commit="1" * 40, ref="refs/orchestrator/01-slug")


# ---------------------------------------------------------------------------
# _has_signal — match within the last-5-line window
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _factory_dir(tmp_path):
    (tmp_path / ".ai-factory").mkdir(exist_ok=True)



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
# _has_signal — window exclusion and exact-match rejection
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
# TestRunner._extract_test_command — backtick-wrapped command
# ---------------------------------------------------------------------------


def test_extract_test_command_backtick_wrapped(tmp_path):
    """Should return the command without backticks when the command line is wrapped in single backticks."""
    p = tmp_path / ".ai-factory" / "plan.md"
    p.write_text("## Test Command\n`uv run pytest tests/ -v`")
    assert TestRunner._extract_test_command(p) == "uv run pytest tests/ -v"


# ---------------------------------------------------------------------------
# TestRunner._extract_test_command — bare command without backticks
# ---------------------------------------------------------------------------


def test_extract_test_command_bare(tmp_path):
    """Should return the command string as-is when the command line has no backticks."""
    p = tmp_path / ".ai-factory" / "plan.md"
    p.write_text("## Test Command\nuv run pytest tests/ -v")
    assert TestRunner._extract_test_command(p) == "uv run pytest tests/ -v"


# ---------------------------------------------------------------------------
# TestRunner._extract_test_command — command after intervening blank lines
# ---------------------------------------------------------------------------


def test_extract_test_command_blank_lines(tmp_path):
    """Should return the first non-empty non-heading line when blank lines follow the heading."""
    p = tmp_path / ".ai-factory" / "plan.md"
    p.write_text("## Test Command\n\n\nuv run pytest -v")
    assert TestRunner._extract_test_command(p) == "uv run pytest -v"


# ---------------------------------------------------------------------------
# TestRunner._extract_test_command — heading absent
# ---------------------------------------------------------------------------


def test_extract_test_command_no_heading(tmp_path):
    """Should return None when the plan has no Test Command heading."""
    p = tmp_path / ".ai-factory" / "plan.md"
    p.write_text("# Some Plan\n## Other Section\nrun this")
    assert TestRunner._extract_test_command(p) is None


# ---------------------------------------------------------------------------
# TestRunner._extract_test_command — empty section before next heading
# ---------------------------------------------------------------------------


def test_extract_test_command_empty_section(tmp_path):
    """Should return None when the Test Command section is blank up to the next heading."""
    p = tmp_path / ".ai-factory" / "plan.md"
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
# _resolve_claude — PATH-hit tests
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
# _resolve_claude — not-found tests -> FileNotFoundError with install message
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
# _resolve_claude — nvm candidate selection -- multi-candidate, partial-install,
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
# _sorted_nvm_node_dirs -- direct unit cases on the pure helper
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
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    assert _read_sessions(plan_path) == {}


def test_read_sessions_valid_json_roundtrips(tmp_path):
    """Should return the parsed dict when the sidecar contains valid JSON."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
    sidecar.write_text(json.dumps({"planner": "sid-123", "step": "planned"}))
    assert _read_sessions(plan_path) == {"planner": "sid-123", "step": "planned"}


def _halt_lines(exc_info):
    return str(exc_info.value).splitlines()


def test_read_sessions_malformed_json_halts_and_keeps_evidence(tmp_path):
    """Should halt, not return {}, when the sidecar holds malformed JSON — and leave
    the sidecar bytes untouched so the evidence survives."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
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
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
    sidecar.write_bytes(content.encode())
    with pytest.raises(HaltError) as exc_info:
        _read_sessions(plan_path)
    assert _halt_lines(exc_info)[0] == "Sidecar JSON is not an object"
    assert sidecar.read_bytes() == content.encode()


def test_read_sessions_invalid_utf8_halts(tmp_path):
    """Should halt when the sidecar bytes are not valid UTF-8."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
    sidecar.write_bytes(b'{"step": "\xff"}')
    with pytest.raises(HaltError) as exc_info:
        _read_sessions(plan_path)
    assert _halt_lines(exc_info)[0] == "Sidecar is not valid UTF-8 text"


def test_read_sessions_unreadable_sidecar_halts(tmp_path):
    """Should halt, not return {}, when the sidecar path exists but cannot be read."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    plan_path.with_suffix(".json").mkdir()
    with pytest.raises(HaltError) as exc_info:
        _read_sessions(plan_path)
    assert _halt_lines(exc_info)[0] == "Sidecar could not be read"


def test_read_sessions_read_failure_is_not_absence(tmp_path, monkeypatch):
    """Should halt when the read itself fails, even if exists() reports False —
    an injected read failure is not mistaken for an absent sidecar."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
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
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
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
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    _write_session(plan_path, "planner", "sid-abc")
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text()) == {"planner": "sid-abc"}


def test_write_session_merges_without_clobbering_other_keys(tmp_path):
    """Should merge the new key into an existing sidecar without clobbering other keys."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
    sidecar.write_text(json.dumps({"planner": "sid-abc", "step": "planned"}))
    _write_session(plan_path, "implementer", "sid-xyz")
    assert json.loads(sidecar.read_text()) == {
        "planner": "sid-abc",
        "step": "planned",
        "implementer": "sid-xyz",
    }


def test_write_session_overwrites_existing_key(tmp_path):
    """Should overwrite the value of an existing key when writing the same key again."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
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
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
    original = b"{not valid json"
    sidecar.write_bytes(original)
    with pytest.raises(HaltError):
        _write_session(plan_path, "step", "planned")
    assert sidecar.read_bytes() == original
    assert replace_spy == []
    assert not plan_path.with_suffix(".json.tmp").exists()


def test_write_session_non_object_preserves_original(tmp_path, replace_spy):
    """Should halt and keep the original bytes when the sidecar is not a JSON object."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
    original = b"[1, 2]"
    sidecar.write_bytes(original)
    with pytest.raises(HaltError):
        _write_session(plan_path, "step", "planned")
    assert sidecar.read_bytes() == original
    assert replace_spy == []
    assert not plan_path.with_suffix(".json.tmp").exists()


def test_write_session_read_failure_preserves_original(tmp_path, monkeypatch, replace_spy):
    """Should halt and keep the original bytes when the existing sidecar cannot be read."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
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
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
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
    plan = tmp_path / ".ai-factory" / "01-work.md"
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
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    sidecar = tmp_path / ".ai-factory" / "01-foo-plan.json"
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
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
    _write_session(plan_path, "planner", "sid-abc")
    tmp_sidecar = plan_path.with_suffix(".json.tmp")
    sidecar = plan_path.with_suffix(".json")
    assert not tmp_sidecar.exists()
    assert sidecar.exists()


def test_write_session_content_is_rereadable_via_read_sessions(tmp_path):
    """Should produce sidecar content that is re-readable via _read_sessions —
    the two functions agree on path derivation and format."""
    plan_path = tmp_path / ".ai-factory" / "01-foo-plan.md"
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
# kill_active_child no-op branches
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
# kill_active_child kills a live process group
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
# kill_active_child killpg-failure fallback
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


def test_run_claude_argv_fresh_call(monkeypatch, tmp_path, clean_active_proc):
    """A fresh call's full argv: the binary, -p with the prompt, the output,
    verbose and permission flags, the role's allowed tools, the shared
    disallowed tools, then the system prompt; the environment is the
    process's own plus the shared turn variable."""
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    fake = _FakeOkProc()
    recorded: dict = {}

    def _fake_popen(*args, **kwargs):
        recorded["cmd"] = args[0]
        recorded["env"] = kwargs.get("env")
        return fake

    monkeypatch.setattr(claude_cli.subprocess, "Popen", _fake_popen)

    claude_cli._run_claude("do it", cwd=str(tmp_path), system_prompt="instructions")

    assert recorded["cmd"] == [
        "/usr/local/bin/claude",
        "-p", "do it",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep",
        "--disallowedTools", "Monitor,ScheduleWakeup,CronCreate",
        "--system-prompt", "instructions",
    ]
    assert recorded["env"] == {
        **os.environ, "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}


def test_run_claude_argv_resumed_call(monkeypatch, tmp_path, clean_active_proc):
    """A resumed call's full argv is the fresh call's with --resume <id> in
    place of the system prompt, and the environment still carries the shared
    turn variable."""
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    fake = _FakeOkProc()
    recorded: dict = {}

    def _fake_popen(*args, **kwargs):
        recorded["cmd"] = args[0]
        recorded["env"] = kwargs.get("env")
        return fake

    monkeypatch.setattr(claude_cli.subprocess, "Popen", _fake_popen)

    claude_cli._run_claude("do it", cwd=str(tmp_path), session_id="s1")

    assert recorded["cmd"] == [
        "/usr/local/bin/claude",
        "-p", "do it",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep",
        "--disallowedTools", "Monitor,ScheduleWakeup,CronCreate",
        "--resume", "s1",
    ]
    assert recorded["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


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
    """plan() raises EscalationError and the sidecar ends with step=escalated plus the
    escalation summary naming the plan file when the planner's answer ends with ESCALATION."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    backend = FakeAgentBackend(on_call=lambda call: ESCALATION_ARTIFACT, sid="sid-1")
    pr = Planner(tmp_path, backend)

    with pytest.raises(EscalationError):
        pr.plan("Task title", "Task description", plan_path)

    assert plan_path.read_text() == ESCALATION_ARTIFACT
    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions["step"] == "escalated"
    assert sessions["escalation"] == f"planner: Missing decision: pick A or B. ({plan_path})"


def test_review_escalation_raises_before_review_pass_check(tmp_path, monkeypatch):
    """review() raises EscalationError and writes the sidecar when the reviewer's
    answer ends with ESCALATION, without falling through to the REVIEW_PASS check.
    The answer is recorded at the review path before the raise."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / ".ai-factory" / "01-slug-review-1.md"
    backend = FakeAgentBackend(on_call=lambda call: ESCALATION_ARTIFACT, sid="sid-1")
    pr = CodeReviewer(tmp_path, backend)

    with pytest.raises(EscalationError):
        pr.review(plan_path, review_path, iteration_state=_ITERATION)

    assert review_path.read_text() == ESCALATION_ARTIFACT
    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions["step"] == "escalated"
    assert sessions["escalation"]


def test_review_plan_escalation_raises_and_writes_sidecar(tmp_path):
    """review_plan() raises EscalationError and writes the sidecar when the reviewer's
    answer ends with ESCALATION, without falling through to the PLAN_REVIEW_PASS check.
    The answer is recorded at the review path before the raise."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / ".ai-factory" / "01-slug-plan-review-1.md"
    backend = FakeAgentBackend(on_call=lambda call: ESCALATION_ARTIFACT, sid="sid-1")
    reviewer = PlanReviewer(tmp_path, backend)

    with pytest.raises(EscalationError):
        reviewer.review_plan(plan_path, review_path, 1)

    assert review_path.read_text() == ESCALATION_ARTIFACT
    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions["step"] == "escalated"
    assert sessions["escalation"]


def test_implement_escalation_raises_and_writes_sidecar(tmp_path):
    """implement() raises EscalationError and writes the sidecar when the implementer's
    answer ends with ESCALATION. The answer lands at the answer path and the sidecar's
    escalation records that file."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    answer_path = tmp_path / ".ai-factory" / "01-slug-implementer-1.md"
    backend = FakeAgentBackend(on_call=lambda call: ESCALATION_ARTIFACT, sid="sid-1")
    impl = Implementer(tmp_path, backend, result=WorkingTreeResult())

    with pytest.raises(EscalationError):
        impl.implement(plan_path, answer_path)

    assert answer_path.read_text() == ESCALATION_ARTIFACT
    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions["step"] == "escalated"
    assert sessions["escalation"] == f"implementer: Missing decision: pick A or B. ({answer_path})"


# ---------------------------------------------------------------------------
# Regression: REVIEW_PASS/PLAN_REVIEW_PASS with no ESCALATION behaves as before
# ---------------------------------------------------------------------------


def test_review_regression_no_escalation_returns_bool(tmp_path, monkeypatch):
    """An answer ending with REVIEW_PASS and no ESCALATION line returns True, with
    no raise, and lands verbatim at the review path."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / ".ai-factory" / "01-slug-review-1.md"
    backend = FakeAgentBackend(on_call=lambda call: "All good\n\nREVIEW_PASS", sid="sid-1")
    pr = CodeReviewer(tmp_path, backend)

    assert pr.review(plan_path, review_path, iteration_state=_ITERATION) is True

    assert review_path.read_text() == "All good\n\nREVIEW_PASS"
    sessions = json.loads(plan_path.with_suffix(".json").read_text())
    assert sessions.get("step") != "escalated"
    assert "escalation" not in sessions


def test_review_plan_regression_no_escalation_returns_bool(tmp_path):
    """An answer ending with PLAN_REVIEW_PASS and no ESCALATION line returns True, with
    no raise, and lands verbatim at the review path. The sidecar holds only the
    reviewer's session id, with no 'step' and no 'escalation'."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / ".ai-factory" / "01-slug-plan-review-1.md"
    backend = FakeAgentBackend(on_call=lambda call: "All good\n\nPLAN_REVIEW_PASS", sid="sid-1")
    reviewer = PlanReviewer(tmp_path, backend)

    assert reviewer.review_plan(plan_path, review_path, 1) is True

    assert review_path.read_text() == "All good\n\nPLAN_REVIEW_PASS"
    assert json.loads(plan_path.with_suffix(".json").read_text()) == {"plan-reviewer-1": "sid-1"}


# ---------------------------------------------------------------------------
# A review that was never written halts
# ---------------------------------------------------------------------------


def test_review_blank_answer_raises_empty_answer_error(tmp_path, monkeypatch):
    """CodeReviewer.review raises EmptyAnswerError, not a False verdict, when the
    reviewer's answer is blank. The code-reviewer write still happens, no review file is
    written, and `step` stays at its value before the turn, so a resumed run
    repeats the review."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    plan_path.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": "implemented:1"}))
    review_path = tmp_path / ".ai-factory" / "01-slug-review-1.md"
    backend = FakeAgentBackend(on_call=lambda call: "  \n", sid="sid-1")
    pr = CodeReviewer(tmp_path, backend)

    with pytest.raises(EmptyAnswerError) as exc_info:
        pr.review(plan_path, review_path, iteration_state=_ITERATION)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Agent returned an empty answer"
    assert lines[1] == str(review_path)

    assert not review_path.exists()
    assert json.loads(plan_path.with_suffix(".json").read_text()) == {
        "runtime": "claude", "step": "implemented:1", "code-reviewer": "sid-1",
    }


def test_review_plan_blank_answer_raises_empty_answer_error(tmp_path):
    """PlanReviewer.review_plan raises EmptyAnswerError, not a False verdict, when the
    agent's answer is blank. The session id is written even though no review exists,
    no review file is written, and no 'step' is written."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    review_path = tmp_path / ".ai-factory" / "01-slug-plan-review-1.md"
    backend = FakeAgentBackend(on_call=lambda call: "  \n\t\n", sid="sid-1")
    reviewer = PlanReviewer(tmp_path, backend)

    with pytest.raises(EmptyAnswerError) as exc_info:
        reviewer.review_plan(plan_path, review_path, 1)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Agent returned an empty answer"
    assert lines[1] == str(review_path)

    assert not review_path.exists()
    assert json.loads(plan_path.with_suffix(".json").read_text()) == {"plan-reviewer-1": "sid-1"}


# ---------------------------------------------------------------------------
# Roles reach the runtime only through the injected backend
# ---------------------------------------------------------------------------


def _artifact_writing_backend(plan_review_text: str = "ok\n\nPLAN_REVIEW_PASS",
                              review_text: str = "All good\n\nREVIEW_PASS") -> FakeAgentBackend:
    """A fake that writes whatever artifact the prompt names, shaped by the role;
    the plan reviewer and the code reviewer return their reviews as their answers."""

    def _write(call):
        if call.role is AgentRole.PLAN_REVIEWER:
            return plan_review_text
        if call.role is AgentRole.CODE_REVIEWER:
            return review_text
        return "# Plan"

    return FakeAgentBackend(on_call=_write)


def test_review_message_carries_plan_answer_and_no_previous_review_or_diff(tmp_path):
    """A first review carries no diff section, holds the plan and the implementer's
    answer under their headings with no path of the run's tree, and names the base
    and the iteration's commit on the run's ref."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan: The work")
    answer_path = tmp_path / ".ai-factory" / "01-slug-implementer-1.md"
    answer_path.write_text("Did the work.\nDEVIATION: said x / showed y / did z")
    review_path = tmp_path / ".ai-factory" / "01-slug-review-1.md"
    backend = FakeAgentBackend(on_call=lambda call: "All good\n\nREVIEW_PASS", sid="sid-1")
    pr = CodeReviewer(tmp_path, backend)
    iteration = IterationState(base="0" * 40, commit="1" * 40, ref="refs/orchestrator/01-slug")

    assert pr.review(plan_path, review_path, implementer_answer_path=answer_path, iteration_state=iteration) is True

    prompt = backend.calls[0].prompt
    assert (
        f"Run `git diff {iteration.base} {iteration.commit}` to see ALL changes: "
        f"the base is `{iteration.base}`, the iteration's commit is `{iteration.commit}` "
        f"on the ref `{iteration.ref}`."
    ) in prompt
    assert "git diff HEAD" not in prompt
    assert "## Diff against the base" not in prompt
    assert "## Plan\n\n# Plan: The work" in prompt
    assert "## The implementer's answer\n\nDid the work.\nDEVIATION: said x / showed y / did z" in prompt
    assert "## The previous review" not in prompt
    assert "Return your full review as your answer." in prompt
    assert "If you have no findings at all, end your answer with REVIEW_PASS on its own line." in prompt
    for path in (plan_path, review_path, answer_path):
        assert str(path) not in prompt


def test_re_review_message_keeps_the_quoted_obligations_and_carries_the_previous_review(tmp_path):
    """A re-review message carries the previous review's text under its heading,
    tells the reviewer to see the changes itself, carries no diff section and
    keeps the re-review obligations byte-identical."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    review_1 = tmp_path / ".ai-factory" / "01-slug-review-1.md"
    review_1.write_text("Finding one.")
    review_2 = tmp_path / ".ai-factory" / "01-slug-review-2.md"
    backend = FakeAgentBackend(on_call=lambda call: "All good\n\nREVIEW_PASS", sid="sid-1")
    pr = CodeReviewer(tmp_path, backend)
    iteration = IterationState(base="0" * 40, commit="1" * 40, ref="refs/orchestrator/01-slug")

    assert pr.review(plan_path, review_2, prev_review_path=review_1, iteration_state=iteration) is True

    prompt = backend.calls[0].prompt
    assert "This is a RE-REVIEW after fixes were applied to address the previous review under `## The previous review`." in prompt
    assert "Do NOT trust your session memory about file contents — the code has changed since your last pass." in prompt
    assert (
        "For each finding in the previous review, open the cited file as it is now and read it in full, "
        "quote the CURRENT content relevant to that finding, and give a verdict: Fixed or Not fixed, "
        "with the quote as evidence."
    ) in prompt
    assert "Then run the normal full review for new issues:" in prompt
    assert "Return your full review (per-finding verdicts plus any new issues) as your answer." in prompt
    assert "## The previous review\n\nFinding one." in prompt
    assert (
        f"Run `git diff {iteration.base} {iteration.commit}` to see ALL changes: "
        f"the base is `{iteration.base}`, the iteration's commit is `{iteration.commit}` "
        f"on the ref `{iteration.ref}`."
    ) in prompt
    assert "git diff HEAD" not in prompt
    assert "## Diff against the base" not in prompt
    assert str(review_1) not in prompt and str(review_2) not in prompt


def test_plan_messages_carry_the_task_and_the_revision_context(tmp_path):
    """The first planning message carries the task and asks for the plan as the answer;
    a revision message carries the current plan's and the review's text under their
    headings and names no path of either."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan: The work")
    review_path = tmp_path / ".ai-factory" / "01-slug-plan-review-1.md"
    review_path.write_text("Missing step for X.")
    backend = FakeAgentBackend(on_call=lambda call: "# Plan: The work", sid="sid-1")
    pr = Planner(tmp_path, backend)

    pr.plan("Task title", "Task description", plan_path)
    first = backend.calls[0].prompt
    assert "**Task title**" in first and "Task description" in first
    assert "Return the plan as your answer." in first
    assert "## Plan review" not in first
    assert str(plan_path) not in first

    pr.plan("Task title", "Task description", plan_path, plan_review_path=review_path)
    revision = backend.calls[1].prompt
    assert "Your plan was reviewed and has issues." in revision
    assert "Address the issues of the review below and return the revised plan as your answer." in revision
    assert "## Plan\n\n# Plan: The work" in revision
    assert "## Plan review\n\nMissing step for X." in revision
    assert str(plan_path) not in revision and str(review_path) not in revision


def test_plan_answer_lands_verbatim_at_plan_path(tmp_path):
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    backend = FakeAgentBackend(on_call=lambda call: "# Plan: Fresh\n\nbody\n", sid="sid-1")
    pr = Planner(tmp_path, backend)

    pr.plan("Task title", "Task description", plan_path)

    assert plan_path.read_text() == "# Plan: Fresh\n\nbody\n"


def test_artifacts_round_trip_through_one_utf_8_reader(tmp_path, monkeypatch):
    """An artifact _write_artifact wrote with non-ASCII text reads back identical through
    _read_artifact under a default text encoding that is not UTF-8, modelled by patching
    io.text_encoding so a missing encoding resolves to latin-1 — pathlib routes a missing
    encoding through it, so the modelled locale reaches Path.read_text and not the read."""
    path = tmp_path / ".ai-factory" / "01-slug-plan-review-1.md"
    text = "Ревью — résumé ✓\n"
    _write_artifact(path, text)

    monkeypatch.setattr(io, "text_encoding", lambda enc, *a, **k: "latin-1" if enc is None else enc)
    assert path.read_text() != text  # the modelled default reaches the read, and it is not UTF-8

    assert _read_artifact(path) == text


def test_prompt_building_reads_artifacts_as_utf_8_whatever_the_locale(tmp_path, monkeypatch):
    """A revision planning message carries a non-ASCII plan's and plan review's exact text
    under a default text encoding that is not UTF-8, the artifacts having been written by
    _write_artifact."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    _write_artifact(plan_path, "# План: работа ✓")
    review_path = tmp_path / ".ai-factory" / "01-slug-plan-review-1.md"
    _write_artifact(review_path, "Проблемы —缺少一步。")
    backend = FakeAgentBackend(on_call=lambda call: "# План: работа ✓", sid="sid-1")
    pr = Planner(tmp_path, backend)

    monkeypatch.setattr(io, "text_encoding", lambda enc, *a, **k: "latin-1" if enc is None else enc)
    pr.plan("Task title", "Task description", plan_path, plan_review_path=review_path)

    revision = backend.calls[0].prompt
    assert "## Plan\n\n# План: работа ✓" in revision
    assert "## Plan review\n\nПроблемы —缺少一步。" in revision


def test_plan_blank_answer_halts_and_leaves_the_round_recorded(tmp_path):
    """A blank planner answer raises EmptyAnswerError, leaves an existing plan exactly
    as it was and leaves step at planning:N, so a resumed run repeats the round."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan: The old plan")
    plan_path.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": "planning:2"}))
    backend = FakeAgentBackend(on_call=lambda call: " \n", sid="sid-1")
    pr = Planner(tmp_path, backend)

    with pytest.raises(EmptyAnswerError) as exc_info:
        pr.plan("Task title", "Task description", plan_path)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Agent returned an empty answer"
    assert lines[1] == str(plan_path)
    assert plan_path.read_text() == "# Plan: The old plan"
    assert json.loads(plan_path.with_suffix(".json").read_text()) == {
        "runtime": "claude", "step": "planning:2", "planner": "sid-1",
    }


def test_review_plan_message_carries_the_plan_text_and_names_no_path(tmp_path):
    """The reviewer's message carries the plan's text under a ## Plan heading, keeps the
    Check-for sentence byte-identical, and names neither the plan's path nor the
    review's path."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan: Add the door\n\n1. Do it.\n")
    review_path = tmp_path / ".ai-factory" / "01-slug-plan-review-1.md"
    backend = _artifact_writing_backend()
    reviewer = PlanReviewer(tmp_path, backend)

    assert reviewer.review_plan(plan_path, review_path, 1) is True

    prompt = backend.calls[0].prompt
    assert "## Plan\n\n# Plan: Add the door\n\n1. Do it.\n" in prompt
    assert (
        "Check for: missing steps, wrong assumptions about the codebase, "
        "architectural mistakes, missing migrations, security issues, "
        "incorrect file paths or API usage.\n"
    ) in prompt
    assert str(plan_path) not in prompt
    assert str(review_path) not in prompt


def test_planner_and_code_reviewer_pass_complete_instructions_in_their_own_sessions(tmp_path, monkeypatch):
    """Every Planner call carries the complete planner + escalation instructions and
    every CodeReviewer call the reviewer + escalation ones, resumed ones included;
    each role continues the session id it was handed and never the other's."""
    backend = _artifact_writing_backend()
    planner = Planner(tmp_path, backend)
    code_reviewer = CodeReviewer(tmp_path, backend)
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    review_1 = tmp_path / ".ai-factory" / "01-slug-review-1.md"
    review_2 = tmp_path / ".ai-factory" / "01-slug-review-2.md"

    planner.plan("Title", "Description", plan_path)
    planner.plan("Title", "Description", plan_path)
    assert code_reviewer.review(plan_path, review_1, iteration_state=_ITERATION) is True
    assert code_reviewer.review(plan_path, review_2, prev_review_path=review_1, iteration_state=_ITERATION) is True

    assert len(backend.calls) == 4
    for call in backend.calls[:2]:
        assert call.role is AgentRole.PLANNER
        assert call.instructions == planner.system_prompt
        for name in ("planner", "escalation"):
            assert _load_prompt(name) in call.instructions
    for call in backend.calls[2:]:
        assert call.role is AgentRole.CODE_REVIEWER
        assert call.instructions == code_reviewer.system_prompt
        for name in ("reviewer", "escalation"):
            assert _load_prompt(name) in call.instructions
    for call in backend.calls:
        assert call.cwd == str(tmp_path)
    assert [c.session_id for c in backend.calls] == [
        None, backend.returned[0], None, backend.returned[2],
    ]


def test_planner_test_planner_prompt_reaches_instructions(tmp_path):
    backend = _artifact_writing_backend()
    planner = Planner(tmp_path, backend, planner_prompt_name="test-planner")

    planner.plan("Title", "Description", tmp_path / ".ai-factory" / "01-slug.md")

    assert _load_prompt("test-planner") in backend.calls[0].instructions


def test_implementer_fixes_continue_session_with_complete_instructions(tmp_path):
    backend = FakeAgentBackend()
    impl = Implementer(tmp_path, backend, result=WorkingTreeResult())
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan: The work")
    feedback = tmp_path / ".ai-factory" / "01-slug-review-1.md"
    feedback.write_text("Fix the edge case.")
    answer_1 = tmp_path / ".ai-factory" / "01-slug-implementer-1.md"
    answer_2 = tmp_path / ".ai-factory" / "01-slug-implementer-2.md"

    impl.implement(plan_path, answer_1)
    impl.implement(plan_path, answer_2, feedback_path=feedback)

    first, second = backend.calls
    for call in (first, second):
        assert call.role is AgentRole.IMPLEMENTER
        assert call.instructions == impl.system_prompt
        assert _load_prompt("implementer") in call.instructions
        assert _load_prompt("escalation") in call.instructions
    assert first.session_id is None
    assert second.session_id == backend.returned[0]
    assert "## Plan\n\n# Plan: The work" in first.prompt
    assert str(plan_path) not in first.prompt
    assert "Apply the fixes of the review below." in second.prompt
    assert "## Review\n\nFix the edge case." in second.prompt
    assert str(feedback) not in second.prompt
    assert str(plan_path) not in second.prompt
    assert answer_1.read_text() == "fake output"
    assert answer_2.read_text() == "fake output"


def test_implementer_blank_answer_raises_empty_answer_error(tmp_path):
    """implement() raises EmptyAnswerError when the implementer's answer is blank.
    No answer file is written; the sidecar keeps the implementer's id and `step`
    stays at its value before the turn, so a resumed run calls the implementer again."""
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")
    plan_path.with_suffix(".json").write_text(json.dumps({"runtime": "claude", "step": "plan_reviewed"}))
    answer_path = tmp_path / ".ai-factory" / "01-slug-implementer-1.md"
    backend = FakeAgentBackend(on_call=lambda call: " \n", sid="sid-1")
    impl = Implementer(tmp_path, backend, result=WorkingTreeResult())

    with pytest.raises(EmptyAnswerError) as exc_info:
        impl.implement(plan_path, answer_path)

    lines = str(exc_info.value).splitlines()
    assert lines[0] == "Agent returned an empty answer"
    assert lines[1] == str(answer_path)
    assert not answer_path.exists()
    assert json.loads(plan_path.with_suffix(".json").read_text()) == {
        "runtime": "claude", "step": "plan_reviewed", "implementer": "sid-1",
    }


def test_implementer_continuing_session_without_feedback_sends_plan_prompt(tmp_path):
    backend = FakeAgentBackend()
    impl = Implementer(tmp_path, backend, result=WorkingTreeResult())
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan: The work")
    roadmap_path = tmp_path / "roadmap.md"

    impl.implement(plan_path, tmp_path / ".ai-factory" / "01-slug-implementer-1.md", roadmap_path=roadmap_path, line_number=4)
    impl.implement(plan_path, tmp_path / ".ai-factory" / "01-slug-implementer-2.md", feedback_path=None, roadmap_path=roadmap_path, line_number=4)

    first, second = backend.calls
    assert second.session_id == backend.returned[0]
    assert second.prompt == (
        f"Roadmap: {roadmap_path} (line 5)\n"
        "Implement the plan below.\n"
        "\n"
        "## Plan\n"
        "\n"
        "# Plan: The work\n"
        "\n"
        "Your answer states what was done, and carries one `DEVIATION: <plan said / "
        "file showed / done>` line for each point where the file disagreed with the plan."
    )
    assert "Apply the fixes" not in second.prompt


def test_implementer_fresh_session_with_feedback_path_sends_plan_prompt(tmp_path):
    backend = FakeAgentBackend()
    impl = Implementer(tmp_path, backend, result=WorkingTreeResult())
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan: The work")
    roadmap_path = tmp_path / "roadmap.md"
    feedback = tmp_path / ".ai-factory" / "01-slug-review-1.md"
    feedback.write_text("Findings.")

    impl.implement(plan_path, tmp_path / ".ai-factory" / "01-slug-implementer-1.md", feedback_path=feedback, roadmap_path=roadmap_path, line_number=4)

    call = backend.calls[0]
    assert call.session_id is None
    assert call.prompt == (
        f"Roadmap: {roadmap_path} (line 5)\n"
        "Implement the plan below.\n"
        "\n"
        "## Plan\n"
        "\n"
        "# Plan: The work\n"
        "\n"
        "Your answer states what was done, and carries one `DEVIATION: <plan said / "
        "file showed / done>` line for each point where the file disagreed with the plan."
    )
    assert "Apply the fixes" not in call.prompt


def test_plan_review_attempts_inherit_no_session(tmp_path):
    backend = _artifact_writing_backend()
    plan_path = tmp_path / ".ai-factory" / "01-slug.md"
    plan_path.write_text("# Plan")

    for attempt in (1, 2):
        reviewer = PlanReviewer(tmp_path, backend)
        review_path = tmp_path / ".ai-factory" / f"01-slug-plan-review-{attempt}.md"
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
    AgentRole.PLANNER: ("opus", "high", ["Read", "Write", "Glob", "Grep", "Bash"]),
    AgentRole.PLAN_REVIEWER: ("opus", "high", ["Read", "Write", "Glob", "Grep", "Bash"]),
    AgentRole.IMPLEMENTER: ("sonnet", "high", ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]),
    AgentRole.CODE_REVIEWER: ("opus", "high", ["Read", "Write", "Glob", "Grep", "Bash"]),
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
    agents["planner"] = {"model": "later-planner"}
    assert backend.role_settings == expected
    for role in AgentRole:
        backend.run(role=role, prompt="p", cwd="c", instructions="FULL")

    for role, call in zip(AgentRole, seen, strict=True):
        assert (call["model"], call["effort"]) == expected[role]
        assert call["allowed_tools"] == _EXPECTED_POLICY[role][2]


def test_claude_backend_policy_does_not_depend_on_instruction_text(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(claude_cli, "_CLAUDE_BIN", "/usr/local/bin/claude")
    monkeypatch.setattr(claude_cli, "_run_claude", lambda **kw: seen.append(kw) or ("out", "sid"))

    ClaudeAgentBackend().run(role=AgentRole.PLANNER, prompt="p", cwd="c", instructions="SAME")
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


def test_each_role_passes_the_exchange_its_artifact_gives(tmp_path):
    factory = tmp_path / ".ai-factory"
    plans = factory / "plans" / "john-doe"
    plans.mkdir(parents=True)
    (factory / "plan-reviews" / "john-doe").mkdir(parents=True)
    (factory / "reviews" / "john-doe").mkdir(parents=True)
    plan_path = plans / "01-slug.md"
    plan_path.write_text("# Plan")
    answer_path = factory / "reviews" / "john-doe" / "01-slug-implementer-1.md"
    answer_path.write_text("Did the work.")
    plan_review_path = factory / "plan-reviews" / "john-doe" / "01-slug-plan-review-1.md"
    plan_review_path.write_text("Review notes")
    review_1 = factory / "reviews" / "john-doe" / "01-slug-review-1.md"
    review_1.write_text("Finding one.")
    review_2 = factory / "reviews" / "john-doe" / "01-slug-review-2.md"
    iteration = IterationState(base="0" * 40, commit="1" * 40, ref="refs/orchestrator/01-slug")
    backend = FakeAgentBackend(on_call=lambda call: {
        AgentRole.PLANNER: "# Plan\n\n## Test Command\n`true`\n",
    }.get(call.role, "REVIEW_PASS\n" if call.role is not AgentRole.IMPLEMENTER else "Did the work.\n"))

    Planner(tmp_path, backend).plan("T", "D", plan_path)
    assert backend.calls[-1].exchange == "plans/john-doe/01-slug"

    Planner(tmp_path, backend).plan("T", "D", plan_path, plan_review_path=plan_review_path)
    assert backend.calls[-1].exchange == "plan-reviews/john-doe/01-slug-plan-review-1"

    PlanReviewer(tmp_path, backend).review_plan(plan_path, plan_review_path, 1)
    assert backend.calls[-1].exchange == "plan-reviews/john-doe/01-slug-plan-review-1"

    CodeReviewer(tmp_path, backend).review(plan_path, review_2, prev_review_path=review_1,
                                           iteration_state=iteration)
    assert backend.calls[-1].exchange == "reviews/john-doe/01-slug-review-2"

    Implementer(tmp_path, backend, result=WorkingTreeResult()).implement(plan_path, answer_path)
    assert backend.calls[-1].exchange is None  # the implementer passes none


# ---------------------------------------------------------------------------
# SpecLayer: the plan stage without a planner
# ---------------------------------------------------------------------------

_SPEC_TITLE = "Runtime task"
_SPEC_TEXT = "# The task's spec\n\nBuild it — über/plain, no trailing newline"


def _spec_project(tmp_path: Path, text: str = _SPEC_TEXT) -> Path:
    spec = tmp_path / ".ai-factory" / "specs" / "01-thing.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(text, encoding="utf-8")
    return spec


def _spec_description(relpath: str = ".ai-factory/specs/01-thing.md") -> str:
    return f"Do the work. Spec: `{relpath}`."


def test_spec_layer_lays_the_spec_byte_exact_under_the_plan_heading(tmp_path):
    """The laid file is the plan's heading, a blank line, then the spec's bytes unchanged."""
    _spec_project(tmp_path)
    plan = tmp_path / "plans" / "01-slug.md"

    SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(), plan)

    assert plan.read_bytes() == f"# Plan: {_SPEC_TITLE}\n\n{_SPEC_TEXT}".encode("utf-8")


def test_spec_layer_lays_a_spec_without_a_trailing_newline_and_non_ascii_text(tmp_path):
    """A spec's trailing newline, or its lack, and its non-ASCII text are kept."""
    _spec_project(tmp_path, "spec ends without newline: ünïcödé")
    plan = tmp_path / "plans" / "01-slug.md"

    SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(), plan)

    assert plan.read_bytes() == b"# Plan: Runtime task\n\nspec ends without newline: \xc3\xbcn\xc3\xafc\xc3\xb6d\xc3\xa9"


def test_spec_layer_takes_the_last_of_two_tags(tmp_path):
    """A description with two tags lays the spec the last one names."""
    first = tmp_path / ".ai-factory" / "specs" / "01-first.md"
    second = tmp_path / ".ai-factory" / "specs" / "02-second.md"
    first.parent.mkdir(parents=True)
    first.write_text("first spec", encoding="utf-8")
    second.write_text("second spec", encoding="utf-8")
    plan = tmp_path / "plans" / "01-slug.md"
    description = (
        f"Earlier note. Spec: `{first.relative_to(tmp_path).as_posix()}`. "
        f"Later note. Spec: `{second.relative_to(tmp_path).as_posix()}`."
    )

    SpecLayer(tmp_path).plan(_SPEC_TITLE, description, plan)

    assert plan.read_text(encoding="utf-8") == f"# Plan: {_SPEC_TITLE}\n\nsecond spec"


def test_spec_layer_ignores_the_other_arguments_and_repeats_the_same_bytes(tmp_path):
    """plan_review_path, roadmap_path and line_number are ignored, and a repeated call leaves the same file."""
    _spec_project(tmp_path)
    layer = SpecLayer(tmp_path)
    plan = tmp_path / "plans" / "01-slug.md"

    layer.plan(_SPEC_TITLE, _spec_description(), plan,
               plan_review_path=tmp_path / "plan-reviews" / "01.md",
               roadmap_path=tmp_path / ".ai-factory" / "ROADMAP.md", line_number=7)
    first = plan.read_bytes()
    layer.plan(_SPEC_TITLE, _spec_description(), plan,
               plan_review_path=tmp_path / "plan-reviews" / "02.md",
               roadmap_path=None, line_number=None)

    assert plan.read_bytes() == first
    assert layer.session_id is None


@pytest.mark.parametrize("description", [
    "Do the work with no tag at all.",
    "Do the work. Spec: .ai-factory/specs/01-thing.md. — a tag without backticks is no tag",
])
def test_spec_layer_without_a_tag_halts_naming_the_task(tmp_path, description):
    """A description whose last tag is absent halts; the contract line never stands in."""
    _spec_project(tmp_path)
    with pytest.raises(HaltError) as caught:
        SpecLayer(tmp_path).plan(_SPEC_TITLE, description, tmp_path / "plans" / "01-slug.md")
    first_line = str(caught.value).splitlines()[0]
    assert _SPEC_TITLE in first_line
    assert "no spec" in first_line


def test_spec_layer_refuses_a_path_outside_the_factory(tmp_path):
    """A tag naming a path that does not begin .ai-factory/ halts naming the task and the cause."""
    with pytest.raises(HaltError) as caught:
        SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description("specs/01-thing.md"),
                                 tmp_path / "plans" / "01-slug.md")
    lines = str(caught.value).splitlines()
    assert _SPEC_TITLE in lines[0]
    assert "outside .ai-factory/" in lines[0]
    assert lines[1] == "specs/01-thing.md"


def test_spec_layer_refuses_a_dot_dot_segment(tmp_path):
    """A tag holding a '..' segment halts naming the task and the cause, even under .ai-factory/."""
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    with pytest.raises(HaltError) as caught:
        SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(".ai-factory/../outside.md"),
                                 tmp_path / "plans" / "01-slug.md")
    lines = str(caught.value).splitlines()
    assert _SPEC_TITLE in lines[0]
    assert "'..' segment" in lines[0]
    assert lines[1] == ".ai-factory/../outside.md"


def test_spec_layer_refuses_a_missing_file(tmp_path):
    """A tag whose file does not exist halts naming the task and the cause."""
    with pytest.raises(HaltError) as caught:
        SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(), tmp_path / "plans" / "01-slug.md")
    lines = str(caught.value).splitlines()
    assert _SPEC_TITLE in lines[0]
    assert "no spec file" in lines[0]
    assert lines[1] == str(tmp_path / ".ai-factory" / "specs" / "01-thing.md")


def test_spec_layer_refuses_a_spec_that_is_not_utf8(tmp_path):
    """A spec that cannot be read as UTF-8 halts naming the task and the cause."""
    spec = tmp_path / ".ai-factory" / "specs" / "01-thing.md"
    spec.parent.mkdir(parents=True)
    spec.write_bytes(b"# spec\n\xff\xfe not utf-8\n")
    with pytest.raises(HaltError) as caught:
        SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(), tmp_path / "plans" / "01-slug.md")
    lines = str(caught.value).splitlines()
    assert _SPEC_TITLE in lines[0]
    assert "not UTF-8" in lines[0]
    assert lines[1] == str(spec)


def _spec_bytes_project(tmp_path: Path, data: bytes) -> Path:
    """Stand the spec file with exact bytes."""
    spec = tmp_path / ".ai-factory" / "specs" / "01-thing.md"
    spec.parent.mkdir(parents=True)
    spec.write_bytes(data)
    return spec


def test_spec_layer_lays_a_spec_with_carriage_returns_byte_exact(tmp_path):
    """`a\r\nb\rc\r\n` keeps its carriage returns: the spec's bytes are the plan's bytes."""
    _spec_bytes_project(tmp_path, b"a\r\nb\rc\r\n")
    plan = tmp_path / "plans" / "01-slug.md"

    SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(), plan)

    assert plan.read_bytes() == f"# Plan: {_SPEC_TITLE}\n\n".encode("utf-8") + b"a\r\nb\rc\r\n"


def test_spec_layer_lays_a_spec_with_a_byte_order_mark_unchanged(tmp_path):
    """A byte-order mark is a spec's byte like any other and is kept."""
    _spec_bytes_project(tmp_path, "﻿spec with a mark\n".encode("utf-8"))
    plan = tmp_path / "plans" / "01-slug.md"

    SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(), plan)

    assert plan.read_bytes() == f"# Plan: {_SPEC_TITLE}\n\n".encode("utf-8") + "﻿spec with a mark\n".encode("utf-8")


@pytest.mark.parametrize("content", [b"", b"  \n\t\n", "  \xa0\t\n".encode("utf-8")])
def test_spec_layer_refuses_an_empty_spec(tmp_path, content):
    """A spec that is empty or only whitespace halts naming the task, and no plan file is written."""
    _spec_bytes_project(tmp_path, content)
    plan = tmp_path / "plans" / "01-slug.md"

    with pytest.raises(HaltError) as caught:
        SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(), plan)

    lines = str(caught.value).splitlines()
    assert lines[0] == f"The task '{_SPEC_TITLE}' has an empty spec"
    assert lines[1] == str(tmp_path / ".ai-factory" / "specs" / "01-thing.md")
    assert not plan.exists()


def test_spec_layer_refuses_a_directory_at_the_specs_path(tmp_path):
    """A directory at the spec's path is a read failure, not a missing file."""
    (tmp_path / ".ai-factory" / "specs" / "01-thing.md").mkdir(parents=True)

    with pytest.raises(HaltError) as caught:
        SpecLayer(tmp_path).plan(_SPEC_TITLE, _spec_description(), tmp_path / "plans" / "01-slug.md")

    lines = str(caught.value).splitlines()
    assert lines[0] == f"The task '{_SPEC_TITLE}' has a spec that cannot be read"
    assert lines[1] == str(tmp_path / ".ai-factory" / "specs" / "01-thing.md")


def test_spec_layer_reads_through_a_linked_factory_directory(tmp_path):
    """.ai-factory may be a link out of the project: the path is read, never resolved."""
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "specs").mkdir(parents=True)
    (elsewhere / "specs" / "01-thing.md").write_text("spec through the link", encoding="utf-8")
    (project / ".ai-factory").symlink_to(elsewhere, target_is_directory=True)
    plan = project / "plans" / "01-slug.md"

    SpecLayer(project).plan(_SPEC_TITLE, _spec_description(), plan)

    assert plan.read_text(encoding="utf-8") == f"# Plan: {_SPEC_TITLE}\n\nspec through the link"
