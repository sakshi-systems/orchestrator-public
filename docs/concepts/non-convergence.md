# When the loop does not converge

Non-convergence is [failure](outcomes.md): a signature (`PLAN_REVIEW_PASS` / `REVIEW_PASS`) did not appear within the `max_iterations` limit, the task is not marked done, and every round's artifacts stay on disk. But failure itself is not one scenario — it is two different ones, distinguished by reading review-file tails.

## Pattern 1: convergence without a signature

Finding severity **drops** round over round, each round's actionable findings are closed and verified by the next, the final round holds only low/informational items — but the signature is never given. The work is correct; the reviewer keeps finding something new, small, and real each time, and never commits to signing off.

The resolution is outside the loop: commit what is done as-is, turn the residual findings into a separate roadmap task. Another run is very likely to loop the same way — on cosmetics.

## Pattern 2: escalation around a single blocker

**The same finding repeats every round**, severity climbs round over round — this is the only channel of pressure available to the reviewer. Often the very first round names the nature of the blocker outright: "pending a spec-owner decision." Pressure works against execution defects, but is powerless against an unresolved semantic question — no matter how high the colour climbs, the implementer cannot decide what has not been decided a tier above it.

Characteristic symptom: the reviewer proposes a workaround, and the implementer refuses to apply it, because the code and the spec say otherwise. This is not a malfunction — it is two agents who raised their [context trees](context-model.md) from different roots (the planner, down from the roadmap and notes; the implementer, up from the plan and code) and diverged in the middle, in territory no document ratifies.

The resolution is respecification at the roadmap tier: re-decomposing the task, moving the disputed semantics into the phase's governing spec, and only then a re-run. More iterations inside the loop do not help.

A deadlock an agent can name outright does not have to wait for this resolution: it stops the run on its own, through escalation (see [escalation.md](../features/escalation.md) for the mechanism, [outcomes.md](outcomes.md) for the outcome) — before the iteration budget is exhausted. This applies only to the part of the pattern where one of the agents names the boundary of its own authority outright; a deadlock that no agent names still shows up only through reading review-tail trends, as described below.

## Pattern 3: a persistent session's stale verdict

`PlannerReviewer.review()` holds one session for the whole code review: the same session that wrote the plan reviews both the first round and every round after it. Left unchecked, this creates its own failure mode — the reviewer anchors on its own verdict from a past round and carries a finding forward as "carried over / unaddressed" into the new review file, even if the implementer has already fixed the code: the model trusts its session's memory of the file's content rather than rereading it.

Outwardly this is indistinguishable from pattern 2 — the same finding repeats round after round — but the nature is different: the blocker is not semantic, the finding is already closed in the code, the reviewer simply did not check the file's current state.

The countermeasure is at the prompt level, not the loop level: starting with the second round, `review()` passes the path to the previous review file and requires, for each of its findings, explicitly rereading the cited file via Read, quoting the current state of the lines, and giving a Fixed / Not fixed verdict with that quote as evidence, only then moving on to the normal search for new problems. This forces the model to check the file instead of its session memory on every round.

## Why the limit is small, and that is correct

The iteration limit is the price of not distinguishing the two patterns, not a criterion for telling them apart. It bounds the loss in pattern 2, without breaking anything in pattern 1. For the part of pattern 2 an agent does not recognize, as for pattern 1, there is no need to formalize an in-loop stop indicator — the classifier lives in the artifacts and is applied after the fact. The part of pattern 2 that an agent names outright does not wait for that classifier: it exits the loop live, through escalation (see [escalation.md](../features/escalation.md) for the mechanism, [outcomes.md](outcomes.md) for the outcome).

## What to read after a stop

- **The severity trend across rounds** — dropping (pattern 1) or climbing (pattern 2).
- **Repetition** — different findings in different places (pattern 1), or one blocker across every round (pattern 2).
- **The review's language** — phrasing like "spec-owner decision," "by design," "carried over" in the early rounds.

Diagnosis and repair are carried out by a chat skill over the remaining artifacts: `/task-rescue` diagnoses how deep the root cause runs (spec / plan / code), repairs to that depth, and rolls the sidecar and artifacts back to the repaired state.
