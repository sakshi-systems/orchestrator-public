"""Loading a declared scenario file.

The file is one YAML 1.2 document. It is read with the safe loader and refused
whole, through ScenarioRefusal, when it does not fit; no value is converted.
The module holds no step kind of its own: the caller hands each kind's values,
the options a step of it reads and the outcomes a step of it returns. `cycle`
is the engine's own key. The behaviour is docs/features/scenario-engine.md; the
values live in docs/reference/scenario-engine.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

from .errors import ScenarioRefusal

_QUOTED_NAME = re.compile(r'"([^"]*)"')


@dataclass(frozen=True)
class StepOption:
    """A key a kind reads beside its kind key: the values it allows (`None`
    allows any word, as it does for `StepKind.values`) and whether every step
    of the kind must hold it."""

    values: tuple[str, ...] | None
    required: bool


@dataclass(frozen=True)
class Step:
    """One step of a scenario: its kind, the value of that kind and the keys
    the step has beside its kind key (its options)."""

    kind: str
    value: str
    options: Mapping[str, str] = field(default_factory=dict, hash=False)


@dataclass(frozen=True, kw_only=True)
class StepKind:
    """What the caller hands for one kind key: the values it allows (`None`
    allows any word: a non-empty string with no whitespace), the options a
    step of it reads and the outcomes a step of it returns."""

    values: tuple[str, ...] | None
    options: Mapping[str, StepOption]
    outcomes: Callable[[Step], Collection[str]]


@dataclass(frozen=True)
class Cycle:
    """Steps repeated, from the first, until one of them returns `until`."""

    until: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Scenario:
    """The steps and cycles of a scenario, in the file's order."""

    steps: tuple[Step | Cycle, ...]


def refusal(path: Path, concern: str, detail: str = "") -> ScenarioRefusal:
    """A refusal whose first line names the file and what it concerns."""
    message = f"The scenario file is refused: {path}: {concern}"
    if detail:
        message += f"\n{detail}"
    return ScenarioRefusal(message)


def _place(exc: YAMLError) -> str | None:
    """The 1-based line and column of a YAML error, when it carries a mark."""
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return None
    return f"line {mark.line + 1}, column {mark.column + 1}"


def _load_document(path: Path) -> object:
    yaml = YAML(typ="safe")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise refusal(
            path, "not one YAML document: the file is not UTF-8", str(exc)
        ) from exc
    try:
        data = yaml.load(text)
    except DuplicateKeyError as exc:
        problem = str(getattr(exc, "problem", "") or "")
        named = _QUOTED_NAME.search(problem)
        place = _place(exc)
        concern = named.group(1) if named else ""
        if place:
            concern = f"{concern} ({place})" if concern else place
        first = f"{concern}: duplicate key" if concern else "duplicate key"
        raise refusal(path, first, problem) from exc
    except YAMLError as exc:
        place = _place(exc)
        concern = f"{place}: not one YAML document" if place else "not one YAML document"
        raise refusal(path, concern, str(exc)) from exc
    if data is None:
        raise refusal(
            path, "not one YAML document: the document is empty or null"
        )
    return data


def _allowed(kind_keys: list[str], in_cycle: bool) -> str:
    names = list(kind_keys) + ([] if in_cycle else ["cycle"])
    return ", ".join(repr(k) for k in names)


def _check_string(path: Path, where: str, value: object, allowed: tuple[str, ...]) -> str:
    """A string among `allowed`, or a refusal on `where`."""
    if type(value) is not str:
        raise refusal(
            path, f"{where}: a string is required, found {type(value).__name__}"
        )
    if value not in allowed:
        listed = ", ".join(repr(v) for v in allowed)
        raise refusal(path, f"{where}: {value!r} is not allowed; allowed: {listed}")
    return value


def _check_word(path: Path, where: str, value: object) -> str:
    """Any word — a non-empty string with no whitespace — or a refusal on `where`."""
    if type(value) is not str:
        raise refusal(
            path, f"{where}: a string is required, found {type(value).__name__}"
        )
    if not value or any(c.isspace() for c in value):
        raise refusal(
            path,
            f"{where}: {value!r} is not allowed; "
            "a non-empty word with no whitespace is required",
        )
    return value


