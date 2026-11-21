"""The one reader of a head's locator: both forms, by name, with typed faults."""

import json
from pathlib import Path

import pytest

from orchestrator.locator import HeadAddress, HeadIdentity, LocatorFault, read_address

OWNER = "max"
FOLDER = "r&d-head"
RUNTIMES = ("claude", "codex")
MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")


def _identity(tmp_path, owner=OWNER, folder=FOLDER) -> HeadIdentity:
    return HeadIdentity(repository=tmp_path, owner=owner, folder=folder)


def _folder_of(tmp_path, owner=OWNER, folder=FOLDER) -> Path:
    return tmp_path / ".ai-factory" / "architects" / owner / folder


def _write_json(tmp_path, payload, owner=OWNER, folder=FOLDER) -> None:
    folder = _folder_of(tmp_path, owner, folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "address.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_legacy(tmp_path, lines, owner=OWNER, folder=FOLDER) -> None:
    folder = _folder_of(tmp_path, owner, folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "address.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _drop_json(tmp_path, owner=OWNER, folder=FOLDER) -> None:
    (_folder_of(tmp_path, owner, folder) / "address.json").unlink(missing_ok=True)


def _assert_names(fault: LocatorFault, kind: str, repository: Path, *things: str) -> None:
    text = str(fault)
    assert kind in text
    assert str(repository) in text
    assert OWNER in text and FOLDER in text
    for thing in things:
        assert thing in text


def test_both_forms_are_read_by_name_with_unknown_fields_ignored(tmp_path):
    _write_json(tmp_path, {
        "session-id": "tid", "runtime": "codex",
        "unknown": {"anything": True}, "another": 7,
    })
    address = read_address(_identity(tmp_path))
    assert address == HeadAddress(runtime="codex", session_id="tid",
                                  session_name=None, permission_mode=None)

    _drop_json(tmp_path)
    _write_legacy(tmp_path, [
        "runtime: claude",
        "session-id: another-tid",
        "note: a line nobody reads",
        "session-name: the head",
    ])
    address = read_address(_identity(tmp_path))
    assert address == HeadAddress(runtime="claude", session_id="another-tid",
                                  session_name="the head", permission_mode=None)


def test_the_legacy_form_is_read_by_name_never_by_position(tmp_path):
    _write_legacy(tmp_path, [
        "a line without a colon",
        "  session-name : padded  ",
        "runtime:codex",
        "session-id: legacy-tid",
    ])
    address = read_address(_identity(tmp_path))
    assert address.runtime == "codex"  # stripped, whatever its position
    assert address.session_id == "legacy-tid"
    assert address.session_name == "padded"


def test_the_legacy_form_is_never_read_when_address_json_is_present(tmp_path):
    _write_json(tmp_path, {"session-id": "json-tid", "runtime": "codex"})
    _write_legacy(tmp_path, ["session-id: legacy-tid", "runtime: "])

    assert read_address(_identity(tmp_path)).session_id == "json-tid"


def test_a_defective_address_json_is_never_answered_from_the_legacy_form(tmp_path):
    _write_json(tmp_path, {"session-id": "json-tid"})  # no runtime: defective
    _write_legacy(tmp_path, ["session-id: legacy-tid", "runtime: codex"])

    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "missing", tmp_path, "address.json", "runtime")


def test_a_missing_runtime_is_missing_and_never_inferred(tmp_path):
    _write_json(tmp_path, {"session-id": "tid", "session-name": "a head"})
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "missing", tmp_path, "address.json", "runtime")

    _drop_json(tmp_path)
    _write_legacy(tmp_path, ["session-id: tid", "session-name: a head"])
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "missing", tmp_path, "address.md", "runtime")


def test_an_unsupported_runtime_is_malformed_or_unsupported(tmp_path):
    for form, writer in (
        ("address.json", lambda: _write_json(tmp_path, {"session-id": "tid", "runtime": "slack"})),
        ("address.md", lambda: (_drop_json(tmp_path),
                                _write_legacy(tmp_path, ["session-id: tid", "runtime: " + "slack"]))),
    ):
        writer()
        with pytest.raises(LocatorFault) as caught:
            read_address(_identity(tmp_path))
        _assert_names(caught.value, "malformed or unsupported", tmp_path, form, "runtime")


