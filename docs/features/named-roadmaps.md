# Named roadmaps

## What it is

An optional per-developer roadmap, in place of the shared `ROADMAP.md`, so each developer's own task queue runs independently.

The `roadmap_path` field selects which roadmap the orchestrator reads. It has three mutually exclusive states:

- **Absent, or an empty string** — `ROADMAP.md` is used; behaviour is identical to a run without this setting at all (byte-stable by default).
- **The literal `"my"`** — the orchestrator takes a personal roadmap from this workstation's git identity. The user-slug is whatever the user-slug script prints, run in the target project: [skills/src/skills/roadmap-engine/scripts/user-slug.sh](https://github.com/sakshi-systems/skills-public/blob/main/src/skills/roadmap-engine/scripts/user-slug.sh), which a run executes from its installed path `~/.claude/skills/roadmap-engine/scripts/user-slug.sh` — present when the skills package is active. The target file is `.ai-factory/roadmaps/<user-slug>.md`. If the file exists, its first line must be `> Owner: <email>` (the git name when no email is set), matching the current git identity — a mismatch or an unrecognized first line stops the run with an operational halt naming the owner. If the file does not exist, the run halts and names the file it expected; it never runs another roadmap in its place. The run also halts when the script is not installed, when the script reports that no user-slug can be derived because neither an email nor a name is set in git (the prompt is to configure a git identity or pass an explicit path), and when the script fails in any other way.
- **Any other value** — an explicit relative path under `.ai-factory/` (for example, `"roadmaps/alice.md"`), used as-is, with no owner check — an explicit value always wins.

A run's roadmap argument takes precedence over the configured `roadmap_path` for that run, and accepts the same values `roadmap_path` does, the literal `"my"` included.

An absolute path, or a path containing a `..` segment, is a startup error naming the invalid value.

The test roadmap is always derived from the main one: `ROADMAP_TESTS.md` for the default roadmap, the same directory with a `-tests` suffix for a named one (`roadmaps/alice.md` → `roadmaps/alice-tests.md`). There is no separate setting for the test roadmap.

Artifact directories stay flat, byte-for-byte, for the default `ROADMAP.md`/`ROADMAP_TESTS.md` pair (`plans/`, `plan-reviews/`, `reviews/`, `test-runs/`). For any other (named) roadmap, artifacts move into a subdirectory keyed by the roadmap file's stem — for example, `roadmaps/john-doe.md` → `plans/john-doe/`, `reviews/john-doe/`. The named pair's test sibling shares that same subdirectory: `roadmaps/john-doe-tests.md` keys off the same stem, `john-doe`, as `roadmaps/john-doe.md` — both the implement- and test-mode artifacts of a named pair share one numbering axis, `plans/john-doe/`, exactly as the default pair shares its flat directories. This keeps each developer's `{NN}` numbering on its own axis, so merging branches never collides.

## Migrating an existing project

1. **Override config.** Create `<project>/.ai-factory/orchestrator.json` — git-ignored and per-developer, never committed:

   ```json
   { "roadmap_path": "my" }
   ```

   It overrides the global config for this project only: the orchestrator derives your roadmap from your git identity. Leave the global `orchestrator.json` alone — it keeps pointing at the shared roadmap, so projects that have not migrated behave exactly as before. Each developer sets up their own override (and copies it into the next project they migrate).

2. **Roadmaps** → `.ai-factory/roadmaps/`:
   - `.ai-factory/ROADMAP.md` → `.ai-factory/roadmaps/<user-slug>.md`
   - `.ai-factory/ROADMAP_TESTS.md` → `.ai-factory/roadmaps/<user-slug>-tests.md`

   The slug is derived automatically from your git identity, as described above. The first line of each file must be `> Owner: <your git email>`.

3. **Specs** → `.ai-factory/specs/<user-slug>/`: move the roadmap's task specs there and rewrite the `Spec:` tags in the roadmap lines to `specs/<user-slug>/…`.

4. **test-runs** (if any) → `.ai-factory/test-runs/<user-slug>/`.

5. **Verify.** Run the orchestrator against the project — it should resolve `roadmaps/<user-slug>.md` from your git identity. Artifact directories (`plans/`, `plan-reviews/`, `reviews/`, `test-runs/`) are created under `<user-slug>/` automatically; the main and test roadmaps share one numbering axis under the `<user-slug>` stem.