def _load_step(
    path: Path,
    raw: object,
    where: str,
    kinds: Mapping[str, StepKind],
    in_cycle: bool,
) -> Step | Cycle:
    if not isinstance(raw, dict):
        raise refusal(
            path, f"{where}: a mapping is required, found {type(raw).__name__}"
        )
    allowed = _allowed(list(kinds), in_cycle)
    if not raw:
        raise refusal(path, f"{where}: no kind key; allowed: {allowed}")
    if in_cycle and "cycle" in raw:
        raise refusal(path, f"{where}.cycle: a cycle may not hold a cycle")
    present = [
        k
        for k in raw
        if isinstance(k, str) and (k in kinds or (not in_cycle and k == "cycle"))
    ]
    if len(present) > 1:
        listed = ", ".join(repr(k) for k in present)
        raise refusal(path, f"{where}: two kind keys, found {listed}")
    if not present:
        first = next(iter(raw))
        raise refusal(path, f"{where}.{first}: not a kind key; allowed: {allowed}")
    kind = present[0]
    if kind == "cycle":
        return _load_cycle(path, raw, where, kinds)

    spec = kinds[kind]
    value = raw[kind]
    if type(value) is not str:
        raise refusal(
            path,
            f"{where}.{kind}: a string is required, found {type(value).__name__}",
        )
    if spec.values is None:
        _check_word(path, f"{where}.{kind}", value)
    else:
        _check_string(path, f"{where}.{kind}", value, spec.values)
    options: dict[str, str] = {}
    for key, option_value in raw.items():
        if key == kind:
            continue
        if not isinstance(key, str) or key not in spec.options:
            raise refusal(path, f"{where}.{key}: not read by {kind!r}")
        allowed_values = spec.options[key].values
        if allowed_values is None:
            options[key] = _check_word(path, f"{where}.{key}", option_value)
        else:
            options[key] = _check_string(
                path, f"{where}.{key}", option_value, allowed_values
            )
    for name, option in spec.options.items():
        if option.required and name not in options:
            raise refusal(path, f"{where}.{name}: missing")
    return Step(kind, value, options)


def _load_cycle(
    path: Path, raw: dict, where: str, kinds: Mapping[str, StepKind]
) -> Cycle:
    for key in raw:
        if key != "cycle":
            raise refusal(path, f"{where}.{key}: not read by 'cycle'")
    body = raw["cycle"]
    here = f"{where}.cycle"
    if not isinstance(body, dict):
        raise refusal(
            path, f"{here}: a mapping is required, found {type(body).__name__}"
        )
    for key in body:
        if key not in ("until", "steps"):
            raise refusal(path, f"{here}.{key}: not read by 'cycle'")
    if "until" not in body:
        raise refusal(path, f"{here}.until: missing")
    until = body["until"]
    if type(until) is not str:
        raise refusal(
            path, f"{here}.until: a string is required, found {type(until).__name__}"
        )
    if "steps" not in body:
        raise refusal(path, f"{here}.steps: missing")
    raw_members = body["steps"]
    if not isinstance(raw_members, list):
        raise refusal(
            path,
            f"{here}.steps: a list is required, found {type(raw_members).__name__}",
        )
    if not raw_members:
        raise refusal(path, f"{here}.steps: empty")
    members: list[Step] = []
    for j, raw_member in enumerate(raw_members):
        member = _load_step(path, raw_member, f"{here}.steps[{j}]", kinds, True)
        assert isinstance(member, Step)
        members.append(member)
    if not any(until in kinds[m.kind].outcomes(m) for m in members):
        raise refusal(
            path, f"{here}.until: {until!r} is returned by none of its steps"
        )
    return Cycle(until, tuple(members))


def load_scenario(path: Path, kinds: Mapping[str, StepKind]) -> Scenario:
    """Load the scenario file at `path`, or raise ScenarioRefusal.

    `kinds` maps each kind key to its StepKind. The whole Scenario is returned
    only after every check has passed.
    """
    data = _load_document(path)

    if not isinstance(data, dict):
        raise refusal(
            path, f"top level: a mapping is required, found {type(data).__name__}"
        )
    for key in data:
        if key != "steps":
            raise refusal(path, f"{key}: only `steps` is allowed at the top level")
    if "steps" not in data:
        raise refusal(path, "steps: missing")
    raw_steps = data["steps"]
    if not isinstance(raw_steps, list):
        raise refusal(
            path, f"steps: a list is required, found {type(raw_steps).__name__}"
        )
    if not raw_steps:
        raise refusal(path, "steps: empty")

    steps = [
        _load_step(path, raw, f"steps[{i}]", kinds, False)
        for i, raw in enumerate(raw_steps)
    ]
    return Scenario(steps=tuple(steps))
