"""The `dispose` command over a built project tree: a deposit of each kind,
the same deposit sent twice, a different disposition for a disposed entry,
each refusal with nothing written, and `show` before and after — the entry
ids taken from `show` as the head that runs a closure takes them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.run_tree import build

REPO = Path(__file__).resolve().parents[1]

SPEC_0300 = ".ai-factory/specs/trickster77777/0300-a-spec-a-routed-remark-lands-in.md"

# The tree's entries, each by the affects and text show reports it under.
ALPHA_LICENCE = ("unknown", "The greeting module holds no word of the licence its helper wraps.")
ALPHA_PROMOTED = ("Phase 2", "The doorway's width is a later phase's own concern.")
ALPHA_SPECCED = (
    ".ai-factory/specs/trickster77777/0288-deferred-observations-are-read-as-entries-with-dispositions.md",
    "The reading joins an entry by its affects and its text only, where the skills joined them by gist.\n"
    "  A remark reworded in a later review is another entry.",
)
BETA_ROADMAP = ("Phase 2", "The doorway's width belongs to a later phase's plan.")
BETA_SPECCED = (
    ".ai-factory/specs/trickster77777/0285-the-skills-reach-a-runs-data-only-through-the-orchestrators-commands.md",
    "The commands' deposit needs its writer spelled.",
)
BETA_LEGACY = ("unknown", "An old note whose marker no live vocabulary names.")
ETA_OBSERVATION = ("Phase 2", "the doorway's width belongs to a later phase's plan.")


def _command(root: Path, command: str, payload: bytes | None, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Run a command over a run's data as a client runs it, against the tree
    built under `root`, with the machine's base the tree carries and, for
    `dispose`, the deposit on standard input."""
    environment = dict(os.environ)
    environment["ORCHESTRATOR_CONFIG"] = str(root / "base.json")
    return subprocess.run(
        [sys.executable, "-m", "orchestrator.main", command, *arguments],
        cwd=REPO, env=environment, capture_output=True, input=payload,
    )


