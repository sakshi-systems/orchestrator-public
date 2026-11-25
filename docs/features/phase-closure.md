# The closure of a phase

Under `orchestrator run implement`, a phase whose tasks are all done and whose deferred observations are not all disposed is closed before the run goes on. The closure is one step of the scenario, `close` ([The scenario engine](scenario-engine.md)): it calls a head and waits for its answer. What the heads do at a closure is theirs, in their own skills, and what is asked of them is the shipped template, data ([The template](#the-template)). `orchestrator implement` and `orchestrator run test` have no closure: whoever holds the project's work disposes of the observations by hand.

## When a phase owes a closure

A phase owes a closure when every one of its tasks is done and it holds a deferred observation that has no disposition ([Disposing of deferred observations](run-data.md#disposing-of-deferred-observations)). The tasks after a `---STOP---` marker count, so a phase half run to the marker is not called done.

The run reads this from the roadmap's marks and the recorded dispositions every time, and keeps no state of its own: nothing remembers which steps have run. A walk step that declares `stop: closure-owed` checks it before every task and before each empty-queue test, the one at the run's start included, so that:

- the roadmap's last phase is closed, though no task follows it;
- a phase whose last task stands right above the marker is closed;
- a run that stopped after a phase's last commit finds the closure owed when it starts again.

When a phase owes a closure the walk stops before the next task, returns `closure-owed`, and the close step runs; a run whose last act is a closure ends at the walk's start with the run's done notification, once ([The scenario engine](scenario-engine.md#how-a-scenario-runs)). A walk without the declaration never stops at a phase, and the walk of `test` never does either, whatever it declares, since a test task's reviews are no one's to close. A close step that finds no phase owing a closure calls no head and returns `done` at once.

## What the step does

The close step's value, written in the scenario file, names the responsible head, and its `peer` the helper head. The person's run settings only map each word to a head's address ([A scenario's heads](../reference/configuration.md#a-scenarios-heads)); what each head does is its skill's, never the engine's. The step takes the first phase that owes a closure and sends the responsible head one message: the closure template with the phase's number and the helper's address filled in, and the `## How to answer` section. It then waits for the responsible head's answer and asks for no order of the work.

The run waits with no bound, as for any call to a head (The head's answer), and sends the message once: a marked send is not sent again. It does not check that the helper was called; the template asks the answer to name the helper's reading that it reconciled against, which makes a closure done alone visible in the answer.

## How the step ends

The step ends when the answer is deposited by the command the message names, never by a word in the head's chat. The run reads the deposit and not who ran the command. Then:

- an answer that ends in `ESCALATION` ends the scenario as an escalation of a role ends a run, naming the closure ([Escalation](escalation.md));
- otherwise every entry of the phase must hold a disposition, and where an entry does not the run halts, naming each undisposed entry;
- otherwise the step returns `done` and the walk goes on with the next task.

Each turn of the cycle either closes a phase or ends the run, so the cycle needs no count.

## What the run needs

The shipped `implement` scenario names its heads by the words `builder` and `razor`, and the person's run settings map each to a head's address ([A scenario's heads](../reference/configuration.md#a-scenarios-heads)). The scenario refuses to start unless both words are mapped, because the message names the helper by its address. It also refuses to start when a walk declares `stop: closure-owed` and no close step follows it in the same cycle, since the walk would return `closure-owed` on every pass with no step to close the phase. The addresses are read fresh before any step, and one the locator cannot read halts the run before any task.

## The records

The head makes no commit in the queued tree. The closure's dispositions are in the tree when the next task is committed, and the next task's commit takes them as it takes every head's write; after the last phase they wait for the head's commit after the run.

## Interruption

A run that stops while the close step waits finds the phase still owing a closure where an entry is undisposed, and the step finds its marker standing and waits for the answer again, or finds the deposit and reads it. Where every entry is disposed by then, the phase owes nothing and the answer, deposited or not, is not read; the call's files stay where they stand, and a closure of the same phase after a finding reopens it finds them and reads the deposit that stands as its answer, which is part of the same accepted gap. An escalation in the answer reaches the person as an escalation, and starting the run again without acting on it is the person's choice.

## The template

The closure's message is a file of the orchestrator's, `closure.md`, found as the scenarios are ([Scenario engine reference — Templates and placeholders](../reference/scenario-engine.md#templates-and-placeholders)); a project's own file of that name in its `templates/` folder is the one that runs. It is the trigger for the heads and not their instructions: it carries what the run knows, the phase's number, where the phase's entries are read, the project's path and the helper's address, and what the heads are asked to produce. How a head reads, disposes or reconciles is in its skill. What is asked of the heads is that file, and a change of how a team closes a phase is an edit of it, never of the engine.
