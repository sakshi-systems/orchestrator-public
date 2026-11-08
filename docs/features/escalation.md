# Escalation

## When it fires

An agent — planner, plan-reviewer, implementer, or reviewer — stops the run immediately, before exhausting its iteration budget, when its own mandated output cannot honestly be produced without a decision outside its authority: the scope of a neighboring task, the ratified spec above the current phase, or a deadlock between two agents that no document resolves (see [non-convergence.md](../concepts/non-convergence.md) for the shape of that deadlock).

## What it is not

Escalation is not a failure — no verdict is made about the work; it may be entirely correct. It is not a halt either — the cause is not an external resource or an infrastructure fault, but the agent's own honest recognition of the boundary of its authority. The failure/halt distinction lives in [outcomes.md](../concepts/outcomes.md); escalation is a third outcome alongside them.

## What it must carry

The escalating agent names the missing decision and its options; it never picks one — the choice is exactly the authority it does not have.

## How it is signalled

The agent signals this by writing `ESCALATION` as the last line of the artifact it is already producing — the plan file for the planner and the implementer, the plan-review or review file for the two reviewers (see [pipeline.md](../pipeline.md) for the shared signal protocol). The section above that line carries the missing decision and its options in full.

## What happens next

The run stops there. See [resume.md](resume.md) for what state this leaves on disk and what a run resumed on it does before a human resolves the decision.

## Notification

Escalation carries its own notification colour, listed in the alert table in [configuration.md](../reference/configuration.md).