def _show(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return _command(root, "show", None, *arguments)


def _dispose(root: Path, payload: bytes, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return _command(root, "dispose", payload, *arguments)


def _project(tmp_path: Path) -> Path:
    return build(tmp_path)


def _observations(root: Path, *show_arguments: str) -> list[dict]:
    result = _show(root, *show_arguments)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    shown = json.loads(result.stdout.decode("utf-8"))
    return shown["observations"]


def _observation(root: Path, affects: str, text: str, *show_arguments: str) -> dict:
    for observation in _observations(root, *show_arguments):
        if observation["affects"] == affects and observation["text"] == text:
            return observation
    raise AssertionError(f"no observation {affects!r} — {text!r}")


def _entry_of(root: Path, affects: str, text: str, *show_arguments: str) -> str:
    return _observation(root, affects, text, *show_arguments)["entry"]


def _disposition_of(root: Path, affects: str, text: str, *show_arguments: str) -> dict | None:
    return _observation(root, affects, text, *show_arguments)["disposition"]


def _deposit(*items: dict) -> bytes:
    return json.dumps({"version": 1, "dispositions": list(items)}).encode("utf-8")


def _item(entry: str, disposition: str, target: object = None) -> dict:
    item: dict = {"entry": entry, "disposition": disposition}
    if target is not None:
        item["target"] = target
    return item


def _records(project: Path) -> dict[str, bytes]:
    """Every disposition record the project holds, by path and by bytes, so
    a refusal is seen to write nothing — no new record and no changed one."""
    directory = project / ".ai-factory" / "dispositions"
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _assert_disposed(project: Path, expected_entries: list[str], payload: bytes) -> None:
    result = _dispose(project.parent, payload, "--project-dir", str(project))
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout.decode("utf-8") == json.dumps(
        {"version": 1, "disposed": expected_entries}, ensure_ascii=False, indent=2,
    ) + "\n"


def _assert_refused(project: Path, line: str, payload: bytes) -> None:
    result = _dispose(project.parent, payload, "--project-dir", str(project))
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == f"{line}\n".encode("utf-8")


# ---------------------------------------------------------------------------
# --- a deposit of each kind ---
# ---------------------------------------------------------------------------

def test_a_fixed_deposit_over_a_marker_s_entry(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *ALPHA_LICENCE, "--project-dir", str(project), "1.1")
    assert _disposition_of(project.parent, *ALPHA_LICENCE, "--project-dir", str(project), "1.1") == {"kind": "dismissed"}

    _assert_disposed(project, [entry], _deposit(_item(entry, "fixed")))

    record = json.loads((project / ".ai-factory" / "dispositions" / "01-1-1-alpha.json").read_text(encoding="utf-8"))
    assert record[entry] == {"kind": "fixed"}
    assert _disposition_of(project.parent, *ALPHA_LICENCE, "--project-dir", str(project), "1.1") == {"kind": "fixed"}


def test_a_dismissed_deposit_over_a_legacy_marker(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *BETA_LEGACY, "--project-dir", str(project), "1.2")
    assert _disposition_of(project.parent, *BETA_LEGACY, "--project-dir", str(project), "1.2") == {"kind": "legacy"}

    _assert_disposed(project, [entry], _deposit(_item(entry, "dismissed")))

    record = json.loads((project / ".ai-factory" / "dispositions" / "02-1-2-beta.json").read_text(encoding="utf-8"))
    assert record[entry] == {"kind": "dismissed"}
    assert _disposition_of(project.parent, *BETA_LEGACY, "--project-dir", str(project), "1.2") == {"kind": "dismissed"}


def test_a_routed_deposit_names_the_open_task_s_spec(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *ALPHA_PROMOTED, "--project-dir", str(project), "1.1")
    assert _disposition_of(project.parent, *ALPHA_PROMOTED, "--project-dir", str(project), "1.1") == {"kind": "legacy"}

    target = {"spec": SPEC_0300}
    _assert_disposed(project, [entry], _deposit(_item(entry, "routed", target)))

    record = json.loads((project / ".ai-factory" / "dispositions" / "01-1-1-alpha.json").read_text(encoding="utf-8"))
    assert record[entry] == {"kind": "routed", "target": target}
    assert _disposition_of(project.parent, *ALPHA_PROMOTED, "--project-dir", str(project), "1.1") == {
        "kind": "routed",
        "target": target,
    }


def test_a_routed_deposit_names_a_phase_of_the_roadmap(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *BETA_SPECCED, "--project-dir", str(project), "1.2")
    marker = _disposition_of(project.parent, *BETA_SPECCED, "--project-dir", str(project), "1.2")
    assert marker is not None and marker["kind"] == "routed"

    target = {"roadmap": ".ai-factory/ROADMAP.md", "phase": 3}
    _assert_disposed(project, [entry], _deposit(_item(entry, "routed", target)))

    record = json.loads((project / ".ai-factory" / "dispositions" / "02-1-2-beta.json").read_text(encoding="utf-8"))
    assert record[entry] == {"kind": "routed", "target": target}
    assert _disposition_of(project.parent, *BETA_SPECCED, "--project-dir", str(project), "1.2") == {
        "kind": "routed",
        "target": target,
    }


def test_a_deposit_under_a_named_roadmap_writes_the_segment_s_record(tmp_path):
    project = _project(tmp_path)
    arguments = ("--project-dir", str(project), "--roadmap", "roadmaps/named.md", "7.1")
    entry = _entry_of(project.parent, *ETA_OBSERVATION, *arguments)
    assert _disposition_of(project.parent, *ETA_OBSERVATION, *arguments) is None

    _assert_disposed(project, [entry], _deposit(_item(entry, "dismissed")))

    record = json.loads((project / ".ai-factory" / "dispositions" / "named" / "01-7-1-eta.json").read_text(encoding="utf-8"))
    assert record == {entry: {"kind": "dismissed"}}
    assert _disposition_of(project.parent, *ETA_OBSERVATION, *arguments) == {"kind": "dismissed"}


def test_a_deposit_spanning_two_tasks_writes_each_task_s_record(tmp_path):
    project = _project(tmp_path)
    fixed = _entry_of(project.parent, *ALPHA_LICENCE, "--project-dir", str(project), "1.1")
    dismissed = _entry_of(project.parent, *BETA_LEGACY, "--project-dir", str(project), "1.2")

    _assert_disposed(project, [fixed, dismissed], _deposit(_item(fixed, "fixed"), _item(dismissed, "dismissed")))

    alpha = json.loads((project / ".ai-factory" / "dispositions" / "01-1-1-alpha.json").read_text(encoding="utf-8"))
    beta = json.loads((project / ".ai-factory" / "dispositions" / "02-1-2-beta.json").read_text(encoding="utf-8"))
    assert alpha[fixed] == {"kind": "fixed"}
    assert beta[dismissed] == {"kind": "dismissed"}


# ---------------------------------------------------------------------------
# --- idempotence ---
# ---------------------------------------------------------------------------

def test_the_same_deposit_sent_twice_changes_nothing(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *BETA_SPECCED, "--project-dir", str(project), "1.2")
    payload = _deposit(_item(entry, "dismissed"))
    _assert_disposed(project, [entry], payload)
    record = (project / ".ai-factory" / "dispositions" / "02-1-2-beta.json").read_bytes()

    _assert_disposed(project, [entry], payload)

    assert (project / ".ai-factory" / "dispositions" / "02-1-2-beta.json").read_bytes() == record


def test_a_different_disposition_for_a_disposed_entry_refuses_naming_the_one_it_holds(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *BETA_SPECCED, "--project-dir", str(project), "1.2")
    _assert_disposed(project, [entry], _deposit(_item(entry, "dismissed")))
    before = _records(project)

    _assert_refused(
        project,
        f"Entry {entry!r} already holds the disposition " + json.dumps({"kind": "dismissed"}, ensure_ascii=False),
        _deposit(_item(entry, "fixed")),
    )
    assert _records(project) == before


def test_a_different_disposition_for_a_disposition_the_tree_held_refuses_naming_it(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *ALPHA_SPECCED, "--project-dir", str(project), "1.1")
    before = _records(project)

    held = {"kind": "routed", "target": {"spec": SPEC_0300}}
    _assert_refused(
        project,
        f"Entry {entry!r} already holds the disposition " + json.dumps(held, ensure_ascii=False),
        _deposit(_item(entry, "fixed")),
    )
    assert _records(project) == before


# ---------------------------------------------------------------------------
# --- each refusal, with nothing written ---
# ---------------------------------------------------------------------------

def _fresh_entry(project: Path) -> str:
    return _entry_of(project.parent, *BETA_SPECCED, "--project-dir", str(project), "1.2")


def test_input_that_is_not_utf8_or_json_refuses(tmp_path):
    project = _project(tmp_path)
    before = _records(project)
    _assert_refused(project, "The deposit is not valid UTF-8", b"\xff\xfe not utf-8\n")
    _assert_refused(project, "The deposit is not valid JSON", b"{")
    _assert_refused(project, "The deposit is not valid JSON", b"")
    assert _records(project) == before


def test_a_deposit_that_is_not_the_object_of_version_and_dispositions_refuses(tmp_path):
    project = _project(tmp_path)
    before = _records(project)
    _assert_refused(project, "The deposit is not an object of version and dispositions", b"[1]")
    _assert_refused(project, "The deposit is not an object of version and dispositions", b"{}")
    _assert_refused(project, "The deposit is not an object of version and dispositions",
                    b'{"version": 1}')
    _assert_refused(project, "The deposit is not an object of version and dispositions",
                    b'{"dispositions": []}')
    assert _records(project) == before


def test_a_version_the_command_does_not_support_refuses(tmp_path):
    project = _project(tmp_path)
    before = _records(project)
    _assert_refused(project, "The deposit's version is not supported: 2",
                    b'{"version": 2, "dispositions": []}')
    _assert_refused(project, "The deposit's version is not supported: '1'",
                    b'{"version": "1", "dispositions": []}')
    _assert_refused(project, "The deposit's version is not supported: True",
                    b'{"version": true, "dispositions": []}')
    assert _records(project) == before


def test_dispositions_that_are_not_a_list_refuse(tmp_path):
    project = _project(tmp_path)
    before = _records(project)
    _assert_refused(project, "The deposit's dispositions are not a list",
                    b'{"version": 1, "dispositions": {}}')
    assert _records(project) == before


def test_an_item_that_is_not_an_object_of_entry_and_disposition_refuses(tmp_path):
    project = _project(tmp_path)
    before = _records(project)
    _assert_refused(project, "A disposition of the deposit is not an object of entry and disposition",
                    b'{"version": 1, "dispositions": ["x"]}')
    _assert_refused(project, "A disposition of the deposit is not an object of entry and disposition",
                    b'{"version": 1, "dispositions": [{"entry": "x"}]}')
    _assert_refused(project, "A disposition of the deposit is not an object of entry and disposition",
                    b'{"version": 1, "dispositions": [{"disposition": "fixed"}]}')
    assert _records(project) == before


def test_an_entry_no_task_holds_refuses(tmp_path):
    project = _project(tmp_path)
    before = _records(project)
    _assert_refused(project, "The deposit names an entry no task holds: '0000000000000000000000000000000000000000000000000000000000000000'",
                    _deposit(_item("0" * 64, "fixed")))
    # An entry that is no name at all is an entry no task holds.
    _assert_refused(project, "The deposit names an entry no task holds: ['not a name']",
                    b'{"version": 1, "dispositions": [{"entry": ["not a name"], "disposition": "fixed"}]}')
    assert _records(project) == before


def test_an_entry_the_deposit_names_twice_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)
    _assert_refused(project, f"The deposit names an entry twice: {entry!r}",
                    _deposit(_item(entry, "fixed"), _item(entry, "fixed")))
    assert _records(project) == before


def test_a_disposition_outside_the_three_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)
    _assert_refused(project, "A disposition of the deposit is none of fixed, routed and dismissed: 'legacy'",
                    _deposit(_item(entry, "legacy")))
    assert _records(project) == before


