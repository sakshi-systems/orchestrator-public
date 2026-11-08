# Completion waiter

## Initiator interface

The waiter delivers completion to the head that started the run, whichever runtime that head is, through the [courier](courier.md). It supplies the detached command-completion return path for that caller. The caller is distinct from the agent runtime selected for the ordinary CLI child: [runtime selection](agent-runtime.md#runtime-selection) continues to follow the child's effective configuration.

The launcher prints the absolute external invocation directory as its log pointer and returns after spawning the detached waiter. The waiter runs the ordinary CLI, waits for its exit and sends only the [binary token](../reference/completion-launcher.md#queued-message). Detailed [outcomes](../concepts/outcomes.md), console output and task artifacts retain their existing meanings.

## Launch and output

The [command](../reference/completion-launcher.md#launch-command) names mode, target and the head to call. A fresh UUID-named directory keeps output outside the resolved target Git worktree; the shared [external-path boundary](../../orchestrator/layout.py) includes symlink resolution. The directory pointer is plain text, not another machine-status record or a readiness protocol.

The detached waiter has null stdin and file-backed stdout/stderr. It runs the ordinary CLI with the same Python executable, original mode/target, caller cwd and normal environment/configuration/authentication. Explicit [proof arguments](../reference/completion-launcher.md#ordinary-cli-proof-options) carry the child's completion destination and invocation identity. The proof sink is not injected into role-tool environments. The waiter waits for the ordinary child to exit, and the child's combined output is retained in its run log.

## One run per project

A project is running while its run holds the project's lock. The launcher takes the lock without blocking, before it prints the invocation directory; when it is held, the launcher prints no pointer and exits with its [lock status](../reference/completion-launcher.md#project-lock). Runs on different projects run at once.

The lock passes down to the waiter and to the ordinary CLI child as an inherited descriptor of the same open file description. A CLI handed the lock does not take it again; a CLI started directly takes its own and stops at startup, as a startup error, if it is held. The waiter closes its copy once the child exits, before the courier send, so a slow send never holds the project. The operating system releases the lock when the last process holding it exits, a crash included, so a dead run never leaves its project held, and the lock covers runs started directly as it covers launched ones.

The project is its resolved path, symlinks included. The lock's location is in the [reference](../reference/completion-launcher.md#project-lock).

## Authoritative completion

Each run begins with its process-local queue-exhaustion proof false. Only the dynamic loop establishes exhaustion of its effective mode and roadmap queue, including an initially empty queue and completion of work selected before a [breakpoint](../reference/target-project.md#breakpoint). Ordinary function return and notification emission are not completion proof. A manual stop is not successful exhaustion, even when its current task finishes.

The CLI alone publishes its [terminal proof](../reference/completion-launcher.md#completion-proof), correlated to the invocation identity. Successful proof requires explicit exhaustion and normal terminal completion without a stop or escaping exception. Handled failure, halt or escalation, configuration/startup errors, `SystemExit` and interruption cannot establish success. When startup or abrupt termination prevents publication, the proof is missing. The ordinary direct CLI's exit taxonomy and human reports remain unchanged.

The waiter consumes the child through process exit and reaps it. Success requires both a zero child exit and a valid matching successful proof. Every other observed ending is negative completion, including nonzero/signal exit, missing or malformed proof, a foreign invocation identity, or an explicit negative proof despite zero exit. It never classifies the run by parsing console text or observing the operator-notification channel.

## Evidence and delivery

The [external directory](../reference/completion-launcher.md#invocation-evidence) retains the full child output, waiter diagnostics and child-owned completion proof. The waiter logs actual child exit and the derived completion token. If child creation fails, it logs that failure and selects negative completion without inventing an exit code. Logs are outside the staged target because task commits stage the whole tree under [Git repository](../reference/target-project.md#git-repository).

After the child attempt ends, the waiter hands the [courier](courier.md) the head's identity and the [token](../reference/completion-launcher.md#delivery-attempt). Delivery diagnostics go to the waiter log. The child proof and actual exit decide the token; auxiliary result records and their publication are not another completion condition.

A killed or lost waiter may not deliver a notice. The log pointer supplies access to the account; this mechanism supplies no liveness registry or guaranteed-delivery service.

## Existing lifetime owners

The ordinary CLI retains [sleep prevention and operator-stop behavior](../concepts/fault-handling.md#the-known-faults), native role-child cleanup and task/artifact/commit ownership. The waiter remains outside model-role turns and preserves their [foreground obligations](../pipeline.md#agent-sessions). The bounded send imposes no run-wide timeout.
