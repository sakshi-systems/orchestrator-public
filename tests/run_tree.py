"""A project tree in today's layout — the oracle the commands over a run's
data are tested against: a roadmap with phases, a named roadmap and a flat
one, and for tasks in each state the plan, the sidecar, plan reviews, the
implementer's answers, reviews and test runs, an escalation among them — and
a task's disposition record, the closure's deposit already kept, beside the
markers an older review carries.

`show` runs over this tree as the command a client runs, and what it prints
is compared byte for byte, so a change of layout, naming or JSON shape shows
as a change of bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.observations import entry_id

ROADMAP = """# Roadmap

## Phase 1 — first doors

- [x] **1.1 — Alpha** — the done task
- [x] **1.1a — Refit Alpha's bell** — the landed fix line
- [ ] **1.2 — Beta** — the escalated task
- [ ] **1.3 — Gamma** — the underway task
- [ ] **1.4 — Delta** — the unstarted task

## Phase 2 — later doors

- [ ] **2.1 — Epsilon** — before the marker

---STOP---

- [ ] **2.2 — Zeta** — after the marker

## Phase 3 — routed doors

- [ ] **3.1 — Theta** — the open task a routed remark lands in — Spec: `.ai-factory/specs/trickster77777/0300-a-spec-a-routed-remark-lands-in.md`
"""

TESTS_ROADMAP = """# Test roadmap

## Tests

- [ ] **Cover Alpha** — a flat task, underway
- [ ] **Cover the parser** — a flat title that stands twice
- [ ] **Cover the parser** — the same title again
- [ ] **2.0 migration notes** — a flat title that begins like a number
"""

NAMED_ROADMAP = """# Named roadmap

## Phase 7 — the named phase

- [ ] **7.1 — Eta** — underway under a named roadmap
"""

ALPHA_PLAN = """# Plan: 1.1 — Alpha

Lay the greeting at the door.

## Steps

1. Write the greeting module.
"""

ALPHA_PLAN_REVIEW_1 = """The plan names its steps and the module they touch.

PLAN_REVIEW_PASS
"""

ALPHA_PLAN_REVIEW_2 = """An earlier attempt of this task stopped here once.

## Deferred observations

- Affects: unknown — The greeting module holds no word of the licence its helper wraps. [dismissed]
- Affects: Phase 2 — The doorway's width is a later phase's own concern. [promoted → .ai-factory/ROADMAP.md]

## Escalation

- A leftover stop the resumed task walked past.

ESCALATION
"""

ALPHA_IMPLEMENTER_1 = """Implemented the greeting module and its test.

DEVIATION: nothing — the plan held as written.
"""

ALPHA_REVIEW_1 = """No findings.

## Deferred observations

- Affects: .ai-factory/specs/trickster77777/0288-deferred-observations-are-read-as-entries-with-dispositions.md — The reading joins an entry by its affects and its text only, where the skills joined them by gist.
  A remark reworded in a later review is another entry.
- Affects: unknown — The greeting module holds no word of the licence its helper wraps.

REVIEW_PASS
"""

BETA_PLAN = """# Plan: 1.2 — Beta

Choose how the door locks.

## Steps

1. Add the lock helper.
"""

BETA_PLAN_REVIEW_1 = """An earlier attempt of this task stopped here once.

## Escalation

- A leftover stop the resumed task walked past.

ESCALATION
"""

BETA_IMPLEMENTER_1 = """Added the lock helper behind the door type.

DEVIATION: nothing — the plan held as written.
"""

BETA_REVIEW_1 = """## Review

The lock helper is sound, but the licence of the helper it wraps is not
something the reviewer can choose for the project.

## Deferred observations

- Affects: Phase 2 — The doorway's width belongs to a later phase's plan. [routed → .ai-factory/ROADMAP.md § Phase 2]
- Affects: .ai-factory/specs/trickster77777/0285-the-skills-reach-a-runs-data-only-through-the-orchestrators-commands.md — The commands' deposit needs its writer spelled. [routed → .ai-factory/specs/trickster77777/0290-orchestrator-dispose-and-the-closure-deposit.md]
- Affects: unknown — An old note whose marker no live vocabulary names. [audit-corroborated]

