# Agent runtime behavior

## Agent runtime

An **agent runtime** is the execution environment through which an agent carries out turns, including its native tools, instruction delivery, session continuity and process behavior. Claude Code and Codex are agent runtimes. The invocation boundary and its adapter implementations live in [ports.py](../../orchestrator/ports.py), [claude_cli.py](../../orchestrator/claude_cli.py) and [codex_cli.py](../../orchestrator/codex_cli.py).

## Invoking a role

A role asks the runtime to carry out its current turn in the target project's working directory. Every call supplies the role's semantic identity, its complete instructions, the current assignment and the explicit session identity to continue when one exists. The runtime receives role identity separately from the instruction text, so native execution policy does not depend on recognizing prose.

The complete instructions remain available on resumed calls as well as new ones. For the combined planner/reviewer role, they include planning, code review and escalation together; changing the turn's assignment does not discard part of that role. The adapter chooses how the native runtime receives these instructions. Availability at the boundary does not require appending their full text to the conversation on every turn.

## Shared execution instructions

Every role executed by a model receives common execution instructions alongside its role instructions. The available local capabilities support reading files, enumerating paths, searching contents, editing files, writing assigned artifacts and running shell commands. Planners and reviewers write artifacts as part of their work. Instructions describe executable actions through the runtime's available tools rather than provider-specific tool names. Composition ownership lives in [agents.py](../../orchestrator/agents.py).

