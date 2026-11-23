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
import sys
from pathlib import Path
from typing import NamedTuple, NoReturn

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
from .iteration_ref import remove_iteration_refs
from .layout import _artifact_subdir, _resolve_roadmap_relpath
from .observations import collect_entries
from .ports import Repository
from .protocol import _has_escalation, _read_artifact, _read_sessions, _replace_sessions
from .repositories import find_run_tree
from .resume import select_task_group
from .roadmap import Task, find_task, parse_roadmap

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

# The record's steps that show an implementation: the implementer finished
# an iteration, or the verify that read it failed and waits to run again.
_IMPLEMENTED_STEP_PREFIXES = ("implemented:", "review_failed:", "test_run_failed:")

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
    the project would take where none is given; `task` is the task's ``N.M``,
    ``N.M.k`` or fix line's ``N.Ma``, or a flat task's title, and `phase` a
    phase's number as written — exactly one of the two. Refuses through
    RunDataRefusal, HaltError or the configuration's SystemExit, which the
    dispatch prints as its first line on standard error with status 1.
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


def positions_of(project_dir: Path, relpath: str, task: Task, group: TaskGroup | None) -> list[str]:
    """The position tokens the task can be taken back to, in the order the
    reference lists them.

    `relpath` is the roadmap's own path under `.ai-factory`, whose artifact
    segment holds the group's artifacts. `start` where an attempt exists
    and the task is not done; `plan-review` where the plan exists and the
    record holds the planner; `verify` where the plan exists and the
    attempt has implemented, read from the records alone; `implement` where
    the record holds no planner. A task that is done and a task no attempt
    has begun list none."""
    if group is None or task.done:
        return []
    sessions = _read_sessions(group.plan_path)
    positions: list[str] = []
    positions.append(_START)
    plan_exists = group.plan_path.is_file()
    if plan_exists and "planner" in sessions:
        positions.append(_PLAN_REVIEW)
    if plan_exists and _attempt_implemented(project_dir, relpath, group, sessions):
        positions.append(_VERIFY)
    if "planner" not in sessions:
        positions.append(_IMPLEMENT)
    return positions


