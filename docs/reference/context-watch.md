# Context watch reference

The [context watch](../features/context-watch.md) governs behaviour. This page holds its values.

## The command

```text
orchestrator-watch --runtime <claude|codex>
```

`orchestrator-watch` is a console script of its own, beside `orchestrator` and `orchestrator-launch`. A harness's hook runs it for one session: it reads that harness's hook payload on standard input, and `--runtime` names the harness the payload comes from. Every outcome, a fault included, ends with exit status 0, so the harness never sees the check fail; the command never exits with status 2, which on an end-of-turn or end-of-subagent hook makes the agent keep working. The [hook entries](../../README.md#context-watch-hooks) are in the README.

## The warning

The warning boundary is the [`snapshot_boundary`](configuration.md#settings-keys) setting, one fraction of a session's window for heads and hands alike: a session whose fill reaches it is warned.

The two warnings are exactly these texts:

| Warning | Text |
|---|---|
| A head's own session | `пора писать снапшот` |
| One of the head's hands | `руке пора снапшот` |

## The log

The log file is `~/.local/state/orchestrator/context-watch.log`; every fault of the command is written there.

## The marker

A session's marker is the file `~/.local/state/orchestrator/context-watch/<session identity>`. It is created when the session is warned and removed when its fill falls back below the boundary.
