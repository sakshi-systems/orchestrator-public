"""Tests for orchestrator.artifact_layout: the run's artifact names, held once."""

from pathlib import Path

from orchestrator import artifact_layout as layout
from orchestrator.artifact_layout import (
    IMPLEMENTER_ANSWER_SUFFIX,
    PLAN_REVIEW_SUFFIX,
    PLAN_REVIEWS_DIRNAME,
    PLAN_SUFFIX,
    PLANS_DIRNAME,
    REVIEW_SUFFIX,
    REVIEWS_DIRNAME,
    SIDECAR_SUFFIX,
    TEST_RUNS_DIRNAME,
    TEST_RUN_SUFFIX,
    ArtifactKind,
    artifact_dir,
    implementer_answer_name,
    implementer_answer_pattern,
    plan_groups,
    plan_name,
    plan_review_name,
    plan_review_pattern,
    read_file_name,
    review_name,
    review_pattern,
    sidecar_name,
    sidecar_path,
    task_stem,
)

SEQ = "07"
SLUG = "fix-the-door"
STEM = "07-fix-the-door"


# ---------------------------------------------------------------------------
# --- the directory name of each artifact kind ---
# ---------------------------------------------------------------------------

def test_directory_names_are_pinned_by_value():
    assert PLANS_DIRNAME == "plans"
    assert PLAN_REVIEWS_DIRNAME == "plan-reviews"
    assert REVIEWS_DIRNAME == "reviews"
    assert TEST_RUNS_DIRNAME == "test-runs"
    assert layout.DISPOSITIONS_DIRNAME == "dispositions"


def test_suffix_templates_are_pinned_by_value():
    assert PLAN_SUFFIX == ".md"
    assert SIDECAR_SUFFIX == ".json"
    assert PLAN_REVIEW_SUFFIX == "-plan-review-{n}.md"
    assert IMPLEMENTER_ANSWER_SUFFIX == "-implementer-{n}.md"
    assert REVIEW_SUFFIX == "-review-{n}.md"
    assert TEST_RUN_SUFFIX == "-test-{n}.txt"


def test_artifact_kind_values_are_pinned():
    assert [kind.value for kind in ArtifactKind] == [
        "plan",
        "sidecar",
        "plan_review",
        "implementer_answer",
        "review",
        "test_run",
    ]


# ---------------------------------------------------------------------------
# --- the builders of a task's file names ---
# ---------------------------------------------------------------------------

def test_task_stem_joins_the_sequence_number_and_the_slug():
    assert task_stem(SEQ, SLUG) == STEM


def test_plan_name_is_pinned_by_value():
    assert plan_name(SEQ, SLUG) == f"{STEM}.md"


def test_sidecar_name_is_pinned_by_value():
    assert sidecar_name(SEQ, SLUG) == f"{STEM}.json"


def test_disposition_name_is_pinned_by_value():
    assert layout.disposition_name(SEQ, SLUG) == f"{STEM}.json"


def test_plan_review_name_is_pinned_by_value():
    assert plan_review_name(SEQ, SLUG, 3) == f"{STEM}-plan-review-3.md"


def test_implementer_answer_name_is_pinned_by_value():
    assert implementer_answer_name(SEQ, SLUG, 2) == f"{STEM}-implementer-2.md"


def test_review_name_is_pinned_by_value():
    assert review_name(SEQ, SLUG, 4) == f"{STEM}-review-4.md"


def test_test_run_name_is_pinned_by_value():
    assert layout.test_run_name(SEQ, SLUG, 5) == f"{STEM}-test-5.txt"


def test_sidecar_path_replaces_the_plan_s_own_suffix(tmp_path):
    plan_path = tmp_path / f"{STEM}.md"
    assert sidecar_path(plan_path) == plan_path.with_suffix(".json")


def test_sidecar_path_replaces_only_the_last_suffix(tmp_path):
    # A slug may carry a dot; the record still sits beside the plan it names.
    plan_path = tmp_path / "07-fix-1.2.md"
    assert sidecar_path(plan_path) == tmp_path / "07-fix-1.2.json"


def test_artifact_dir_is_flat_without_a_segment(tmp_path):
    assert artifact_dir(tmp_path, PLANS_DIRNAME, None) == tmp_path / ".ai-factory" / "plans"


def test_artifact_dir_carries_the_segment_below_the_kind(tmp_path):
    assert artifact_dir(tmp_path, PLANS_DIRNAME, "john-doe") == tmp_path / ".ai-factory" / "plans" / "john-doe"


def test_artifact_dir_creates_nothing(tmp_path):
    artifact_dir(tmp_path, REVIEWS_DIRNAME, None)
    assert not (tmp_path / ".ai-factory").exists()


# ---------------------------------------------------------------------------
# --- the patterns that list a task's files of each kind ---
# ---------------------------------------------------------------------------

def test_plan_review_pattern_is_pinned_by_value():
    assert plan_review_pattern(SEQ, SLUG) == f"{STEM}-plan-review-*.md"


def test_implementer_answer_pattern_is_pinned_by_value():
    assert implementer_answer_pattern(SEQ, SLUG) == f"{STEM}-implementer-*.md"