The agent retains any running cell or session identifier returned by a tool and uses the available continuation or wait operation until terminal completion. It inspects exit status and output before claiming completion. Commands and delegated work follow [the current turn's lifetime](../pipeline.md#agent-sessions).

Role instructions preserve mandatory project-context walks, full-file reads and artifact obligations. Re-review reopens current files, quotes the current content relevant to each earlier finding and records a verdict for each finding. Review substance, [completion signals](../pipeline.md#completion-signals) and [escalation](escalation.md) retain their requirements.

Structural updates follow the target project's governing documentation homes. The module map belongs in its architecture document where present; AGENTS.md and CLAUDE.md retain their established routing and symlink arrangement.

Foreground completion is an instruction contract. Native settings govern local execution policy; they do not establish a universal absence of background-capable tools, an operating-system security boundary or unattended access to every plugin. Native option values remain in [Agent defaults](../reference/configuration.md#agent-defaults) and [Codex invocation forms](../reference/codex-runtime.md#invocation-forms).

## Execution validation

Offline validation captures the instructions supplied to a fake runtime backend on initial and continuing calls, observing the full composition, the common execution fragment exactly once and current-file reading obligations in re-review assignments. Native argument policy retains its existing coverage against the [invocation reference](../reference/codex-runtime.md#invocation-forms).

Production capability evidence comes from a disposable full pipeline passage with normal project guidance, configuration and authentication. It starts with fresh native threads created with the new complete instructions, then validates continuations within that same initial policy; existing threads cannot hot reload changed instruction text. The target guidance carries a checkable canary, an assigned artifact destination resolves through a real symlink, and a finite command completes before the dependent artifact, PASS signal and final response. If a tool yields, its continuation reaches terminal completion within that invocation.

The retained evidence establishes the exercised working directory, guidance use, artifact writes and command completion. Its scope is the exercised configuration and tools; it does not establish universal background-tool exclusion. This witness accompanies the full pipeline acceptance passage rather than a separate capability-only pair of model calls.

## Runtime selection

The merged [configuration](../reference/configuration.md#settings-keys) selects the runtime once for a run. The selection is visible in the run's output; choosing Codex is explicit opt-in. A role's model and effort come from the selected runtime's block of the [agent settings](../reference/configuration.md#agent-defaults), and at the start of a run, under the runtime line, the run prints every role with its model and effort, so they show in the console and in `run.log`. Implement and test runs use the same selection behavior, while test verification follows [Test mode](test-mode.md).

Continuing existing task history requires [Resume prerequisites](resume.md#resume-prerequisites); selecting a runtime does not transfer another runtime's sessions.

A recognized selector is distinct from an available runtime. An unavailable selected runtime surfaces an invocation error before usage probing, the task loop or task-artifact mutation; the run does not substitute another runtime. The selected adapter validates its executable during assembly, before those operations, with a missing executable following [Fault handling](../concepts/fault-handling.md#the-known-faults).

Explicit Codex selection runs the shared CLI pipeline. The selected runtime's pre-task policy follows [Usage limits](usage-limits.md#selected-pre-task-check), including Codex's read-only account-window check; Claude retains its execution behavior. Construction ownership lives in [main.py](../../orchestrator/main.py).

Disposable full-pipeline validation under [Execution validation](#execution-validation) precedes real Codex project queues. Execution through fallback subagent role loops does not establish native CLI acceptance.

## Native execution

Claude executes the shared roles through its CLI adapter. The adapter owns native tool policy and its flags; a role's model and effort come from the selected runtime's block of the [agent settings](../reference/configuration.md#agent-defaults). It supplies the role instructions as the native system prompt on the initial call and uses the explicit session identity for continuation. Session creation and reuse follow [Pipeline — Agent sessions](../pipeline.md#agent-sessions).

The adapter returns the invocation's output and session identity, or raises the fault described by [Fault handling](../concepts/fault-handling.md). Artifact verdicts and escalation remain governed by [Pipeline — Completion signals](../pipeline.md#completion-signals) and [Escalation](escalation.md); invocation completion alone does not decide the work's outcome. Usage checks follow [Usage limits](usage-limits.md).

The ownership of the runtime boundary, native adapter and shared child-process control is specified in [ports.py](../../orchestrator/ports.py), [claude_cli.py](../../orchestrator/claude_cli.py), [codex_cli.py](../../orchestrator/codex_cli.py) and [process_control.py](../../orchestrator/process_control.py).

## Codex creation and continuation

Codex uses the shared invocation boundary with its native adapter. Creation delivers the complete role instructions in the developer layer and the current assignment on standard input. A supplied thread continues the same role-instruction composition: native continuation retains the initial developer policy. The combined planner/reviewer keeps its planning, review and escalation instructions throughout its assignments.

The adapter validates supplied continuation syntax under [Codex invocation forms](../reference/codex-runtime.md#invocation-forms) before constructing arguments or starting a child. Same-runtime storage and caller preparation remain governed by [Resume prerequisites](resume.md#resume-prerequisites).

The adapter keeps normal project guidance, user configuration, rules and authentication available. It removes inherited caller identity from the child environment and uses the target project as the child working directory. Exact command forms, encoding and environment names live in [Codex runtime — Invocation forms](../reference/codex-runtime.md#invocation-forms). These are intended invocation policy; production permission and foreground-tool behavior remain subject to execution and acceptance validation under [Runtime selection](#runtime-selection).

## Codex completion and failure evidence

Each invocation is read through process exit. Success requires a successful process, an unambiguous terminal completion, one consistent valid thread identity and a nonempty final completed agent message. A continued thread's emitted identity matches the requested identity. The return text is that last completed agent message, rather than concatenated commentary or tool output. [Codex runtime — Stream fields](../reference/codex-runtime.md#stream-fields) defines the consumed native fields.

Unknown event kinds are ignored. Malformed JSON, malformed consumed fields, conflicting identities and conflicting terminal outcomes surface as protocol defects. A completed successful invocation ignores stale intermediate errors and stderr warnings; assistant prose and tool output are never fault diagnostics.

For a failed or incomplete invocation, its terminal failure message is primary. Without that message, the last top-level native error supplies the primary diagnostic; without either, the last explicit stderr error record does. The exact record boundary and auxiliary-diagnostic rule live in [Failure recognition](../reference/codex-runtime.md#failure-recognition). An unknown primary failure surfaces rather than being replaced by a convenient transient phrase elsewhere. Missing completion or identity, empty output and nonzero exit alone do not establish transport failure. Native output, stderr and exit status remain available in error reports.

Known exhaustion halts the run. Explicit nontransient causes surface ahead of retryable causes; the precise positive forms and exclusions live in [Codex runtime — Failure recognition](../reference/codex-runtime.md#failure-recognition), under [Fault handling](../concepts/fault-handling.md). These decisions concern failed-invocation evidence; recovered intermediate errors never turn success into a fault. A failed turn does not return a result or advance the sidecar even if a plausible artifact exists. The adapter does not certify such an artifact; restart selection remains flow-owned under [Resume](resume.md). Artifact verdicts remain the roles' and pipeline's responsibility.

## Codex retry continuity

Recognized transient faults receive the bounded attempts and delay in [Retry limits](../reference/codex-runtime.md#retry-limits). A requested identity remains the continuation target. When a fresh attempt emits a valid identity before failing, the next attempt resumes that thread with the unchanged assignment and role policy. Only an original fresh invocation with no known identity can retry fresh. A rejected continuation surfaces its fault; retry exhaustion produces the existing infrastructure-halt outcome.

## Codex child lifetime

The adapter starts the child in its own process session and immediately registers it as the active child. It communicates the assignment through stdin while draining separate stdout and stderr pipes through exit; the native stream is parsed after communication completes. Communication closes stdin without bespoke reader workers. Native executable availability is checked during adapter construction without a model call.

Launch, communication and interruption failures retain the native error while cleaning up and reaping any local child and clearing its active reference. Forced quit uses shared child cleanup; soft stop retains the existing [run behavior](../concepts/fault-handling.md#the-known-faults). Process-control ownership lives in [process_control.py](../../orchestrator/process_control.py).
