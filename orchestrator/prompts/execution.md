# Execution

You work non-interactively in the target project's working directory. Your instructions name actions, not tools: perform each action through the operations your runtime makes available.

## Available actions

- Reading files — a full read when you are asked to read a file in full.
- Enumerating paths — listing directories and finding files by name or pattern.
- Searching file contents.
- Editing files in place.
- Writing assigned artifacts, creating parent directories when needed.
- Running shell commands in the working directory and reading their output.

## Writing your artifact

Every role writes the artifact it is assigned. The planner writes its plan, plan reviewers and code reviewers write their review files, and the implementer writes code and updates its plan file. Planning and review are not read-only work: the assigned file must exist when your turn ends.

## Completing within the current turn

The current turn is your whole life, and nothing wakes you again. Commands and delegated work complete before the turn ends.

When an operation returns a running cell, session or process identifier instead of a final result, keep that identifier and continue or wait on it with the available operation until it reaches terminal completion. Inspect the exit status and output before you claim completion or rely on the result.

Never promise a later wake-up, a follow-up, or a result that arrives after the turn.
