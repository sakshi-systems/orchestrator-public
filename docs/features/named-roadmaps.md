# Named roadmaps

## What it is

An optional per-developer roadmap, in place of the shared `ROADMAP.md`, so each developer's own task queue runs independently.

The `roadmap_path` field selects which roadmap the orchestrator reads. It has three mutually exclusive states:

- **Absent, or an empty string** — `ROADMAP.md` is used; behaviour is identical to a run without this setting at all (byte-stable by default).
- **The literal `"my"`** — the orchestrator takes a personal roadmap from this workstation's git identity. The orchestrator derives the user-slug in its own code, in the target project, by the family's rule, whose home is [multiuser roadmaps](https://github.com/sakshi-systems/skills-public/blob/main/docs/philosophy/multiuser-roadmaps.md). It holds one derivation, used by `"my"` and by [a person's run settings](../reference/configuration.md#a-persons-run-settings) alike, so an installed orchestrator needs no skills package for it; the skills' script [skills/src/skills/roadmap-engine/scripts/user-slug.sh](https://github.com/sakshi-systems/skills-public/blob/main/src/skills/roadmap-engine/scripts/user-slug.sh) is the skills' implementation of the same rule, and both follow it in lockstep. Where neither `git config user.email` nor `git config user.name` yields a slug, the run halts naming both settings (the prompt is to configure a git identity or pass an explicit path). The target file is `.ai-factory/roadmaps/<user-slug>.md`. If the file exists, its first line must be `> Owner: <email>` (the git name when no email is set), matching the current git identity — a mismatch or an unrecognized first line stops the run with an operational halt naming the owner. If the file does not exist, the run halts and names the file it expected; it never runs another roadmap in its place.
- **Any other value** — an explicit relative path under `.ai-factory/` (for example, `"roadmaps/alice.md"`), used as-is, with no owner check — an explicit value always wins.

A run's roadmap argument takes precedence over the configured `roadmap_path` for that run, and accepts the same values `roadmap_path` does, the literal `"my"` included.

An absolute path, or a path containing a `..` segment, is a startup error naming the invalid value.

The test roadmap is always derived from the main one: `ROADMAP_TESTS.md` for the default roadmap, the same directory with a `-tests` suffix for a named one (`roadmaps/alice.md` → `roadmaps/alice-tests.md`). There is no separate setting for the test roadmap.

Artifact directories stay flat, byte-for-byte, for the default `ROADMAP.md`/`ROADMAP_TESTS.md` pair (`plans/`, `plan-reviews/`, `reviews/`, `test-runs/`). For any other (named) roadmap, artifacts move into a subdirectory keyed by the roadmap file's stem — for example, `roadmaps/john-doe.md` → `plans/john-doe/`, `reviews/john-doe/`. The named pair's test sibling shares that same subdirectory: `roadmaps/john-doe-tests.md` keys off the same stem, `john-doe`, as `roadmaps/john-doe.md` — both the implement- and test-mode artifacts of a named pair share one numbering axis, `plans/john-doe/`, exactly as the default pair shares its flat directories. This keeps each developer's `{NN}` numbering on its own axis, so merging branches never collides.

The implementer's answer of each iteration is a file of the run's beside the reviews, `reviews/<seq>-<slug>-implementer-N.md`, which follows the same routing.

## Migrating an existing project

1. **Run settings.** Create `<project>/.ai-factory/run-settings/<user-slug>.json` — your own file of run settings, committed with the project, since the project's history shows how each phase ran ([A person's run settings](../reference/configuration.md#a-persons-run-settings)):

   ```json
   { "roadmap_path": "my" }
   ```

   The orchestrator derives your roadmap from your git identity. Move every key you kept in `<project>/.ai-factory/orchestrator.json` into this file and delete that file: where it exists the run stops at its start, naming it and the file its values move to. The machine's base keeps only the machine's keys, the Hermes instances and the Telegram credentials: every run key it holds moves into this file, and a base that still carries one stops the run at its start, naming the file the key belongs in. A project without the person's file runs on the defaults. Commit the file before the run starts. Each person has a file of their own, and a project's people share none.

2. **Roadmaps** → `.ai-factory/roadmaps/`:
   - `.ai-factory/ROADMAP.md` → `.ai-factory/roadmaps/<user-slug>.md`
   - `.ai-factory/ROADMAP_TESTS.md` → `.ai-factory/roadmaps/<user-slug>-tests.md`

   The slug is derived automatically from your git identity, as described above. The first line of each file must be `> Owner: <your git email>`.

3. **Specs** → `.ai-factory/specs/<user-slug>/`: move the roadmap's task specs there and rewrite the `Spec:` tags in the roadmap lines to `specs/<user-slug>/…`.

4. **test-runs** (if any) → `.ai-factory/test-runs/<user-slug>/`.

5. **Verify.** Run the orchestrator against the project — it should resolve `roadmaps/<user-slug>.md` from your git identity. Artifact directories (`plans/`, `plan-reviews/`, `reviews/`, `test-runs/`) are created under `<user-slug>/` automatically; the main and test roadmaps share one numbering axis under the `<user-slug>` stem.
