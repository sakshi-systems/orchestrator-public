"""Resume / step-detection — figure out where a previous run stopped."""

from __future__ import annotations

from pathlib import Path

from .errors import HaltError
from .ports import VerifyKind
from .protocol import PLAN_REVIEW_PASS, _has_signal, _read_sessions


def _parse_counter(step_value: str, prefix: str) -> int:
    """Return the positive attempt counter that follows `prefix` in `step_value`.

    The suffix must be ASCII digits only and greater than zero; leading zeroes are read by value.
    Anything else is a malformed counter and halts the run.
    """
    suffix = step_value[len(prefix):]
    if not suffix or not (suffix.isascii() and suffix.isdigit()) or int(suffix) <= 0:
        raise HaltError(f"Resume step has a malformed attempt counter\n{step_value!r}")
    return int(suffix)


def _check_attempt(label: str, attempt: int, max_iterations: int) -> None:
    """Halt when `attempt` lies past the attempt budget."""
    if attempt > max_iterations:
        raise HaltError(
            f"Resume exceeds the attempt budget\n"
            f"{label} attempt {attempt} exceeds max_iterations ({max_iterations})\n"
            f"Raise max_iterations in orchestrator.json to continue."
        )


def _interpret_step(
    step_value: str, verify: VerifyKind,
) -> tuple[str, int, int | None] | None:
    """Map a recognized sidecar step to (dispatch_step, next_attempt, explicit_counter).

    This is the only step -> dispatch map. Returns None for an empty or unrecognized value.
    A recognized indexed marker with a malformed counter halts.
    """
    if step_value.startswith("planning:"):
        n = _parse_counter(step_value, "planning:")
        return ("plan", n, n)
    if step_value.startswith("planned:"):
        n = _parse_counter(step_value, "planned:")
        return ("plan_review", n, n)
    if step_value.startswith("plan_review_failed:"):
        n = _parse_counter(step_value, "plan_review_failed:")
        return ("plan", n + 1, n)
    if step_value == "plan_reviewed":
        return ("implement", 1, None)
    if step_value.startswith("implemented:"):
        n = _parse_counter(step_value, "implemented:")
        return (verify.step, n, n)
    if step_value.startswith(verify.fail_tag):
        n = _parse_counter(step_value, verify.fail_tag)
        return ("implement", n + 1, n)
    if step_value == "escalated":
        return ("escalated", 0, None)
    return None


def _validate_sidecar_step(
    step_value: str,
    seq: str,
    slug: str,
    plan_reviews_dir: Path,
    artifact_dir: Path,
    fail_prefix: str,
    fail_suffix: str,
) -> str:
    """Return step_value if the referenced artifact exists on disk, or "" if stale.

    Clears step_value when:
    - plan_review_failed:N and the corresponding plan-review file is missing.
    - plan_reviewed and no plan-review file carries PLAN_REVIEW_PASS as an exact line among its last five lines.
    - <fail_prefix>N and the corresponding artifact (artifact_dir / seq-slug<fail_suffix>) is missing.
    planned:N and implemented:N are structurally valid whenever N parses — they
    carry no artifact reference to check against disk.
    escalated is always valid — a terminal, unindexed step with no artifact to check against disk.
    A malformed :N counter halts the run.
    This function only validates artifacts; the step -> dispatch map is _interpret_step.
    """
    if not step_value:
        return step_value
    if step_value.startswith("planned:"):
        _parse_counter(step_value, "planned:")
        return step_value
    if step_value.startswith("implemented:"):
        _parse_counter(step_value, "implemented:")
        return step_value
    if step_value.startswith("plan_review_failed:"):
        n = _parse_counter(step_value, "plan_review_failed:")
        if not (plan_reviews_dir / f"{seq}-{slug}-plan-review-{n}.md").exists():
            return ""
        return step_value
    if step_value == "plan_reviewed":
        if not any(
            _has_signal(f.read_text(), PLAN_REVIEW_PASS)
            for f in plan_reviews_dir.glob(f"{seq}-{slug}-plan-review-*.md")
        ):
            return ""
        return step_value
    if step_value.startswith(fail_prefix):
        n = _parse_counter(step_value, fail_prefix)
        if not (artifact_dir / f"{seq}-{slug}{fail_suffix.format(n=n)}").exists():
            return ""
        return step_value
    if step_value == "escalated":
        return step_value
    # unrecognized → return as-is; dispatch falls through to detection from the plan reviews on disk
    return step_value


