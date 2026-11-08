# Completion waiter reference

[Completion waiter](../features/completion-launcher.md) governs behavior. The ordinary CLI completion proof remains its correlated machine boundary.

## Launch command

```text
orchestrator-launch implement TARGET --head-repository PATH --head-owner OWNER --head-folder FOLDER [--roadmap VALUE] [--evidence-root PATH]
orchestrator-launch test TARGET --head-repository PATH --head-owner OWNER --head-folder FOLDER [--roadmap VALUE] [--evidence-root PATH]
```

The three head options are required. They name the head the run calls at its end by its repository path, owner and folder, the identity the [courier](../features/courier.md) takes; they are not the child runtime selector or a role session identity. `TARGET` and mode retain their original argument values. Commands use separate argv elements without a shell.

The default evidence root is `~/.local/state/orchestrator/invocations`. An explicit relative root resolves against caller cwd. A new canonical UUID names a fresh invocation directory; existing directories are not adopted. The resolved evidence root and invocation directory are outside the resolved target Git worktree, including symlink aliases.

The waiter uses the installed launcher's Python executable and its own process session. Its stdin is the null device; stdout/stderr go to `waiter.log`. The ordinary child inherits that detached session, caller cwd and normal environment. Its stdin is null and combined stdout/stderr go to `run.log`. Its argv is:

```text
PYTHON -m orchestrator.main MODE TARGET --completion-file ABSOLUTE_PROOF_PATH --invocation-id INVOCATION_UUID [--roadmap VALUE] --lock-fd FD
```

`PYTHON` is `sys.executable`, `MODE` is `implement` or `test`, and the proof path is `completion.json` in the fresh directory. The ordinary child has no run timeout.

## Launch pointer

After spawning the waiter, caller stdout contains only the absolute resolved invocation directory followed by a newline. It is a plain log pointer: no JSON object, PID field or additional launch-status protocol.

## Ordinary CLI proof options

`--completion-file PATH` and `--invocation-id UUID` are optional paired arguments on the ordinary implement/test command. Supplying only one is an argument error. The invocation identity is canonical lowercase hyphenated UUID text, exactly equal to `str(uuid.UUID(value))`. The completion destination is outside the resolved target Git worktree. The detached waiter supplies its fresh invocation's proof path; ordinary invocations without these options publish no completion file.

These options do not change direct CLI exit taxonomy: handled unconverged, halted and escalated runs retain their existing zero exit; ordinary argument errors and unhandled failures retain their normal nonzero behavior. Neither option is an environment variable or a role-port parameter.

## Roadmap option

`--roadmap VALUE` is an optional argument on `orchestrator-launch` and on the ordinary implement/test command, and the launcher forwards it to the child. Its meaning is stated in [named roadmaps](../features/named-roadmaps.md#what-it-is).

## Project lock

The lock is an operating-system file lock on `~/.local/state/orchestrator/locks/<key>.lock`, outside every target tree, under the same state root as the invocation evidence. `<key>` is the SHA-256 hexadecimal digest of the project's resolved path, symlinks included.

The launcher takes the lock without blocking, before it prints the invocation directory. When the lock is held it exits with status `3` and prints no pointer. The lock passes from the launcher to its waiter and from the waiter to the ordinary child as an inherited descriptor of the same open file description; `--lock-fd FD` gives the child the descriptor's number. A CLI given `--lock-fd` does not take the lock again; a CLI without it takes its own, and a held lock is a startup error. The waiter closes its copy once the child exits, before the courier send.

## Completion proof

The file is `completion.json` in the invocation directory. Only the ordinary CLI child writes it, using a sibling temporary file and atomic replacement at its terminal boundary. Its JSON object has exactly these fields:

| Field | Value |
|-------|-------|
| `invocation_id` | The supplied canonical invocation UUID string |
| `outcome` | Exactly `SUCCESS` or `FAIL`, under [Authoritative completion](../features/completion-launcher.md#authoritative-completion) |

A valid success example is:

```json
{"invocation_id":"11111111-1111-4111-8111-111111111111","outcome":"SUCCESS"}
```

The waiter requires a valid object with exactly these keys and types, the invocation identity it created, and a known outcome token. A missing file, unreadable/malformed JSON, unknown fields/value, non-object data or mismatched identity is unusable proof. The waiter never writes or repairs this child-owned file. Abrupt termination or an early startup failure may leave no proof.

## Invocation evidence

| Artifact | Content |
|----------|---------|
| `run.log` | Full combined ordinary CLI output |
| `waiter.log` | Actual child exit/outcome or child-creation error, and the courier's send diagnostics |
| `completion.json` | The unchanged child-owned terminal proof, when publication is possible |

The waiter neither rewrites the proof nor publishes auxiliary result/delivery records. Its ordinary subprocess observations are logged rather than serialized into another status schema. A child-creation failure selects negative completion and has no actual child exit to report.

## Queued message

The entire message the waiter hands the courier is exactly one token:

```text
SUCCESS
```

or

```text
FAIL
```

It contains no prefix, suffix, decoration, reason, project name, invocation ID or evidence path. The directory printed at launch is the caller's log pointer.

## Delivery attempt

The waiter makes one send of the token through the [courier](../features/courier.md), handing it the head identity given at launch. The send's mechanics, bound and failure handling belong to the courier. Sender diagnostics go to `waiter.log`; no separate sender-output files or delivery record is written.
