# Scenario engine reference

[The scenario engine](../features/scenario-engine.md) governs behaviour. This page holds its values: the command, the resolution order, the file's keys, the step kinds, the outcomes, the templates and the refusals.

## The command

```text
orchestrator run SCENARIO [project_dir]
```

| Argument | Meaning |
|---|---|
| `SCENARIO` | The scenario's name, a single path component without its `.yaml` suffix |
| `project_dir` | The project the scenario runs in; the current directory when omitted, as for `orchestrator implement` |

The run reads the person's [run settings](configuration.md#a-persons-run-settings) as the script commands do, and takes the optional `--completion-file` and `--invocation-id` pair, supplied together, with the meaning the script commands give it (Ordinary CLI proof options).

## Where a name resolves

A name resolves to the first of these files that exists:

1. `<project>/.ai-factory/scenarios/<name>.yaml`
2. the scenario of that name that the orchestrator ships

When both exist the project's file runs, and the run prints a line naming the file at its start. The shipped scenarios are `implement` and `test`.

The closure's template, `closure.md`, resolves the same way, to the first of `<project>/.ai-factory/scenarios/templates/closure.md` and the template of that name that the orchestrator ships.

## The file

The file is YAML 1.2, one scenario to a file, named by its stem. Its top level is a mapping with one key:

| Key | Value |
|---|---|
| `steps` | A non-empty list of steps, run in order |

A step is a mapping that holds one kind key, `walk`, `close` or `cycle`, and the other keys that kind reads. The kind keys and their keys:

| Kind key | Value | Other keys |
|---|---|---|
| `walk` | `implement` or `test` | `stop`, optional: the one value `closure-owed` |
| `close` | The word of the responsible head, a non-empty string without whitespace | `peer`, required: the word of the helper head, a non-empty string without whitespace |
| `cycle` | A mapping of `until`, an outcome name, and `steps`, a non-empty list of steps of the kinds `walk` and `close` | none |

The shipped `test` scenario:

```yaml
# The test flow over the project's test roadmap
steps:
  - walk: test
```

The shipped `implement` scenario:

```yaml
# The implement flow, and the closure of each phase that owes one
steps:
  - cycle:
      until: no-task
      steps:
        - walk: implement
          stop: closure-owed
        - close: builder
          peer: razor
```

An example of a project's own scenario, a single walk, with the reason of its step in a comment:

```yaml
# The implement flow over the project's roadmap
steps:
  - walk: implement
```

## Step kinds

| Kind | What it does |
|---|---|
| `walk` | Runs the open tasks through today's flow with the person's run settings, as the script command of the same name does, until no task is open or the `---STOP---` marker; with `stop: closure-owed` it also stops before the first task that follows a phase that owes a closure |
| `close` | Closes the first phase that owes a closure: sends the responsible head one message, the closure's template with its placeholders filled in and the `## How to answer` section, waits with no bound for the responsible head's deposit, and reads an `ESCALATION` among its last lines as a role's answer is read; where no phase owes a closure it returns at once and calls no head ([The closure of a phase](../features/phase-closure.md)) |
| `cycle` | Runs its steps in order and again from the first, and ends the moment a step returns the outcome `until` names, before the later steps of that pass |

## Outcomes

A step returns its outcome as a value to the scenario engine:

| Step | Outcomes |
|---|---|
| `walk: implement` | `no-task`: no task is open or the marker is reached; with `stop: closure-owed` the run's done notification is sent also where no task is open at the walk's start ([The scenario engine](../features/scenario-engine.md#how-a-scenario-runs)). `closure-owed`, only with `stop: closure-owed`: a phase owes a closure, found before every task and before each empty-queue test; the walk stops and sends no notification |
| `walk: test` | `no-task` |
| `close` | `done`: no phase owes a closure and no head is called, or the responsible head's deposit is read, holds no escalation, and every entry of the phase is disposed |

`until` names an outcome that a step of its cycle can return.

## Templates and placeholders

The template is the file `closure.md` in a `templates/` folder beside the scenarios. It is the message a close step sends, the run adding to it the `## How to answer` section (Head exchange reference — The message). The shipped template is a trigger: it names the occasion, where the phase's deferred entries are read, the helper's address and the answer awaited, and never how a head reads, disposes or reconciles, which are the heads' skills ([The closure of a phase — The template](../features/phase-closure.md#the-template)).

A placeholder is a lowercase word in braces. The closed set:

| Placeholder | Replaced by |
|---|---|
| `{phase}` | The number of the first phase that owes a closure, resolved when the step is called |
| `{roadmap}` | The roadmap's path in the form `orchestrator show --roadmap` takes, relative to `.ai-factory`, such as `roadmaps/alice.md` |
| `{project}` | The project's absolute path |
| `{peer}` | The address of the close step's helper head, its `peer`, as `repository: <path>, owner: <owner>, folder: <folder>` |

## Refusals

A file is refused at load, naming the file and the key, for:

- a file that is not one YAML document: a syntax error, a second document or an empty file;
- a top level that is not a mapping, a key other than `steps`, or a missing `steps`;
- an empty `steps`;
- a step that is not a mapping, a step with no kind key or with two, or a key that the step's kind does not read;
- a `close` without `peer`;
- a duplicate key anywhere in the file;
- a `walk` other than `implement` and `test`;
- any `stop` value other than `closure-owed`;
- a `cycle` without `until`, with an empty `steps`, holding a cycle, or whose `until` names an outcome none of its steps returns;
- a closure template that no place holds, naming both places it looked in, and a placeholder outside the closed set;
- a value of the wrong type: nothing is converted, so a number or a boolean where a string is required is refused.

A run is refused at its start, before any step, for a head word, a `close`'s value or its `peer`, that the run settings' `heads` do not map ([A scenario's heads](configuration.md#a-scenarios-heads)), naming the word and the step, and for a walk with `stop: closure-owed` that has no `close` step after it in the same `cycle`, or stands in no cycle, since it would return `closure-owed` on every pass with no step to close the phase. The check asks only that a `close` step follows the walk in its cycle: a step between them that returns the outcome the cycle's `until` names ends the cycle before the `close` is reached, so the closure belongs directly after the walk that stops for it, as the shipped `implement` has it. A head whose address cannot be read, or an answer command that is missing, halts the run before any step, as for a head that takes a role.

`orchestrator run` is also refused for a `SCENARIO` that is not a single path component, and for a name that resolves to no file, which names both places it looked in.

A refusal prints its first line on standard error and ends the command with exit status 1, before any step starts.

## How a run ends

The outcomes, notifications and exit statuses are those of the script commands ([Outcomes](../concepts/outcomes.md)). The person's stop ends the scenario with the one notification of a stop. A `close` whose responsible head's deposit ends in `ESCALATION` ends the scenario as an escalation, naming the closure, and a `close` whose deposit leaves an entry of its phase undisposed halts the run, naming each such entry.