## Escalation

- The licence of the wrapped helper is a decision outside the reviewer's authority.
- The decision belongs to whoever holds the project.

ESCALATION
"""

GAMMA_PLAN = """# Plan: 1.3 — Gamma

Widen the doorway.

## Steps

1. Widen the frame constant.
"""

GAMMA_PLAN_REVIEW_1 = """The plan names its steps.

PLAN_REVIEW_PASS
"""

GAMMA_IMPLEMENTER_1 = """Widened the frame constant and its test.

DEVIATION: nothing — the plan held as written.
"""

COVER_PLAN = """# Plan: Cover Alpha

Cover the greeting module with tests.

## Test Command

`python -m pytest tests/test_greeting.py`

## Steps

1. Write the cases.
"""

COVER_PLAN_REVIEW_1 = """The cases miss the empty input.

"""

COVER_PLAN_REVIEW_2 = """The cases cover the empty input now.

PLAN_REVIEW_PASS
"""

COVER_IMPLEMENTER_1 = """Wrote the cases, the empty input included.

DEVIATION: nothing — the plan held as written.
"""

COVER_TEST_1 = """$ python -m pytest tests/test_greeting.py
exit code: 1
elapsed: 0.2s

1 failed, 2 passed
"""

ETA_PLAN = """# Plan: 7.1 — Eta

Paint the named door.

## Steps

1. Paint the door.
"""

ETA_PLAN_REVIEW_1 = """The plan names its steps.

## Deferred observations

- Affects: Phase 2 — the doorway's width belongs to a later phase's plan.

PLAN_REVIEW_PASS
"""

ETA_IMPLEMENTER_1 = """Painted the door.

DEVIATION: nothing — the plan held as written.
"""

THETA_SPEC = """# A spec a routed remark lands in

