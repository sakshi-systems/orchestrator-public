"""The `dispose` command over a built project tree: a deposit of each kind,
the same deposit sent twice, a different disposition for a disposed entry,
each refusal with nothing written, and `show` before and after — the entry
ids taken from `show` as the head that runs a closure takes them."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.conftest import git_environment
from tests.run_tree import build

REPO = Path(__file__).resolve().parents[1]

SPEC_0300 = ".ai-factory/specs/trickster77777/0300-a-spec-a-routed-remark-lands-in.md"
SPEC_0301 = ".ai-factory/specs/trickster77777/0301-a-spec-of-the-run-s-own-roadmap-s-open-task.md"

# The roadmap the configuration names, a file outside the default pair and
# `roadmaps/`: its open task's spec is the target only a run of the project
# can name.
RUN_ROADMAP = (
    "# The run's roadmap\n"
    "\n"
    "## Phase 4 — the run's own doors\n"
    "\n"
    f"- [ ] **4.1 — Iota** — the open task of the run's own roadmap — Spec: `{SPEC_0301}`\n"
)

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
    built under `root`, with the machine's base the tree carries, git's
    configuration files silenced so the command reads no config of the
    machine it runs on, and, for `dispose`, the deposit on standard
    input."""
    environment = git_environment()
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


def _item(entry: str, kind: str, target: object = None) -> dict:
    """A deposit's item in the shape the interface takes: the entry id, and
    the disposition as one object of its `kind` and, where it is routed, its
    `target` — the object `show` reports, passed back as it was read."""
    disposition: dict = {"kind": kind}
    if target is not None:
        disposition["target"] = target
    return {"entry": entry, "disposition": disposition}


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


def _with_run_settings(project: Path, settings: dict) -> None:
    """Give the project a person's run settings, the only place a value that
    shapes a run — `roadmap_path` among them — is read from: a git identity
    the slug derives from, run@example.com → `run`, and the settings file
    under that slug."""
    environment = git_environment()
    subprocess.run(["git", "init", "-q", str(project)], env=environment, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "run@example.com"],
        cwd=project, env=environment, check=True, capture_output=True,
    )
    directory = project / ".ai-factory" / "run-settings"
    directory.mkdir()
    (directory / "run.json").write_text(json.dumps(settings), encoding="utf-8")


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


def test_a_routed_deposit_names_a_spec_of_the_roadmap_the_run_takes(tmp_path):
    project = _project(tmp_path)
    # The person's settings name a roadmap outside the default pair and
    # `roadmaps/`, whose open task's spec no other roadmap file names: the
    # roadmap a run of the project takes, and only it, holds the target.
    (project / SPEC_0301).write_text(
        "# A spec of the run's roadmap's open task\n\nThe run's roadmap holds the task.\n",
        encoding="utf-8",
    )
    (project / ".ai-factory" / "run.md").write_text(RUN_ROADMAP, encoding="utf-8")
    _with_run_settings(project, {"roadmap_path": "run.md"})
    arguments = ("--project-dir", str(project), "--roadmap", "ROADMAP.md", "1.1")

    entry = _entry_of(project.parent, *ALPHA_PROMOTED, *arguments)
    target = {"spec": SPEC_0301}
    _assert_disposed(project, [entry], _deposit(_item(entry, "routed", target)))

    record = json.loads((project / ".ai-factory" / "dispositions" / "01-1-1-alpha.json").read_text(encoding="utf-8"))
    assert record[entry] == {"kind": "routed", "target": target}
    assert _disposition_of(project.parent, *ALPHA_PROMOTED, *arguments) == {"kind": "routed", "target": target}


def test_a_routed_deposit_refuses_where_the_run_s_roadmap_cannot_be_resolved(tmp_path):
    project = _project(tmp_path)
    # The person's settings name the literal "my", whose roadmap the
    # project holds nowhere — a resolution that halts, which the deposit's
    # route refuses on as the other commands do.
    _with_run_settings(project, {"roadmap_path": "my"})
    entry = _entry_of(project.parent, *ALPHA_PROMOTED, "--project-dir", str(project), "--roadmap", "ROADMAP.md", "1.1")
    before = _records(project)

    _assert_refused(
        project,
        "Named roadmap not found",
        _deposit(_item(entry, "routed", {"spec": SPEC_0300})),
    )

    assert _records(project) == before


def test_a_deposit_with_no_routed_entry_does_not_resolve_the_run_s_roadmap(tmp_path):
    project = _project(tmp_path)
    # The same unresolvable "my", a deposit with no routed entry in it: the
    # roadmap is resolved only where a routed disposition asks for the open
    # specs, so this deposit is taken as it stands.
    _with_run_settings(project, {"roadmap_path": "my"})
    entry = _entry_of(project.parent, *ALPHA_LICENCE, "--project-dir", str(project), "--roadmap", "ROADMAP.md", "1.1")

    _assert_disposed(project, [entry], _deposit(_item(entry, "fixed")))


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


