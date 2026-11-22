"""The commands over a run's data: `show` returns what the orchestrator holds
for a task or a phase as versioned JSON, content by step name and never a
path; `rollback` takes a task back to a position `show` listed for it;
`sweep` removes the run's artifacts of a finished phase; and `dispose` keeps
a closure's deposit of each deferred observation's disposition — so a client
reaches a run's data through the interface alone.

The behaviour is docs/features/run-data.md; the values — the commands'
arguments, the JSON's fields, the positions and the refusals — are
docs/reference/run-data.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import NamedTuple, NoReturn

from .agents import _SPEC_TAG
from .artifact_layout import (
    DISPOSITIONS_DIRNAME,
    PLAN_REVIEWS_DIRNAME,
    PLANS_DIRNAME,
    REVIEWS_DIRNAME,
    TEST_RUNS_DIRNAME,
    artifact_dir,
    disposition_name,
    implementer_answer_pattern,
    plan_groups,
    plan_review_pattern,
    read_file_name,
    review_pattern,
    sidecar_path,
    test_run_pattern,
)
from .config import OrchestratorConfig, validate_roadmap_path
from .dispositions import merge_items, read_record, record_path
from .errors import RunDataRefusal
from .exchange import remove_task
from .git_runner import _changed_paths, _paths_of, _untracked_paths, _working_changes, run_git_sync
from .implementer_result import ref_name_of
from .iteration_ref import ref_of, remove_iteration_refs
from .layout import _artifact_subdir, _resolve_roadmap_relpath
from .observations import collect_entries
from .protocol import _has_escalation, _read_artifact, _read_sessions, _replace_sessions
from .resume import select_task_group
from .roadmap import PHASE_HEADING_RE, Task, find_task, parse_roadmap

# The version of the JSON the commands print: a field added keeps it, and any
# change to or removal of a command, a field, a token or a refusal takes the
# next.
RUN_DATA_VERSION = 1

# The positions a task can be taken back to, in the order `show` lists them.
_START = "start"
_PLAN_REVIEW = "plan-review"
_VERIFY = "verify"
_IMPLEMENT = "implement"

# The record's step each position but `start` leaves — `start` deletes the
# record whole — and the positions that drop each session beside the
# escalation every position clears. The implementer's session survives
# `verify`, which goes on checking the tree it wrote; the code reviewer's
# session goes under every position, with the reviews it remembers.
_STEP_OF_POSITION = {
    _PLAN_REVIEW: "planned:1",
    _VERIFY: "implemented:1",
    _IMPLEMENT: "planning:1",
}
_DROPS_IMPLEMENTER = (_PLAN_REVIEW, _IMPLEMENT)
_DROPS_CODE_REVIEWER = (_PLAN_REVIEW, _VERIFY, _IMPLEMENT)

# The role of each step, in the spelling the configuration gives it — the
# names the `roles` object keys take.
_ROLE_OF_STEP = {
    "plan": "planner",
    "plan-review": "plan_reviewer",
    "implement": "implementer",
    "review": "code_reviewer",
    "test-run": "code_reviewer",
}

# The steps whose artifacts a reviewer writes and may leave deferred
# observations in; the implementer's answer, a plan and a test run carry
# none.
_OBSERVATION_STEPS = ("plan-review", "review")

# The dispositions a closure disposes an entry in, the three a deposit may
# hold.
_KINDS = ("fixed", "routed", "dismissed")


class TaskGroup(NamedTuple):
    """A task's located group on the roadmap's plans axis: the group's stem,
    its plan's path, the sequence number and the slug."""

    group: str
    plan_path: Path
    seq: str
    slug: str


def locate_task(project_dir: Path, roadmap_relpath: str, task: Task) -> TaskGroup | None:
    """The task's group on the roadmap's plans axis, by the run's own
    selection: the lowest-numbered group of the slug whose sidecar is not
    ``done``, and where every group of the slug is finished the
    highest-numbered finished one. None where the slug has no group at all —
    no attempt has begun."""
    plans_dir = artifact_dir(project_dir, PLANS_DIRNAME, _artifact_subdir(roadmap_relpath))
    if not plans_dir.is_dir():
        return None
    selected = select_task_group(plans_dir, task.slug)
    if selected is None:
        groups = plan_groups(plans_dir, task.slug)
        if not groups:
            return None
        selected = max(groups.values(), key=lambda path: int(path.stem.split("-", 1)[0]))
    seq = selected.stem.split("-", 1)[0]
    return TaskGroup(selected.stem, selected, seq, task.slug)


def refuse(exception: BaseException) -> NoReturn:
    """Print a refusal's first line on standard error and exit with status 1.

    A refusal prints nothing on standard output: the object is built whole
    before any of it is written.
    """
    message = str(exception)
    line = message.splitlines()[0] if message else type(exception).__name__
    sys.stderr.write(f"{line}\n")
    sys.exit(1)


