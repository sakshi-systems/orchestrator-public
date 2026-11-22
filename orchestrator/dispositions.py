"""The store of a closure's deposits: one disposition record per task, the
reading of a record and its atomic merge — the orchestrator's own record of
how each deferred observation was disposed, which `show` reports over the
markers an older review carries."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from .artifact_layout import DISPOSITIONS_DIRNAME, artifact_dir, disposition_name
from .errors import HaltError


def record_path(project_dir: Path, segment: str | None, seq: str, slug: str) -> Path:
    """The task's disposition record's path: the dispositions directory under
    the roadmap's segment, the task's own stem with the record's suffix;
    creates nothing."""
    return artifact_dir(project_dir, DISPOSITIONS_DIRNAME, segment) / disposition_name(seq, slug)


def read_record(path: Path) -> dict:
    """Read the disposition record at `path`.

    An absent record reads as `{}`; an existing record that cannot be read,
    is not UTF-8, is not valid JSON or is not a JSON object halts, as a
    sidecar does, so its contents are never mistaken for an empty record.
    An entry's value that is not a disposition of `kind` — and `target`
    where the kind is `routed` — halts the same way.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise HaltError(f"Disposition record could not be read\n{path}\n{e}") from e
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise HaltError(f"Disposition record is not valid UTF-8 text\n{path}\n{e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise HaltError(f"Disposition record is not valid JSON\n{path}\n{e}") from e
    if not isinstance(data, dict):
        raise HaltError(f"Disposition record JSON is not an object\n{path}\n{type(data).__name__}")
    for entry, held in data.items():
        valid = isinstance(held, dict) and isinstance(held.get("kind"), str)
        if valid and "target" in held:
            valid = isinstance(held["target"], dict)
        if not valid:
            raise HaltError(f"Disposition record holds an invalid entry\n{path}\n{entry}: {held!r}")
    return data


def merge_items(path: Path, items: dict[str, dict]) -> None:
    """Merge `items` into the record at `path`: the record as read, updated
    with the items, written whole through a temporary file and an atomic
    replace, creating the dispositions directory where no deposit has
    written one yet.

    An unreadable or invalid existing record halts as `read_record` does; an
    I/O failure halts without replacing the original, the temporary file
    removed in any case.
    """
    record = read_record(path)
    record.update(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise HaltError(f"Disposition record could not be written\n{path}\n{e}") from e
