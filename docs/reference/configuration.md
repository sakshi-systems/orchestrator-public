# Configuration

## Configuration files

Two files configure the orchestrator, and a key neither sets takes its default ([Settings keys](#settings-keys)). The machine's base holds what the machine has, the Hermes instances and the notification credentials, and a person's run settings hold every value that shapes a run. The layers, lowest to highest, are the defaults, the base and the person's file, and [A person's run settings](#a-persons-run-settings) states how they merge.

The orchestrator runs from a copy installed as a tool from a release tag, in an environment of its own outside every repository. The base lies outside that copy, so installing the next release leaves it as it is.

The machine's base is `~/.local/state/orchestrator/orchestrator.json`, in the orchestrator's state root beside its other machine state, in a directory only its owner reads. It is optional: a machine that reaches no Hermes instance and sends no notification has none. It holds `hermes_instances`, `telegram_bot_token` and `telegram_chat_id` and nothing else:

```json
{
  "hermes_instances": {
    "hermes4": { "server": "127.0.0.1", "port": 8642, "key_file": "/path/to/hermes4.key" }
  },
  "telegram_bot_token": "1234567890:AAF...",
  "telegram_chat_id": "-1001234567890"
}
```

A key that shapes a run, found in the base, is a startup error naming the key, the base file and the person's file the key belongs in. A base that is not valid JSON, or not an object, is a startup error naming its path.

The base's path can be overridden through an environment variable, which then names the base. A path that names no file halts the run at its start, naming the path, since the operator asked for that file; only the default path's absence means no base:

```bash
ORCHESTRATOR_CONFIG=/path/to/my-base.json orchestrator implement /path/to/project
```

## Settings keys

| Key | Default | Lives in | Meaning |
|---|---|---|---|
| `max_iterations` | `3` | the person's file | The iteration limit for code review and the test run, and for plan review unless `plan_review_iterations` is set — an integer of at least 1, checked at startup with a diagnostic that names the file that supplies it. See [pipeline.md](../pipeline.md). |
| `plan_review_iterations` | absent | the person's file | The number of plan reviews a plan gets, a non-negative integer. Absent, plan review takes `max_iterations`; `0` turns plan review off. Has no effect when `planning` is `false`. See [The plan-review budget](../pipeline.md#the-plan-review-budget). |
| `planning` | `true` | the person's file | Whether an implement run plans, a JSON boolean. `false` lays the task's spec at the plan's path in place of a plan and turns plan review off with it; test mode plans whatever it holds. See [Planning is a stage a project can switch off](../pipeline.md#planning-is-a-stage-a-project-can-switch-off). |
| `usage_threshold_5h` | `90` | the person's file | Threshold, a finite number, for a reported five-hour window of each local runtime a role takes. See [usage-limits.md](../features/usage-limits.md). |
| `usage_threshold_weekly` | `95` | the person's file | Threshold, a finite number, for a reported weekly window of each local runtime a role takes. See [usage-limits.md](../features/usage-limits.md). |
| `enable_phase_sessions` | `false` | the person's file | Whether the planner's session carries over between tasks within a phase, a JSON boolean. See [phase-sessions.md](../features/phase-sessions.md). |
| `roadmap_path` | absent | the person's file | Which roadmap the orchestrator reads. See [named-roadmaps.md](../features/named-roadmaps.md). |
| `runtime` | `claude` | the person's file | Optional runtime selector: exactly `claude` or `codex`. It is the default for every role of the run. See [Runtime selection](../features/agent-runtime.md#runtime-selection). |
| `roles` | absent | the person's file | The runtime of each role of the run that does not take the default: an object keyed by role. See [A role's runtime](#a-roles-runtime). |
| `hermes_instances` | absent | the base | The map of Hermes instances a role may name, in the base only. See [Hermes instances](#hermes-instances). |
| `claude_agents` | per role, below | the person's file | Model and effort of each role when the runtime is `claude`: an object keyed by role. See [Agent defaults](#agent-defaults). |
| `codex_agents` | per role, below | the person's file | Model and effort of each role when the runtime is `codex`: an object keyed by role. See [Agent defaults](#agent-defaults). |
| `telegram_bot_token` | absent | the base | The bot's credential for notifications. See [Telegram notifications](#telegram-notifications). |
| `telegram_chat_id` | absent | the base | The chat the notifications go to. See [Telegram notifications](#telegram-notifications). |
| `telegram_alerts` | absent, no events | the person's file | The event types the person is told of. See [Telegram notifications](#telegram-notifications). |

`max_iterations`, `usage_threshold_5h`, `usage_threshold_weekly` and `enable_phase_sessions` take their defaults when no file sets them, and are validated after the merge and never converted: `max_iterations` is an integer, not a boolean, of at least 1; `usage_threshold_5h` and `usage_threshold_weekly` are finite numbers, integer or fractional, not booleans, `null` or strings, so `NaN` and `Infinity` are errors, and no range is required of them; `enable_phase_sessions` is a JSON boolean and nothing else, so `"false"`, `0` and `1` are errors. A quoted number is a string and is rejected. A violation exits naming the key, the value, the requirement and the file that supplies it, the base or the person's file. No key is required of either file: where neither supplies a key, its default stands.

`plan_review_iterations` and `planning` are validated after the merge too. Only an absent key takes its default; for `plan_review_iterations` a negative number, a boolean, a non-integer, a string or `null` is a configuration error, and for `planning` anything but a JSON boolean is, each naming the file that supplies the value.

`task_reviewer` is not a setting: who reviews a task is the runtime of the `code_reviewer` role. A merged configuration that carries the key is a configuration error naming the file that supplies it and pointing to `roles.code_reviewer`.

`runtime` is validated after the merge. Only an absent key uses its default; an empty string, `null`, a non-string or any other value is a configuration error. The visible startup label is `Agent runtime:`. Under it the run prints one line per role of the run, in the order `planner`, `plan_reviewer`, `implementer`, `code_reviewer`, and in an implement run under `planning` `false` only `implementer` and `code_reviewer`, with two leading spaces: the role's runtime with the model and effort the run uses for a role on Claude or Codex, the instance for a role on Hermes and the address for a role on a head. Who reviews a task is the `code_reviewer` line. After the role lines the run prints `Planning: on|off`, the stage the run runs, and, when planning is on, `Plan review iterations: <the effective number>`, the setting when `plan_review_iterations` is set and `max_iterations` otherwise; a run on the defaults prints them too. Test mode plans whatever `planning` holds, so it prints all four role lines, `Planning: on` and its budget.

## Agent defaults

A role's model and effort are settings, one block per runtime: `claude_agents` and `codex_agents`, applying to the roles that run on Claude or Codex; a role on Hermes or on a head carries none. Each is an object keyed by the run's role, the same four as [`roles`](#a-roles-runtime) — `planner`, `plan_reviewer`, `implementer` and `code_reviewer` — whose value holds `model` and `effort`. A role reads its own entry in the block of its own runtime, the code reviewer under planning off included. The tables below are the defaults; a role left out of a block takes its default. The block of a runtime that no role takes stays in the file, unused. Their application belongs to [Agent runtime](../features/agent-runtime.md#runtime-selection); session policies belong to [Pipeline](../pipeline.md#agent-sessions).

```json
{
  "claude_agents": { "implementer": { "model": "sonnet", "effort": "high" } },
  "codex_agents": { "planner": { "model": "gpt-6.1-sol", "effort": "high" } }
}
```

The blocks merge shallowly like every key: a block in the person's file replaces the whole default block, and a role it leaves out takes its default. A role may hold only `model` or only `effort`, the missing field taking its default. Both blocks are validated after the merge, like `runtime`: an unknown role name, a block that is not an object, a role whose value is not an object, a field inside a role other than `model` and `effort`, or a `model` or `effort` that is not a non-empty string, is a configuration error naming the file that supplies it.

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
| `{"runtime": "head", "head": {"repository": <path>, "owner": <slug>, "folder": <NN>}}` | The role is taken by the head at that address, which takes every role (A head takes a role of the run). |

`roles` is validated after the merge, like `runtime`: an unknown role name, a value that is not an object, a runtime other than the four, a Hermes role without an `instance` or one that `hermes_instances` does not hold, a head role without a complete address, a head whose `repository` the filesystem cannot name (a NUL character, or text that does not encode to the filesystem's encoding) and a Hermes implementer where git does not track the project's `.ai-factory` (the refusal) are each a configuration error naming the file that supplies the value. A role that names another runtime in the person's file replaces `roles` wholesale, as every key does.

A head's address lives where the role is named, in the person's run settings. Its `repository` is an absolute path on this machine, and the person's file is committed with the project, so the committed file carries that path: it names the person's directories, it is public wherever the project's repository is public, and it holds only on the machine the path belongs to. That is a pitfall of naming a head in a committed file, and nothing refuses it. When the run starts it reads the address of each head a role names, and one the locator cannot read halts the run before any task (A head takes a role of the run).

The pre-task usage check runs for each local runtime, Claude and Codex, that any role of the run takes, a head counting as the runtime its address names; a Hermes instance adds none ([Usage limits](../features/usage-limits.md#selected-pre-task-check)).

## Hermes instances

`hermes_instances` is an object in the machine's base, `~/.local/state/orchestrator/orchestrator.json`, and only there: a person's run settings that carry it are a configuration error naming the file and the key. It maps an instance's name to `{"server", "port", "key_file"}`, the server to reach, its port and the path of the file holding the key that opens his API. The key stays in its file and is never printed or copied. The file holds one key: surrounding whitespace and a trailing line break are ignored, and a file with no key, with whitespace, a line break or another control character inside the key, or with a character outside latin-1 is a configuration fault that names the file and never the key, raised before any request. The values are the stand's and are written in no repository.

## A person's run settings

A person's run settings are the one home of every value that shapes a run: `max_iterations`, `plan_review_iterations`, `planning`, `usage_threshold_5h`, `usage_threshold_weekly`, `enable_phase_sessions`, `roadmap_path`, `runtime`, `roles`, `claude_agents`, `codex_agents` and `telegram_alerts`. They are one JSON file for each person who runs the project, `<project>/.ai-factory/run-settings/<slug>.json`, committed with the project, so that the project's history shows which executor, reviewer and limits each phase ran under and two executors can be compared on the same phases. The slug is the one `roadmap_path: "my"` derives from the git identity ([named-roadmaps.md](../features/named-roadmaps.md#what-it-is)).

The run looks for the person's file under the slug derived as [named-roadmaps.md](../features/named-roadmaps.md#what-it-is) states, and a missing file runs on the base and the defaults. Only where `.ai-factory/run-settings/` exists and no slug can be derived does the run halt, and no other file stands in for the person's. No layer shared by the project's people stands beside the file: it is the one home of the values, and a second file would be a second home.

Priority, lowest to highest:

1. The defaults of [Settings keys](#settings-keys).
2. The machine's base, which supplies the machine's facts alone.
3. The person's file.

Example — a person who runs their own roadmap, with the implementer on a Hermes instance, a Claude code reviewer, planning off, and a notification on a failure, a stop and the end of the run:

```json
{
  "roadmap_path": "my",
  "planning": false,
  "roles": { "implementer": { "runtime": "hermes", "instance": "hermes4" } },
  "claude_agents": { "code_reviewer": { "model": "opus", "effort": "high" } },
  "telegram_alerts": ["task-fail", "stop", "done"]
}
```

Everything else takes its default, and `hermes4` is an instance the base's map holds.

The file refuses, each naming the file and the key: a key the configuration does not know; `hermes_instances`, which the base alone holds ([Hermes instances](#hermes-instances)); and `telegram_bot_token` and `telegram_chat_id`, the machine's credentials, which never enter a committed file. A project's `.ai-factory/orchestrator.json` is a startup error naming it and the person's file its values move to; the run never reads it.

An invalid-setting diagnostic names the configuration file that supplies the effective value after the merge.

Merge rules:

- The merge is **shallow**: a key in the person's file replaces the value below it wholesale, a list or an object included, and does not union with it.
- The `roadmap_path` of the person's file goes through the guard (a relative path, no `..` segment) and the three-state resolver — see [named-roadmaps.md](../features/named-roadmaps.md).
- A person's file that is not valid JSON, or not an object, is a startup error naming its path.

## Telegram notifications

Optional keys for sending Telegram notifications when the orchestrator halts, a task completes, or the whole run finishes. The credentials are the machine's, in the base; the event types are the person's, in the run settings:

```json
{
  "telegram_bot_token": "1234567890:AAF...",
  "telegram_chat_id": "-1001234567890"
}
```

```json
{
  "telegram_alerts": ["task-fail", "stop", "task", "done"]
}
```

`telegram_bot_token` and `telegram_chat_id` are the bot's credentials. If either is empty or absent, notifications are not sent (a silent no-op, not an error). A person's file that carries either is a configuration error.

`telegram_alerts` is the list of event types to notify on. Absent, it is empty and nothing is sent, so a machine with credentials in its base and a person's file without the key sends no notification. What each colour says lives in [fault-handling.md](../concepts/fault-handling.md)'s own table:

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
