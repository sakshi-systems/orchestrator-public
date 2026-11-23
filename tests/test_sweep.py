"""The `sweep` command over a built project tree: a finished phase swept to
nothing while another phase's tree stands, each verify kind taken by its own
roadmap's sweep alone; a phase that holds an open task refused with nothing
removed; a phase the roadmap lacks refused; and the named roadmap's segment
and the flat pair both, an unfinished group of the same slug staying."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.conftest import git_environment
from tests.run_tree import build

REPO = Path(__file__).resolve().parents[1]


def _sweep(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Run the sweep command as a client runs it, against the tree built
    under `root`, with the machine's base the tree carries and git's
    configuration files silenced, so the command reads no config of the
    machine it runs on."""
    environment = git_environment()
    environment["ORCHESTRATOR_CONFIG"] = str(root / "base.json")
    return subprocess.run(
        [sys.executable, "-m", "orchestrator.main", "sweep", *arguments],
        cwd=REPO, env=environment, capture_output=True,
    )


def _project(tmp_path: Path) -> Path:
    return build(tmp_path)


def _factory_files(project: Path) -> dict[str, bytes]:
    """Every file under `.ai-factory`, by its path relative to the factory
    and by its bytes, so a refusal is seen to remove nothing and a sweep's
    whole reach is seen at once."""
    factory = project / ".ai-factory"
    return {
        path.relative_to(factory).as_posix(): path.read_bytes()
        for path in sorted(factory.rglob("*"))
        if path.is_file()
    }


def _check_lines(project: Path, relpath: str, numbers: tuple[str, ...]) -> None:
    """Check the roadmap lines of the given task numbers — the closed phase
    the sweep takes."""
    path = project / ".ai-factory" / relpath
    lines = path.read_text(encoding="utf-8").splitlines()
    wanted = list(numbers)
    for i, line in enumerate(lines):
        for number in wanted:
            if line.lstrip().startswith(f"- [ ] **{number} "):
                lines[i] = line.replace("- [ ]", "- [x]", 1)
                wanted.remove(number)
    assert not wanted, f"lines not found: {wanted}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _done(project: Path, stem: str, segment: str | None = None) -> None:
    """Close a group's record the way a landed task's reads."""
    directory = project / ".ai-factory" / "plans"
    if segment is not None:
        directory = directory / segment
    (directory / f"{stem}.json").write_text(json.dumps({"step": "done"}), encoding="utf-8")


def _write(project: Path, dirname: str, name: str, text: str = "planted\n", segment: str | None = None) -> None:
    directory = project / ".ai-factory" / dirname
    if segment is not None:
        directory = directory / segment
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def _assert_swept(project: Path, before: dict[str, bytes], swept: set[str]) -> None:
    """Exactly the named files are gone and every other byte of the tree
    stands as it stood."""
    assert _factory_files(project) == {
        name: content for name, content in before.items() if name not in swept
    }


def _assert_refused(project: Path, line: str, *arguments: str) -> None:
    before = _factory_files(project)
    result = _sweep(project.parent, "--project-dir", str(project), *arguments)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == f"{line}\n".encode("utf-8")
    assert _factory_files(project) == before


# ---------------------------------------------------------------------------
# --- a finished phase, swept to nothing ---
# ---------------------------------------------------------------------------

def test_a_finished_phase_is_swept_to_nothing_while_another_phase_s_tree_stands(tmp_path):
    project = _project(tmp_path)
    _check_lines(project, "ROADMAP.md", ("1.2", "1.3", "1.4"))
    _done(project, "02-1-2-beta")
    _done(project, "03-1-3-gamma")
    # A finished group of Phase 2, and a test run planted on a Phase 1 slug:
    # the sweep of the review roadmap takes neither.
    _write(project, "plans", "05-2-1-epsilon.md", "# Plan: 2.1 — Epsilon\n")
    _done(project, "05-2-1-epsilon")
    _write(project, "plan-reviews", "05-2-1-epsilon-plan-review-1.md")
    _write(project, "reviews", "05-2-1-epsilon-implementer-1.md")
    _write(project, "reviews", "05-2-1-epsilon-review-1.md")
    _write(project, "dispositions", "05-2-1-epsilon.json", "{}\n")
    _write(project, "test-runs", "01-1-1-alpha-test-1.txt")
    before = _factory_files(project)

    result = _sweep(project.parent, "--project-dir", str(project), "--phase", "1")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == json.dumps({"version": 1, "phase": 1}, indent=2).encode("utf-8") + b"\n"
    _assert_swept(project, before, {
        "plans/01-1-1-alpha.md", "plans/01-1-1-alpha.json",
        "plans/02-1-2-beta.md", "plans/02-1-2-beta.json",
        "plans/03-1-3-gamma.md", "plans/03-1-3-gamma.json",
        "plan-reviews/01-1-1-alpha-plan-review-1.md",
        "plan-reviews/01-1-1-alpha-plan-review-2.md",
        "plan-reviews/02-1-2-beta-plan-review-1.md",
        "plan-reviews/03-1-3-gamma-plan-review-1.md",
        "reviews/01-1-1-alpha-implementer-1.md",
        "reviews/01-1-1-alpha-review-1.md",
        "reviews/02-1-2-beta-implementer-1.md",
        "reviews/02-1-2-beta-review-1.md",
        "reviews/03-1-3-gamma-implementer-1.md",
        "dispositions/01-1-1-alpha.json",
        "dispositions/02-1-2-beta.json",
    })