def test_a_missing_or_empty_session_id_is_a_fault(tmp_path):
    for payload, kind in (
        ({"runtime": "codex"}, "missing"),
        ({"runtime": "codex", "session-id": ""}, "malformed or unsupported"),
        ({"runtime": "codex", "session-id": 3}, "malformed or unsupported"),
        ({"runtime": "codex", "session-id": "tid", "session-name": ""}, "malformed or unsupported"),
        ({"runtime": "codex", "session-id": "tid", "session-name": None}, "malformed or unsupported"),
    ):
        _write_json(tmp_path, payload)
        with pytest.raises(LocatorFault) as caught:
            read_address(_identity(tmp_path))
        _assert_names(caught.value, kind, tmp_path, "address.json")


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_is_read_for_a_claude_address(tmp_path, mode):
    _write_json(tmp_path, {"session-id": "tid", "runtime": "claude",
                           "session-name": "the head", "permission-mode": mode})

    assert read_address(_identity(tmp_path)).permission_mode == mode


@pytest.mark.parametrize("bad_mode", ["plan ", "sudo", 3, None, True, [], "PLAN"])
def test_a_defective_mode_is_malformed_for_claude_and_ignored_for_codex(tmp_path, bad_mode):
    _write_json(tmp_path, {"session-id": "tid", "runtime": "claude",
                           "session-name": "the head", "permission-mode": bad_mode})
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "malformed or unsupported", tmp_path, "address.json", "permission-mode")

    _write_json(tmp_path, {"session-id": "tid", "runtime": "codex", "permission-mode": bad_mode})
    assert read_address(_identity(tmp_path)).permission_mode is None  # ignored for a Codex address

    _write_legacy(tmp_path, ["session-id: tid", "runtime: claude", "permission-mode: plan"])
    address = read_address(_identity(tmp_path))  # the legacy form carries no mode
    assert address.permission_mode is None


@pytest.mark.parametrize("owner,folder", [
    ("..", FOLDER), (".", FOLDER), ("", FOLDER), ("a/b", FOLDER), ("a/..", FOLDER),
    (OWNER, ".."), (OWNER, "."), (OWNER, ""), (OWNER, "a/b"),
])
def test_an_owner_or_folder_that_is_not_one_component_is_malformed(tmp_path, owner, folder):
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path, owner=owner, folder=folder))
    assert "malformed or unsupported" in str(caught.value)
    assert str(tmp_path) in str(caught.value)
    assert owner in str(caught.value) and folder in str(caught.value)


def test_no_files_present_is_missing(tmp_path):
    _folder_of(tmp_path).mkdir(parents=True)
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "missing", tmp_path, "address.json", "address.md", str(_folder_of(tmp_path)))


def test_an_address_json_that_cannot_be_read_is_unreadable(tmp_path):
    folder = _folder_of(tmp_path)
    folder.mkdir(parents=True)
    (folder / "address.json").mkdir()  # a directory, not a file
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "unreadable", tmp_path, "address.json")

    (folder / "address.json").rmdir()
    (folder / "address.json").write_bytes(b'{"session-id": "\xff\xfe"}')  # not UTF-8
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "unreadable", tmp_path, "address.json")


def test_an_address_json_that_is_not_an_object_is_malformed(tmp_path):
    folder = _folder_of(tmp_path)
    folder.mkdir(parents=True)
    (folder / "address.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "malformed or unsupported", tmp_path, "address.json")

    (folder / "address.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "malformed or unsupported", tmp_path, "address.json")


def test_a_change_between_two_calls_is_read_by_the_second(tmp_path):
    _write_json(tmp_path, {"session-id": "first", "runtime": "codex"})
    assert read_address(_identity(tmp_path)).session_id == "first"
    _write_json(tmp_path, {"session-id": "second", "runtime": "codex"})
    assert read_address(_identity(tmp_path)).session_id == "second"
    (_folder_of(tmp_path) / "address.json").unlink()
    _write_legacy(tmp_path, ["session-id: legacy", "runtime: codex"])
    assert read_address(_identity(tmp_path)).session_id == "legacy"


def test_a_symlinked_ai_factory_is_read_through(tmp_path):
    real = tmp_path / "elsewhere"
    (real / "architects" / OWNER / FOLDER).mkdir(parents=True)
    (real / "architects" / OWNER / FOLDER / "address.json").write_text(
        json.dumps({"session-id": "tid", "runtime": "claude", "session-name": "the head"}),
        encoding="utf-8",
    )
    (tmp_path / ".ai-factory").symlink_to(real, target_is_directory=True)

    address = read_address(_identity(tmp_path))

    assert address == HeadAddress(runtime="claude", session_id="tid",
                                  session_name="the head", permission_mode=None)


@pytest.mark.parametrize("field", ["owner", "folder"])
@pytest.mark.parametrize("bad", ["max\x00", "max\ud800"])
def test_a_name_that_cannot_be_a_path_is_malformed(tmp_path, field, bad):
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path,
                               owner=bad if field == "owner" else OWNER,
                               folder=bad if field == "folder" else FOLDER))

    text = str(caught.value)
    assert "malformed or unsupported" in text
    assert str(tmp_path) in text and field in text
    assert repr(bad) in text