def test_review_pattern_is_pinned_by_value():
    assert review_pattern(SEQ, SLUG) == f"{STEM}-review-*.md"


def test_test_run_pattern_is_pinned_by_value():
    assert layout.test_run_pattern(SEQ, SLUG) == f"{STEM}-test-*.txt"


def test_the_patterns_list_exactly_the_task_s_files(tmp_path):
    (tmp_path / f"{STEM}-plan-review-1.md").touch()
    (tmp_path / f"{STEM}-plan-review-2.md").touch()
    (tmp_path / "08-other-task-plan-review-9.md").touch()
    found = sorted(p.name for p in tmp_path.glob(plan_review_pattern(SEQ, SLUG)))
    assert found == [f"{STEM}-plan-review-1.md", f"{STEM}-plan-review-2.md"]


# ---------------------------------------------------------------------------
# --- the plans directory's groups ---
# ---------------------------------------------------------------------------

def test_plan_groups_take_every_numbered_pair_of_the_slug(tmp_path):
    (tmp_path / "01-neo-slug.md").touch()
    (tmp_path / "01-neo-slug.json").touch()
    (tmp_path / "02-neo-slug.md").touch()
    assert plan_groups(tmp_path, "neo-slug") == {
        "01-neo-slug": tmp_path / "01-neo-slug.md",
        "02-neo-slug": tmp_path / "02-neo-slug.md",
    }


def test_plan_groups_map_a_lone_sidecar_to_its_plan_path(tmp_path):
    (tmp_path / "10-neo-slug.json").touch()
    assert plan_groups(tmp_path, "neo-slug") == {"10-neo-slug": tmp_path / "10-neo-slug.md"}


def test_plan_groups_leave_other_shapes_out(tmp_path):
    (tmp_path / "03-other-slug.md").touch()          # another task's slug
    (tmp_path / "neo-slug.md").touch()               # no sequence number
    (tmp_path / "x-neo-slug.md").touch()             # a non-number prefix
    (tmp_path / "notes.txt").touch()                 # not a plan or a sidecar
    (tmp_path / "04-neo-slug").mkdir()               # a directory is never a group
    assert plan_groups(tmp_path, "neo-slug") == {}


# ---------------------------------------------------------------------------
# --- the reading of a file name back into its kind and round ---
# ---------------------------------------------------------------------------

def test_every_written_name_reads_back_into_its_kind_and_round():
    assert read_file_name(f"{STEM}.md") == (ArtifactKind.PLAN, None)
    assert read_file_name(f"{STEM}.json") == (ArtifactKind.SIDECAR, None)
    assert read_file_name(f"{STEM}-plan-review-3.md") == (ArtifactKind.PLAN_REVIEW, 3)
    assert read_file_name(f"{STEM}-implementer-2.md") == (ArtifactKind.IMPLEMENTER_ANSWER, 2)
    assert read_file_name(f"{STEM}-review-4.md") == (ArtifactKind.REVIEW, 4)
    assert read_file_name(f"{STEM}-test-5.txt") == (ArtifactKind.TEST_RUN, 5)


def test_every_built_name_reads_back_the_round_it_was_built_with():
    assert read_file_name(plan_name(SEQ, SLUG)) == (ArtifactKind.PLAN, None)
    assert read_file_name(sidecar_name(SEQ, SLUG)) == (ArtifactKind.SIDECAR, None)
    assert read_file_name(plan_review_name(SEQ, SLUG, 12)) == (ArtifactKind.PLAN_REVIEW, 12)
    assert read_file_name(implementer_answer_name(SEQ, SLUG, 9)) == (ArtifactKind.IMPLEMENTER_ANSWER, 9)
    assert read_file_name(review_name(SEQ, SLUG, 8)) == (ArtifactKind.REVIEW, 8)
    assert read_file_name(layout.test_run_name(SEQ, SLUG, 7)) == (ArtifactKind.TEST_RUN, 7)


def test_a_plan_review_never_reads_as_the_review_of_a_slug_ending_in_plan():
    # The name carries both infixes; the plan review is read first.
    name = f"{STEM}-plan-review-3.md"
    assert read_file_name(name) == (ArtifactKind.PLAN_REVIEW, 3)


def test_a_kind_s_infix_without_a_round_reads_as_the_plan_s_slug():
    assert read_file_name("07-plan-review.md") == (ArtifactKind.PLAN, None)
    assert read_file_name("07-review.md") == (ArtifactKind.PLAN, None)
    assert read_file_name("07-implementer.md") == (ArtifactKind.PLAN, None)


def test_a_name_without_a_numbered_stem_reads_as_none():
    assert read_file_name("notes.md") is None
    assert read_file_name("notes.json") is None
    assert read_file_name("neo-slug.md") is None
    assert read_file_name("review-2.md") is None


def test_a_number_without_a_slug_reads_as_none():
    assert read_file_name("07.md") is None
    assert read_file_name("07.json") is None
    assert read_file_name("07-.md") is None


def test_a_bare_test_suffix_reads_as_none():
    # `.txt` belongs to a test run alone, and this name carries no round.
    assert read_file_name(f"{STEM}.txt") is None
    assert read_file_name(f"{STEM}-review-4.txt") is None
