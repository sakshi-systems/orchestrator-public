"""Finding the file a scenario name resolves to.

A name is looked up in the project's `.ai-factory/scenarios/` first and then
among the scenarios the orchestrator ships; the result says whether the
project's file shadows a shipped one. A name that is not a single path
component, or that matches no file, is refused through ScenarioRefusal. The
behaviour is docs/features/scenario-engine.md; the values live in
docs/reference/scenario-engine.md.

The closure template is found the same way, the project's own copy in
`.ai-factory/scenarios/templates/` before the shipped one, and filled over the
closed set of placeholders; a missing file and a placeholder outside the set
are refused through ScenarioRefusal. One function checks the template, used
both at a run's start and at the fill. The set and its values are
docs/reference/scenario-engine.md, section "Templates and placeholders".
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ScenarioRefusal

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
TEMPLATES_DIR = SCENARIOS_DIR / "templates"

PLACEHOLDERS = ("phase", "roadmap", "project", "peer")
_PLACEHOLDER_RE = re.compile(r"\{([a-z]+)\}")


@dataclass(frozen=True)
class ResolvedScenario:
    """The file a name resolved to, and whether it shadows a shipped file."""

    path: Path
    shadows: bool


def _is_single_component(name: str) -> bool:
    if name in ("", ".", ".."):
        return False
    separators = {"/", os.sep}
    if os.altsep:
        separators.add(os.altsep)
    return not any(sep in name for sep in separators)


def resolve_scenario(name: str, project_dir: Path) -> ResolvedScenario:
    """Resolve `name` to a scenario file, or raise ScenarioRefusal.

    The project's file wins; `shadows` is True only when it wins over a
    shipped file of the same name.
    """
    if not _is_single_component(name):
        raise ScenarioRefusal(
            f"The scenario name is refused: {name!r}: not a single path component"
        )
    project_file = project_dir / ".ai-factory" / "scenarios" / f"{name}.yaml"
    shipped_file = SCENARIOS_DIR / f"{name}.yaml"
    if project_file.is_file():
        return ResolvedScenario(project_file, shadows=shipped_file.is_file())
    if shipped_file.is_file():
        return ResolvedScenario(shipped_file, shadows=False)
    raise ScenarioRefusal(
        f"The scenario {name!r} resolves to no file: "
        f"looked in {project_file} and {shipped_file}"
    )


def resolve_template(project_dir: Path) -> Path:
    """Resolve the closure template, or raise ScenarioRefusal.

    The project's `.ai-factory/scenarios/templates/closure.md` wins over the
    shipped file.
    """
    project_file = (
        project_dir / ".ai-factory" / "scenarios" / "templates" / "closure.md"
    )
    shipped_file = TEMPLATES_DIR / "closure.md"
    if project_file.is_file():
        return project_file
    if shipped_file.is_file():
        return shipped_file
    raise ScenarioRefusal(
        f"The closure template resolves to no file: "
        f"looked in {project_file} and {shipped_file}"
    )


def check_template(path: Path) -> str:
    """Read the template at `path` and return its text, or raise ScenarioRefusal.

    A placeholder is a lowercase word in braces; any other brace content is
    plain text. A word outside the closed set is refused, naming the word.
    """
    text = path.read_text(encoding="utf-8")
    for match in _PLACEHOLDER_RE.finditer(text):
        word = match.group(1)
        if word not in PLACEHOLDERS:
            raise ScenarioRefusal(
                f"The closure template {path} holds a placeholder "
                f"outside the closed set: {{{word}}}"
            )
    return text


def fill_template(path: Path, values: Mapping[str, str]) -> str:
    """Read the template at `path` and fill its placeholders from `values`.

    A word outside the closed set is refused before anything is replaced, as
    `check_template` refuses it. `values` holds a string for each of the four
    placeholders:

    - `phase`: the phase's number;
    - `roadmap`: the roadmap's path relative to `.ai-factory`, such as
      `roadmaps/alice.md`;
    - `project`: the project's absolute path;
    - `peer`: `repository: <path>, owner: <owner>, folder: <folder>`.

    A key missing from `values` is a caller's bug and raises KeyError.
    """
    text = check_template(path)
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)
