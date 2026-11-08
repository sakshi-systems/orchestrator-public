# Codex runtime reference

The [agent runtime feature](../features/agent-runtime.md#codex-creation-and-continuation) governs behavior. Role models and effort have their single home in [Agent defaults](configuration.md#agent-defaults). The native contract here specifies intended policy against standalone CLI 0.166.0 and the [tagged source anchors](#native-source-anchors). Source-derived fault cases are not live quota or transport observations.

## Invocation forms

These are argument-vector templates, with each option and its value passed as separate arguments rather than through a shell. `MODEL` and the displayed effort use [Agent defaults](configuration.md#agent-defaults); `CWD` is the target project, `UUID` is the explicit persisted thread identity, and `TOML_STRING` is the complete initial role instructions encoded as a TOML basic string.

Only `session_id=None` selects creation. A supplied identity must be a string exactly equal to `str(uuid.UUID(value))`: canonical lowercase, hyphenated UUID text. Validation precedes argument-vector construction and process launch. Blank strings, names, whitespace, option-looking selectors and noncanonical UUID spellings fail without spawning a child; values are not trimmed or normalized.

Syntax validation does not establish session existence. A valid but missing UUID surfaces the native unusable-session fault without an existence probe or fresh-session fallback. Caller preparation and runtime compatibility follow [Resume prerequisites](../features/resume.md#resume-prerequisites).

Fresh:

```text
codex exec --json --color never -m MODEL -c model_reasoning_effort="high" -c approval_policy="never" -s danger-full-access -C CWD -c developer_instructions=TOML_STRING -
```

Continuation:

```text
codex exec -C CWD -s danger-full-access -c approval_policy="never" resume --json -m MODEL -c model_reasoning_effort="high" UUID -
```

The subprocess `cwd` is also `CWD`; the trailing `-` receives only the assignment on stdin. TOML encoding escapes quotes, backslashes and control characters while preserving Unicode code points. JSON string escaping is usable only where TOML-compatible: Unicode is preserved rather than represented as surrogate-pair escapes.

The child environment excludes `CODEX_THREAD_ID`, `CODEX_SESSION_ID` and `CODEX_TOOL_CALL_ID`; other normal runtime/authentication environment remains available. Neither form uses `--ignore-user-config`, `--ignore-rules`, `project_doc_max_bytes=0`, `--last` or `--ephemeral`. Role invocation uses `exec`, not the native generic review command, and initial role policy stays in the developer layer. These command forms specify intended policy; production tool/permission acceptance remains distinct from the isolated evidence.

## Account quota query

The [Codex quota precheck](../features/usage-limits.md#codex-quota-precheck) uses a short-lived native process with the argument vector `codex app-server --stdio`, resolving the executable through the adapter. Its subprocess working directory is the selected target; normal configuration/authentication and the caller-identity cleanup from [Invocation forms](#invocation-forms) remain in effect. Role invocation continues to use `exec`.

### Quota RPC dialogue

Exchange newline-delimited JSON objects over separate stdin/stdout/stderr pipes:

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"orchestrator","version":"0.1.0"}}}
```

Wait for a successful response correlated to request 1, then send:

```json
{"method":"initialized"}
{"id":2,"method":"account/rateLimits/read"}
```

Drain stdout and stderr concurrently from launch while awaiting replies. Keep stdin open until the response correlated to request 2 arrives. Notifications and unrelated response IDs do not substitute for that reply. A matching RPC error, malformed protocol or non-object result makes the query unusable. The adapter returns the correlated request-2 result object after closing stdin, finishing pipe drainage and reaping the child. A usable correlated result and successful process completion are both required; exit 0 alone is insufficient.

The client issues no thread creation, turn creation, model, login/logout, authentication mutation or reset-credit operation. It neither discovers a control socket nor depends on a persistent service.

### Quota response fields

When a non-null `rateLimitsByLimitId` is supplied, select its `codex` object. Other buckets are not alternatives. A malformed supplied map or missing/non-object Codex bucket is unavailable data; it does not trigger legacy fallback. An absent or null map permits the legacy `rateLimits` object.

In either map or legacy view, a supplied non-null `limitId` on the selected bucket must be the exact string `codex`. A foreign, malformed, empty or noncanonical value makes the snapshot unavailable, including a conflicting explicit identity under the `codex` map key. Missing or null `limitId` is accepted for older unlabeled single-view compatibility; the map key already identifies a selected Codex bucket. Other names, including `limitName`, do not establish identity.

Inspect the selected bucket's `primary` and `secondary` independently. An absent or `null` window is absent data. A usable window supplies an actual finite nonnegative JSON number in `usedPercent`, excluding booleans and strings. Its `windowDurationMins` is a numeric duration exactly matching a row below, also excluding booleans and strings. Non-finite decoder extensions are invalid.

| Reported `windowDurationMins` | Configured threshold |
|---|---|
| `300` | `usage_threshold_5h` |
| `10080` | `usage_threshold_weekly` |

Slot names do not imply either duration. A malformed, missing or unknown-duration window leaves other valid windows eligible for checking. `resetsAt`, account identifiers, balances, spend caps and reset-credit data do not determine threshold usage. The policy logs window usage rather than raw account payloads. [Configuration](configuration.md#settings-keys) owns threshold defaults; [Usage limits](../features/usage-limits.md#codex-quota-precheck) owns comparison and unavailable-data behavior.

### Quota child lifetime

One **30-second operation deadline** covers initialization, account-response reading and process finish, with no fresh budget per stage. The adapter immediately registers its own-session child as active, retains the local process handle, and drains stdout and stderr concurrently while advancing the dialogue. Read, write, protocol, launch and timeout failures clean up and locally reap any owned child and clear active ownership. Shared force-quit clearing does not remove the local reaping obligation. `KeyboardInterrupt` and `SystemExit` propagate after cleanup rather than becoming a policy warning.

### Quota source anchors

- [App Server — Rate limits](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt) defines the machine account request, bucket views and variable-duration windows.
- [Developer commands](https://learn.chatgpt.com/docs/developer-commands) documents interactive status; its human rendering is separate from the machine query.

## Stream fields

The native stream is JSON Lines on stdout. Every line is parsed as a JSON object with a string `type`; unknown event kinds are ignored. The consumed fields are:

| Event | Consumed content |
|---|---|
| `thread.started` | `thread_id`: a valid UUID string; consistent across the invocation and equal to the requested UUID on resume |
| `item.completed` | For `item.type = agent_message`, `item.text` is a string; the last completed message supplies return text |
| `turn.completed` | Terminal successful-turn marker |
| `turn.failed` | Terminal failure, with `error.message` as a string |
| `error` | Top-level native `message` string; intermediate until the invocation ends |

Malformed consumed fields are protocol defects. A completion marker with an empty final message, no usable identity or a contradictory terminal event is not successful. Item events for tools, reasoning and other non-message content do not supply return text or fault evidence. Native usage accompanies completion but represents thread totals; this adapter does not sum resumed values as per-invocation usage.

## Failure recognition

Recognition applies only after the invocation fails or is incomplete, using the primary diagnostic chosen by [Completion and failure evidence](../features/agent-runtime.md#codex-completion-and-failure-evidence). An explicit stderr error record is one line whose trimmed text begins with `Error:` (case-insensitive). The rest of that line is its diagnostic; following lines remain available for reports but are not independently searched for a matching phrase. Ordinary warnings and log lines are not error records.

For matching, normalize case and straight/curly apostrophes, trim surrounding whitespace and one leading native `Error:` prefix. The primary diagnostic must itself match a form below; an unknown primary surfaces regardless of auxiliary text. A recognized quota primary produces `RateLimitError`. For a transient primary, inspect explicit stderr error records as auxiliary diagnostics: any quota form produces `RateLimitError`; otherwise, any auxiliary form outside both positive lists surfaces as a defect; otherwise, the transient remains retryable. Historical top-level error events are not auxiliary diagnostics when terminal failure supplies the primary. Successful invocations never enter this recognition path.

### Exhausted usage

These native forms produce `RateLimitError` before transient recognition:

- Opening prefix `You’ve hit your usage limit`, followed by end of text, punctuation or the source's ` for ` model-name variant.
- Opening sentence `Your workspace is out of credits.`
- Opening sentence `You hit your spend cap set in your workspace.`
- Opening sentence `You hit your spend cap set by the owner of your workspace.`
- Complete message `Quota exceeded. Check your plan and billing details.`

The workspace opening sentences may carry their native recovery guidance afterward. Bare HTTP 429, reset-time wording and generic rate-limit text are not evidence of exhausted user quota.

### Nontransient and unrecognized causes

The source's `ContentFilter` message, including `reason: content_filter`, and `ContextWindowExceeded` are outside the positive lists and surface as invocation defects. Authentication, configuration, argument and unusable-session errors are treated by that same outside-the-lists rule, without a semantic category detector. Missing-session output can consist of stderr alone, including native response code `-32600`. Such an explicit auxiliary error blocks an otherwise retryable primary under the rule above.

The prefixes `Connection failed`, `Error while reading the server response` and `stream disconnected before completion` do not establish a transient. Their details can name authentication, quota or policy failures; the stream prefix is also used by `ContentFilter`. Neither output absence nor an exit status supplies that missing classification.

### Recognized transients

Recognize only these complete normalized forms, subject to the primary/auxiliary rule above:

- `request timed out`
- `Selected model is at capacity. Please try a different model.`
- `exceeded retry limit, last status: STATUS` with an optional `, request id: ID` suffix, where `STATUS` is exactly a row below and `ID` is nonempty single-line native text.

| Accepted STATUS |
|---|
| `500 Internal Server Error` |
| `502 Bad Gateway` |
| `503 Service Unavailable` |
| `504 Gateway Timeout` |

The full diagnostic must match the format, including its canonical HTTP reason phrase. Numeric substrings, other status values, appended unrelated text and generic rate-limit messages do not match.

## Retry limits

The Codex adapter allows **3 total subprocess attempts**, with **30 seconds** between recognized transient failures. Exhausting this bound raises `NetworkError`; it does not start a fresh role session. [Retry continuity](../features/agent-runtime.md#codex-retry-continuity) governs thread ownership across attempts.

## Native source anchors

All links name tag `rust-v0.166.0`:

- [exec/src/lib.rs](https://raw.githubusercontent.com/openai/codex/rust-v0.166.0/codex-rs/exec/src/lib.rs): `thread_resume_params_from_config` and `thread_config_overrides_from_config` retain initial developer-policy semantics.
- [exec/src/exec_events.rs](https://raw.githubusercontent.com/openai/codex/rust-v0.166.0/codex-rs/exec/src/exec_events.rs): `ThreadEvent` and message-only `ThreadErrorEvent` define exec wire fields.
- [exec/src/event_processor_with_jsonl_output.rs](https://raw.githubusercontent.com/openai/codex/rust-v0.166.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs): intermediate errors, terminal status, final messages and `usage_from_last_total`.
- [protocol/src/error.rs](https://raw.githubusercontent.com/openai/codex/rust-v0.166.0/codex-rs/protocol/src/error.rs): `CodexErrorDetails`, `UsageLimitReachedError::fmt` and `RetryLimitReachedError::fmt` define the recognized native diagnostic text.
