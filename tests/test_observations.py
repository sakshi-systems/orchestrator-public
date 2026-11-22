"""The reading of a review's `## Deferred observations` section: each entry
with its target, its text and the disposition its markers give, and the
joining of one task's entries over the files that carry them."""

from __future__ import annotations

import hashlib

from orchestrator.observations import (
    Disposition,
    collect_entries,
    entry_id,
    read_entries,
)


def _review(section: str) -> str:
    """A review whose only content is the section as given."""
    return f"## Review\n\nSound.\n\n{section}"


def test_a_multi_line_entry_keeps_its_continuation_lines():
    text = _review(
        "## Deferred observations\n"
        "\n"
        "- Affects: Phase 2 — The doorway's width is a later phase's own concern.\n"
        "  The frame constant names it in one place.\n"
    )
    assert read_entries(text) == [
        ("Phase 2", "The doorway's width is a later phase's own concern.\n  The frame constant names it in one place.", None),
    ]


def test_a_marker_on_the_first_line_of_a_multi_line_entry_marks_it():
    text = _review(
        "## Deferred observations\n"
        "\n"
        "- Affects: unknown — The licence of the wrapped helper is not the reviewer's to choose. [fixed]\n"
        "  The choice belongs to whoever holds the project.\n"
    )
    assert read_entries(text) == [
        ("unknown", "The licence of the wrapped helper is not the reviewer's to choose.\n  The choice belongs to whoever holds the project.", Disposition("fixed", None)),
    ]


def test_each_live_marker_reads_as_its_disposition():
    rows = [
        ("[fixed]", Disposition("fixed", None)),
        ("[routed → .ai-factory/specs/trickster77777/0290-orchestrator-dispose-and-the-closure-deposit.md]",
         Disposition("routed", {"spec": ".ai-factory/specs/trickster77777/0290-orchestrator-dispose-and-the-closure-deposit.md"})),
        ("[routed → .ai-factory/roadmaps/trickster77777.md § Phase 7]",
         Disposition("routed", {"roadmap": ".ai-factory/roadmaps/trickster77777.md", "phase": 7})),
        ("[dismissed]", Disposition("dismissed", None)),
    ]
    for marker, disposition in rows:
        text = _review(f"## Deferred observations\n\n- Affects: unknown — A remark someone disposed of. {marker}\n")
        assert read_entries(text) == [("unknown", "A remark someone disposed of.", disposition)], marker


def test_each_retired_marker_reads_as_legacy():
    rows = [
        "[promoted → .ai-factory/ROADMAP.md]",
        "[audit-corroborated]",
        "[audit-dismissed]",
        "[unrouted-reported]",
    ]
    for marker in rows:
        text = _review(f"## Deferred observations\n\n- Affects: unknown — An older review's note. {marker}\n")
        assert read_entries(text) == [("unknown", "An older review's note.", Disposition("legacy", None))], marker


def test_two_files_carrying_an_identical_entry_are_one_entry():
    first = _review("## Deferred observations\n\n- Affects: unknown — The same remark, word for word.\n")
    second = _review("## Deferred observations\n\n- Affects: unknown — The same remark, word for word. [dismissed]\n")
    listed = collect_entries(None, "01-1-1-alpha", [("plan-review", 2, first), ("review", 1, second)])
    assert len(listed) == 1
    assert listed[0]["affects"] == "unknown"
    assert listed[0]["text"] == "The same remark, word for word."
    assert listed[0]["sources"] == [
        {"step": "plan-review", "round": 2},
        {"step": "review", "round": 1},
    ]
    assert listed[0]["disposition"] == {"kind": "dismissed"}


def test_entries_that_differ_by_a_word_are_two_entries():
    first = _review("## Deferred observations\n\n- Affects: unknown — The doorway is wide enough.\n")
    second = _review("## Deferred observations\n\n- Affects: unknown — The doorway is wide enough today.\n")
    listed = collect_entries(None, "01-1-1-alpha", [("review", 1, first), ("review", 2, second)])
    assert [entry["text"] for entry in listed] == [
        "The doorway is wide enough.",
        "The doorway is wide enough today.",
    ]
    assert listed[0]["entry"] != listed[1]["entry"]


