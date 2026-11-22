"""The run's artifact names, held once: the directory of each artifact kind,
a task's file names, the patterns that list a task's files of each kind, the
plans directory's groups, and the reading of a file name back into its kind
and round."""

from __future__ import annotations

import enum
import re
from pathlib import Path

PLANS_DIRNAME = "plans"
PLAN_REVIEWS_DIRNAME = "plan-reviews"
REVIEWS_DIRNAME = "reviews"
TEST_RUNS_DIRNAME = "test-runs"
DISPOSITIONS_DIRNAME = "dispositions"

PLAN_SUFFIX = ".md"
SIDECAR_SUFFIX = ".json"
PLAN_REVIEW_SUFFIX = "-plan-review-{n}.md"
IMPLEMENTER_ANSWER_SUFFIX = "-implementer-{n}.md"
REVIEW_SUFFIX = "-review-{n}.md"
TEST_RUN_SUFFIX = "-test-{n}.txt"


class ArtifactKind(enum.StrEnum):
    """The kinds of artifact file a run writes for one task."""

    PLAN = "plan"
    SIDECAR = "sidecar"
    PLAN_REVIEW = "plan_review"
    IMPLEMENTER_ANSWER = "implementer_answer"
    REVIEW = "review"
    TEST_RUN = "test_run"


def artifact_dir(project_dir: Path, dirname: str, segment: str | None) -> Path:
    """The artifact directory `dirname` under `<project>/.ai-factory`, under
    the roadmap's `segment` when one is in play; creates nothing."""
    base = project_dir / ".ai-factory" / dirname
    return base / segment if segment else base


def task_stem(seq: str, slug: str) -> str:
    """The stem every artifact file of a task carries: its sequence number
    and its slug, joined by the one hyphen the number ends with."""
    return f"{seq}-{slug}"


def plan_name(seq: str, slug: str) -> str:
    """A task's plan file name."""
    return f"{task_stem(seq, slug)}{PLAN_SUFFIX}"


def sidecar_name(seq: str, slug: str) -> str:
    """A task's sidecar file name: the plan's stem with the record's suffix."""
    return f"{task_stem(seq, slug)}{SIDECAR_SUFFIX}"


def sidecar_path(plan_path: Path) -> Path:
    """The sidecar of the plan at `plan_path`: the plan's own suffix replaced,
    so whatever stem the plan carries, its record sits beside it."""
    return plan_path.with_suffix(SIDECAR_SUFFIX)


def disposition_name(seq: str, slug: str) -> str:
    """A task's disposition record's file name: the stem every artifact of
    the task carries, with the record's own suffix."""
    return f"{task_stem(seq, slug)}{SIDECAR_SUFFIX}"


def plan_review_name(seq: str, slug: str, attempt: int) -> str:
    """A task's plan-review file name for `attempt`."""
    return f"{task_stem(seq, slug)}{PLAN_REVIEW_SUFFIX.format(n=attempt)}"


def implementer_answer_name(seq: str, slug: str, iteration: int) -> str:
    """The implementer's answer file name for `iteration`."""
    return f"{task_stem(seq, slug)}{IMPLEMENTER_ANSWER_SUFFIX.format(n=iteration)}"


def review_name(seq: str, slug: str, iteration: int) -> str:
    """A task's review file name for `iteration`."""
    return f"{task_stem(seq, slug)}{REVIEW_SUFFIX.format(n=iteration)}"


def test_run_name(seq: str, slug: str, iteration: int) -> str:
    """A task's test-run file name for `iteration`."""
    return f"{task_stem(seq, slug)}{TEST_RUN_SUFFIX.format(n=iteration)}"


def plan_review_pattern(seq: str, slug: str) -> str:
    """The pattern that lists every plan review of a task."""
    return f"{task_stem(seq, slug)}{PLAN_REVIEW_SUFFIX.format(n='*')}"


def implementer_answer_pattern(seq: str, slug: str) -> str:
    """The pattern that lists every implementer's answer of a task."""
    return f"{task_stem(seq, slug)}{IMPLEMENTER_ANSWER_SUFFIX.format(n='*')}"


def review_pattern(seq: str, slug: str) -> str:
    """The pattern that lists every review of a task."""
    return f"{task_stem(seq, slug)}{REVIEW_SUFFIX.format(n='*')}"


def test_run_pattern(seq: str, slug: str) -> str:
    """The pattern that lists every test run of a task."""
    return f"{task_stem(seq, slug)}{TEST_RUN_SUFFIX.format(n='*')}"


def plan_groups(plans_dir: Path, slug: str) -> dict[str, Path]:
    """The plans directory's groups for a task's slug: every plan and sidecar
    pair whose stem is `<number>-<slug>`, keyed by stem and mapped to the
    plan's own path. A file of another shape is not one the run wrote and
    stays out; a directory is never a group."""
    groups: dict[str, Path] = {}
    for f in plans_dir.glob("*"):
        if f.suffix not in (PLAN_SUFFIX, SIDECAR_SUFFIX) or f.is_dir():
            continue
        prefix, separator, candidate_slug = f.stem.partition("-")
        if separator and prefix.isascii() and prefix.isdigit() and candidate_slug == slug:
            groups[f.stem] = f.with_suffix(PLAN_SUFFIX)
    return groups


_STEM_RE = re.compile(r"[0-9]+-.+")
_ROUND_RES: tuple[tuple[ArtifactKind, re.Pattern[str]], ...] = (
    (ArtifactKind.PLAN_REVIEW, re.compile(r"-plan-review-([0-9]+)\.md\Z")),
    (ArtifactKind.IMPLEMENTER_ANSWER, re.compile(r"-implementer-([0-9]+)\.md\Z")),
    (ArtifactKind.REVIEW, re.compile(r"-review-([0-9]+)\.md\Z")),
    (ArtifactKind.TEST_RUN, re.compile(r"-test-([0-9]+)\.txt\Z")),
)


def read_file_name(name: str) -> tuple[ArtifactKind, int | None] | None:
    """Read one artifact file name back into its kind and its round.

    The round is the attempt or iteration the name carries; the plan and the
    sidecar carry none and read with round None. A plan review is read before
    a review, so a name carrying `-plan-review-` never reads as the review of
    a slug that ends in `plan`. The stem must be `<number>-<slug>`: a name
    the run would not write reads as None, and a stem that is only a number,
    or carries no slug, reads as None too.
    """
    for kind, pattern in _ROUND_RES:
        found = pattern.search(name)
        if found and _STEM_RE.fullmatch(name[: found.start()]):
            return (kind, int(found.group(1)))
    if name.endswith(SIDECAR_SUFFIX) and _STEM_RE.fullmatch(name[: -len(SIDECAR_SUFFIX)]):
        return (ArtifactKind.SIDECAR, None)
    if name.endswith(PLAN_SUFFIX) and _STEM_RE.fullmatch(name[: -len(PLAN_SUFFIX)]):
        return (ArtifactKind.PLAN, None)
    return None