def test_a_routed_disposition_without_a_target_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)
    _assert_refused(project, f"A routed disposition of the deposit names no target: {entry!r}",
                    _deposit(_item(entry, "routed")))
    assert _records(project) == before


def test_a_disposition_other_than_routed_with_a_target_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)
    _assert_refused(project, f"A disposition of the deposit that is not routed names a target: {entry!r}",
                    _deposit(_item(entry, "fixed", {"spec": SPEC_0300})))
    _assert_refused(project, f"A disposition of the deposit that is not routed names a target: {entry!r}",
                    _deposit(_item(entry, "dismissed", {"roadmap": ".ai-factory/ROADMAP.md", "phase": 3})))
    assert _records(project) == before


def test_a_target_no_open_task_s_spec_names_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)
    line = f"The target of an entry of the deposit is neither an open task's spec nor a phase of the roadmap: {entry!r}"

    # A spec that does not exist.
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"spec": ".ai-factory/specs/trickster77777/0400-no-spec-at-all.md"})))
    # A spec that exists but no open task names.
    unnamed = project / ".ai-factory" / "specs" / "trickster77777" / "0401-a-spec-no-task-names.md"
    unnamed.parent.mkdir(parents=True, exist_ok=True)
    unnamed.write_text("# A spec no task names\n", encoding="utf-8")
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"spec": ".ai-factory/specs/trickster77777/0401-a-spec-no-task-names.md"})))
    # A spec a task names whose line is checked.
    roadmap = project / ".ai-factory" / "ROADMAP.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8").replace("- [ ] **3.1 — Theta**", "- [x] **3.1 — Theta**"),
        encoding="utf-8",
    )
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"spec": SPEC_0300})))
    # A path outside `.ai-factory/`, and one with a `..` segment.
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"spec": "specs/trickster77777/0300-a-spec-a-routed-remark-lands-in.md"})))
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"spec": ".ai-factory/../specs/trickster77777/0300-a-spec-a-routed-remark-lands-in.md"})))
    # A target that is neither form.
    _assert_refused(project, line, _deposit(_item(entry, "routed", {".ai-factory/ROADMAP.md": SPEC_0300})))
    _assert_refused(project, line, _deposit(_item(entry, "routed", SPEC_0300)))
    assert _records(project) == before


