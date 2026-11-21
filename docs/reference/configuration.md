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
| `max_iterations` | `3` | The iteration limit for code review and the test run, and for plan review unless `plan_review_iterations` is set — an integer of at least 1, checked at startup with a diagnostic that names the file that supplies it. See [pipeline.md](../pipeline.md). |
| `plan_review_iterations` | absent | The number of plan reviews a plan gets, a non-negative integer. Absent, plan review takes `max_iterations`; `0` turns plan review off. Not read when `planning` is `false`. See [The plan-review budget](../pipeline.md#the-plan-review-budget). |
| `planning` | `true` | Whether the run plans, a JSON boolean. `false` lays the task's spec at the plan's path in place of a plan and turns plan review off with it. See [Planning is a stage a project can switch off](../pipeline.md#planning-is-a-stage-a-project-can-switch-off). |
| `usage_threshold_5h` | `90` | Threshold, a finite number, for a reported five-hour window of each local runtime a role takes. See [usage-limits.md](../features/usage-limits.md). |
| `usage_threshold_weekly` | `95` | Threshold, a finite number, for a reported weekly window of each local runtime a role takes. See [usage-limits.md](../features/usage-limits.md). |
| `enable_phase_sessions` | `false` | Whether the planner's session carries over between tasks within a phase, a JSON boolean. See [phase-sessions.md](../features/phase-sessions.md). |
| `roadmap_path` | absent | Which roadmap the orchestrator reads. See [named-roadmaps.md](../features/named-roadmaps.md). |
| `runtime` | `claude` | Optional runtime selector: exactly `claude` or `codex`. It is the default for every role of the run. See [Runtime selection](../features/agent-runtime.md#runtime-selection). |
| `roles` | absent | The runtime of each role of the run that does not take the default: an object keyed by role. See [A role's runtime](#a-roles-runtime). |
| `hermes_instances` | absent | The map of Hermes instances a role may name, in the base file only. See [Hermes instances](#hermes-instances). |
| `claude_agents` | per role, below | Model and effort of each role when the runtime is `claude`: an object keyed by role. See [Agent defaults](#agent-defaults). |
| `codex_agents` | per role, below | Model and effort of each role when the runtime is `codex`: an object keyed by role. See [Agent defaults](#agent-defaults). |

The required keys of the base are validated after the merge and never converted: `max_iterations` is an integer, not a boolean, of at least 1; `usage_threshold_5h` and `usage_threshold_weekly` are finite numbers, integer or fractional, not booleans, `null` or strings, so `NaN` and `Infinity` are errors, and no range is required of them; `enable_phase_sessions` is a JSON boolean and nothing else, so `"false"`, `0` and `1` are errors. A quoted number is a string and is rejected. A violation exits naming the key, the value, the requirement and the file that supplies it, the base or the overlay.

`plan_review_iterations` and `planning` are validated after the merge too. Only an absent key takes its default; for `plan_review_iterations` a negative number, a boolean, a non-integer, a string or `null` is a configuration error, and for `planning` anything but a JSON boolean is, each naming the file that supplies the value.

`task_reviewer` is not a setting: who reviews a task is the runtime of the `code_reviewer` role. A merged configuration that carries the key is a configuration error naming the file that supplies it and pointing to `roles.code_reviewer`.

`runtime` is validated after the existing base/overlay merge. Only an absent key uses its default; an empty string, `null`, a non-string or any other value is a configuration error. The visible startup label is `Agent runtime:`. Under it the run prints one line per role of the run, in the order `planner`, `plan_reviewer`, `implementer`, `code_reviewer`, with two leading spaces: the role's runtime with the model and effort the run uses for a role on Claude or Codex, the instance for a role on Hermes and the address for a role on a head. Who reviews a task is the `code_reviewer` line. After the role lines the run prints `Planning: on|off`, and, when planning is on, `Plan review iterations: <the effective number>`, the setting when `plan_review_iterations` is set and `max_iterations` otherwise; a run on the defaults prints them too.

## Agent defaults

A role's model and effort are settings, one block per runtime: `claude_agents` and `codex_agents`, applying to the roles that run on Claude or Codex; a role on Hermes or on a head carries none. Each is an object keyed by the run's role, the same four as [`roles`](#a-roles-runtime) — `planner`, `plan_reviewer`, `implementer` and `code_reviewer` — whose value holds `model` and `effort`. A role reads its own entry in the block of its own runtime, the code reviewer under planning off included. The tables below are the defaults; a role left out of a block takes its default. The block of a runtime that no role takes stays in the file, unused. Their application belongs to [Agent runtime](../features/agent-runtime.md#runtime-selection); session policies belong to [Pipeline](../pipeline.md#agent-sessions).

```json
{
  "claude_agents": { "implementer": { "model": "sonnet", "effort": "high" } },
  "codex_agents": { "planner": { "model": "gpt-6.1-sol", "effort": "high" } }
}
```

The blocks merge shallowly like every key: a project's block replaces the global block wholesale, and a role it leaves out takes its default. A role may hold only `model` or only `effort`, the missing field taking its default. Both blocks are validated after the merge, like `runtime`: an unknown role name, a block that is not an object, a role whose value is not an object, a field inside a role other than `model` and `effort`, or a `model` or `effort` that is not a non-empty string, is a configuration error naming the file that supplies it.

Claude defaults:

| Role | Key | Model | Effort |
|---|---|---|---|
| Planner | `planner` | `opus` | `high` |
| PlanReviewer | `plan_reviewer` | `opus` | `high` |
| Implementer | `implementer` | `sonnet` | `high` |
| CodeReviewer | `code_reviewer` | `opus` | `high` |

The Claude native allowed-tool lists are fixed adapter values, not part of the setting:

| Role | Native allowed-tool list |
|---|---|
| Planner, CodeReviewer | `Read`, `Write`, `Glob`, `Grep`, `Bash` |
| PlanReviewer | `Read`, `Write`, `Glob`, `Grep`, `Bash` |
| Implementer | `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep` |

The Claude invocation passes `--dangerously-skip-permissions`, `--allowedTools` with the role's list, and `--disallowedTools Monitor,ScheduleWakeup,CronCreate`; the allowed list is not a restrictive whitelist under permission bypass. `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` accompanies the call. The native instruction and continuation options are `--system-prompt` and `--resume`; their delivery behavior belongs to [Agent runtime — Native execution](../features/agent-runtime.md#native-execution).

Codex defaults; native command and diagnostic values live in [Codex runtime](codex-runtime.md):

| Role | Key | Model | Effort |
|---|---|---|---|
| Planner | `planner` | `gpt-6.1-sol` | `high` |
| PlanReviewer | `plan_reviewer` | `gpt-6.1-sol` | `high` |
| Implementer | `implementer` | `gpt-6.1-sol` | `high` |
| CodeReviewer | `code_reviewer` | `gpt-6.1-sol` | `high` |

## A role's runtime

`roles` is an object keyed by the run's role — `planner`, `plan_reviewer`, `implementer` and `code_reviewer` — whose value names the role's runtime. A role the object leaves out takes `runtime`.

| Value | Meaning |
|---|---|
| `{"runtime": "claude"}` or `{"runtime": "codex"}` | The role runs on that local runtime. |
| `{"runtime": "hermes", "instance": <name>}` | The role is taken by the named Hermes instance, which takes every role, the implementer only where git tracks the project's `.ai-factory` (Hermes takes any role inside the run, the refusal). |
| `{"runtime": "head", "head": {"repository": <path>, "owner": <slug>, "folder": <NN>}}` | The role is taken by the head at that address, which takes every role but the implementer (A head takes a role of the run). |

`roles` is validated after the merge, like `runtime`: an unknown role name, a value that is not an object, a runtime other than the four, a Hermes role without an `instance` or one that `hermes_instances` does not hold, a head role without a complete address, a head whose `repository` the filesystem cannot name (a NUL character, or text that does not encode to the filesystem's encoding), a head named for the implementer and a Hermes implementer where git does not track the project's `.ai-factory` (the refusal) are each a configuration error naming the file that supplies the value. A role that names another runtime in the overlay replaces `roles` wholesale, as every key does.

A head's address lives where the role is named, in the project's overlay, `<project>/.ai-factory/orchestrator.json`. Whether the overlay is tracked is the project's own `.gitignore` choice, and the `repository` path is a path on this machine. When the run starts it reads the address of each head a role names, and one the locator cannot read halts the run before any task (A head takes a role of the run).

The pre-task usage check runs for each local runtime, Claude and Codex, that any role of the run takes; a Hermes instance and a head carry none ([Usage limits](../features/usage-limits.md#selected-pre-task-check)).

## Hermes instances

`hermes_instances` is an object in the machine's base `orchestrator.json`, and only there: a project's overlay that carries it is a configuration error. It maps an instance's name to `{"server", "port", "key_file"}`, the server to reach, its port and the path of the file holding the key that opens his API. The key stays in its file and is never printed or copied. The file holds one key: surrounding whitespace and a trailing line break are ignored, and a file with no key, with whitespace, a line break or another control character inside the key, or with a character outside latin-1 is a configuration fault that names the file and never the key, raised before any request. The values are the stand's and are written in no repository.

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
