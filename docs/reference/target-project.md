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

The run's tree is the project's repository and, where `.ai-factory` resolves into a different git toplevel (a link into another repository, or a repository of its own), that repository too, of which the run takes only the folder `.ai-factory` resolves to, in the task's commit and in the iteration's: another project's folder in it, or a file of its own, is never staged, committed or pushed by this run. The run finds its tree at its start, from the tree on disk, and finds it the same way again at a resume; nothing records it. An `.ai-factory` that lies in no repository leaves the tree the project's repository alone.

After each task the run commits and pushes in each repository of the tree, with the task's title as the commit message. In the project's repository it runs `git add -A`, commits, and `git push -u origin HEAD`; in the other it stages and commits only the folder `.ai-factory` resolves to, and pushes. The project's repository goes first and the other after it, so that a crash between the two leaves the code committed and the records (the roadmap's mark, the plan, the reviews, the sidecar) uncommitted in the folder, where the next task's commit takes them; the reverse order would leave a task marked done over uncommitted code. "Nothing to commit" is decided for each repository, and a push that fails (no network) logs a warning for that repository and the run continues.

Each repository of the tree must be initialized, hold at least one commit and be free of conflicts before the run starts: each iteration's commit takes the commit the task began on in that repository as its parent. Each has an `origin` the run can push to and delete from: after each implementer turn the run pushes the iteration's ref to each origin, `refs/orchestrator-iteration/<name>`, beside the last laid tip's `refs/orchestrator-laid/<name>` that is never pushed, a push that fails halts the run, and the refs are removed in each repository when the task ends ([Invoking a role](../features/agent-runtime.md#invoking-a-role)), as a rollback of the task to `start` and a sweep of its phase remove them ([A run's data is reached through commands](../features/run-data.md)). `<name>` is the task's plan stem under its roadmap's artifact segment, and the implementer's branch is `orchestrator/<name>`, which no ref of the run shares a short name with. Those are the names in the project's repository. The other repository is shared by design, one repository holding the artifacts of several projects' folders, so there every name the run gives a ref or a branch, the iteration's ref, the laid ref and the branch alike, carries the project's folder, the path `.ai-factory` resolves to inside it, in front of `<name>`: with the folder `ai-factory/project-a`, `refs/orchestrator-iteration/ai-factory/project-a/<name>` and `orchestrator/ai-factory/project-a/<name>`. Two projects in one repository never share a name, whatever their slugs and plan stems. Where `.ai-factory` is a repository of its own the folder is empty, the repository is the project's alone, and its names are the project's repository's.

The run's own git calls run in the repositories of the tree whatever git environment the run was started in: the names that point git at another repository, such as `GIT_DIR` and `GIT_INDEX_FILE` from a hook, are not passed to it. Each call never waits for a credential and ends at a time bound, except the task's commit, which runs the target project's own hooks and takes none; a call that passes its bound ends with every process it started, an ssh process or a hook among them, as it fails.

Run it on a clean tree: `git add -A` stages everything indiscriminately, so any uncommitted changes, including edits made by hand during the run, ride along into the next task's commit; the folder `.ai-factory` resolves to is staged whole in the same way, in the other repository too.