def test_a_roadmap_target_the_named_file_does_not_hold_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)
    line = f"The target of an entry of the deposit is neither an open task's spec nor a phase of the roadmap: {entry!r}"

    # A phase the roadmap does not hold.
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"roadmap": ".ai-factory/ROADMAP.md", "phase": 9})))
    # A roadmap file that does not exist.
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"roadmap": ".ai-factory/roadmaps/none.md", "phase": 1})))
    # A path outside `.ai-factory/`, with a `..` segment, and a phase that is no integer.
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"roadmap": "ROADMAP.md", "phase": 1})))
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"roadmap": ".ai-factory/../ROADMAP.md", "phase": 1})))
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"roadmap": ".ai-factory/ROADMAP.md", "phase": "3"})))
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"roadmap": ".ai-factory/ROADMAP.md", "phase": True})))
    assert _records(project) == before


def test_a_roadmap_target_that_cannot_be_read_or_decoded_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)
    line = f"The target of an entry of the deposit is neither an open task's spec nor a phase of the roadmap: {entry!r}"

    # A roadmap file that is not UTF-8 holds no heading that can be read.
    (project / ".ai-factory" / "bin.md").write_bytes(b"\xff\xfe not utf-8\n")
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"roadmap": ".ai-factory/bin.md", "phase": 1})))
    # A roadmap file the command cannot read holds none either.
    locked = project / ".ai-factory" / "locked.md"
    locked.write_text("## Phase 1 — behind a lock\n", encoding="utf-8")
    locked.chmod(0o000)
    _assert_refused(project, line, _deposit(_item(entry, "routed", {"roadmap": ".ai-factory/locked.md", "phase": 1})))
    assert _records(project) == before


