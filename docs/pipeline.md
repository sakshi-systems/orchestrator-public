# Pipeline

## The agent pipeline

Every task goes through two phases, each bounded by the same iteration limit, unless the project chooses who reviews a task or how many plan reviews it takes (see [Who reviews a task](#who-reviews-a-task) and [The plan-review budget](#the-plan-review-budget)).

The plan phase runs first: `PlannerReviewer` explores the codebase and writes a plan. `PlanReviewer` checks it and writes the result to `.ai-factory/plan-reviews/` — ending with `PLAN_REVIEW_PASS`, or a description of the problems. If the plan does not pass, the planner reads the review file and rewrites the plan. The cycle repeats up to `max_iterations` times (set in [configuration.md](reference/configuration.md)). [The reviewers' strictness is intended](concepts/non-convergence.md#the-reviewers-strictness-is-intended).

Once the plan is approved, the implement phase begins: the implementer writes code, `PlannerReviewer` reviews the changes and writes the result to `.ai-factory/reviews/`. If problems are found, the path to the review file is passed to the implementer as an explicit parameter; it reads the file and applies fixes, and review repeats. The limit is the same, `max_iterations`. Exhausting the limit in either phase without a PASS signal is a task failure (see [outcomes.md](concepts/outcomes.md)). [The reviewers' strictness is intended](concepts/non-convergence.md#the-reviewers-strictness-is-intended).

## Who reviews a task

Who reviews a task is a kind of the verify step, chosen once where the run is assembled and never a branch inside the flow. The implement mode takes the kind that the setting `task_reviewer` names: the run's reviewer, by default, or the head that ordered the task. The head is the agent or person who ordered the task and reviews it outside the run. Test mode's kind is its real test run, whatever the setting says ([test-mode.md](features/test-mode.md)). At its start the run prints who reviews a task and the plan-review budget, after the agent lines.

Where the head reviews, the run ends a task at the implementer's turn. The implementer implements the plan once, and the task closes as a passed review closes it: the roadmap task is marked done with its duration, the record is closed and the change is committed. The run's reviewer is not called, and the run waits for no review and no signal. The head reads what landed and writes its review as one review file per landed task under `.ai-factory/reviews/`, after each task when the run steps by task and after the run when it goes through a phase whole, in the grammar of any review: its findings are fix rounds, each a new roadmap line which the run executes like any task, and its remarks for elsewhere stand under `## Deferred observations`, as the review grammar has it. A task that is done is not entered again.

One check stands in for the verdict. The implementer's turn must leave the project changed outside `.ai-factory/`, tracked or untracked, against the project's tree as it stood when the turn began. A turn that leaves the tree as it was fails the implement step, a failure notified red as `task-fail` ([Outcomes](concepts/outcomes.md)): the run stops on the task, the task is not marked done and nothing is committed, and a later run answers by calling the implementer again. A plan review spent and an implement step that changed nothing are failures, and a failure is repaired at the stop by fixing the task's spec or plan and running it again. A partial implementation that changed something passes the check; the head's review is what catches it. Escalation, faults, usage limits and the stop marker act as in the full flow.

## The plan-review budget

The plan review is chosen where the run is assembled, like every other difference of a run, and the flow never asks whether it is off. Where `plan_review_iterations` is set, it is the number of plan reviews a plan gets, in place of `max_iterations`: after a review that fails the planner revises the plan and the revised plan is reviewed again, until a review passes or the number is spent, and then the task stops. At `0` there is no plan review: the planner's plan goes straight to the implementer, `PlanReviewer` is never called and no plan-review file is written. Only the no-plan skip is unchanged.

The plan-review budget and who reviews a task are independent: with plan review off and the run's reviewer reviewing, the planner's plan goes to the implementer and its code is still reviewed. A project sets them through its [per-project overlay](reference/configuration.md#per-project-overlay); a project that sets nothing runs the full flow described above. An interrupted run resumes in a shortened flow with no step tokens of its own, as [Resume](features/resume.md#step-values) states.

## Agent sessions

Planner and reviewer are one agent (`PlannerReviewer`) in a single continuing session. The reviewer knows the planning context, including why the plan takes its shape. The implementer also continues its session across the fix cycle, accumulating the context of its own fixes.

Invocation and native instruction delivery live in [Agent runtime](features/agent-runtime.md); native model and effort values live in [Agent defaults](reference/configuration.md#agent-defaults).

`PlanReviewer` is a separate agent with a clean session on every attempt. This is deliberate: it looks at the plan without the author's own bias, as an independent reviewer.

Interrupted task continuation follows [Resume](features/resume.md): runtime ownership, saved identities, pending planning and pre-call attempt validation are checked before resumed work proceeds.

An agent's turn is the whole of its life. Each agent runs as one non-interactive call whose process ends when the agent ends its turn, and nothing wakes it again for pending work. Commands and delegated work complete before that turn ends; the agent does not promise a later wake-up or result. A tool returning a running operation has not completed that work. [Shared execution instructions](features/agent-runtime.md#shared-execution-instructions) govern how the agent continues it within the active invocation.

`PlannerReviewer`'s session also persists across a whole roadmap phase — see [phase-sessions.md](features/phase-sessions.md) for the phase boundary, the reset behaviour, and why the default disables carrying it forward.

For Claude, every invocation prints its session ID after the `--- Claude agent (...) ---` line. That identifier locates the session's own file under `~/.claude/projects/` for diagnosis.

## The file protocol

Agents never communicate directly — only through files in the target project's `.ai-factory/`. The planner writes the plan, the implementer reads it. `PlanReviewer` writes the plan review to `plan-reviews/`, the planner reads it on the next attempt. The reviewer writes the code review to `reviews/`, and the path to that file is passed to the implementer as an explicit parameter on the next iteration. The orchestrator coordinates the call order and watches for signals (`PLAN_REVIEW_PASS`, `REVIEW_PASS`, `ESCALATION`).

Artifact directory layout and per-roadmap routing live on [named-roadmaps.md](features/named-roadmaps.md); resume state built on top of this protocol lives on [resume.md](features/resume.md).

## Completion signals

Every agent uses the same protocol: it writes its result to a file, and puts a signal as the file's last line if all is well — or leaves the file without one if problems remain. A reader recognizes a signal when it stands as an exact line among the file's last five lines, so stray text an agent leaves after it does not hide it. `PlanReviewer` writes `PLAN_REVIEW_PASS`, the reviewer writes `REVIEW_PASS`. In test mode the test run, which is not an agent, writes `TEST_PASS` as the last line of its output file when the test command exits 0, and a reader recognizes it the same way (see [test-mode.md](features/test-mode.md)). The orchestrator reads the file, counts iterations, and decides whether to continue or to stop and show the last review to the user. A review file that was never written is not a result without a signal — it is no result at all, and the run halts rather than counting it (see [fault-handling.md](concepts/fault-handling.md)).

Any of the four roles may instead write `ESCALATION` as that last line — see [escalation.md](features/escalation.md) for what it means and what it requires.

A review file may carry a `## Deferred observations` section before the signal line — a non-blocking channel for observations that are verified but deliberately not blocking, outside the task's own scope. This section's presence does not affect PASS/FAIL: the orchestrator still determines the outcome only from the completion signal.
