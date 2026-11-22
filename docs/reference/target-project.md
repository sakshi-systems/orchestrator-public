# Target project requirements

## What to prepare

The orchestrator works with any project, provided it has a handful of files under `.ai-factory/` and an initialized git repository.

`ROADMAP.md` — the list of tasks as markdown checkboxes. The orchestrator reads them top to bottom and picks the first unchecked one. Completed ones are marked `[x]` automatically before the task's commit, which carries the mark and the task's time: ` [<m>m <s>s]`, or ` [<h>h <m>m <s>s]` from an hour up, appended to the line, the sum of the times the run recorded for the task's steps ([What the sidecar holds](../features/resume.md#what-the-sidecar-holds)).

`ROADMAP_TESTS.md` — the equivalent file for test tasks, read only in `test` mode. A separate file keeps test tasks apart from the main roadmap.

A run's target can also be a named roadmap under `.ai-factory/roadmaps/` — a specific developer's personal queue instead of the shared `ROADMAP.md`. Which roadmap is used is selected by the `roadmap_path` setting in the person's run settings — see [named-roadmaps.md](../features/named-roadmaps.md); without it, behaviour is unchanged.

`ARCHITECTURE.md` — an optional file describing module structure, layer boundaries, and dependency rules in detail. When present, both the planner and the implementer treat it as a hard requirement for file placement.

`RULES.md` — an optional file of short project rules. Every rule is treated by the agents as a mandatory requirement, not a suggestion.

## Task format

A task line must look like this:

```
- [ ] **Name** — Description of what needs to be done
```

The name is used as the plan's title and the git commit message. The planner reads the description when composing the plan — the more specific it is, the better the result. A vague description like "improve performance" produces a vague plan. With planning off the task's spec is the plan, found by the contract line's `Spec:` tag, so the tag is required and a task without it halts the run ([Planning is a stage a project can switch off](../pipeline.md#planning-is-a-stage-a-project-can-switch-off)).

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

After each task, the orchestrator runs `git add -A`, commits, and `git push -u origin HEAD`; if that push fails (no network), a warning is logged and the run continues. The repository must be initialized, hold at least one commit and be free of conflicts before the run starts: each iteration's commit takes the commit the task began on as its parent. It has an `origin` the run can push to and delete from: after each implementer turn the run pushes the iteration's ref there, `refs/orchestrator-iteration/<name>`, beside the last laid tip's `refs/orchestrator-laid/<name>` that is never pushed, a push that fails halts the run, and the ref is removed when the task ends ([Invoking a role](../features/agent-runtime.md#invoking-a-role)). `<name>` is the task's plan stem under its roadmap's artifact segment, and the implementer's branch is `orchestrator/<name>`, which no ref of the run shares a short name with. The run's own git calls run in the target project's repository whatever git environment the run was started in: the names that point git at another repository, such as `GIT_DIR` and `GIT_INDEX_FILE` from a hook, are not passed to it. Each call never waits for a credential and ends at a time bound, except the task's commit, which runs the target project's own hooks and takes none; a call that passes its bound ends with every process it started, an ssh process or a hook among them, as it fails. Run it on a clean tree: `git add -A` stages everything indiscriminately, so any uncommitted changes — including edits made by hand during the run — ride along into the next task's commit.