@pytest.mark.parametrize("field", ["owner", "folder"])
@pytest.mark.parametrize("bad", ["max\x00", "max\ud800"])
def test_a_name_that_cannot_be_a_path_is_a_non_send_through_send(tmp_path, field, bad):
    from orchestrator.courier import send

    result = send(_identity(tmp_path,
                            owner=bad if field == "owner" else OWNER,
                            folder=bad if field == "folder" else FOLDER), "a text")

    assert result.sent is False
    assert "malformed or unsupported" in result.reason
    assert field in result.reason


def _beside_valid_legacy(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "address.md").write_text("session-id: legacy-tid\nruntime: codex\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["loop", "dangling"])
def test_a_defective_address_json_symlink_is_unreadable_and_never_answered_from_the_legacy_form(tmp_path, kind):
    folder = _folder_of(tmp_path)
    _beside_valid_legacy(folder)
    link = folder / "address.json"
    if kind == "loop":
        link.symlink_to(link)  # a symlink to itself
    else:
        link.symlink_to(folder / "nowhere")  # a symlink to no target

    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))

    _assert_names(caught.value, "unreadable", tmp_path, "address.json")


def test_an_address_md_that_cannot_be_read_is_unreadable(tmp_path):
    folder = _folder_of(tmp_path)
    folder.mkdir(parents=True)
    (folder / "address.md").mkdir()  # a directory in its place
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "unreadable", tmp_path, "address.md")

    (folder / "address.md").rmdir()
    (folder / "address.md").write_bytes(b"session-id: \xff\xfe")  # not UTF-8
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "unreadable", tmp_path, "address.md")


def test_json_nested_past_the_parser_s_limit_is_malformed(tmp_path):
    _write_json(tmp_path, {})  # the folder
    (_folder_of(tmp_path) / "address.json").write_text(
        "[" * 100000 + "]" * 100000, encoding="utf-8",
    )
    with pytest.raises(LocatorFault) as caught:
        read_address(_identity(tmp_path))
    _assert_names(caught.value, "malformed or unsupported", tmp_path, "address.json")


def test_a_folder_that_cannot_be_probed_is_unreadable_naming_the_folder(tmp_path):
    if tmp_path.owner() == "root":  # a root process probes through any mode
        pytest.skip("the probe barrier is a permission bit")
    folder = _folder_of(tmp_path)
    folder.mkdir(parents=True)
    (folder / "address.json").write_text(
        json.dumps({"session-id": "tid", "runtime": "codex"}), encoding="utf-8",
    )
    folder.chmod(0)
    try:
        with pytest.raises(LocatorFault) as caught:
            read_address(_identity(tmp_path))
        _assert_names(caught.value, "unreadable", tmp_path, str(folder))
    finally:
        folder.chmod(0o755)


@pytest.mark.parametrize("kind", ["unprobeable", "loop", "dangling", "md-directory", "md-bytes", "deep-json"])
def test_every_locator_fault_reaches_send_as_a_non_send(tmp_path, monkeypatch, kind):
    from orchestrator.courier import send

    folder = _folder_of(tmp_path)
    folder.mkdir(parents=True)
    if kind == "unprobeable":
        if tmp_path.owner() == "root":
            pytest.skip("the probe barrier is a permission bit")
        (folder / "address.json").write_text("{}", encoding="utf-8")
        folder.chmod(0)
        try:
            result = send(_identity(tmp_path), "a text")
        finally:
            folder.chmod(0o755)
    elif kind in ("loop", "dangling"):
        _beside_valid_legacy(folder)
        link = folder / "address.json"
        link.symlink_to(link if kind == "loop" else folder / "nowhere")
        result = send(_identity(tmp_path), "a text")
    elif kind == "md-directory":
        (folder / "address.md").mkdir()
        result = send(_identity(tmp_path), "a text")
    elif kind == "md-bytes":
        (folder / "address.md").write_bytes(b"\xff\xfe")
        result = send(_identity(tmp_path), "a text")
    else:
        (folder / "address.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        result = send(_identity(tmp_path), "a text")

    assert result.sent is False
    assert result.reason and str(tmp_path) in result.reason
