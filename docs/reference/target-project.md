# Target project requirements

## What to prepare

The orchestrator works with any project, provided it has a handful of files under `.ai-factory/` and an initialized git repository.

`ROADMAP.md` — the list of tasks as markdown checkboxes. The orchestrator reads them top to bottom and picks the first unchecked one. Completed ones are marked `[x]` automatically before the task's commit, which carries the mark.

`ROADMAP_TESTS.md` — the equivalent file for test tasks, read only in `test` mode. A separate file keeps test tasks apart from the main roadmap.

A run's target can also be a named roadmap under `.ai-factory/roadmaps/` — a specific developer's personal queue instead of the shared `ROADMAP.md`. Which roadmap is used is selected by the `roadmap_path` setting in `orchestrator.json` — see [named-roadmaps.md](../features/named-roadmaps.md); without it, behaviour is unchanged.

`ARCHITECTURE.md` — an optional file describing module structure, layer boundaries, and dependency rules in detail. When present, both the planner and the implementer treat it as a hard requirement for file placement.

`RULES.md` — an optional file of short project rules. Every rule is treated by the agents as a mandatory requirement, not a suggestion.

## Task format

A task line must look like this:

```
- [ ] **Name** — Description of what needs to be done
```

The name is used as the plan's title and the git commit message. The planner reads the description when composing the plan — the more specific it is, the better the result. A vague description like "improve performance" produces a vague plan.

## Phases

Tasks can be grouped into phases using `##` or `###` headings:

```markdown
## Phase 1 — Core types

- [ ] **Task A** — ...
- [ ] **Task B** — ...

## Phase 2 — Services

- [ ] **Task C** — ...
```

Within one phase, the planner's session persists across every task in it — see [phase-sessions.md](../features/phase-sessions.md) for what that means and how it resets.

Phases are optional. A roadmap without headings works normally: every task then runs in one continuous session. Adding phases is worth it once a roadmap has 5+ related tasks in a row, with a substantial context shift between groups.

## Breakpoint

To stop the orchestrator after a specific task, insert a `---STOP---` marker into `ROADMAP.md`:

```markdown
- [ ] **Task A** — ...
- [ ] **Task B** — ...

---STOP---

- [ ] **Task C** — ...
- [ ] **Task D** — ...
```

The orchestrator runs Task A and Task B, then stops. Task C and Task D are left untouched. On startup, the log prints a message that processing stopped at the breakpoint.

To continue, remove the marker from the file and run the orchestrator again.

## Git repository

After each task, the orchestrator runs `git add -A`, commits, and `git push -u origin HEAD`; if the push fails (no remote, no network), a warning is logged and the run continues. The repository must be initialized and free of conflicts before the run starts. Run it on a clean tree: `git add -A` stages everything indiscriminately, so any uncommitted changes — including edits made by hand during the run — ride along into the next task's commit.