def show(config: OrchestratorConfig, project_dir: Path, *, roadmap: str | None = None, task: str | None = None, phase: str | None = None) -> None:
    """The `show` command: print what the orchestrator holds for the addressed
    task or phase as one JSON object, UTF-8 through the standard output, its
    `version` first and its keys in the order the reference gives them.

    `roadmap` is the ``--roadmap`` option's value, taking the roadmap a run of
    the project would take where none is given; `task` is the task's ``N.M``
    or a flat task's title, and `phase` a phase's number as written — exactly
    one of the two. Refuses through RunDataRefusal, HaltError or the
    configuration's SystemExit, which the dispatch prints as its first line on
    standard error with status 1.
    """
    if task is not None:
        if phase is not None:
            raise RunDataRefusal("A selector is doubled: give a task or --phase, not both")
    elif phase is None:
        raise RunDataRefusal("A selector is missing: give a task or --phase")
    validate_roadmap_path(roadmap, None)
    relpath = _resolve_roadmap_relpath(config, project_dir, roadmap)
    roadmap_path = project_dir / ".ai-factory" / relpath
    if not roadmap_path.is_file():
        raise RunDataRefusal(f"The roadmap does not exist: .ai-factory/{relpath}")
    listed = parse_roadmap(roadmap_path, keep_after_breakpoint=True)
    if task is None:
        # A task was not given, so a phase was — the selector check above saw
        # to it, restated here so the branch reads on the phase alone; a phase
        # that is not a number is one the roadmap lacks.
        if phase is None:
            raise RunDataRefusal("A selector is missing: give a task or --phase")
        if not (phase.isascii() and phase.isdigit()):
            raise RunDataRefusal(f"No phase {phase!r} in the roadmap")
        number = int(phase)
        under_phase = [entry for entry in listed.tasks if entry.phase == number]
        if not under_phase:
            raise RunDataRefusal(f"No phase {number} in the roadmap")
        shown = _phase_object(project_dir, relpath, number, under_phase)
    else:
        shown = _task_object(project_dir, relpath, find_task(listed.tasks, task))
    _print_object(shown)


