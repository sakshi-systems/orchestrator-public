# Test mode

## Why a separate mode

Writing tests is a specialized task: the planner must read the source code, derive concrete behavior cases from it, and phrase them as `should … when …` test cases. The regular planner does not do this — it plans feature implementation. Test mode plugs in a planner prompt specialized for this task, leaving the rest of the pipeline unchanged.

Test tasks live in their own `ROADMAP_TESTS.md` file, so they do not clutter the main roadmap.

## How it works

```
PlannerReviewer.plan()      ← uses the test-planner prompt
  └─► PlanReviewer.review_plan()  ×N
        └─► Implementer.implement()
              └─► TestRunner.verify()  ×N   ← a real test run, not an LLM
                    └─► mark_done() + git commit
```

The key difference from `implement` is the final step. Instead of an LLM reviewer reading the test file "by eye," a real test command runs. `TestRunner` reads the `## Test Command` section from the plan file (the test planner always writes it), runs the command via shell, and captures stdout+stderr and the exit code. Exit code 0 means the task is done. Otherwise, the path to the test-output file in `test-runs/` is passed to the implementer directly as feedback for the next iteration.

Unlike the implement flow, the final check here is a real test run, not an LLM review — tests either pass or they do not. This closes a class of error an LLM cannot see: a test compiles and looks plausible, but fails at runtime because of a wrong mock or a wrong invariant.

In test mode, `PlannerReviewer` uses a specialized prompt: it reads the full source of the target files, finds existing `*.spec.*` / `*.test.*` patterns, and writes a plan where each task is one `describe` block with named test cases. The plan's required field is `## Test Command`, with the exact command to run.

## Task format

Tasks for test mode must explicitly name what to test:

```
- [ ] **Tests: PriceFormatter** — Write unit tests for `src/format/price-formatter.ts`.
  Target file: `src/format/price-formatter.spec.ts`.
```

The more precisely the target files and classes are named, the more precise the plan.

## Running it

```bash
uv run orchestrator test /path/to/project
```

The orchestrator reads the target project's test roadmap: `.ai-factory/ROADMAP_TESTS.md`, or for a named roadmap its sibling — see [named-roadmaps.md](named-roadmaps.md) for the name. After each task, the task is marked done in the test roadmap and a git commit is made that carries the mark.