def _detect_step(
    seq: str, slug: str,
    plan_path: Path, plan_reviews_dir: Path, output_dir: Path,
    verify: VerifyKind, *, max_iterations: int, runtime: str,
) -> tuple[str, int, Path]:
    """Detect where a previous run stopped and return (step, counter, plan_path) to resume from.

    Steps: "plan", "plan_review", "implement", <verify.step>, "escalated".
    Counter is the attempt/iteration number to use next.
    A recognized explicit step is checked against `max_iterations` (both its own counter and the
    next attempt) before artifact validation can discard it, so a stale marker cannot erase an
    exhausted budget; an over-budget one halts. Counters inferred from artifacts are checked by the caller.
    The returned plan_path is the canonical path discovered from the lowest-seq file matching
    the slug whose sidecar is not `done` (handles the case where a previous run was interrupted and the current run computes
    a different seq via _next_number).
    """
    plans_dir = plan_path.parent
    groups: dict[str, Path] = {}
    for f in plans_dir.glob("*"):
        if f.suffix not in (".md", ".json") or f.is_dir():
            continue
        prefix, separator, candidate_slug = f.stem.partition("-")
        if separator and prefix.isascii() and prefix.isdigit() and candidate_slug == slug:
            groups[f.stem] = f.with_suffix(".md")
    # Validate every matching group before excluding terminal history.
    records = [(f, _read_sessions(f)) for f in groups.values()]
    unfinished = [(f, record) for f, record in records if record.get("step") != "done"]
    numbers: dict[int, str] = {}
    for f, _ in unfinished:
        prefix = f.stem.split("-", 1)[0]
        number = int(prefix)
        if number in numbers:
            raise HaltError(f"Resume record number is ambiguous\n{numbers[number]}\n{f}")
        numbers[number] = str(f)
    if not unfinished:
        return ("plan", 1, plan_path)
    plan_path, sessions = min(unfinished, key=lambda item: int(item[0].stem.split("-", 1)[0]))
    seq = plan_path.stem.split("-", 1)[0]

    raw = sessions.get("step", "")
    if raw == "escalated":
        return ("escalated", 0, plan_path)

    owner = sessions.get("runtime", "claude")
    if owner != runtime:
        raise HaltError(
            f"Resume runtime does not match the record owner\n{plan_path.with_suffix('.json')}\n"
            f"Recorded: {owner}; selected: {runtime}"
        )
    if "runtime" not in sessions and not plan_path.exists():
        raise HaltError(f"Resume record has no legacy plan\n{plan_path.with_suffix('.json')}")

    interpreted = _interpret_step(raw, verify)
    if interpreted is None and "runtime" in sessions:
        raise HaltError(f"Resume record has no usable explicit step\n{plan_path.with_suffix('.json')}\n{raw!r}")
    if interpreted is not None:
        dispatch_step, next_attempt, explicit_counter = interpreted
        if explicit_counter is not None:
            _check_attempt(raw, explicit_counter, max_iterations)
        _check_attempt(dispatch_step, next_attempt, max_iterations)
        if raw.startswith("planning:"):
            return (dispatch_step, next_attempt, plan_path)
    if not plan_path.exists():
        raise HaltError(f"Resume boundary has no plan\n{plan_path}\n{raw!r}")

    step_value = _validate_sidecar_step(
        raw, seq, slug, plan_reviews_dir, output_dir,
        verify.fail_tag, verify.output_suffix,
    )
    if step_value and interpreted is not None:
        dispatch_step, next_attempt, _ = interpreted
        return (dispatch_step, next_attempt, plan_path)
    # unrecognized or stale → fall through to detection from the plan reviews on disk

    # 3. No plan-review files → need to do first plan review
    plan_review_files = sorted(plan_reviews_dir.glob(f"{seq}-{slug}-plan-review-*.md"))
    if not plan_review_files:
        return ("plan_review", 1, plan_path)

    # 4. Latest plan-review didn't pass → plan needs revision
    if not _has_signal(plan_review_files[-1].read_text(), PLAN_REVIEW_PASS):
        return ("plan", len(plan_review_files) + 1, plan_path)

    # 5. Latest plan review passed → implement from iteration 1
    return ("implement", 1, plan_path)
