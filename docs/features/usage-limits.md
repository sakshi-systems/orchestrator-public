# Usage limits

## Selected pre-task check

Implement and test runs use the pre-task usage policy selected with their agent runtime at [assembly](../../orchestrator/main.py). The shared loop invokes that policy after selecting a pending task and its sequence, before task processing begins. An empty or completed queue performs no quota probe. Task execution follows [Pipeline](../pipeline.md) and the existing [stop semantics](../concepts/fault-handling.md#the-known-faults).

## Thresholds

The selected runtime's available five-hour and weekly windows are checked independently. Reaching or exceeding a configured threshold halts the run before the task starts; see [outcomes.md](../concepts/outcomes.md). Comparisons use the observed percentage without rounding. Default values and overlay rules live in [configuration.md](../reference/configuration.md).

| Threshold | Config key | Scope |
|---|---|---|
| Five-hour | `usage_threshold_5h` | The selected runtime's reported five-hour window |
| Weekly | `usage_threshold_weekly` | The selected runtime's reported weekly window |

Claude retains its `claude /usage` probe and session/weekly interpretation. Window availability is runtime- and account-dependent; Codex does not guarantee a five-hour window.

## Codex quota precheck

The Codex policy reads an account rate-limit snapshot through the native adapter's [account quota query](../reference/codex-runtime.md#account-quota-query), using the target project's normal configuration and authentication. It performs no model turn, constructs or probes no Claude runtime, and does not parse human `/status` output. Claude exhaustion does not prevent checking a selected Codex account.

The [native response contract](../reference/codex-runtime.md#quota-response-fields) selects the Codex bucket and identifies windows by their reported duration, not by slot position. Missing windows are absent, never zero usage. Malformed or unsupported windows do not suppress checks on other valid configured windows. Every available valid configured window receives its own threshold check; unknown durations acquire no invented threshold. Native token totals, balances and billing limits do not supply quota percentages, and the check consumes no reset credits or paid fallback.

An ordinary query, timeout, authentication or schema failure, or a snapshot with no applicable window, produces the [unavailable warning](#codex-unavailable-precheck) and continues without protection for unavailable data. This best-effort fallback does not absorb a halt raised by an observed threshold. `KeyboardInterrupt` and `SystemExit` propagate after native child cleanup rather than becoming unavailable status. Failure to obtain a snapshot is not a claim that Codex has no status API.

## Log line

### Claude usage

Available Claude percentages appear before the task, showing the accumulation trend:

```
  [usage: session 26% · week 52%]
```

### Codex unavailable precheck

Codex usage logs identify the observed windows and used percentages without inventing a missing window or remaining headroom. When no usable configured data is available, the policy writes this exact warning:

```text
  [usage check: Codex quota precheck unavailable; continuing without quota protection]
```

## When `/usage` cannot be parsed

If the Claude probe raises or its output contains no recognizable usage figure, a warning is logged and the run continues. Each available figure retains its own threshold check: a missing session figure does not skip the weekly check, and a missing weekly figure does not skip the session check. Only the unavailable figure's check is skipped.

## Runtime-reported limits and faults

The pre-task check does not replace invocation fault handling. Exhausted quota reported by the selected native runtime halts the run; recognized transient faults retain bounded retry, and unknown invocation faults surface under [Fault handling](../concepts/fault-handling.md#the-known-faults). Native recognition remains with the runtime adapter under [Agent runtime](agent-runtime.md#native-execution).