def test_a_roadmap_file_that_cannot_be_read_names_no_spec(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    # An unrelated roadmap file that is not UTF-8 names no spec, and it does
    # not keep the command from reading the other specs or writing the record.
    (project / ".ai-factory" / "roadmaps" / "corrupt.md").write_bytes(b"\xff\xfe not utf-8\n")

    _assert_disposed(project, [entry], _deposit(_item(entry, "routed", {"spec": SPEC_0300})))

    record = json.loads((project / ".ai-factory" / "dispositions" / "02-1-2-beta.json").read_text(encoding="utf-8"))
    assert record[entry] == {"kind": "routed", "target": {"spec": SPEC_0300}}


def test_an_invalid_item_beside_a_valid_one_writes_nothing(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)

    _assert_refused(
        project,
        "The deposit names an entry no task holds: '1'",
        _deposit(_item(entry, "fixed"), _item("1", "fixed")),
    )
    assert _records(project) == before


def test_a_disposed_entry_beside_a_fresh_one_writes_nothing(tmp_path):
    project = _project(tmp_path)
    disposed = _entry_of(project.parent, *ALPHA_SPECCED, "--project-dir", str(project), "1.1")
    fresh = _entry_of(project.parent, *BETA_LEGACY, "--project-dir", str(project), "1.2")
    before = _records(project)

    _assert_refused(
        project,
        f"Entry {disposed!r} already holds the disposition "
        + json.dumps({"kind": "routed", "target": {"spec": SPEC_0300}}, ensure_ascii=False),
        _deposit(_item(fresh, "dismissed"), _item(disposed, "fixed")),
    )
    assert _records(project) == before


# ---------------------------------------------------------------------------
# --- the record the command reads ---
# ---------------------------------------------------------------------------

def test_a_corrupt_disposition_record_halts_as_a_sidecar_does(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    (project / ".ai-factory" / "dispositions" / "02-1-2-beta.json").write_text("{", encoding="utf-8")
    before = _records(project)

    result = _dispose(project.parent, _deposit(_item(entry, "fixed")), "--project-dir", str(project))
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.decode("utf-8").splitlines()[0] == "Disposition record is not valid JSON"
    assert _records(project) == before


def test_a_disposition_record_that_is_not_an_object_halts(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    (project / ".ai-factory" / "dispositions" / "02-1-2-beta.json").write_text("[]", encoding="utf-8")

    result = _dispose(project.parent, _deposit(_item(entry, "fixed")), "--project-dir", str(project))
    assert result.returncode == 1
    assert result.stderr.decode("utf-8").splitlines()[0] == "Disposition record JSON is not an object"


# ---------------------------------------------------------------------------
# --- the command's own edges ---
# ---------------------------------------------------------------------------

def test_an_empty_deposit_prints_no_disposed_and_writes_nothing(tmp_path):
    project = _project(tmp_path)
    before = _records(project)
    _assert_disposed(project, [], _deposit())
    assert _records(project) == before


def test_an_unknown_option_refuses_as_one_line(tmp_path):
    project = _project(tmp_path)
    result = _dispose(project.parent, b"", "--project-dir", str(project), "--bogus")
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"unrecognized arguments: --bogus\n"