def rollback(config: OrchestratorConfig, project_dir: Path, *, roadmap: str | None = None, task: str | None = None, position: str | None = None) -> None:
    """The `rollback` command: take the addressed task back to the position
    `show` listed for it and print `version` and `position`.

    `roadmap` is the ``--roadmap`` option's value, taking the roadmap a run of
    the project would take where none is given; `task` is the task's ``N.M``,
    ``N.M.k`` or fix line's ``N.Ma``, or a flat task's title, and `position`
    one of the tokens `show` lists.
    Refuses through RunDataRefusal, HaltError or the configuration's
    SystemExit, which the dispatch prints as its first line on standard error
    with status 1.

    The order is the record first for every position but `start`, then the
    artifacts; `start` goes on to the task's iteration refs and the sidecar
    last. Each step states the end state, so a command repeated after a
    crash finishes what the crash left: `positions_of` still lists the
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
    positions = positions_of(project_dir, relpath, addressed, group)
    if position not in positions:
        raise RunDataRefusal(
            f"Position '{position}' is not one task '{address}' can take: "
            + (", ".join(positions) if positions else "none")
        )
    if position == _START:
        _remove_artifacts(project_dir, relpath, group, _START)
        # The removal's own warning line — a remote that cannot be reached —
        # goes to standard error for this command, so standard output holds
        # the one JSON object and nothing else.
        with contextlib.redirect_stdout(sys.stderr):
            remove_iteration_refs(project_dir, find_run_tree(project_dir), group.plan_path)
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
    # The tree is found once for the whole phase: every group's removal
    # takes the same repositories the run's own iteration commits went to.
    tree = find_run_tree(project_dir)
    for plan_path in to_sweep:
        _sweep_group(project_dir, relpath, plan_path, tree)
    _print_object({"version": RUN_DATA_VERSION, "phase": number})


def dispose(project_dir: Path) -> None:
    """The `dispose` command: take the closure's deposit from standard input —
    the disposition of each deferred observation it names, in the shape
    `show` reports it, as UTF-8 JSON — and keep it in the orchestrator's own
    record, one per task, printing `version` and `disposed`, the entries the
    deposit named.

    The command takes no configuration and reads no roadmap: each entry is
    resolved by the plan reviews and reviews of every group of the project,
    an entry id naming its own task, and a routed target is kept as the head
    names it — where a routed remark went is the head's judgment, and no
    target is opened to judge it. The whole deposit is checked before any
    record is written, and a deposit that names an entry already disposed
    the same way changes nothing, so a head resuming its closure re-sends
    the deposit without harm. Refuses through RunDataRefusal or HaltError,
    which the dispatch prints as its first line on standard error with
    status 1.
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
    records: dict[tuple[str | None, str], dict] = {}
    for entry, _kind, _target in items:
        group = known[entry]
        if group not in records:
            seq, _, slug = group[1].partition("-")
            records[group] = read_record(record_path(project_dir, group[0], seq, slug))
    held: dict[str, dict | None] = {
        entry: records[known[entry]].get(entry) for entry, _kind, _target in items
    }
    for entry, kind, target in items:
        if held[entry] is not None and held[entry] != _carried(kind, target):
            raise RunDataRefusal(
                f"Entry {entry!r} already holds the disposition {json.dumps(held[entry], ensure_ascii=False)}"
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
    """The deposit's items as (entry, kind, target), each checked for
    its own form: an object of `entry` and `disposition`, the disposition the
    object `show` reports — of `kind` and, for `routed`, `target` — so a
    client passes back what it read. The rules the disposition itself holds:
    its kind one of the three, and its target present exactly where the kind
    is `routed`, which names one of the target's two forms — the target's
    form and types the whole of its check, for no target is opened."""
    listed = deposit["dispositions"]
    if not isinstance(listed, list):
        raise RunDataRefusal("The deposit's dispositions are not a list")
    items: list[tuple[str, str, dict | None]] = []
    for given in listed:
        if not isinstance(given, dict) or "entry" not in given or "disposition" not in given:
            raise RunDataRefusal("A disposition of the deposit is not an object of entry and disposition")
        disposition = given["disposition"]
        if not isinstance(disposition, dict) or "kind" not in disposition:
            raise RunDataRefusal("A disposition of the deposit is not an object of kind and, for routed, target")
        entry, kind, target = given["entry"], disposition["kind"], disposition.get("target")
        if kind not in _KINDS:
            raise RunDataRefusal(f"A disposition of the deposit is none of fixed, routed and dismissed: {kind!r}")
        if kind == "routed" and target is None:
            raise RunDataRefusal(f"A routed disposition of the deposit names no target: {entry!r}")
        if kind != "routed" and target is not None:
            raise RunDataRefusal(f"A disposition of the deposit that is not routed names a target: {entry!r}")
        if kind == "routed" and not _target_form(target):
            raise RunDataRefusal(
                f"A routed disposition of the deposit names a target that is "
                f"neither a spec nor a phase of a roadmap: {entry!r}"
            )
        items.append((entry, kind, target))
    return items


def _target_form(target: object) -> bool:
    """True where `target` is one of the two forms a routed disposition
    names: an object of `spec`, a string, or of `roadmap`, a string, and
    `phase`, an integer — its form and types the whole of the check, for
    the command opens no target and reads no roadmap to judge one."""
    if not isinstance(target, dict):
        return False
    if set(target) == {"spec"}:
        return isinstance(target["spec"], str)
    if set(target) == {"roadmap", "phase"}:
        return (
            isinstance(target["roadmap"], str)
            and isinstance(target["phase"], int)
            and not isinstance(target["phase"], bool)
        )
    return False


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


def _sweep_group(project_dir: Path, relpath: str, plan_path: Path, tree: tuple[Repository, ...]) -> None:
    """Remove one finished group's run: every plan review, the implementer's
    answers, the verify artifacts of the roadmap's kind and the disposition
    record, then the plan, the task's exchange files and its iteration refs —
    in each repository of `tree`, the run's own tree — whose failures only
    warn, as they do where a task lands — and the sidecar
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
        remove_iteration_refs(project_dir, tree, plan_path)
    sidecar_path(plan_path).unlink(missing_ok=True)


def _attempt_implemented(project_dir: Path, relpath: str, group: TaskGroup, sessions: dict) -> bool:
    """True where the attempt has implemented, read from the records alone:
    the record's step shows an implementation — `implemented:N`, or a verify
    that failed and waits to run again (`review_failed:N`,
    `test_run_failed:N`) — or an implementer's answer stands among the
    group's artifacts, in the roadmap's own artifact segment. The working
    tree is never read: `rollback` touches it at no position, so it holds
    no word of where the attempt stands."""
    step = sessions.get("step")
    if isinstance(step, str) and step.startswith(_IMPLEMENTED_STEP_PREFIXES):
        return True
    answers = artifact_dir(project_dir, REVIEWS_DIRNAME, _artifact_subdir(relpath))
    return bool(_rounds(answers, implementer_answer_pattern(group.seq, group.slug)))


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
        "positions": positions_of(project_dir, relpath, task, group),
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
