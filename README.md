# Orchestrator

> Autonomous AI-agent orchestrator — it executes a project's finished roadmap, task by task.

The orchestrator is a command-line tool that reads the pending tasks of a target project's roadmap and runs each one through five stages — plan → plan review → implement → review → commit — on Claude or Codex. A planner writes the plan, a plan reviewer checks it until it passes, an implementer writes the code, a reviewer checks the result until it passes, and the orchestrator commits the work and marks the task done. It authors nothing on the roadmap: the roadmap arrives already decomposed, and the orchestrator only executes it. The stages, the files the agents exchange and the signals that end each stage are described in [Pipeline](docs/pipeline.md).

## What a target project provides

A target project needs an initialized git repository and a handful of files under `.ai-factory/`: a `ROADMAP.md` of tasks written as markdown checkboxes, and optionally `ARCHITECTURE.md` and `RULES.md` that the agents treat as requirements. The roadmap format, the task line, phases and the stop marker are in [Target project requirements](docs/reference/target-project.md).

## Install

The orchestrator needs Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

It also needs the command-line tool of the runtime it runs on, installed and authorized; the runtime is chosen in the configuration, see [Agent runtime — Runtime selection](docs/features/agent-runtime.md#runtime-selection).

## Configure

```bash
cp orchestrator.json.example orchestrator.json
```

`orchestrator.json` is the local configuration, one per developer, and it can hold Telegram credentials, so it stays out of version control. The `runtime` key selects Claude or Codex, `roadmap_path` selects which roadmap to run (empty means the default `ROADMAP.md`), and the thresholds bound the usage a run may spend. Any key can be overridden per project in `<project>/.ai-factory/orchestrator.json`. Every key and its default are in the [configuration reference](docs/reference/configuration.md).

## Modes

| Command | What it does |
|---------|-------------|
| `uv run orchestrator implement /path/to/project` | Plans and implements every pending task |
| `uv run orchestrator test /path/to/project` | Writes tests for the tasks in `ROADMAP_TESTS.md` |

Without a mode the orchestrator runs `implement` on the current directory. [Test mode](docs/features/test-mode.md) describes the second mode.

## Documentation

Start with [Workflow](docs/workflow.md), the working pattern from filling a roadmap to test coverage, and [Pipeline](docs/pipeline.md).

| Page | What it covers |
|------|----------------|
| [Agent runtime](docs/features/agent-runtime.md) | Role invocation, native execution policy and instruction delivery |
| [Named roadmaps](docs/features/named-roadmaps.md) | Per-developer roadmaps and where their artifacts go |
| [Test mode](docs/features/test-mode.md) | Writing tests through the orchestrator |
| [Phase sessions](docs/features/phase-sessions.md) | The token economics behind `enable_phase_sessions` |
| [Usage limits](docs/features/usage-limits.md) | Session and weekly usage thresholds |
| [Resume](docs/features/resume.md) | Continuing after an interruption |
| [Escalation](docs/features/escalation.md) | When an agent stops the run for a decision outside its authority |
| [Outcomes](docs/concepts/outcomes.md) | Success, failure, halt and escalation |
| [Fault handling](docs/concepts/fault-handling.md) | Telling a verdict from a fault, and the known faults |
| [Non-convergence](docs/concepts/non-convergence.md) | Reading a stalled review loop |
| [Context model](docs/concepts/context-model.md) | What an agent is handed literally and what it reaches by links |
| [Target project requirements](docs/reference/target-project.md) | What a target project must provide |
| [Configuration](docs/reference/configuration.md) | Settings, overlay precedence and agent models |
| [Codex runtime](docs/reference/codex-runtime.md) | Native invocation, faults and retry limits |

## License

MIT — see [LICENSE](LICENSE).
