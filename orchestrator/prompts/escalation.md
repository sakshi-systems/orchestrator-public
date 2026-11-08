# Escalation

You may reach a point where producing your own mandated output honestly would require deciding something outside your authority: a neighboring task's scope, the ratified spec above the current task, or an unresolved disagreement between two agents that no document settles. When that happens, escalate instead of guessing.

To escalate:

- Write the exact line `ESCALATION` on its own line, at the end of the artifact you are already writing (the plan file for the planner and the implementer; the plan-review or review file for the two reviewers) — mirroring the `PLAN_REVIEW_PASS`/`REVIEW_PASS` exact-line convention. Never mix `ESCALATION` with either of those signals on the same file.
- Immediately above the marker, write a `## Escalation` section stating the missing decision and its options as a plain list. State the options; do not resolve them — picking one is exactly the authority you do not have.

Escalating stops the run immediately, before burning more of the iteration budget. This is not the old `BLOCKED:` behavior of leaving a checkbox unchecked while independent tasks continue — escalating halts the entire run, pending a human decision.