def test_a_disposition_read_from_show_is_taken_as_the_deposit_s_own(tmp_path):
    project = _project(tmp_path)
    arguments = ("--project-dir", str(project), "1.1")
    # A client passes back what it read: the disposition object `show`
    # reports — a marker's dismissed, a record's routed — is deposited as it
    # stands, taken apart by no one; the marker-read entry is a fresh
    # deposit, and the record's own object one already held.
    dismissed = _disposition_of(project.parent, *ALPHA_LICENCE, *arguments)
    routed = _disposition_of(project.parent, *ALPHA_SPECCED, *arguments)
    assert dismissed == {"kind": "dismissed"}
    assert routed == {"kind": "routed", "target": {"spec": SPEC_0300}}
    licence = _entry_of(project.parent, *ALPHA_LICENCE, *arguments)
    specced = _entry_of(project.parent, *ALPHA_SPECCED, *arguments)

    _assert_disposed(
        project, [licence, specced],
        _deposit({"entry": licence, "disposition": dismissed}, {"entry": specced, "disposition": routed}),
    )

    record = json.loads((project / ".ai-factory" / "dispositions" / "01-1-1-alpha.json").read_text(encoding="utf-8"))
    assert record[licence] == {"kind": "dismissed"}
    assert record[specced] == {"kind": "routed", "target": {"spec": SPEC_0300}}


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


def test_a_routed_deposit_replayed_once_its_task_is_done_is_accepted(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *ALPHA_PROMOTED, "--project-dir", str(project), "1.1")
    target = {"spec": SPEC_0300}
    _assert_disposed(project, [entry], _deposit(_item(entry, "routed", target)))

    # Theta, the open task whose spec the deposit routed to, has since run.
    roadmap = project / ".ai-factory" / "ROADMAP.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8").replace("- [ ] **3.1 — Theta**", "- [x] **3.1 — Theta**"),
        encoding="utf-8",
    )
    before = _records(project)

    _assert_disposed(project, [entry], _deposit(_item(entry, "routed", target)))

    assert _records(project) == before


def test_a_replayed_routed_deposit_is_not_checked_against_its_target_at_all(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *ALPHA_PROMOTED, "--project-dir", str(project), "1.1")
    target = {"spec": SPEC_0300}
    _assert_disposed(project, [entry], _deposit(_item(entry, "routed", target)))

    # The spec the deposit named is gone whole, and no open task names it.
    (project / SPEC_0300).unlink()
    before = _records(project)

    _assert_disposed(project, [entry], _deposit(_item(entry, "routed", target)))

    assert _records(project) == before


def test_a_first_routing_to_the_spec_of_a_done_task_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _entry_of(project.parent, *BETA_LEGACY, "--project-dir", str(project), "1.2")
    roadmap = project / ".ai-factory" / "ROADMAP.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8").replace("- [ ] **3.1 — Theta**", "- [x] **3.1 — Theta**"),
        encoding="utf-8",
    )
    before = _records(project)

    _assert_refused(
        project,
        f"The target of an entry of the deposit is neither an open task's spec "
        f"nor a phase of the roadmap: {entry!r}",
        _deposit(_item(entry, "routed", {"spec": SPEC_0300})),
    )
    assert _records(project) == before


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
                    b'{"version": 1, "dispositions": [{"disposition": {"kind": "fixed"}}]}')
    assert _records(project) == before


def test_a_disposition_that_is_not_the_object_show_reports_refuses(tmp_path):
    project = _project(tmp_path)
    entry = _fresh_entry(project)
    before = _records(project)
    line = "A disposition of the deposit is not an object of kind and, for routed, target"

    # A string — the shape an older deposit sent — and every other value that
    # is not the object `show` reports, an object without its `kind` among
    # them.
    for disposition in ("fixed", ["fixed"], 5, None, True, {"target": {"spec": SPEC_0300}}):
        _assert_refused(project, line, _deposit({"entry": entry, "disposition": disposition}))
    assert _records(project) == before


def test_an_entry_no_task_holds_refuses(tmp_path):
    project = _project(tmp_path)
    before = _records(project)
    _assert_refused(project, "The deposit names an entry no task holds: '0000000000000000000000000000000000000000000000000000000000000000'",
                    _deposit(_item("0" * 64, "fixed")))
    # An entry that is no name at all is an entry no task holds.
    _assert_refused(project, "The deposit names an entry no task holds: ['not a name']",
                    b'{"version": 1, "dispositions": [{"entry": ["not a name"], "disposition": {"kind": "fixed"}}]}')
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
