# Outcomes

A run ends in one of these: success, **failure**, **halt**, or **escalation**. Failure and halt are different in kind, and that difference is the semantic core of all failure-handling behaviour.

## Failure ≠ halt

**Failure** — a step exhausted its attempt budget without its result: a review cycle without a PASS signal, or an implement step that changed nothing. Each review cycle is bounded by a finite attempt budget; if a PASS signal does not appear within it, the task is declared unconverged, and where the head reviews, an implementer's turn that leaves the project as it was fails the implement step. This is a **judgment about the work**: the orchestrator gave it its allotted attempts, and it did not converge.

**Halt** — the run stops for a reason unrelated to the correctness of the work: an external resource is exhausted, an infrastructure fault could not be absorbed, or an operator stopped the run by hand. This is **not a judgment about the work** — the current task may have been in perfectly good shape.

The same stoppage falls into one of these two categories by a single criterion: did the orchestrator reach a verdict about the work, or did it stop before reaching one.

## Invariants

1. **The attempt budget is finite.** The review cycle — plan and code — has a fixed iteration limit. Exhausting it without a PASS signal is a failure. A task whose code the head reviews fails in one more way: the implement step fails when the implementer's turn leaves the project unchanged, since no verdict follows to catch it (see [Who reviews a task](../pipeline.md#who-reviews-a-task)). Both are verdicts on the work, notified red as `task-fail`. There is no other way to "fail" a task.
2. **Failure, halt, and escalation are signalled differently to the operator.** The outcome is visible from the operator notification colour, not merely from the fact that the run ended. The colour-to-event binding, and how to enable notifications, live in [configuration.md](../reference/configuration.md).
3. **Transient faults are absorbed.** Recoverable failures — an API overload, a brief network error — are not surfaced immediately: the call is retried a bounded number of times. Only a persistent fault surfaces outward as a halt.
4. **Any halt is resumable.** An interrupted run — for any reason — leaves state on disk sufficient for the next run to continue from the last completed phase, not from the start of the task. Resume mechanics are described in [resume.md](../features/resume.md).
5. **Side effects are fail-safe.** A failed notification send, or any other external call, never changes the run's outcome. What delivery itself guarantees is described in [fault-handling.md](fault-handling.md).
6. **On failure, nothing is lost.** An unconverged task is not marked done, and every round's artifacts stay on disk for review. How to read that record is described in [non-convergence.md](non-convergence.md).

## Causes of a halt

A halt makes no judgment about the work, so its causes lie outside the review cycle. Every cause is enumerated in [fault-handling.md](fault-handling.md)'s catalogue.

In operator notifications, every halt case is yellow: the work is recognized as neither successful nor failed.

## Escalation

An agent stops the run immediately, before exhausting its iteration budget, when its own mandated output cannot honestly be produced without a decision outside its authority. Escalation is not a failure — no verdict is made about the work — and not a halt — the cause is the agent's own recognition of the boundary of its authority, not an external resource or infrastructure fault. Like a halt, it is resumable (invariant 4 extends to it), and it carries its own notification colour, distinct from failure's and halt's. What triggers it, what it must carry, and what a resumed run does are described in full on [escalation.md](../features/escalation.md).
