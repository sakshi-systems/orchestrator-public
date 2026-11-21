"""The one home of reading a head's locator: a head's address in its folder.

A head's folder is `<repository>/.ai-factory/architects/<owner>/<folder>/`,
holding `address.json` or the legacy `address.md`. The module imports nothing
of the package: it reads a file and judges its fields, nothing more.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NamedTuple

JSON_ADDRESS = "address.json"
LEGACY_ADDRESS = "address.md"

_ACCEPTED_RUNTIMES = ("claude", "codex")
_PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")


class HeadIdentity(NamedTuple):
    """A head's identity: the repository it works in, its owner and its folder."""

    repository: Path
    owner: str
    folder: str


class HeadAddress(NamedTuple):
    """A head's address, read fresh from its locator at every call."""

    runtime: str
    session_id: str
    session_name: str | None
    permission_mode: str | None


class LocatorFault(Exception):
    """A locator that is missing, malformed or unsupported, or unreadable.

    The text carries its kind's word beside the repository, owner and folder
    and the faulty file or field.
    """


def _where(identity: HeadIdentity) -> str:
    return f"repository {identity.repository}, owner {identity.owner!r}, folder {identity.folder!r}"


def _fault(word: str, identity: HeadIdentity, detail: str) -> LocatorFault:
    return LocatorFault(f"{word} locator for {_where(identity)}: {detail}")


def is_path_text(value: str) -> bool:
    """True for a string the filesystem can name: no NUL character, and it
    encodes to the filesystem's encoding."""
    if "\x00" in value:
        return False
    try:
        os.fsencode(value)
    except UnicodeEncodeError:
        return False
    return True


def is_single_component(value: object) -> bool:
    """True for a name that is one path component: a nonempty string holding no
    separator, not `.` or `..`, and text the filesystem can name."""
    if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
        return False
    return is_path_text(value)


def _component(identity: HeadIdentity, field: str, value: str) -> str:
    if is_single_component(value):
        return value
    if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
        raise _fault("malformed or unsupported", identity, f"{field} is not a single path component")
    if "\x00" in value:
        raise _fault("malformed or unsupported", identity, f"{field} holds a NUL character")
    try:
        os.fsencode(value)
    except UnicodeEncodeError:
        raise _fault("malformed or unsupported", identity,
                     f"{field} does not encode to the filesystem's encoding") from None
    return value


def _required_field(fields: dict, name: str, identity: HeadIdentity, file_name: str) -> str:
    if name not in fields:
        raise _fault("missing", identity, f"{file_name} has no {name} field")
    value = fields[name]
    if not isinstance(value, str) or not value:
        raise _fault("malformed or unsupported", identity, f"{file_name} field {name} is not a nonempty string")
    return value


def _optional_field(fields: dict, name: str, identity: HeadIdentity, file_name: str) -> str | None:
    if name not in fields:
        return None
    value = fields[name]
    if not isinstance(value, str) or not value:
        raise _fault("malformed or unsupported", identity, f"{file_name} field {name} is not a nonempty string")
    return value


def _address_of(fields: dict, identity: HeadIdentity, file_name: str) -> HeadAddress:
    session_id = _required_field(fields, "session-id", identity, file_name)
    runtime = _required_field(fields, "runtime", identity, file_name)
    if runtime not in _ACCEPTED_RUNTIMES:
        raise _fault("malformed or unsupported", identity,
                     f"{file_name} field runtime is not one of {_ACCEPTED_RUNTIMES}")
    session_name = _optional_field(fields, "session-name", identity, file_name)
    permission_mode = None
    if runtime == "claude" and "permission-mode" in fields:
        mode = fields["permission-mode"]
        if not isinstance(mode, str) or mode not in _PERMISSION_MODES:
            raise _fault("malformed or unsupported", identity,
                         f"{file_name} field permission-mode is not one of {_PERMISSION_MODES}")
        permission_mode = mode
    return HeadAddress(runtime=runtime, session_id=session_id,
                       session_name=session_name, permission_mode=permission_mode)


def _fields_of_json(path: Path, identity: HeadIdentity) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _fault("unreadable", identity, f"{path.name} could not be read: {exc}") from exc
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise _fault("malformed or unsupported", identity,
                     f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _fault("malformed or unsupported", identity, f"{path.name} is not a JSON object")
    return payload


def _fields_of_legacy(path: Path, identity: HeadIdentity) -> dict:
    """The legacy `name: value` lines, read by name: each line splits at its
    first colon with both sides stripped, lines not of that form and unknown
    names are ignored, and no mode is carried."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _fault("unreadable", identity, f"{path.name} could not be read: {exc}") from exc
    fields: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if not separator:
            continue
        name, value = name.strip(), value.strip()
        if name and value and name not in ("permission-mode",):
            fields[name] = value
    return fields


def _present(path: Path, folder: Path, identity: HeadIdentity) -> bool:
    """Whether a name exists in the folder as the folder lists it: a symlink
    counts whether its target exists or not, and it is not followed. A folder
    that does not exist, or is a file, counts as absence; any other failed
    probe is unreadable."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except ValueError as exc:  # a path that cannot even be asked of the filesystem
        raise _fault("malformed or unsupported", identity,
                     f"the folder {folder} cannot be asked of the filesystem: {exc}") from exc
    except OSError as exc:
        raise _fault("unreadable", identity, f"the folder {folder} could not be probed: {exc}") from exc
    return True


def read_address(identity: HeadIdentity) -> HeadAddress:
    """Read a head's address from its folder, fresh at every call.

    `address.json` is read when it is present, otherwise `address.md`; neither
    present is missing. A present `address.json` that cannot be read is
    unreadable, and one that is not valid JSON or not an object is malformed or
    unsupported — a defective `address.json` is never answered from
    `address.md`. `session-id` and `runtime` are required; `session-name` when
    present must be a nonempty string; `permission-mode` is read from
    `address.json` only for a Claude address and must be one of the accepted
    modes. Unknown fields are ignored, the runtime is never inferred, and any
    defect raises LocatorFault.
    """
    _component(identity, "owner", identity.owner)
    _component(identity, "folder", identity.folder)
    folder = identity.repository / ".ai-factory" / "architects" / identity.owner / identity.folder
    json_path = folder / JSON_ADDRESS
    legacy_path = folder / LEGACY_ADDRESS
    if _present(json_path, folder, identity):
        return _address_of(_fields_of_json(json_path, identity), identity, JSON_ADDRESS)
    if _present(legacy_path, folder, identity):
        return _address_of(_fields_of_legacy(legacy_path, identity), identity, LEGACY_ADDRESS)
    raise _fault("missing", identity, f"no {JSON_ADDRESS} or {LEGACY_ADDRESS} in {folder}")
