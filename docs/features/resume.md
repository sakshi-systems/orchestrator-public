# Resume after an interruption

## Detecting where to continue

After an interruption, the orchestrator selects the task's existing record and continues from its durable boundary. A completed boundary avoids repeating completed work; pending planning repeats the interrupted planning round. Unusable or ambiguous state halts visibly under [Fault handling](../concepts/fault-handling.md#the-known-faults).

## Resume prerequisites

The caller prepares the executable, authentication, target working directory, project guidance and access to the original same-runtime session storage of each local runtime that a role takes, and the reach of each Hermes instance and each head a role names. Runtime selection follows [Agent runtime](agent-runtime.md#runtime-selection); changing the runtime of a role that holds a saved identity is a rare, externally prepared operation.

Record ownership follows [What the sidecar holds](#what-the-sidecar-holds). For resumable records, a role that would continue an identity under a different runtime, instance or head address than the one recorded halts, naming the role, before session identities are restored, artifact fallback is applied or the record is modified. Terminal handling follows [Plan selection](#plan-selection). Session identities are not converted or reconstructed, and an unusable continuation does not silently create a fresh session.

A deliberate fresh pipeline requires caller preparation. Resume does not automatically delete artifacts or migrate sessions to make a runtime switch possible.

## What the sidecar holds

The JSON sidecar shares the plan's exact stem: `.ai-factory/plans/{NN}-{slug}.json`. Named roadmaps use their existing artifact segment under [Named roadmaps](named-roadmaps.md). The record holds each role's runtime beside its identity, the current boundary, saved session identities, the time of each step of the task by attempt and any [escalation account](escalation.md).

`runtime` records the [agent runtime](agent-runtime.md#agent-runtime) that the effective run configuration names for each role the run has, the four roles, or, in an implement run under planning off, the implementer and the code reviewer alone, test mode recording all four whatever `planning` holds, with the Hermes instance or the head's address where a role has one, not the enclosing architect's identity, runtime or environment. When present, each is an exact recognized selector from [Configuration](../reference/configuration.md#settings-keys). Absence means legacy Claude; a provided unknown or malformed owner halts. A role with no identity yet takes the runtime the settings name.

`planner`, `implementer` and `code-reviewer` hold the continuing task identities, each the identity of that role's own session; the code reviewer is fresh per task and continues across the task's fix iterations, and never continues the `planner` identity. `plan-reviewer-<N>` holds the independent plan reviewer identity for the attempt named by its plan-review artifact. It is recorded as soon as that reviewer returns, even without a review artifact, and is audit-only: resume never uses it. A role taken by a head records the head's `<owner>/<folder>` as its identity, like every role's, and the run's call takes no continuation from it: the head's chat is its own, and the record holds the head's address beside the role's runtime.

Known session-identity fields, when present, are nonblank strings retained without trimming. Invalid presence halts rather than permitting a fallback. `times`, when present, has the shape below, and a value of another shape halts like any known field. Unknown optional keys are preserved.

`times` holds the time of each step of the task by member and attempt: an object keyed by member name, `planner`, `plan-reviewer`, `implementer`, `code-reviewer` and `test-runner`, each holding an object keyed by the attempt number as a decimal string, whose value is whole seconds as a nonnegative decimal-seconds string of ASCII digits. The attempt is the step's round, which the run already counts: the planning round, the plan review's attempt, the iteration. A step's time runs from its start to the next step's start or the task's end, so the sum of the record is the task's wall time. A repeated step, by a rollback and a re-run or a resume, adds to its key, and nothing a rollback leaves in the sidecar is deleted. The sum is the stamp a finished task's roadmap line carries ([Target project — What to prepare](../reference/target-project.md#what-to-prepare)). An interrupted step's time until the halt is lost, since a hard kill has no step end to write. A record in flight from the earlier release still carries `elapsed`, which nothing reads, so its task's stamp loses the time before the upgrade once.

### Reading and atomic updates

An absent sidecar differs from an existing unreadable file, malformed JSON or a non-object JSON value. Existing invalid data halts; reads and merges never replace bad evidence with an empty object. Invalid known field values also halt.

Updates merge the existing record and use a temporary file plus atomic replacement. Runtime ownership and the corresponding explicit boundary are saved together, including during [legacy adoption](#legacy-adoption-and-artifact-fallback). There is no owner-only setup boundary: a tagged nonterminal record without a usable explicit step halts, even when otherwise empty.

## Planning intent and saved identities

Before every initial or revised planner invocation, the orchestrator atomically records the owner, `planning:N` and any known effective planner identity. A saved task planner identity takes precedence by presence. The current in-memory phase identity is used only when the task planner field is absent, under [Phase sessions](phase-sessions.md). Implementer identities, audit identities and other record data are retained.

`planning:N` is producer-owned pending intent. A failed or incomplete invocation leaves it authoritative even if plausible Markdown or review artifacts exist. Restart repeats round N in the same selected record group, using its saved identity when available; pending intent never falls back to artifacts.

Every initial, resumed or revised planning assignment carries the current task title and description, the supplied roadmap location and the grammar the planner's answer takes. Review feedback is included inline only when its artifact exists. Missing feedback does not reset the recorded round, invent findings or erase or replace a known session identity. Pending planning above round 1 still receives this complete task context when neither a saved task identity nor a phase identity exists. This assignment delivery does not replace native instruction policy or reconstruct a session.

With planning off, the laying of the task's spec is the planner's round: it passes through `planning:1` to `planned:1`, and a crash between the two repeats the laying ([Planning is a stage a project can switch off](../pipeline.md#planning-is-a-stage-a-project-can-switch-off)).

A successful planner return advances the boundary to `planned:N`. Returned role identities retain their normal persistence and escalation handling. An identity that the runtime never returns and the orchestrator never persists has no recovery guarantee.

## `step` values

The `:N` suffix is an ASCII decimal integer greater than zero. Leading zeroes may parse as the same number; their spelling requires no state-token migration. `plan_reviewed`, `escalated` and `done` are unindexed markers.

| `step` value | Meaning | What resume does |
|---|---|---|
| `planning:N` | Planner invocation for round N is pending | Repeats planning round N, regardless of existing artifacts |
| `planned:N` | Round N's plan is ready; no verdict yet | Reviews the plan at attempt N; with plan review off, starts implementation at iteration 1 |
| `plan_review_failed:N` | Round N's plan review failed | Plans round N + 1 |
| `plan_reviewed` | The plan passed review | Starts implementation at iteration 1 |
| `implemented:N` | Round N's code is ready; no verdict yet | Runs the verification of the run's kind at iteration N, as [Closing a record](#closing-a-record) says; where the reviewer is a head, a deposit already made for iteration N is read before the head is called again |
| `review_failed:N` | Round N's code review failed | Implements iteration N + 1 |
| `test_run_failed:N` | Round N's test run failed in test mode | Implements iteration N + 1 |
| `escalated` | An unresolved decision stops the task | Stops immediately without restoring session identities |
| `done` | The record is closed by successful completion | Excludes the record from continuation selection |

A rollback of a task to an earlier step leaves a call's head exchange files where they stand, and the comparison of the stored message with the one the call would send decides whether the call is kept or sent again.

### Attempt limits

Explicit state and the resolved next call are validated against their budget before role invocation or progress rewrite. This covers initial and revised planning, plan review, implementation and verification. The budget is `max_iterations`, except for the plan family, planning rounds and plan review, where `plan_review_iterations` is set: it bounds them, planning always allowed its first round, so that a record at `planned:1` meets the empty plan-review loop at `0`; with planning off the plan family's bound is `1`, `plan_review_iterations` is read by nothing, and the setting that gives the bound is `planning`: a record whose next plan-family attempt lies past the first, `planning:N` or `planned:N` with N past 1 or `plan_review_failed:N` at any N, halts naming `planning` and its value, and the halt says the task continues from a rollback to `planning:1` or with `planning` set to true. A halt names the setting whose budget was passed and that setting's own value. An over-budget indexed state cannot evade the check through artifact fallback; malformed indexed counters halt rather than falling back.

A resume past the configured attempt budget is the existing [halt](../concepts/fault-handling.md#the-known-faults). A live review cycle that spends its budget without a pass remains a work failure under [Outcomes](../concepts/outcomes.md). Terminal escalation and done retain their meanings.

## Plan selection

Selection stays inside the current mode and roadmap's artifact segment. Markdown and JSON are grouped by exact stem, with a numeric prefix followed by the exact task slug. Temporary files do not participate. Candidates are strictly validated and valid done groups are excluded first. Equal numeric prefixes among the remaining unfinished groups halt as ambiguous; otherwise the lowest numbered unfinished group is adopted. Malformed data cannot masquerade as valid done history. [Target project](../reference/target-project.md) permits repeated task names.

The selected prefix and stem are preserved exactly for every artifact lookup; selecting an existing single-digit prefix does not reformat its related paths. Both Markdown and JSON reserve sequence numbers, including JSON-only history. New numbering is one greater than the greatest numeric prefix; when none exists, the existing nonnumeric-Markdown count-plus-one fallback remains.

| Selected record | Continuation |
|---|---|
| No matching record | Creates a new attempt with pending planning recorded before invocation |
| Valid legacy Markdown with an absent or valid untagged sidecar | Uses legacy adoption under Claude |
| Tagged `planning:N`, with or without Markdown | Repeats that planning round in the selected group |
| Completed nonterminal boundary with Markdown | Uses its validated boundary and artifact requirements |
| Completed nonterminal boundary without Markdown | Halts |
| Untagged nonterminal JSON-only orphan | Halts |
| Valid `done`, including JSON-only | Skips the record and reserves its sequence |
| Valid `escalated`, with or without Markdown | Stops immediately without restoring identities, independently of the selected runtime |

Invalid record shape or a malformed owner does not constitute a valid terminal record. An otherwise valid done record needs no runtime compatibility check because its identities are not resumed.

## Legacy adoption and artifact fallback

Valid legacy Markdown may infer its dispatch only under Claude. An absent sidecar, absent step or unknown string step permits inference from plan reviews on disk. With no plan review, the plan is reviewed at attempt 1, or, with plan review off, implementation starts at iteration 1. When the latest plan review does not pass, planning continues at the next attempt. When it passes, implementation starts at iteration 1. [Completion signals](../pipeline.md#completion-signals) govern those verdicts.

Compatibility, known identity values, explicit counters and the resolved dispatch are checked first. The orchestrator then atomically records ownership with the inferred explicit boundary before invocation: `planned:N` for plan review, `plan_reviewed` for implementation (`planned:1` with plan review off, since no review passed), or `planning:N` for planning. No intermediate owner-only record is written.

A tagged record with a missing or unknown step halts. A non-string step or malformed counter on a recognized indexed step also halts. A recognized completed boundary whose referenced review is missing may retain artifact inference when the plan exists, including after deliberate review removal. Pending planning never uses this fallback. Completed nonterminal progress without its plan cannot be inferred into a fresh task.

## Closing a record

Normal successful verification marks the roadmap task done, records `done`, then commits through the existing pipeline. The verification of iteration N decides the task, the review in implement mode and the test run in test mode: a pass closes it, and findings, or a failing test run, send the implementer to iteration N + 1. A verification always follows the implementer's turn, and in implement mode a turn that changed nothing reaches the reviewer with an empty diff ([Who reviews a task](../pipeline.md#who-reviews-a-task)).

No planner return closes a record without a plan: an empty planner answer halts like the empty answer of any role, and the resumed run repeats the planning round. A task whose work already holds goes through its stages like any other, planning off included, and its review sees an empty diff.