def test_a_sweep_repeated_after_it_ran_prints_the_same_object_and_removes_nothing(tmp_path):
    project = _project(tmp_path)
    _check_lines(project, "ROADMAP.md", ("1.2", "1.3", "1.4"))
    _done(project, "02-1-2-beta")
    _done(project, "03-1-3-gamma")
    first = _sweep(project.parent, "--project-dir", str(project), "--phase", "1")
    assert first.returncode == 0, first.stderr.decode("utf-8")
    after_first = _factory_files(project)

    second = _sweep(project.parent, "--project-dir", str(project), "--phase", "1")

    assert second.returncode == 0, second.stderr.decode("utf-8")
    assert second.stdout == first.stdout
    assert _factory_files(project) == after_first


def test_a_task_past_the_breakpoint_marker_is_swept_with_its_phase(tmp_path):
    project = _project(tmp_path)
    _check_lines(project, "ROADMAP.md", ("2.1", "2.2"))
    for stem in ("05-2-1-epsilon", "06-2-2-zeta"):
        _write(project, "plans", f"{stem}.md", f"# Plan: {stem}\n")
        _done(project, stem)
        _write(project, "plan-reviews", f"{stem}-plan-review-1.md")
        _write(project, "reviews", f"{stem}-implementer-1.md")
        _write(project, "reviews", f"{stem}-review-1.md")
    before = _factory_files(project)

    result = _sweep(project.parent, "--project-dir", str(project), "--phase", "2")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == json.dumps({"version": 1, "phase": 2}, indent=2).encode("utf-8") + b"\n"
    _assert_swept(project, before, {
        "plans/05-2-1-epsilon.md", "plans/05-2-1-epsilon.json",
        "plans/06-2-2-zeta.md", "plans/06-2-2-zeta.json",
        "plan-reviews/05-2-1-epsilon-plan-review-1.md",
        "plan-reviews/06-2-2-zeta-plan-review-1.md",
        "reviews/05-2-1-epsilon-implementer-1.md",
        "reviews/05-2-1-epsilon-review-1.md",
        "reviews/06-2-2-zeta-implementer-1.md",
        "reviews/06-2-2-zeta-review-1.md",
    })


def test_a_group_an_earlier_attempt_left_unfinished_stays(tmp_path):
    project = _project(tmp_path)
    _check_lines(project, "ROADMAP.md", ("1.2", "1.3", "1.4"))
    _done(project, "03-1-3-gamma")
    # Beta's line is checked but its group never closed: the group stays
    # whole, and Alpha's finished group still goes.
    before = _factory_files(project)

    result = _sweep(project.parent, "--project-dir", str(project), "--phase", "1")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == json.dumps({"version": 1, "phase": 1}, indent=2).encode("utf-8") + b"\n"
    _assert_swept(project, before, {
        "plans/01-1-1-alpha.md", "plans/01-1-1-alpha.json",
        "plan-reviews/01-1-1-alpha-plan-review-1.md",
        "plan-reviews/01-1-1-alpha-plan-review-2.md",
        "reviews/01-1-1-alpha-implementer-1.md",
        "reviews/01-1-1-alpha-review-1.md",
        "dispositions/01-1-1-alpha.json",
        "plans/03-1-3-gamma.md", "plans/03-1-3-gamma.json",
        "plan-reviews/03-1-3-gamma-plan-review-1.md",
        "reviews/03-1-3-gamma-implementer-1.md",
    })


# ---------------------------------------------------------------------------
# --- the refusals ---
# ---------------------------------------------------------------------------

def test_a_phase_that_holds_an_open_task_is_refused_and_nothing_is_removed(tmp_path):
    project = _project(tmp_path)
    _assert_refused(
        project, "Phase 1 holds a task that is not done: 1.2, 1.3, 1.4", "--phase", "1",
    )


def test_an_open_task_past_the_breakpoint_marker_refuses_the_sweep(tmp_path):
    project = _project(tmp_path)
    _check_lines(project, "ROADMAP.md", ("2.1",))
    _assert_refused(
        project, "Phase 2 holds a task that is not done: 2.2", "--phase", "2",
    )


def test_a_phase_the_roadmap_lacks_is_refused(tmp_path):
    project = _project(tmp_path)
    _assert_refused(project, "No phase 9 in the roadmap", "--phase", "9")


def test_a_phase_that_is_not_a_number_is_one_the_roadmap_lacks(tmp_path):
    project = _project(tmp_path)
    _assert_refused(project, "No phase 'x' in the roadmap", "--phase", "x")


