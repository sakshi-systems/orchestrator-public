# Context model: the agent's tree of perception

The pipeline passes the agent a minimum: the task's contract line and the path to the roadmap, with the plan and the results of the roles before it inline. All other context the agent raises itself, by following links out from that line. This page describes what an agent's effective perception is made of, and why it is assembled lazily — as a tree around the task, not a graph of the whole project.

## What the agent receives literally

- **The planner** — the task's title and description (the whole contract line) plus the line `Roadmap: <path> (line N)`, the path relative to the project's root; on a revision, the current plan and the plan review inline.
- **The plan reviewer** — the plan inline; a fresh session on every attempt.
- **The reviewer** — the plan inline, the implementer's answer and the previous review, and the changes, which reach it as [Invoking a role](../features/agent-runtime.md#invoking-a-role) states; it runs in a session of its own, never the planner's, fresh per task and continued across the task's fix iterations, so it knows the plan from its message and none of the planner's exploration.
- **The implementer** — the plan inline, or under planning off the task's spec laid as the plan, plus, on its first message, the line `Roadmap: <path> (line N)`, the path relative to the project's root — optional context for what is already done, what comes next, and where a neighbouring task's scope begins, which it is not forced to read; on a fix iteration, the review inline; it holds one session through the whole fix cycle.

Every message is self-contained: an agent that runs in the run's tree may read what it names, and an agent that does not, such as a Hermes instance or a head, needs no path ([Invoking a role](../features/agent-runtime.md#invoking-a-role)).

Neighboring tasks and architecture documents are not injected into the prompt, and neither is a spec note, except under planning off, where the task's spec is the plan the implementer receives ([Planning is a stage a project can switch off](../pipeline.md#planning-is-a-stage-a-project-can-switch-off)).

## The edges context is raised along

Effective context is assembled by walking links:

- **The target project's `CLAUDE.md`** — the one unconditional channel: the Claude CLI picks it up as project context on every run of every agent.
- **`ARCHITECTURE.md`, `RULES.md`** — mandatory reading per the planner prompts' Step 0.
- **The `Spec:` tag in the contract line** — the path to the task's spec note; a note links to adjacent notes, and the agent follows those links. Spec notes live under `specs/` (the current name) or `notes/` (legacy) — the orchestrator does not care, the agent follows the literal path from the `Spec:` tag. The web of notes is the primary carrier of cross-task links.
- **The roadmap at the passed path** — reading its own line, the agent sees neighboring tasks, the phase header, and its `Governing spec:`; the reviewer checks changes against the roadmap per instruction.
- **The session within a task** — the reviewer accumulates the context of its own reviews and inherits none of the planner's; the implementer accumulates the context of its own fixes.

## A tree instead of a graph

`enable_phase_sessions` chooses between two context-raising strategies:

- **`true` — eager loading.** The planner's session carries over between the tasks of a phase; by the last task, its session prefix holds the whole phase's graph — every plan, review, and tool output. The agent sees everything, but context burns out even at a million tokens, and cost grows super-linearly — see [phase-sessions.md](../features/phase-sessions.md) for the token economics.
- **`false` (default) — lazy raising.** For each task, only the tree around it is raised into memory: the root is the contract line, the branches are the spec note with its own links, roadmap neighbors, architecture documents. Cross-task memory lives not in the session but in files — artifacts, notes, the roadmap — and is fetched on demand.

A lazy tree scales as the roadmap grows: the depth raised is set by how connected the notes are, not by the length of accumulated history. An eager graph does not: its prefix grows with every task regardless of whether the current task needs that memory.

## Consequences for the target project

- **The contract line is the only guaranteed entry point.** Everything material to a task must be reachable from it by links: the `Spec:` tag, a note's own links to adjacent notes, the phase's `Governing spec:`.
- **`CLAUDE.md` is the channel for rules that always apply.** What must be in an agent's head on every single run, without exception, lives there, not in notes.
- **Breadth of perception equals density of links.** A note that does not link to adjacent notes leaves the agent blind to them; how complete the lazy tree is where it matters is set by the quality of the edges, not by the size of the session.
