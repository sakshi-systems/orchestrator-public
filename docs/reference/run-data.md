# Run data reference

[A run's data is reached through commands](../features/run-data.md) governs behaviour. This page holds its values: the commands' arguments, the JSON they print with its version, the positions, the dispositions and the refusals.

## The commands

The four commands are subcommands of `orchestrator`, which a head, a skill or a person runs in the project's directory, or with `--project-dir DIR` for another. Where `--roadmap VALUE` is absent, a command takes the roadmap a run of the project would take, and a given value means what it means for a run ([Named roadmaps — What it is](../features/named-roadmaps.md#what-it-is)).

```text
orchestrator show     [--project-dir DIR] [--roadmap VALUE] TASK
orchestrator show     [--project-dir DIR] [--roadmap VALUE] --phase N
orchestrator rollback [--project-dir DIR] [--roadmap VALUE] TASK POSITION
orchestrator sweep    [--project-dir DIR] [--roadmap VALUE] --phase N
orchestrator dispose  [--project-dir DIR]
```

`TASK` is the task's number as its contract line writes it, `N.M`, a split child's `N.M.k` or a fix line's `N.Ma`, or, for a flat task, its title exactly as its roadmap line gives it. `dispose` reads its deposit from standard input as UTF-8.

Each command exits with status `0` when it acts and prints one JSON object on standard output. It exits with status `1`, prints one line on standard error, prints nothing on standard output and changes nothing when it refuses. A refusal is any of these:

- the roadmap does not exist, or the phase or the task is not in it;
- a flat task's title stands on more than one line of the roadmap, the line numbers being named;
- a selector is missing or doubled;
- `rollback` is given a position that `show` did not list for the task, or a task that is done or has no attempt;
- `sweep` is given a phase that holds a task whose roadmap line is not done, the tasks being named;
- `dispose` is given input that is not valid UTF-8 or JSON, a `version` it does not support, an entry that no task holds or that the deposit names twice, a disposition that is not an object of `kind` and, for `routed`, `target`, a `kind` outside the three, a `routed` with no `target` or another `kind` with one, a target that is no open task's spec and no phase of the roadmap, for a disposition not already recorded for the entry, or a disposition for an entry that already holds a different recorded one, the refusal naming the one it holds. An entry whose recorded disposition equals the deposit's is accepted whatever its target's state.

## The JSON

Every object holds `version`, an integer, `1` at present. The interface changes by version: a field added keeps it, and any change to or removal of a command, a field, a token or a refusal takes the next.

`show` holds `scope`, `task` or `phase`, and then:

| Scope | Further fields |
|---|---|
| `task` | `roadmap`, `task`, `steps`, `escalation`, `observations`, `positions` |
| `phase` | `roadmap`, `phase`, `tasks` |

`task`, in a task's object, holds `address`, `title` and `state`, which is one of `unstarted`, `underway`, `escalated` and `done`. `steps` is a list in run order of `step`, the step's name, and `rounds`, a list of `round`, an integer, and `text`, the round's content in full. Step names are the scenario's and are reported as given: a client reads them and never branches on them, and the tokens it may branch on are the positions, the dispositions, `state` and the tokens an agent writes. The run's steps today, `plan`, `plan-review`, `implement` and `review`, and in test mode `test-run`, are an example. `escalation` is `null`, or holds `role`, `step`, `round` and `text`. `positions` is the list of position tokens that `rollback` takes, empty when it takes none.

`tasks`, in a phase's object, is a list of each task's `address`, `title`, `state`, `escalation` and `observations`.

`observations` is a list of entries, each of `entry`, a string the orchestrator gives and `dispose` takes back, `affects`, `text`, `sources`, a list of `step` and `round` for each review that repeats it, and `disposition`, `null` or an object of `kind` and, for `routed`, `target`. `target` is an object of `spec`, a repository-root-relative path, or of `roadmap` and `phase`.

`rollback` prints `version` and `position`; `sweep` prints `version` and `phase`; `dispose` prints `version` and `disposed`, the list of entries the deposit named, those already disposed the same way included.

The deposit `dispose` reads is an object of `version` and `dispositions`, a list of `entry` and `disposition`, the disposition being the object `show` reports: `kind` and, for `routed`, `target`.

## Positions

A position is a token of the interface's version. `show` lists the ones a task can take, and `rollback` accepts only those. The escalation is cleared under every one, and each row says the rest.

| Token | Listed when | What stays | What goes | The next run |
|---|---|---|---|---|
| `start` | the task has an attempt and is not done | nothing of the attempt | the plan, every plan review, every review and test run, the implementer's answers and the task's record; the working tree outside `.ai-factory` is restored to the attempt's base, an ignored path never touched | plans, or lays the spec, afresh |
| `plan-review` | planning is on and the plan exists | the plan and the working tree | every plan review, review and test run and the implementer's answers; the implementer's session and the code reviewer's session | reviews the plan from its first attempt |
| `verify` | the plan exists and the working tree holds a change outside `.ai-factory` | the plan, the plan reviews that passed, the working tree and the implementer's session | every review and test run, the implementer's answers and the code reviewer's session | verifies the change from its first iteration |
| `implement` | planning is off | the working tree | every review and test run, the implementer's answers, the implementer's session and the code reviewer's session | implements again from the spec |

## Dispositions

A disposition's `kind` is one of three literals, `fixed`, `routed` and `dismissed`. A marker an older review carries reads as follows, and the markers' grammar is no part of the interface.

| Marker on an entry line | Reads as |
|---|---|
| `[fixed]` | `fixed` |
| `[routed → <spec path>]` | `routed`, the target's `spec` the path |
| `[routed → <roadmap path> § Phase N]` | `routed`, the target's `roadmap` the path and `phase` `N` |
| `[dismissed]` | `dismissed` |
| `[promoted → …]`, `[audit-corroborated]`, `[audit-dismissed]`, `[unrouted-reported]` | `legacy`, disposed |

## What an agent writes

What an agent writes in its answer is a token a program scans, and it stays as it is: the PASS signals and `ESCALATION` ([Completion signals](../pipeline.md#completion-signals)), and the deferred observations section a review may carry, whose entries are lines of this form, the reviewer writing nothing after the text:

```text
## Deferred observations
- Affects: <phase / task-spec path / "unknown"> — <observation>
```