def test_a_missing_phase_is_refused(tmp_path):
    project = _project(tmp_path)
    _assert_refused(project, "A phase is required: sweep --phase N")


def test_an_unknown_option_is_refused_as_one_line(tmp_path):
    project = _project(tmp_path)
    _assert_refused(
        project, "unrecognized arguments: --bogus", "--bogus", "--phase", "1",
    )


def test_a_group_whose_record_cannot_be_read_halts_the_sweep_before_anything_is_removed(tmp_path):
    project = _project(tmp_path)
    _check_lines(project, "ROADMAP.md", ("1.2", "1.3", "1.4"))
    _done(project, "03-1-3-gamma")
    (project / ".ai-factory" / "plans" / "01-1-1-alpha.json").write_text("not json", encoding="utf-8")
    before = _factory_files(project)

    result = _sweep(project.parent, "--project-dir", str(project), "--phase", "1")

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"Sidecar is not valid JSON\n"
    assert _factory_files(project) == before


def test_a_later_group_s_unreadable_record_halts_before_the_earlier_groups_are_removed(tmp_path):
    project = _project(tmp_path)
    _check_lines(project, "ROADMAP.md", ("1.2", "1.3", "1.4"))
    _done(project, "02-1-2-beta")
    (project / ".ai-factory" / "plans" / "03-1-3-gamma.json").write_text("not json", encoding="utf-8")

    # Every group's record is read before the first group is removed, so the
    # halt names the record and leaves the whole phase's tree standing —
    # Alpha's and Beta's included, though their own records read fine.
    _assert_refused(project, "Sidecar is not valid JSON", "--phase", "1")


# ---------------------------------------------------------------------------
# --- the named roadmap's segment and the flat pair both ---
# ---------------------------------------------------------------------------

def test_the_named_roadmap_s_segment_is_swept_and_its_unfinished_group_stays(tmp_path):
    project = _project(tmp_path)
    _check_lines(project, "roadmaps/named.md", ("7.1",))
    _done(project, "01-7-1-eta", segment="named")
    # An unfinished attempt of the same slug, a disposition record under the
    # segment and a test run of the review roadmap's kind: the first stays
    # whole, the record goes and the test run stays.
    _write(project, "plans", "02-7-1-eta.md", "# Plan: 7.1 — Eta\n", segment="named")
    _write(project, "plans", "02-7-1-eta.json", json.dumps({"step": "implemented:1"}), segment="named")
    _write(project, "plan-reviews", "02-7-1-eta-plan-review-1.md", segment="named")
    _write(project, "reviews", "02-7-1-eta-implementer-1.md", segment="named")
    _write(project, "dispositions", "01-7-1-eta.json", "{}\n", segment="named")
    _write(project, "test-runs", "01-7-1-eta-test-1.txt", segment="named")
    before = _factory_files(project)

    result = _sweep(project.parent, "--project-dir", str(project), "--roadmap", "roadmaps/named.md", "--phase", "7")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == json.dumps({"version": 1, "phase": 7}, indent=2).encode("utf-8") + b"\n"
    _assert_swept(project, before, {
        "plans/named/01-7-1-eta.md", "plans/named/01-7-1-eta.json",
        "plan-reviews/named/01-7-1-eta-plan-review-1.md",
        "reviews/named/01-7-1-eta-implementer-1.md",
        "dispositions/named/01-7-1-eta.json",
    })
    # The directories a sweep empties stay.
    factory = project / ".ai-factory"
    assert (factory / "plans" / "named").is_dir()
    assert (factory / "plan-reviews" / "named").is_dir()
    assert (factory / "reviews" / "named").is_dir()
    assert (factory / "dispositions" / "named").is_dir()


def test_the_flat_pair_s_test_roadmap_is_swept_by_its_own_kind(tmp_path):
    project = _project(tmp_path)
    (project / ".ai-factory" / "ROADMAP_TESTS.md").write_text(
        "# Test roadmap\n\n## Phase 4 — coverage\n\n- [x] **Cover Alpha** — a flat task, done\n",
        encoding="utf-8",
    )
    _done(project, "04-cover-alpha")
    # A review planted on the same stem: the test roadmap's sweep never
    # removes it.
    _write(project, "reviews", "04-cover-alpha-review-1.md")
    before = _factory_files(project)

    result = _sweep(project.parent, "--project-dir", str(project), "--roadmap", "ROADMAP_TESTS.md", "--phase", "4")

    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == json.dumps({"version": 1, "phase": 4}, indent=2).encode("utf-8") + b"\n"
    _assert_swept(project, before, {
        "plans/04-cover-alpha.md", "plans/04-cover-alpha.json",
        "plan-reviews/04-cover-alpha-plan-review-1.md",
        "plan-reviews/04-cover-alpha-plan-review-2.md",
        "reviews/04-cover-alpha-implementer-1.md",
        "test-runs/04-cover-alpha-test-1.txt",
    })
