# The scenario engine

The scenario engine runs a declared file of ordered steps with `orchestrator run`, beside the script commands `orchestrator implement` and `orchestrator test`, which stay as they are. The file's keys, the step kinds and the refusals are in the [scenario engine reference](../reference/scenario-engine.md).

## What a scenario is

A scenario is one file of ordered steps, named by its stem. A step is one of three kinds:

- **A walk** runs the project's open tasks through the [agent flow](../pipeline.md#the-agent-flow), as `orchestrator implement` or `orchestrator test` runs them. It may declare that it stops where a phase owes a closure.
- **A close** closes a phase that owes it: it calls its responsible head, named by a word that the person's run settings map to an address ([A scenario's heads](../reference/configuration.md#a-scenarios-heads)), and names a helper head for it to work with ([The closure of a phase](phase-closure.md)). The call is outside the four roles of the run.
- **A cycle** repeats its steps until one of them returns the outcome the cycle names.

The format keeps comments, the place for a step's reason.

## Where a scenario is found

`orchestrator run` takes a scenario by name. The scenario engine looks for the name in the project's `.ai-factory/scenarios/` first and then among the scenarios the orchestrator ships, so a new scenario is a file in the project and never a change of the orchestrator. A project file that shares a name with a shipped scenario is the one that runs, and the run says so when it starts. The closure's message template is found the same way, in `templates/` beside the scenarios.

Two scenarios ship. `test` is one walk step and runs the tasks as `orchestrator test` does. `implement` is a cycle of a walk that declares its stop and a close: it runs the tasks as `orchestrator implement` does, and closes each phase that owes it ([The closure of a phase](phase-closure.md)).

## How a scenario runs

Steps run in order, and the next step starts only when the one before it has ended. A step runs once, except in a cycle, which runs its steps again from the first until it ends. The scenario engine keeps no state of its own: a scenario that is started again runs from its first step. A walk step goes on where the roadmap and each task's record leave the project, as [Resume](resume.md) states for a script command, so a started-again scenario repeats no task that is done.

A walk step runs the open tasks one after another with the person's [run settings](../reference/configuration.md#a-persons-run-settings), exactly as the script command of its mode does: the same roadmap, the same stages, the same roles and runtimes. It runs until no task is open or until the [`---STOP---` marker](../reference/target-project.md#breakpoint) of the roadmap, and its end is the end of the step.

The marker ends the walk of its own roadmap and stays in the file: a later walk of the same roadmap ends at once, and a `test` walk reads the marker of its own roadmap, the test roadmap, and never the main roadmap's.

A step ends in an outcome that it returns as a value, never as a line of text. A walk returns `no-task` when it ends for want of a task or at the marker. A walk that declares `stop: closure-owed` also stops before a task where a phase owes a closure, and returns `closure-owed`; a walk without the declaration never stops at a phase and runs through one that owes a closure, as the script command does. A walk that declares the stop also ends for want of a task at its start as it ends in its loop, with the run's done notification, so a scenario whose last act is a closure sends that notification once; a walk that stops for a closure sends none, and the script commands declare no stop and keep their behaviour. A close step returns `done` when its responsible head's answer is read, holds no escalation and every entry of the phase is disposed, and returns `done` at once, calling no head, where no phase owes a closure. A cycle ends the moment a step returns the outcome the cycle's `until` names, before the later steps of that pass, and the scenario goes on to the step after the cycle, or ends.

## When a scenario is refused

A scenario file the scenario engine refuses, a name it cannot resolve, a head word that the run settings do not map and a walk that stops for a closure with no close step after it in its cycle end the command at once: the refusal's first line goes to standard error and the exit status is 1. No step starts, and no run begins. The [reference](../reference/scenario-engine.md#refusals) lists what is refused.

## Beside the script commands

The scenario engine does not replace the script commands, and it neither extends nor reduces them. `orchestrator implement` and `orchestrator test` keep their behaviour and their settings, and `orchestrator run test` runs the same flow as `orchestrator test`. `orchestrator run implement` runs the flow of `orchestrator implement` and adds the closure of each phase that owes it, which the script command never does. The agent that starts a run picks the script mode or the scenario mode. The walk is the one flow both modes share, so what a task does does not depend on the mode.

## How a scenario ends

A scenario ends as the script commands end, with the same outcomes, notifications and exit statuses ([Outcomes](../concepts/outcomes.md)):

- The last step ends, or a cycle ends and no step follows it: the scenario is done.
- A task of a walk step stops, halts or escalates: the scenario ends as the script command would end.
- A close step ends on an escalation in its responsible head's answer, which ends the scenario as an escalation of a role ends a run ([Escalation](escalation.md)), or leaves an entry of the owed phase undisposed, which halts the run.
- The person stops the run: the scenario ends there, with the one notification of a stop, and no later step runs.