def test_a_file_with_no_section_has_no_entries():
    assert read_entries("## Review\n\nNo findings.\n\nREVIEW_PASS\n") == []


def test_a_section_ends_at_the_next_heading():
    text = _review(
        "## Deferred observations\n"
        "\n"
        "- Affects: Phase 2 — The doorway's width is a later phase's own concern.\n"
        "\n"
        "## Verdict\n"
        "\n"
        "- Affects: unknown — A line the heading's section carries, not an entry.\n"
    )
    assert read_entries(text) == [("Phase 2", "The doorway's width is a later phase's own concern.", None)]


def test_a_trailing_bracket_no_marker_names_stays_in_the_text():
    text = _review("## Deferred observations\n\n- Affects: Phase 2 — See the note [in the frame's spec].\n")
    assert read_entries(text) == [("Phase 2", "See the note [in the frame's spec].", None)]


def test_markers_accumulate_and_the_latest_gives_the_disposition():
    text = _review(
        "## Deferred observations\n"
        "\n"
        "- Affects: unknown — A remark handled twice. [fixed] [routed → .ai-factory/ROADMAP.md § Phase 3]\n"
    )
    assert read_entries(text) == [
        ("unknown", "A remark handled twice.", Disposition("routed", {"roadmap": ".ai-factory/ROADMAP.md", "phase": 3})),
    ]


def test_a_marker_on_a_continuation_line_is_text():
    text = _review(
        "## Deferred observations\n"
        "\n"
        "- Affects: unknown — A remark whose second line looks marked.\n"
        "  The reviewer never writes one, so it stays. [fixed]\n"
    )
    assert read_entries(text) == [
        ("unknown", "A remark whose second line looks marked.\n  The reviewer never writes one, so it stays. [fixed]", None),
    ]


def test_a_marked_line_with_no_separator_still_reads_its_disposition():
    rows = [
        ("- Affects: `.ai-factory/specs/trickster77777/0252-the-flows-own-git-calls-run-through-the-scrubbed-runner.md` (97.2). Once the task's commit runs with no bound, a hook that holds a stream keeps it waiting. [fixed]",
         "fixed", None),
        ("- Affects: the phase's closure (01). The recording of done still runs before the commit. [routed → .ai-factory/roadmaps/trickster77777.md § Phase 100]",
         "routed", {"roadmap": ".ai-factory/roadmaps/trickster77777.md", "phase": 100}),
        ("- Affects: the phase's closure (01). A stale line in a landed task's spec. [dismissed]",
         "dismissed", None),
    ]
    for line, kind, target in rows:
        text = _review(f"## Deferred observations\n\n{line}\n")
        (affects, observed, disposition), = read_entries(text)
        assert disposition == Disposition(kind, target), line
        assert "[" not in affects and "[" not in observed, line
        assert observed == "", line


def test_a_section_is_closed_by_the_answer_s_closing_signal():
    for signal in ("REVIEW_PASS", "PLAN_REVIEW_PASS", "ESCALATION"):
        text = _review(
            "## Deferred observations\n"
            "\n"
            "- Affects: unknown — The remark the reviewer deferred.\n"
            "\n"
            f"{signal}\n"
            "\n"
            "- Affects: unknown — A line after the answer closed, not an entry.\n"
        )
        assert read_entries(text) == [("unknown", "The remark the reviewer deferred.", None)], signal


def test_an_identical_remark_in_a_failing_and_a_passing_round_is_one_entry():
    failing = _review("## Deferred observations\n\n- Affects: unknown — The same remark, twice found.\n")
    passing = (
        "## Review\n"
        "\n"
        "The fix holds.\n"
        "\n"
        "## Deferred observations\n"
        "\n"
        "- Affects: unknown — The same remark, twice found.\n"
        "\n"
        "REVIEW_PASS\n"
    )
    listed = collect_entries(None, "01-1-1-alpha", [("review", 1, failing), ("review", 2, passing)])
    assert len(listed) == 1
    assert listed[0]["text"] == "The same remark, twice found."
    assert listed[0]["sources"] == [
        {"step": "review", "round": 1},
        {"step": "review", "round": 2},
    ]


