# Configuration

## Configuration file

All orchestrator settings are set in `orchestrator.json` at the repository root. The file is mandatory — the orchestrator will not start without it. It is gitignored, so setting changes never show up in `git diff`; the template to copy from is `orchestrator.json.example`.

```json
{
  "max_iterations": 3,
  "usage_threshold_5h": 90,
  "usage_threshold_weekly": 95,
  "enable_phase_sessions": false
}
```

All four fields are mandatory. A missing file, or a missing field, is a startup error with a clear message.

The file's path can be overridden through an environment variable:

```bash
ORCHESTRATOR_CONFIG=/path/to/my-config.json uv run orchestrator implement /path/to/project
```

## Settings keys

| Key | Default | Meaning |
|---|---|---|
| `max_iterations` | `3` | The iteration limit for every cycle — plan review, code review, and test run. See [pipeline.md](../pipeline.md). |
| `usage_threshold_5h` | `90` | Threshold for a reported five-hour window of the selected runtime. See [usage-limits.md](../features/usage-limits.md). |
| `usage_threshold_weekly` | `95` | Threshold for a reported weekly window of the selected runtime. See [usage-limits.md](../features/usage-limits.md). |
| `enable_phase_sessions` | `false` | Whether the `PlannerReviewer` session carries over between tasks within a phase. See [phase-sessions.md](../features/phase-sessions.md). |
| `roadmap_path` | absent | Which roadmap the orchestrator reads. See [named-roadmaps.md](../features/named-roadmaps.md). |
| `runtime` | `claude` | Optional runtime selector: exactly `claude` or `codex`. See [Runtime selection](../features/agent-runtime.md#runtime-selection). |
| `architect_roots` | `[]` | Setting of the context watch: the list of repository paths whose architect folders it reads, under [Finding the head](../features/context-watch.md#finding-the-head). An empty list names no head. |
| `context_windows` | `{}` | Context-watch setting: an object mapping a model name to its context window in tokens, for sessions whose runtime record states none. See [Context watch](../features/context-watch.md#reading-the-fill). |
| `claude_agents` | per role, below | Model and effort of each role when the runtime is `claude`: an object keyed by role. See [Agent defaults](#agent-defaults). |
| `codex_agents` | per role, below | Model and effort of each role when the runtime is `codex`: an object keyed by role. See [Agent defaults](#agent-defaults). |
| `snapshot_boundary` | `0.8` | Context-watch setting: the fraction of a session's window at which the watch warns a head or a hand, one boundary for both. A number above 0 and at most 1. See [The warning](context-watch.md#the-warning). |

`runtime` is validated after the existing base/overlay merge. Only an absent key uses its default; an empty string, `null`, a non-string or any other value is a configuration error. The visible startup label is `Agent runtime:`. Under it the run prints one line per role, in the order `planner_reviewer`, `plan_reviewer`, `implementer`, of the form `<role key>: <model> (<effort>)` with two leading spaces, giving the values the run uses.

The machine-wide settings, `architect_roots`, `context_windows` and `snapshot_boundary`, belong to the machine rather than to a project, so they are read from the global file only, and a project overlay's value for them is not consulted. Each is validated when the context watch starts: `architect_roots` is a list of paths, `context_windows` an object of positive integers and `snapshot_boundary` a number above 0 and at most 1. A bad setting is a [logged](context-watch.md#the-log) fault, and the command still ends with exit status 0.

## Agent defaults

A role's model and effort are settings, one block per runtime: `claude_agents` and `codex_agents`. Each is an object keyed by role — `planner_reviewer`, `plan_reviewer` and `implementer` — whose value holds `model` and `effort`. The tables below are the defaults; a role left out of a block takes its default. The runtime stays the one `runtime` key, and the block of the runtime that is not selected stays in the file, unused. Their application belongs to [Agent runtime](../features/agent-runtime.md#runtime-selection); session policies belong to [Pipeline](../pipeline.md#agent-sessions).

```json
{
  "claude_agents": { "implementer": { "model": "sonnet", "effort": "high" } },
  "codex_agents": { "planner_reviewer": { "model": "gpt-6.1-sol", "effort": "high" } }
}
```

The blocks merge shallowly like every key: a project's block replaces the global block wholesale, and a role it leaves out takes its default. A role may hold only `model` or only `effort`, the missing field taking its default. Both blocks are validated after the merge, like `runtime`: an unknown role name, a block that is not an object, a role whose value is not an object, a field inside a role other than `model` and `effort`, or a `model` or `effort` that is not a non-empty string, is a configuration error naming the file that supplies it.

Claude defaults:

| Role | Key | Model | Effort |
|---|---|---|---|
| PlannerReviewer | `planner_reviewer` | `opus` | `high` |
| PlanReviewer | `plan_reviewer` | `opus` | `high` |
| Implementer | `implementer` | `sonnet` | `high` |

The Claude native allowed-tool lists are fixed adapter values, not part of the setting:

| Role | Native allowed-tool list |
|---|---|
| PlannerReviewer | `Read`, `Write`, `Glob`, `Grep`, `Bash` |
| PlanReviewer | `Read`, `Write`, `Glob`, `Grep`, `Bash` |
| Implementer | `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep` |

The Claude invocation passes `--dangerously-skip-permissions`, `--allowedTools` with the role's list, and `--disallowedTools Monitor,ScheduleWakeup,CronCreate`; the allowed list is not a restrictive whitelist under permission bypass. `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` accompanies the call. The native instruction and continuation options are `--system-prompt` and `--resume`; their delivery behavior belongs to [Agent runtime — Native execution](../features/agent-runtime.md#native-execution).

Codex defaults; native command and diagnostic values live in [Codex runtime](codex-runtime.md):

| Role | Key | Model | Effort |
|---|---|---|---|
| PlannerReviewer | `planner_reviewer` | `gpt-6.1-sol` | `high` |
| PlanReviewer | `plan_reviewer` | `gpt-6.1-sol` | `high` |
| Implementer | `implementer` | `gpt-6.1-sol` | `high` |

## Per-project overlay

The global `orchestrator.json` sets the settings for the whole fleet — they apply to every target project alike. A project overrides the settings it needs with its own `<project>/.ai-factory/orchestrator.json` file, layered **on top of** the global base.

Priority, lowest to highest:

1. `OrchestratorConfig`'s own defaults (for optional keys).
2. The global `orchestrator.json` (or the path from `ORCHESTRATOR_CONFIG`) — the base.
3. `<project>/.ai-factory/orchestrator.json` — the project overlay.

The overlay is **partial**: a project names only the keys it wants to change — it does not need to repeat the whole set. The base's four mandatory keys (`max_iterations`, `usage_threshold_5h`, `usage_threshold_weekly`, `enable_phase_sessions`) are **not** mandatory in the overlay — the base has already supplied and validated them. Keys absent from the overlay are inherited from the base; keys present in it win.

Example — a project needs 5 iterations instead of the global 3:

```json
{ "max_iterations": 5 }
```

Everything else comes from the global config.

An invalid-setting diagnostic names the configuration file that supplies the effective value after the merge.

Merge rules:

- The merge is **shallow**: `telegram_alerts` in the overlay **replaces** the base's list wholesale, it does not union with it.
- The overlay's `roadmap_path` goes through the same guard (a relative path, no `..` segment) and the same three-state resolver as the global one — see [named-roadmaps.md](../features/named-roadmaps.md).
- Broken overlay JSON is a startup error naming the overlay file's path.
- **The absence of an overlay file is byte-stable**: a project without one behaves exactly as before, to the byte.

The orchestrator's overlay lives in its own `orchestrator.json` inside the project's `.ai-factory/`.

## Telegram notifications

Optional fields for sending Telegram notifications when the orchestrator halts, a task completes, or the whole run finishes:

```json
{
  "telegram_bot_token": "1234567890:AAF...",
  "telegram_chat_id": "-1001234567890",
  "telegram_alerts": ["task-fail", "stop", "task", "done"]
}
```

`telegram_bot_token` and `telegram_chat_id` are the bot's credentials. If either is empty or absent, notifications are not sent (a silent no-op, not an error).

`telegram_alerts` is the list of event types to notify on. What each colour says lives in [fault-handling.md](../concepts/fault-handling.md)'s own table:

| Type | Colour |
|---|---|
| `task-fail` | 🔴 |
| `stop` | 🟡 |
| `escalation` | 🔵 |
| `blocker` | 🔵 |
| `task` | 🟢 |
| `done` | 🟢 |

The `stop` token denotes an operational halt, so an alert list carried over from an older configuration is worth re-checking against the current table.

An empty list, `[]`, disables all notifications even with credentials filled in. A network error on send does not stop the run — see [fault-handling.md](../concepts/fault-handling.md) for what happens to the alert itself.
