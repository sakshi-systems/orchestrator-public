# A run's data is reached through commands

## Who reaches a run's data

What a run leaves, its plans, reviews, answers and the record of where each task stands, is the orchestrator's data, and the orchestrator owns it whole: where it lies, how its files are named and what states its records take are the orchestrator's alone and change freely. A client of the orchestrator, a skill, a head or a person's own tool, reaches that data only through the orchestrator's commands, and never reads or writes a file of it. A command returns content and never a path, since a path handed to a client puts the layout back in its hands. The code in the project's working tree is the project's, never the orchestrator's data, and no command touches it.

What a client may rely on is the interface and nothing behind it: the commands, the JSON they print with its version, the positions a task can be taken back to as versioned tokens, and the tokens an agent writes in its answer. [Run data reference](../reference/run-data.md) holds them. The orchestrator is the one that changes its layout, and a change of layout alters no client, so a skill carries no copy of it to keep in step.

The shape follows `orchestrator-answer`: a head answers a run through that command and never writes the exchange's files (Head exchange reference — The command).

## Addressing a task and a phase

A command names the roadmap and, within it, a task or a phase. The roadmap is the one a run of the project would take, and `--roadmap` names another exactly as it does for a run ([Named roadmaps — What it is](named-roadmaps.md#what-it-is)). A task is addressed by the number its roadmap line carries: `N.M` or a split child's `N.M.k`. A flat task, one with no number, as the test roadmap and legacy roadmaps hold, is addressed by its title as the line gives it; a title that stands on more than one line of the roadmap makes the address ambiguous, and the command refuses it, naming the lines. A phase is addressed by its number.

## What show gives

`show` returns what the orchestrator holds for the addressed task or phase as content, each piece under the name of the step that produced it, never under a path.

- **For a task:** where the task stands (not started, underway, escalated or done); under each step of its run, in order, the text of each round: the plan, each plan review, the implementer's answer of each iteration and each review or test run; the escalation when the task stopped on one, with the role that raised it and its text; its deferred observations with their dispositions; and the positions a rollback may take it back to.
- **For a phase:** each task's standing, its escalation where it stopped on one and its deferred observations with their dispositions.

A deferred observation is reported once when reviews of the task carry it with identical text, with where each of them stood, and carries its disposition or none; a remark reworded in a later review is another entry. A task the orchestrator holds nothing for reports where it stands and no steps.

## Rolling a task back

`rollback` takes a task back to a position that `show` listed for it, and any other position is refused, so no client keeps a list of positions of its own and learns the vocabulary from the orchestrator when it acts. The next run of the project goes on from the position. [Positions](../reference/run-data.md#positions) holds each by meaning.

The rollback resets the orchestrator's own state for the task, and its scope is a contract:

- It acts on the orchestrator's records of the task alone: its step, its reviews and rounds, the implementer's answers, the sessions and the escalation, and at `start` the task's record whole with the run's artifacts and its refs in each repository of [the run's tree](../reference/target-project.md#git-repository).
- It acts on nothing else, the working tree at every position included: a rollback leaves the tree as the head left it, and the next run starts on that tree. What becomes of an attempt's code, kept, repaired or discarded, is the head's own act and a part of the depth of its repair; a head that discards it does so itself with git. The spec and the roadmap line, and what any head wrote during the run, memory included, stay. Deleting a task's roadmap line, its spec or a phase's header, where a task proves wrong and goes, is the skill's own act on its own artifacts, after the rollback to the first position.

A rollback also clears the escalation a task stopped on. A task that is done has no position, and a task that no attempt has begun lists none.

## Sweeping a phase

`sweep` removes the run's artifacts of every task the roadmap lists under the addressed phase, and the refs of those tasks that remain in each repository of [the run's tree](../reference/target-project.md#git-repository), and nothing a skill owns. Removing one task's run is a rollback to its first position, not a sweep. It refuses a phase that still holds a task that is not done, naming those tasks, so the artifacts of a task in flight are never removed. The command does not decide whether a phase may be pruned: that is the prune's rule, which reads what `show` reports, and the disposition of every deferred observation is what lets a prune proceed.

## Disposing of deferred observations

A review may leave remarks for whoever holds the project's work, its deferred observations ([Completion signals](../pipeline.md#completion-signals)). At a phase's closure that head disposes of each entry, and a disposition is one of three:

- **fixed:** the gap is fixed; the fix is the change itself;
- **routed:** the remark is written into an open task's spec or onto a phase of the roadmap, and the disposition names that target;
- **dismissed:** the remark is moot, stale or already handled.

`dispose` takes the closure's deposit: the disposition of each entry it covers, in the shape `show` reports it so that a client passes back what it read, a closure may deposit entry by entry across several calls, since the command records the dispositions of the entries a deposit covers and checks nothing of the phase as a whole. What shows a phase closed is that every entry of it reads as disposed through `show`, which is what the run checks when a scenario's close step ends ([The closure of a phase](phase-closure.md#how-the-step-ends)). It is one command for any head, whether the closure runs as a step of a scenario or a head does it by hand, and it is not `orchestrator-answer`, which answers a pending call of a run, which a closure is not. The orchestrator holds the fact that an entry is disposed and how, and `show` reports it, so a routed or fixed entry reads as disposed though its content lies in a spec or a commit. A routed remark's content also lies in the target spec, where the task's run reads it; the spec holds the content and the deposit holds the fact of disposal, which is no second home. `dispose` checks only its own data: a routed target is recorded as the head names it, in one of its two forms, and whether it names an open task's spec or a phase of the roadmap is the head's judgment, which the command never opens a target to judge. A disposition covers every occurrence of the entry in the task's reviews. A deposit that names an entry already disposed the same way changes nothing, so a closure that is resumed and sends its deposit again succeeds; a different disposition is refused only against a disposition the orchestrator itself recorded, the refusal naming it. An entry that reads as disposed from an older review's marker, `legacy` included, takes the deposit's disposition, and the recorded one is what `show` reports from then on.

A rollback to `plan-review`, `verify` or `implement` keeps the task's dispositions: the working tree stays, so a disposition made of it still holds, and an entry that a later review repeats word for word shows as already disposed. Only a rollback to `start` removes them, with the reviews they describe.

No client appends anything to a review file, and the reviewer still writes no disposition: its entries carry none.

## Markers in older reviews

Reviews written before the commands carry a marker at the end of an entry line, `[fixed]`, `[routed → …]` or `[dismissed]`, which a head appended. `show` reads each as the disposition it names, and the retired markers as disposed in a kind of their own ([Dispositions](../reference/run-data.md#dispositions)), so an old review needs no migration. Nothing writes a marker any more.

## A client without the commands

A team that does not run the orchestrator has no command, and its agent looks in `.ai-factory/` itself. A skill names only the area it works with, such as what a run left for a task, and leaves where it lies to the agent or to the command.