def test_an_affects_line_outside_the_section_is_not_an_entry():
    text = (
        "- Affects: unknown — A line before any section.\n"
        "\n"
        "## Review\n"
        "\n"
        "- Affects: unknown — A line in a section that is not the one.\n"
    )
    assert read_entries(text) == []


def test_a_section_later_in_the_file_is_read_after_its_heading():
    text = (
        "## Review\n"
        "\n"
        "- Affects: unknown — Not an entry: no section has begun.\n"
        "\n"
        "## Deferred observations\n"
        "\n"
        "- Affects: Phase 3 — The one entry the file carries.\n"
    )
    assert read_entries(text) == [("Phase 3", "The one entry the file carries.", None)]


def test_trailing_blank_continuation_lines_are_not_text():
    first = _review("## Deferred observations\n\n- Affects: unknown — The same remark.\n\n")
    second = _review("## Deferred observations\n\n- Affects: unknown — The same remark.\n\n## Verdict\n")
    listed = collect_entries(None, "01-1-1-alpha", [("review", 1, first), ("review", 2, second)])
    assert len(listed) == 1
    assert listed[0]["text"] == "The same remark."


def test_entry_id_is_the_digest_of_segment_stem_affects_and_text():
    expected = hashlib.sha256(
        "\0".join(("named", "01-7-1-eta", "unknown", "A remark.")).encode("utf-8")
    ).hexdigest()
    assert entry_id("named", "01-7-1-eta", "unknown", "A remark.") == expected
    assert entry_id(None, "01-1-1-alpha", "unknown", "A remark.") != expected
    assert entry_id("named", "02-1-2-beta", "unknown", "A remark.") != expected
    assert entry_id("named", "01-7-1-eta", "Phase 2", "A remark.") != expected
    assert entry_id("named", "01-7-1-eta", "unknown", "A remark, reworded.") != expected


def test_collect_entries_names_an_entry_by_its_first_appearance():
    first = _review("## Deferred observations\n\n- Affects: unknown — First seen in the review.\n")
    earlier = _review("## Deferred observations\n\n- Affects: Phase 2 — First seen in the plan review.\n")
    listed = collect_entries(
        None, "01-1-1-alpha", [("plan-review", 1, earlier), ("review", 1, first)],
    )
    assert [entry["text"] for entry in listed] == [
        "First seen in the plan review.",
        "First seen in the review.",
    ]
    for entry in listed:
        assert len(entry["entry"]) == 64
        int(entry["entry"], 16)


def test_collect_entries_takes_the_latest_round_s_marker():
    unmarked = _review("## Deferred observations\n\n- Affects: unknown — A remark twice.\n")
    fixed = _review("## Deferred observations\n\n- Affects: unknown — A remark twice. [fixed]\n")
    dismissed = _review("## Deferred observations\n\n- Affects: unknown — A remark twice. [dismissed]\n")
    listed = collect_entries(
        None, "01-1-1-alpha", [("review", 1, fixed), ("review", 2, unmarked), ("review", 3, dismissed)],
    )
    assert len(listed) == 1
    assert listed[0]["disposition"] == {"kind": "dismissed"}


def test_an_entry_without_a_marker_carries_none():
    listed = collect_entries(
        None, "01-1-1-alpha", [("review", 1, _review("## Deferred observations\n\n- Affects: unknown — A remark.\n"))],
    )
    assert listed == [{
        "entry": entry_id(None, "01-1-1-alpha", "unknown", "A remark."),
        "affects": "unknown",
        "text": "A remark.",
        "sources": [{"step": "review", "round": 1}],
        "disposition": None,
    }]
