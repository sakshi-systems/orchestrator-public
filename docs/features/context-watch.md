# Context watch

## What it is

The context watch is a command of its own and not a process that stays alive. A harness runs it from a hook at the end of a session's turn; it reads how full that one session's context is, tells the head when a snapshot is due, and ends. It watches every architect head and each head's hands.

## The hooks

Context grows only during a turn, so the end of a turn is when a measurement is worth taking, and each harness runs a configured command on its own events.

- **Claude Code and Codex** — both run a configured command at `Stop`, the end of a turn, and at `SubagentStop`, the end of a subagent. Each hands the command the session's identity and the location of its record. At the end of a hand, the session is its head's, and the hand's own identity and record come with it.

Both harnesses run the hook in the background, so a check never holds up a turn. The hooks are set once in each harness's user configuration when the orchestrator is installed, as the [README](../../README.md#context-watch-hooks) states. The command's invocation is in the [reference](../reference/context-watch.md#the-command).

## Finding the head

A hook fires for every session of its harness, so the command first asks whether the session is a head. It looks among the heads under the architect roots its [configuration](../reference/configuration.md#settings-keys) lists, reading each locator under [Identity, locator and recovery](https://github.com/sakshi-systems/skills-public/blob/main/docs/architect-runtimes.md#identity-locator-and-recovery), and matches the session's identity. A session no locator names is not a head: the command ends at once and says nothing. A head is covered from its first turn after it publishes its locator. A hand's check arrives named by its head's session, which the hook supplies, so the head is matched by that session and the hand is observed by its own record. A locator that cannot be read, or that has no `runtime`, is skipped and [logged](../reference/context-watch.md#the-log).

## Reading the fill

The fill of a session is read from the runtime's own record of that session, following the locator's `runtime`. The format of those records is internal to each runtime and promised by none; the command reads them knowingly. An unreadable record is a [logged](../reference/context-watch.md#the-log) fault, never silence.

The window comes from the runtime's record where it states one. Otherwise the configuration states it for the model the record names; a model with no window stated is skipped and [logged](../reference/context-watch.md#the-log), as a locator without a `runtime` is.

## The warning

A session is warned when its fill reaches the [configured boundary](../reference/configuration.md#settings-keys) of its window: the command sends, by the [courier](courier.md), the head a snapshot warning, or, when the session is one of the head's hands, the hand's warning. A session is warned once per crossing, and is armed again once its fill falls back below the boundary, as after a compact, since a session's identity holds across a compact. Every check is a process of its own, so a marker file per warned session, outside every target tree, carries the warning from one check to the next: it is created when the session is warned and removed when its fill falls back below the boundary, and a later check that finds it does not warn again. Different sessions have different markers, so checks that end together share no file. The [boundary and the two literals](../reference/context-watch.md#the-warning) and the [marker's location](../reference/context-watch.md#the-marker) are in the reference.

The command never holds up or changes the turn it follows: a fault in it, a failed send included, is [logged](../reference/context-watch.md#the-log) and the command ends.
