# Workflow

The orchestrator is an executor, not a planner. Before running it, the roadmap must be ready: tasks are atomic, described precisely, and grouped in the right order. This document describes the established working pattern from an empty roadmap to a pruned `ARCHITECTURE.md`.

## Phases

### 1. Filling the roadmap

Work starts with a dialogue in Claude Code: `/roadmap-outline` explores the codebase and discusses what needs to be done. Candidate tasks emerge from the discussion.

`/roadmap-decompose` takes these tasks and decomposes them to an atomic level — each task should do one thing and describe it precisely enough that the planner does not have to guess intent. The `/roadmap-outline` → `/roadmap-decompose` cycle repeats a few times, until the tasks are atomic and self-contained.

A good task looks like this:

```
- [ ] **Name** — a specific description with file names, types, behavior.
  More detail means a more precise plan.
```

### 2. The implement flow

Once the roadmap is ready:

```bash
uv run orchestrator implement /path/to/project
```

The orchestrator processes tasks one at a time: plan → plan review → implementation → code review → commit. Each task is marked `[x]` after it completes.

If the reviewer's signature does not appear within the iteration limit, the orchestrator stops with artifacts left on disk — what that means and how to work through it is described in [non-convergence.md](concepts/non-convergence.md).

### 3. Test coverage

Once enough tasks have accumulated, it is worth addressing test coverage. `/roadmap-test-coverage` explores the codebase and identifies what is worth covering — which files, classes, behaviors.

`/roadmap-decompose` in test mode writes test tasks into `ROADMAP_TESTS.md` — a separate file that does not clutter the main roadmap.

Then:

```bash
uv run orchestrator test /path/to/project
```

See [test-mode.md](features/test-mode.md) for what makes this final check different from the implement flow.

### 4. Pruning the roadmap

Once the roadmap accumulates many completed `[x]` tasks, `/roadmap-prune` groups them into named features, records a summary in `ARCHITECTURE.md` anchored to commit hashes, and deletes the completed tasks from the roadmap.

After pruning, the roadmap is short again — only pending tasks.

## Diagram

```
/roadmap-outline      ← exploration + discussion
/roadmap-decompose    ← breaking tasks down into ROADMAP.md
   (repeat a few times)
        │
        ▼
uv run orchestrator implement
        │
        ▼
/roadmap-test-coverage  ← what's worth covering with tests
/roadmap-decompose      ← test tasks into ROADMAP_TESTS.md
        │
        ▼
uv run orchestrator test
        │
        ▼
/roadmap-prune          ← summary into ARCHITECTURE.md, roadmap cleanup
```