The doorway's width belongs to whoever holds Phase 3.
"""

# The dispositions the tree already holds, a closure's deposit kept in the
# orchestrator's own record: Alpha's undisposed observation routed into the
# open task's spec, and Beta's roadmap-routed remark disposed as fixed — the
# record's disposition is what `show` reports over the marker the older
# review carries.
ALPHA_DISPOSITIONS = {
    entry_id(
        None,
        "01-1-1-alpha",
        ".ai-factory/specs/trickster77777/0288-deferred-observations-are-read-as-entries-with-dispositions.md",
        "The reading joins an entry by its affects and its text only, where the skills joined them by gist.\n"
        "  A remark reworded in a later review is another entry.",
    ): {"kind": "routed", "target": {"spec": ".ai-factory/specs/trickster77777/0300-a-spec-a-routed-remark-lands-in.md"}},
}

BETA_DISPOSITIONS = {
    entry_id(
        None,
        "02-1-2-beta",
        "Phase 2",
        "The doorway's width belongs to a later phase's plan.",
    ): {"kind": "fixed"},
}



def build(root: Path) -> Path:
    """Build the project tree under `root` and return the project directory."""
    project = root / "project"
    factory = project / ".ai-factory"
    factory.mkdir(parents=True)
    (factory / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
    (factory / "ROADMAP_TESTS.md").write_text(TESTS_ROADMAP, encoding="utf-8")
    named = factory / "roadmaps"
    named.mkdir()
    (named / "named.md").write_text(NAMED_ROADMAP, encoding="utf-8")

    for dirname in ("plans", "plan-reviews", "reviews", "test-runs"):
        (factory / dirname).mkdir()
        (factory / dirname / "named").mkdir()

    # The open task's spec, the target a routed remark names, and the
    # dispositions the tree already holds, a record per task.
    specs = factory / "specs" / "trickster77777"
    specs.mkdir(parents=True)
    (specs / "0300-a-spec-a-routed-remark-lands-in.md").write_text(THETA_SPEC, encoding="utf-8")
    (factory / "dispositions").mkdir()
    (factory / "dispositions" / "01-1-1-alpha.json").write_text(json.dumps(ALPHA_DISPOSITIONS, indent=2), encoding="utf-8")
    (factory / "dispositions" / "02-1-2-beta.json").write_text(json.dumps(BETA_DISPOSITIONS, indent=2), encoding="utf-8")

    def sidecar(name: str, record: dict, segment: str | None = None) -> None:
        directory = factory / "plans" if segment is None else factory / "plans" / segment
        (directory / name).with_suffix(".json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    def write(dirname: str, name: str, text: str, segment: str | None = None) -> None:
        directory = factory / dirname if segment is None else factory / dirname / segment
        (directory / name).write_text(text, encoding="utf-8")

    # The done task: a full run, every kind one round, the record closed —
    # and a plan review of an earlier attempt that once escalated, a leftover
    # the resumed walk past, which is not the task's escalation.
    write("plans", "01-1-1-alpha.md", ALPHA_PLAN)
    sidecar("01-1-1-alpha.md", {"step": "done"})
    write("plan-reviews", "01-1-1-alpha-plan-review-1.md", ALPHA_PLAN_REVIEW_1)
    write("plan-reviews", "01-1-1-alpha-plan-review-2.md", ALPHA_PLAN_REVIEW_2)
    write("reviews", "01-1-1-alpha-implementer-1.md", ALPHA_IMPLEMENTER_1)
    write("reviews", "01-1-1-alpha-review-1.md", ALPHA_REVIEW_1)

    # The escalated task: the review raised a decision it could not make, and
    # the record names that artifact; a plan review of an earlier attempt
    # carries the marker as well, a leftover the record walks past.
    write("plans", "02-1-2-beta.md", BETA_PLAN)
    sidecar("02-1-2-beta.md", {
        "step": "escalated",
        "escalation": (
            "reviewer: The licence of the wrapped helper is a decision outside "
            f"the reviewer's authority. ({factory / 'reviews' / '02-1-2-beta-review-1.md'})"
        ),
    })
    write("plan-reviews", "02-1-2-beta-plan-review-1.md", BETA_PLAN_REVIEW_1)
    write("reviews", "02-1-2-beta-implementer-1.md", BETA_IMPLEMENTER_1)
    write("reviews", "02-1-2-beta-review-1.md", BETA_REVIEW_1)

    # The underway task: implemented once, no review yet.
    write("plans", "03-1-3-gamma.md", GAMMA_PLAN)
    sidecar("03-1-3-gamma.md", {"step": "implemented:1"})
    write("plan-reviews", "03-1-3-gamma-plan-review-1.md", GAMMA_PLAN_REVIEW_1)
    write("reviews", "03-1-3-gamma-implementer-1.md", GAMMA_IMPLEMENTER_1)

    # The flat task of the test roadmap: two plan-review rounds, a test run
    # that failed.
    write("plans", "04-cover-alpha.md", COVER_PLAN)
    sidecar("04-cover-alpha.md", {"step": "test_run_failed:1"})
    write("plan-reviews", "04-cover-alpha-plan-review-1.md", COVER_PLAN_REVIEW_1)
    write("plan-reviews", "04-cover-alpha-plan-review-2.md", COVER_PLAN_REVIEW_2)
    write("reviews", "04-cover-alpha-implementer-1.md", COVER_IMPLEMENTER_1)
    write("test-runs", "04-cover-alpha-test-1.txt", COVER_TEST_1)

    # The named roadmap's task, its artifacts under the roadmap's segment.
    write("plans", "01-7-1-eta.md", ETA_PLAN, segment="named")
    sidecar("01-7-1-eta.md", {"step": "implemented:1"}, segment="named")
    write("plan-reviews", "01-7-1-eta-plan-review-1.md", ETA_PLAN_REVIEW_1, segment="named")
    write("reviews", "01-7-1-eta-implementer-1.md", ETA_IMPLEMENTER_1, segment="named")

    # The machine's base the commands read: an empty one, so the tree alone
    # decides what they print.
    (root / "base.json").write_text("{}", encoding="utf-8")
    return project