def _print_object(shown: dict) -> None:
    """Print one JSON object whole, UTF-8 through the standard output, its
    keys in the order it was built with: the object is built before any of
    it is written."""
    payload = json.dumps(shown, ensure_ascii=False, indent=2)
    sys.stdout.buffer.write(payload.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


def positions_of(project_dir: Path, task: Task, group: TaskGroup | None) -> list[str]:
    """The position tokens the task can be taken back to, in the order the
    reference lists them.

    `start` where an attempt exists and the task is not done; `plan-review`
    where the plan exists and the record holds the planner; `verify` where
    the plan exists and the working tree holds a change outside `.ai-factory`
    against HEAD, read by git; `implement` where the record holds no planner.
    A task that is done and a task no attempt has begun list none."""
    if group is None or task.done:
        return []
    sessions = _read_sessions(group.plan_path)
    positions: list[str] = []
    positions.append(_START)
    plan_exists = group.plan_path.is_file()
    if plan_exists and "planner" in sessions:
        positions.append(_PLAN_REVIEW)
    if plan_exists and _holds_a_change(project_dir):
        positions.append(_VERIFY)
    if "planner" not in sessions:
        positions.append(_IMPLEMENT)
    return positions


def rollback(config: OrchestratorConfig, project_dir: Path, *, roadmap: str | None = None, task: str | None = None, position: str | None = None) -> None:
    """The `rollback` command: take the addressed task back to the position
    `show` listed for it and print `version` and `position`.

    `roadmap` is the ``--roadmap`` option's value, taking the roadmap a run of
    the project would take where none is given; `task` is the task's ``N.M``
    or a flat task's title, and `position` one of the tokens `show` lists.
    Refuses through RunDataRefusal, HaltError or the configuration's
    SystemExit, which the dispatch prints as its first line on standard error
    with status 1.

    The order is the record first for every position but `start`, then the
    artifacts; `start` goes on to the tree, the task's iteration refs and the
    sidecar last. Each step states the end state, so a command repeated after
    a crash finishes what the crash left: `positions_of` still lists the
    position until its last step is done."""
    if task is None or position is None:
        raise RunDataRefusal("A task and a position are required: rollback TASK POSITION")
    validate_roadmap_path(roadmap, None)
    relpath = _resolve_roadmap_relpath(config, project_dir, roadmap)
    roadmap_path = project_dir / ".ai-factory" / relpath
    if not roadmap_path.is_file():
        raise RunDataRefusal(f"The roadmap does not exist: .ai-factory/{relpath}")
    listed = parse_roadmap(roadmap_path, keep_after_breakpoint=True)
    addressed = find_task(listed.tasks, task)
    address = addressed.number if addressed.number is not None else addressed.title
    if addressed.done:
        raise RunDataRefusal(f"Task '{address}' is done: it takes no position")
    group = locate_task(project_dir, relpath, addressed)
    if group is None:
        raise RunDataRefusal(f"Task '{address}' has no attempt: there is nothing to roll back")
    positions = positions_of(project_dir, addressed, group)
    if position not in positions:
        raise RunDataRefusal(
            f"Position '{position}' is not one task '{address}' can take: "
            + (", ".join(positions) if positions else "none")
        )
    if position == _START:
        _remove_artifacts(project_dir, relpath, group, _START)
        _restore_tree(project_dir, ref_of(ref_name_of(project_dir, group.plan_path)))
        # The removal's own warning line — a remote that cannot be reached —
        # goes to standard error for this command, so standard output holds
        # the one JSON object and nothing else.
        with contextlib.redirect_stdout(sys.stderr):
            remove_iteration_refs(project_dir, group.plan_path)
        _delete_sidecar(group.plan_path)
    else:
        _replace_sessions(group.plan_path, _rolled_back_record(_read_sessions(group.plan_path), position))
        _remove_artifacts(project_dir, relpath, group, position)
    _print_object({"version": RUN_DATA_VERSION, "position": position})


def sweep(config: OrchestratorConfig, project_dir: Path, *, roadmap: str | None = None, phase: str | None = None) -> None:
    """The `sweep` command: remove the run's artifacts of every task the
    roadmap lists under the addressed phase, and print `version` and `phase`.

    `roadmap` is the ``--roadmap`` option's value, taking the roadmap a run of
    the project would take where none is given; `phase` is the phase's number
    as written, the tasks past a ``---STOP---`` marker among them. Every task
    of the phase must be done: a task whose line is not checked is named by
    its address and nothing is removed, so the artifacts of a task in flight
    are never taken. Of each task of the phase, every finished group of its
    slug in the roadmap's segment goes; a group no line of the roadmap names
    stays, and so does a group an earlier attempt left unfinished. The
    directories a sweep empties stay, and the roadmap's own files are never
    touched. Refuses through RunDataRefusal, HaltError or the configuration's
    SystemExit, which the dispatch prints as its first line on standard error
    with status 1.
    """
    if phase is None:
        raise RunDataRefusal("A phase is required: sweep --phase N")
    validate_roadmap_path(roadmap, None)
    relpath = _resolve_roadmap_relpath(config, project_dir, roadmap)
    roadmap_path = project_dir / ".ai-factory" / relpath
    if not roadmap_path.is_file():
        raise RunDataRefusal(f"The roadmap does not exist: .ai-factory/{relpath}")
    listed = parse_roadmap(roadmap_path, keep_after_breakpoint=True)
    # A phase that is not a number is one the roadmap lacks, as `show` reads
    # it.
    if not (phase.isascii() and phase.isdigit()):
        raise RunDataRefusal(f"No phase {phase!r} in the roadmap")
    number = int(phase)
    under_phase = [entry for entry in listed.tasks if entry.phase == number]
    if not under_phase:
        raise RunDataRefusal(f"No phase {number} in the roadmap")
    not_done = [entry for entry in under_phase if not entry.done]
    if not_done:
        addresses = ", ".join(
            entry.number if entry.number is not None else entry.title
            for entry in not_done
        )
        raise RunDataRefusal(f"Phase {number} holds a task that is not done: {addresses}")
    plans_dir = artifact_dir(project_dir, PLANS_DIRNAME, _artifact_subdir(relpath))
    to_sweep: list[Path] = []
    for task in under_phase:
        groups = plan_groups(plans_dir, task.slug)
        for plan_path in sorted(groups.values(), key=lambda path: int(path.stem.split("-", 1)[0])):
            # Every group's record is read before the first group is removed,
            # so a record the run cannot read halts the sweep with the
            # phase's tree standing as it stood; a group whose record is not
            # `done` is an attempt in flight, or one left unfinished, and
            # stays whole.
            if _read_sessions(plan_path).get("step") != "done":
                continue
            to_sweep.append(plan_path)
    for plan_path in to_sweep:
        _sweep_group(project_dir, relpath, plan_path)
    _print_object({"version": RUN_DATA_VERSION, "phase": number})


def dispose(project_dir: Path) -> None:
    """The `dispose` command: take the closure's deposit from standard input —
    the disposition of each deferred observation it names, as UTF-8 JSON —
    and keep it in the orchestrator's own record, one per task, printing
    `version` and `disposed`, the entries the deposit named.

    The command takes no roadmap: each entry is resolved by the plan reviews
    and reviews of every group of the project, an entry id naming its own
    task. The whole deposit is checked before any record is written, and a
    deposit that names an entry already disposed the same way changes
    nothing, so a head resuming its closure re-sends the deposit without
    harm. Refuses through RunDataRefusal or HaltError, which the dispatch
    prints as its first line on standard error with status 1.
    """
    items = _deposit_items(_read_deposit())
    entries: list[str] = []
    for entry, _kind, _target in items:
        if entry in entries:
            raise RunDataRefusal(f"The deposit names an entry twice: {entry!r}")
        entries.append(entry)
    known = _entry_groups(project_dir)
    for entry, _kind, _target in items:
        if not isinstance(entry, str) or entry not in known:
            raise RunDataRefusal(f"The deposit names an entry no task holds: {entry!r}")
    _check_targets(project_dir, items)
    records: dict[tuple[str | None, str], dict] = {}
    for entry, kind, target in items:
        group = known[entry]
        if group not in records:
            seq, _, slug = group[1].partition("-")
            records[group] = read_record(record_path(project_dir, group[0], seq, slug))
        held = records[group].get(entry)
        if held is not None and held != _carried(kind, target):
            raise RunDataRefusal(
                f"Entry {entry!r} already holds the disposition {json.dumps(held, ensure_ascii=False)}"
            )
    additions: dict[tuple[str | None, str], dict[str, dict]] = {}
    for entry, kind, target in items:
        group = known[entry]
        if records[group].get(entry) is None:
            additions.setdefault(group, {})[entry] = _carried(kind, target)
    for group, to_write in additions.items():
        seq, _, slug = group[1].partition("-")
        merge_items(record_path(project_dir, group[0], seq, slug), to_write)
    _print_object({"version": RUN_DATA_VERSION, "disposed": entries})


def _read_deposit() -> dict:
    """The deposit on standard input, read as UTF-8 JSON: an object of
    `version` and `dispositions`, the version the one this interface holds.

    Refuses input that is not valid UTF-8 or JSON, a deposit that is not the
    object of `version` and `dispositions`, and a version it does not
    support.
    """
    try:
        text = sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError as e:
        raise RunDataRefusal("The deposit is not valid UTF-8") from e
    try:
        deposit = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise RunDataRefusal("The deposit is not valid JSON") from e
    if not isinstance(deposit, dict) or "version" not in deposit or "dispositions" not in deposit:
        raise RunDataRefusal("The deposit is not an object of version and dispositions")
    if type(deposit["version"]) is not int or deposit["version"] != RUN_DATA_VERSION:
        raise RunDataRefusal(f"The deposit's version is not supported: {deposit['version']!r}")
    return deposit


def _deposit_items(deposit: dict) -> list[tuple[str, str, dict | None]]:
    """The deposit's items as (entry, disposition, target), each checked for
    its own form: an object of `entry` and `disposition`, the disposition one
    of the three, and a `target` only where the disposition is `routed`,
    which names one."""
    listed = deposit["dispositions"]
    if not isinstance(listed, list):
        raise RunDataRefusal("The deposit's dispositions are not a list")
    items: list[tuple[str, str, dict | None]] = []
    for given in listed:
        if not isinstance(given, dict) or "entry" not in given or "disposition" not in given:
            raise RunDataRefusal("A disposition of the deposit is not an object of entry and disposition")
        entry, kind, target = given["entry"], given["disposition"], given.get("target")
        if kind not in _KINDS:
            raise RunDataRefusal(f"A disposition of the deposit is none of fixed, routed and dismissed: {kind!r}")
        if kind == "routed" and target is None:
            raise RunDataRefusal(f"A routed disposition of the deposit names no target: {entry!r}")
        if kind != "routed" and target is not None:
            raise RunDataRefusal(f"A disposition of the deposit that is not routed names a target: {entry!r}")
        items.append((entry, kind, target))
    return items


def _check_targets(project_dir: Path, items: list[tuple[str, str, dict | None]]) -> None:
    """Check every routed item's target: an existing spec under
    `.ai-factory/` without a `..` segment that an open task's `Spec:` tag
    names, or a phase heading present in the named roadmap file."""
    if not any(kind == "routed" for _entry, kind, _target in items):
        return
    open_specs = _open_specs(project_dir)
    for entry, kind, target in items:
        if kind == "routed" and not _valid_target(project_dir, target, open_specs):
            raise RunDataRefusal(
                f"The target of an entry of the deposit is neither an open task's spec "
                f"nor a phase of the roadmap: {entry!r}"
            )


def _valid_target(project_dir: Path, target: object, open_specs: set[str]) -> bool:
    """True where `target` names an existing spec an open task's `Spec:` tag
    names, or a phase heading present in the named roadmap file; anything
    else is no target a closure may route to."""
    if not isinstance(target, dict):
        return False
    if set(target) == {"spec"}:
        spec = target["spec"]
        return (
            isinstance(spec, str)
            and spec.startswith(".ai-factory/")
            and ".." not in spec.split("/")
            and (project_dir / spec).is_file()
            and spec in open_specs
        )
    if set(target) == {"roadmap", "phase"}:
        roadmap, phase = target["roadmap"], target["phase"]
        if not isinstance(roadmap, str) or not roadmap.startswith(".ai-factory/") or ".." in roadmap.split("/"):
            return False
        path = project_dir / roadmap
        return (
            isinstance(phase, int) and not isinstance(phase, bool)
            and path.is_file()
            and _holds_phase(path, phase)
        )
    return False


def _holds_phase(path: Path, phase: int) -> bool:
    """True where the roadmap file holds a heading naming `phase`, read as
    the roadmap's own task lines read the section they sit under. A file
    that cannot be read or decoded holds no heading, so the target naming it
    is no phase of the roadmap."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            found = PHASE_HEADING_RE.match(stripped.lstrip("#").strip())
            if found is not None and int(found.group(1)) == phase:
                return True
    return False


def _open_specs(project_dir: Path) -> set[str]:
    """The specs the project's open tasks name: every roadmap file's tasks
    whose line is not checked, each its `Spec:` tag's path — the last tag a
    line carries, the one a run lays. A roadmap file that cannot be read or
    decoded names no spec, whatever the deposit names."""
    specs: set[str] = set()
    for path in _roadmap_files(project_dir):
        try:
            listed = parse_roadmap(path, keep_after_breakpoint=True)
        except (OSError, UnicodeDecodeError):
            continue
        for task in listed.tasks:
            if task.done:
                continue
            tags = _SPEC_TAG.findall(task.description)
            if tags:
                specs.add(tags[-1])
    return specs


def _roadmap_files(project_dir: Path) -> list[Path]:
    """Every roadmap file the project holds: the default pair and every file
    under `roadmaps/`."""
    factory = project_dir / ".ai-factory"
    candidates = [factory / "ROADMAP.md", factory / "ROADMAP_TESTS.md"]
    roads = factory / "roadmaps"
    if roads.is_dir():
        candidates.extend(sorted(roads.rglob("*.md")))
    return [path for path in candidates if path.is_file()]


def _entry_groups(project_dir: Path) -> dict[str, tuple[str | None, str]]:
    """Every entry id the project's reviews hold, mapped to its task's group
    — (segment, stem) — over the plan reviews and reviews of every group of
    the project: the command takes no roadmap, so an entry id names its own
    task."""
    plans = project_dir / ".ai-factory" / PLANS_DIRNAME
    if not plans.is_dir():
        return {}
    directories: list[tuple[str | None, Path]] = [(None, plans)]
    directories.extend((child.name, child) for child in sorted(plans.iterdir()) if child.is_dir())
    known: dict[str, tuple[str | None, str]] = {}
    for segment, directory in directories:
        for plan in directory.glob("*.md"):
            if plan.is_dir():
                continue
            seq, separator, slug = plan.stem.partition("-")
            if not (separator and seq.isascii() and seq.isdigit() and slug):
                continue
            rounds: list[tuple[str, int, str]] = []
            for step, dirname, pattern in (
                ("plan-review", PLAN_REVIEWS_DIRNAME, plan_review_pattern(seq, slug)),
                ("review", REVIEWS_DIRNAME, review_pattern(seq, slug)),
            ):
                for number, path in _rounds(artifact_dir(project_dir, dirname, segment), pattern):
                    rounds.append((step, number, _read_artifact(path)))
            for entry in collect_entries(segment, plan.stem, rounds):
                known.setdefault(entry["entry"], (segment, plan.stem))
    return known


def _carried(kind: str, target: dict | None) -> dict:
    """A disposition's JSON form: its `kind`, and its `target` where it is
    routed."""
    carried: dict = {"kind": kind}
    if target is not None:
        carried["target"] = target
    return carried


def _delete_sidecar(plan_path: Path) -> None:
    """Delete the group's record whole — `start`'s last step, so the group
    stops existing only once every other step of the position has stated its
    end state."""
    sidecar_path(plan_path).unlink(missing_ok=True)


def _rolled_back_record(sessions: dict, position: str) -> dict:
    """The record a position leaves the task's group with: its own step, the
    implementer's session dropped where the next run implements afresh, the
    code reviewer's session dropped under every position — its reviews are
    discarded, so the next one reads them with no memory of them — the
    escalation cleared under every position, and every other key — the
    runtime, the planner, the elapsed time among them — kept as it stands."""
    record = dict(sessions)
    record["step"] = _STEP_OF_POSITION[position]
    if position in _DROPS_IMPLEMENTER:
        record.pop("implementer", None)
    if position in _DROPS_CODE_REVIEWER:
        record.pop("code-reviewer", None)
    record.pop("escalation", None)
    return record


def _remove_artifacts(project_dir: Path, relpath: str, group: TaskGroup, position: str) -> None:
    """Remove the run's artifacts the position discards: every review, the
    implementer's answer and every test run under every position; every plan
    review besides for `plan-review` and `start`; the plan file and the
    task's disposition record for `start` alone, the reviews the record
    describes going with the position. The record is not an artifact: each
    position writes or deletes it in its own step, and a file that does not
    exist is not an error."""
    segment = _artifact_subdir(relpath)
    seq, slug = group.seq, group.slug
    listings: list[tuple[str, str]] = [
        (REVIEWS_DIRNAME, implementer_answer_pattern(seq, slug)),
        (REVIEWS_DIRNAME, review_pattern(seq, slug)),
        (TEST_RUNS_DIRNAME, test_run_pattern(seq, slug)),
    ]
    if position in (_PLAN_REVIEW, _START):
        listings.append((PLAN_REVIEWS_DIRNAME, plan_review_pattern(seq, slug)))
    if position == _START:
        listings.append((DISPOSITIONS_DIRNAME, disposition_name(seq, slug)))
    for dirname, pattern in listings:
        directory = artifact_dir(project_dir, dirname, segment)
        for path in directory.glob(pattern):
            path.unlink(missing_ok=True)
    if position == _START:
        group.plan_path.unlink(missing_ok=True)


def _verify_listing(relpath: str, seq: str, slug: str) -> tuple[str, str]:
    """The verify artifacts a run of the roadmap writes for a task — the
    artifact directory's name and the listing pattern of its files — a review,
    or a test run where the roadmap is a run's test sibling, the sibling
    spelling the artifact segment itself derives from: a sweep of one roadmap
    never removes the verify artifacts of the other kind its segment may
    hold."""
    if relpath == "ROADMAP_TESTS.md" or Path(relpath).stem.endswith("-tests"):
        return (TEST_RUNS_DIRNAME, test_run_pattern(seq, slug))
    return (REVIEWS_DIRNAME, review_pattern(seq, slug))


def _sweep_group(project_dir: Path, relpath: str, plan_path: Path) -> None:
    """Remove one finished group's run: every plan review, the implementer's
    answers, the verify artifacts of the roadmap's kind and the disposition
    record, then the plan, the task's exchange files and its iteration refs —
    whose failures only warn, as they do where a task lands — and the sidecar
    last, so the group stops existing only once every other step has stated
    its end state and a sweep repeated after a crash finishes what the crash
    left. A file that does not exist is not an error."""
    segment = _artifact_subdir(relpath)
    seq, slug = plan_path.stem.split("-", 1)
    verify_dirname, verify_pattern = _verify_listing(relpath, seq, slug)
    listings: list[tuple[str, str]] = [
        (PLAN_REVIEWS_DIRNAME, plan_review_pattern(seq, slug)),
        (REVIEWS_DIRNAME, implementer_answer_pattern(seq, slug)),
        (verify_dirname, verify_pattern),
        (DISPOSITIONS_DIRNAME, disposition_name(seq, slug)),
    ]
    for dirname, pattern in listings:
        for path in artifact_dir(project_dir, dirname, segment).glob(pattern):
            path.unlink(missing_ok=True)
    plan_path.unlink(missing_ok=True)
    remove_task(project_dir, plan_path)
    # The removal's own warning line — a remote that cannot be reached — goes
    # to standard error for this command, so standard output holds the one
    # JSON object and nothing else.
    with contextlib.redirect_stdout(sys.stderr):
        remove_iteration_refs(project_dir, plan_path)
    sidecar_path(plan_path).unlink(missing_ok=True)


def _holds_a_change(project_dir: Path) -> bool:
    """True where the working tree holds a change outside `.ai-factory`
    against HEAD, read by git: a path of the working tree or the index that
    differs from HEAD, or an untracked file git lists. A project git cannot
    read holds no change."""
    changed = _working_changes(project_dir, "HEAD") | _untracked_paths(project_dir)
    return bool(_outside_factory(changed))


def _outside_factory(paths: set[str]) -> set[str]:
    """The paths that lie outside `.ai-factory`, in git's coordinate system
    relative to the project directory."""
    return {
        path for path in paths
        if not (path == ".ai-factory" or path.startswith(".ai-factory/"))
    }


def _restore_tree(project_dir: Path, ref: str) -> None:
    """Restore the working tree and the index outside `.ai-factory` to the
    commit the attempt began on — the implementer's result discarded,
    whichever implementer made it.

    The paths to change are those of the working tree and the index that
    differ from the base, with the untracked files git lists — untracked and
    not ignored — and the paths HEAD moved over since the base, all outside
    `.ai-factory`, less the files git lists as untracked and ignored: a
    `.venv`, build output or a `.env` is never touched. A path the index
    tracks is restored whatever the exclude rules say, tracked files being
    the change the restore covers. A path the base lacks is removed — a file
    by `git rm` and the unlink of what no index holds, a nested repository,
    which the untracked listing names as one directory entry, whole — and a
    path the base holds is restored from it, removals first, HEAD never
    moving; each step states the end state, so a command repeated after a
    crash finishes what the crash left."""
    base = _attempt_base(project_dir, ref)
    changed = (
        _changed_paths(project_dir, base, "HEAD")
        | _working_changes(project_dir, base)
        | _untracked_paths(project_dir)
    )
    changed = _outside_factory(changed)
    changed -= _ignored_paths(project_dir, changed)
    if not changed:
        return
    held = run_git_sync(["ls-tree", "-r", "--name-only", "-z", base], cwd=project_dir)
    if held.returncode != 0:
        raise RunDataRefusal("The attempt's base commit could not be read")
    in_base = {path for path in held.stdout.split("\0") if path}
    removed = sorted(changed - in_base)
    written = sorted(changed & in_base)
    if removed:
        gone = run_git_sync(["rm", "-f", "--ignore-unmatch", "--", *removed], cwd=project_dir)
        if gone.returncode != 0:
            raise RunDataRefusal("The working tree could not be restored")
        for path in removed:
            # What `git rm` cannot take — a path no index holds — goes by
            # hand, and a directory the base does not hold empties.
            try:
                _remove_path(project_dir / path)
                parent = (project_dir / path).parent
                while parent != project_dir and parent.is_dir():
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            except OSError as e:
                raise RunDataRefusal(f"The working tree could not be restored\n{path}\n{e}") from e
    if written:
        restored = run_git_sync(
            ["restore", f"--source={base}", "--staged", "--worktree", "--", *written],
            cwd=project_dir,
        )
        if restored.returncode != 0:
            raise RunDataRefusal("The working tree could not be restored")


def _attempt_base(project_dir: Path, ref: str) -> str:
    """The commit the task's attempt began on: the parent of the commit the
    task's iteration ref names — the ref holds the attempt's latest iteration
    commit, whose parent is the base every iteration of the attempt shares —
    and HEAD where the ref does not resolve, an attempt that reached no
    iteration commit never having gained one."""
    resolved = run_git_sync(["rev-parse", "--verify", "--quiet", f"{ref}^"], cwd=project_dir)
    if resolved.returncode == 0 and resolved.stdout.strip():
        return resolved.stdout.strip()
    head = run_git_sync(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=project_dir)
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    raise RunDataRefusal("The attempt's base commit could not be read")


def _remove_path(path: Path) -> None:
    """Remove an untracked path the base lacks: a file or a symbolic link by
    the unlink, a directory — a nested repository, which the untracked
    listing names as one entry — whole, its read-only object store chmod'd
    through: each directory made traversable from the top down before
    ``rmtree`` meets it, an OSError on a chmod or a listing leaving the
    subtree for ``rmtree`` to answer for."""
    if path.is_dir() and not path.is_symlink():
        def walk(current: Path) -> None:
            try:
                os.chmod(current, 0o700)
            except OSError:
                pass
            try:
                with os.scandir(current) as entries:
                    children = list(entries)
            except OSError:
                return
            for entry in children:
                child = Path(entry.path)
                if child.is_dir() and not child.is_symlink():
                    walk(child)

        walk(path)
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _ignored_paths(project_dir: Path, paths: set[str]) -> set[str]:
    """The paths of `paths` git itself lists as untracked and ignored — the
    files ``git ls-files --others --ignored --exclude-standard`` names, so a
    `.venv`, build output, a `.env` or a base `orchestrator.json` at a
    checkout's root is never touched. A file the index tracks is not among
    them: tracked files are the change the restore covers, whatever the
    exclude rules say."""
    listed = run_git_sync(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], cwd=project_dir,
    )
    return _paths_of(listed) & paths


def _task_object(project_dir: Path, relpath: str, task: Task) -> dict:
    """A task's object: where it stands, each step of its run with every
    round's text, its escalation where it stopped on one, its deferred
    observations with their dispositions, and the positions a rollback may
    take it back to."""
    group = locate_task(project_dir, relpath, task)
    sessions = _read_sessions(group.plan_path) if group is not None else {}
    steps, rounds = _group_rounds(project_dir, relpath, group) if group is not None else ([], [])
    return {
        "version": RUN_DATA_VERSION,
        "scope": "task",
        "roadmap": relpath,
        "task": {
            "address": task.number if task.number is not None else task.title,
            "title": task.title,
            "state": _state(task, group, sessions),
        },
        "steps": steps,
        "escalation": _escalation(sessions, rounds),
        "observations": _observations(project_dir, _artifact_subdir(relpath), group, rounds),
        "positions": positions_of(project_dir, task, group),
    }


def _phase_object(project_dir: Path, relpath: str, phase: int, tasks: list[Task]) -> dict:
    """A phase's object: every task the roadmap lists under the phase heading,
    each with its standing, its escalation and its deferred observations."""
    listed = []
    for task in tasks:
        group = locate_task(project_dir, relpath, task)
        sessions = _read_sessions(group.plan_path) if group is not None else {}
        _, rounds = _group_rounds(project_dir, relpath, group) if group is not None else ([], [])
        listed.append({
            "address": task.number if task.number is not None else task.title,
            "title": task.title,
            "state": _state(task, group, sessions),
            "escalation": _escalation(sessions, rounds),
            "observations": _observations(project_dir, _artifact_subdir(relpath), group, rounds),
        })
    return {
        "version": RUN_DATA_VERSION,
        "scope": "phase",
        "roadmap": relpath,
        "phase": phase,
        "tasks": listed,
    }


def _observations(project_dir: Path, segment: str | None, group: TaskGroup | None, rounds: list[tuple[str, int, Path, str]]) -> list[dict]:
    """The group's deferred observations over the rounds a reviewer wrote,
    joined across the files that carry one entry, empty where the task has
    no group; the segment and the group's stem name the entries. An entry's
    disposition is the task's disposition record's where the record holds
    one, else the marker's, else none."""
    if group is None:
        return []
    carried = [
        (name, number, text)
        for name, number, _, text in rounds
        if name in _OBSERVATION_STEPS
    ]
    listed = collect_entries(segment, group.group, carried)
    record = read_record(record_path(project_dir, segment, group.seq, group.slug))
    for entry in listed:
        held = record.get(entry["entry"])
        if held is not None:
            entry["disposition"] = _carried(held["kind"], held.get("target"))
    return listed


def _state(task: Task, group: TaskGroup | None, sessions: dict) -> str:
    """Where a task stands: done where its roadmap line is checked, escalated
    where the selected group's record says so, underway where a group exists,
    unstarted otherwise."""
    if task.done:
        return "done"
    if group is None:
        return "unstarted"
    if sessions.get("step") == "escalated":
        return "escalated"
    return "underway"


def _group_rounds(project_dir: Path, relpath: str, group: TaskGroup) -> tuple[list[dict], list[tuple[str, int, Path, str]]]:
    """The group's steps in run order, each with its rounds' texts, and every
    round as (step, round, path, text) in the same order for the escalation
    scan.

    A step appears where the group holds at least one artifact of its kind;
    the plan is one round. Each text is read as UTF-8 through the artifact
    vocabulary's own names."""
    segment = _artifact_subdir(relpath)
    seq, slug = group.seq, group.slug
    kinds: list[tuple[str, list[tuple[int, Path]]]] = [
        ("plan", [(1, group.plan_path)] if group.plan_path.is_file() else []),
        ("plan-review", _rounds(artifact_dir(project_dir, PLAN_REVIEWS_DIRNAME, segment), plan_review_pattern(seq, slug))),
        ("implement", _rounds(artifact_dir(project_dir, REVIEWS_DIRNAME, segment), implementer_answer_pattern(seq, slug))),
        ("review", _rounds(artifact_dir(project_dir, REVIEWS_DIRNAME, segment), review_pattern(seq, slug))),
        ("test-run", _rounds(artifact_dir(project_dir, TEST_RUNS_DIRNAME, segment), test_run_pattern(seq, slug))),
    ]
    steps: list[dict] = []
    rounds: list[tuple[str, int, Path, str]] = []
    for name, found in kinds:
        if not found:
            continue
        entries = []
        for number, path in found:
            text = _read_artifact(path)
            entries.append({"round": number, "text": text})
            rounds.append((name, number, path, text))
        steps.append({"step": name, "rounds": entries})
    return steps, rounds


def _rounds(directory: Path, pattern: str) -> list[tuple[int, Path]]:
    """The files a task's one kind of artifact takes in `directory`, by the
    round each name carries."""
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob(pattern):
        read = read_file_name(path.name)
        if read is not None and read[1] is not None:
            found.append((read[1], path))
    found.sort(key=lambda entry: entry[0])
    return found


def _escalation(sessions: dict, rounds: list[tuple[str, int, Path, str]]) -> dict | None:
    """The escalation the selected group's record names, where the group
    stopped on one — None everywhere else, older escalating artifacts
    included.

    The record holds ``<role>: <excerpt> (<path>)``; the round whose file name
    the path names is the stop, wherever an artifact of an earlier round
    carries the marker as well. Where the record names no path, the first
    round in run order whose last lines carry the marker is the stop."""
    if sessions.get("step") != "escalated":
        return None
    named = _recorded_artifact_name(sessions.get("escalation"))
    if named is not None:
        rounds = [entry for entry in rounds if entry[2].name == named]
    for name, number, _, text in rounds:
        if _has_escalation(text):
            return {
                "role": _ROLE_OF_STEP[name],
                "step": name,
                "round": number,
                "text": _escalation_section(text),
            }
    return None


def _recorded_artifact_name(record: object) -> str | None:
    """The file name of the artifact a sidecar escalation record names.

    The record is ``<role>: <excerpt> (<path>)`` and the path sits in the last
    parenthesis of the string; no artifact file name a run writes carries a
    parenthesis, so the last opening parenthesis opens the path. None where
    the record is absent or names no path."""
    if not isinstance(record, str):
        return None
    text = record.strip()
    if not text.endswith(")"):
        return None
    opening = text.rfind("(")
    if opening == -1:
        return None
    return Path(text[opening + 1:-1]).name


def _escalation_section(text: str) -> str:
    """The escalation's own text: the section from ``## Escalation`` to the
    next heading, or the whole answer where that heading is absent."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Escalation":
            section = [line]
            for later in lines[i + 1:]:
                if later.lstrip().startswith("#"):
                    break
                section.append(later)
            while section and not section[-1].strip():
                section.pop()
            return "\n".join(section)
    return text
