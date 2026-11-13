# Resume after an interruption

## Detecting where to continue

After an interruption, the orchestrator selects the task's existing record and continues from its durable boundary. A completed boundary avoids repeating completed work; pending planning repeats the interrupted planning round. Unusable or ambiguous state halts visibly under [Fault handling](../concepts/fault-handling.md#the-known-faults).

## Resume prerequisites

The caller prepares the selected runtime's executable, authentication, target working directory, project guidance and access to the original same-runtime session storage. Runtime selection follows [Agent runtime](agent-runtime.md#runtime-selection); changing the selected runtime is a rare, externally prepared operation.

Record ownership follows [What the sidecar holds](#what-the-sidecar-holds). For resumable records, a mismatch with the selected runtime halts before session identities are restored, artifact fallback is applied or the record is modified. Terminal handling follows [Plan selection](#plan-selection). Session identities are not converted or reconstructed, and an unusable continuation does not silently create a fresh session.

A deliberate fresh pipeline requires caller preparation. Resume does not automatically delete artifacts or migrate sessions to make a runtime switch possible.

## What the sidecar holds

The JSON sidecar shares the plan's exact stem: `.ai-factory/plans/{NN}-{slug}.json`. Named roadmaps use their existing artifact segment under [Named roadmaps](named-roadmaps.md). The record holds runtime ownership, the current boundary, saved session identities, accumulated elapsed time and any [escalation account](escalation.md).

`runtime` records the effective run configuration's selected [agent runtime](agent-runtime.md#agent-runtime), not the enclosing architect's identity, runtime or environment. When present, it is an exact recognized selector from [Configuration](../reference/configuration.md#settings-keys). Absence means legacy Claude; a provided unknown or malformed owner halts.

`planner` and `implementer` hold the continuing task identities. `plan-reviewer-<N>` holds the independent reviewer identity for the attempt named by its plan-review artifact. It is recorded as soon as that reviewer returns, even without a review artifact, and is audit-only: resume never uses it. Code review continues the planner's identity and needs no separate key.

Known session-identity fields, when present, are nonblank strings retained without trimming. Invalid presence halts rather than permitting a fallback. `elapsed`, when present, is a nonnegative decimal-seconds string using ASCII digits. Unknown optional keys are preserved.

### Reading and atomic updates

An absent sidecar differs from an existing unreadable file, malformed JSON or a non-object JSON value. Existing invalid data halts; reads and merges never replace bad evidence with an empty object. Invalid known field values also halt.

Updates merge the existing record and use a temporary file plus atomic replacement. Runtime ownership and the corresponding explicit boundary are saved together, including during [legacy adoption](#legacy-adoption-and-artifact-fallback). There is no owner-only setup boundary: a tagged nonterminal record without a usable explicit step halts, even when otherwise empty.

## Planning intent and saved identities

Before every initial or revised planner invocation, the orchestrator atomically records the owner, `planning:N` and any known effective planner identity. A saved task planner identity takes precedence by presence. The current in-memory phase identity is used only when the task planner field is absent, under [Phase sessions](phase-sessions.md). Implementer identities, audit identities and other record data are retained.

`planning:N` is producer-owned pending intent. A failed or incomplete invocation leaves it authoritative even if plausible Markdown or review artifacts exist. Restart repeats round N in the same selected record group, using its saved identity when available; pending intent never falls back to artifacts.

Every initial, resumed or revised planning assignment carries the current task title and description, the supplied roadmap location and the assigned plan output path. Review feedback is included only when its artifact exists. Missing feedback does not reset the recorded round, invent findings or erase or replace a known session identity. Pending planning above round 1 still receives this complete task context when neither a saved task identity nor a phase identity exists. This assignment delivery does not replace native instruction policy or reconstruct a session.

A successful planner return with a plan advances the boundary to `planned:N`. Returned role identities retain their normal persistence and escalation handling. An identity that the runtime never returns and the orchestrator never persists has no recovery guarantee.

## `step` values

The `:N` suffix is an ASCII decimal integer greater than zero. Leading zeroes may parse as the same number; their spelling requires no state-token migration. `plan_reviewed`, `escalated` and `done` are unindexed markers.

| `step` value | Meaning | What resume does |
|---|---|---|
| `planning:N` | Planner invocation for round N is pending | Repeats planning round N, regardless of existing artifacts |
| `planned:N` | Round N's plan is ready; no verdict yet | Reviews the plan at attempt N; with plan review off, starts implementation at iteration 1 |
| `plan_review_failed:N` | Round N's plan review failed | Plans round N + 1 |
| `plan_reviewed` | The plan passed review | Starts implementation at iteration 1 |
| `implemented:N` | Round N's code is ready; no verdict yet | Runs the verification of the run's kind at iteration N; where the head reviews, closes the task as [Closing a record](#closing-a-record) says |
| `review_failed:N` | Round N's code review failed | Implements iteration N + 1 |
| `test_run_failed:N` | Round N's test run failed in test mode | Implements iteration N + 1 |
| `escalated` | An unresolved decision stops the task | Stops immediately without restoring session identities |
| `done` | The record is closed by successful completion or a successful no-plan skip | Excludes the record from continuation selection |

### Attempt limits

Explicit state and the resolved next call are validated against their budget before role invocation or progress rewrite. This covers initial and revised planning, plan review, implementation and verification. The budget is `max_iterations`, except for the plan family, planning rounds and plan review, where `plan_review_iterations` is set: it bounds them, planning always allowed its first round, so that a record at `planned:1` meets the empty plan-review loop at `0`; a halt names the setting whose budget was passed and that setting's own value. An over-budget indexed state cannot evade the check through artifact fallback; malformed indexed counters halt rather than falling back.

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

Normal successful verification marks the roadmap task done, records `done`, then commits through the existing pipeline. Where the head reviews, a finished implementation takes that place: once the project is found changed outside `.ai-factory/` against its tree as it stood when the implementer's turn began, the task closes the same way; otherwise the implement step fails and the run stops on the task, with its record left at the boundary before the implementer, so a later run calls the implementer again, continuing its session with the plan. A resume at `implemented:N` runs in a new process with no such tree to compare against, so it closes the task unchecked.

Only a successful planner return with no plan takes the no-plan skip path. It records `done` before marking the roadmap task skipped, so a later task with the same title cannot adopt that JSON-only record. A crash between those writes can leave the task unchecked; the next run retries that task with a fresh record. Skip retains its existing commit behavior and introduces no separate terminal token.
