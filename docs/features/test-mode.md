# Test mode

## Why a separate mode

Writing tests is a specialized task: the planner must read the source code, derive concrete behavior cases from it, and name them in the target's own test convention, `should … when …` being one example. The regular planner does not do this — it plans feature implementation. Test mode plugs in a planner prompt specialized for this task, leaving the rest of the pipeline unchanged. Test mode keeps its planner whatever a project sets for [planning](../pipeline.md#planning-is-a-stage-a-project-can-switch-off), since the test run reads its command from the plan the test planner returns.

Test tasks live in their own `ROADMAP_TESTS.md` file, so they do not clutter the main roadmap.

## How it works

```
Planner.plan()              ← uses the test-planner prompt
  └─► PlanReviewer.review_plan()  ×N
        └─► Implementer.implement()
              └─► TestRunner.verify()  ×N   ← a real test run, not an LLM
                    └─► mark_done() + git commit
```

The key difference from `implement` is the final step. Instead of an LLM reviewer reading the test file "by eye," a real test command runs. `TestRunner` reads the `## Test Command` section from the plan file (the test planner always returns it in its answer, and the run writes the plan file from it), runs the command via shell, and captures stdout+stderr and the exit code. Exit code 0 means the task is done. Otherwise, the content of the test-output file in `test-runs/` reaches the implementer inline, in its message, as feedback for the next iteration.

Unlike the implement flow, the final check here is a real test run, not an LLM review — tests either pass or they do not. This closes a class of error an LLM cannot see: a test compiles and looks plausible, but fails at runtime because of a wrong mock or a wrong invariant.

In test mode, `Planner` uses a specialized prompt: it reads the full source of the target files and starts discovery from what the task names: its target files, its target test file, its test command and its scope, which win wherever the task states them. Where the task leaves something open, it reads the target's own test conventions and configuration, the tests already beside the target and the project's test configuration, whatever the language and the runner. Where neither the task nor the target tells how the tests are run, the planner [escalates](escalation.md). It returns a plan where each task is one group of related cases in the target's own grouping unit, a `describe` block in one ecosystem, a test class or a module in another, with named test cases. The plan's required field is `## Test Command`, with the exact command to run.

## Task format

Tasks for test mode must explicitly name what to test:

```
- [ ] **Tests: PriceFormatter** — Write unit tests for `app/format/price_formatter.py`.
  Target test file: `tests/test_price_formatter.py`.
```

The more precisely the target files and classes are named, the more precise the plan.

## Running it

```bash
uv run orchestrator test /path/to/project
```

The orchestrator reads the target project's test roadmap: `.ai-factory/ROADMAP_TESTS.md`, or for a named roadmap its sibling — see [named-roadmaps.md](named-roadmaps.md) for the name. After each task, the task is marked done in the test roadmap and a git commit is made that carries the mark.
