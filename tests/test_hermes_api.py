"""The Hermes agent backend: a role's turn as one measured call to a local stand-in of his API."""

import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from orchestrator import git_runner
from orchestrator import hermes_api
from orchestrator.errors import BaseNotOnRemoteError, NetworkError, OriginNotFetchedError
from orchestrator.hermes_api import HermesAgentBackend
from orchestrator.ports import AgentRole, Repository
from tests.conftest import git_in
from tests.test_iteration_ref import _linked_project

KEY = "stand-key-never-to-appear"


class _Stand:
    """A local threaded HTTP stand-in for one Hermes instance, scripted per response.

    Each request is served on its own thread and recorded when it arrives; a test
    that waits for the Nth arrival waits on the condition the handler notifies,
    under a bound that fails.
    """

    def __init__(self, port: int = 0):
        self.requests = []
        self.script = []
        self.serve_sleep = 0.0
        self.truncate_body = 0
        self.condition = threading.Condition()
        handler = type("Handler", (_Handler,), {"stand": self})
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def record(self, request):
        with self.condition:
            self.requests.append(request)
            self.condition.notify_all()

    def wait_for(self, count, timeout=10.0):
        """Wait until `count` requests have arrived, or fail under the bound."""
        with self.condition:
            deadline = time.monotonic() + timeout
            while len(self.requests) < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"the stand saw {len(self.requests)} of {count} arrivals"
                self.condition.wait(remaining)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def reply(self, status=200, body=None, session="echoed-session", raw=None, headers=None):
        self.script.append((status, body, session, raw, headers or {}))


class _Handler(BaseHTTPRequestHandler):
    stand: _Stand

    def do_GET(self):
        time.sleep(self.stand.serve_sleep)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        self.stand.record(SimpleNamespace(
            path=self.path,
            authorization=self.headers.get("Authorization"),
            session=self.headers.get("X-Hermes-Session-Id"),
            content_type=self.headers.get("Content-Type"),
            body=body,
        ))
        if self.stand.serve_sleep:
            time.sleep(self.stand.serve_sleep)
        status, body, session, raw_body, extra_headers = (
            self.stand.script.pop(0) if self.stand.script
            else (200, {}, "echoed-session", None, {})
        )
        payload = raw_body if raw_body is not None else json.dumps(body).encode()
        self.send_response(status)
        for name, value in extra_headers.items():
            self.send_header(name, value)
        if session is not None:
            self.send_header("X-Hermes-Session-Id", session)
        self.send_header("Content-Type", "application/json")
        length = len(payload) + self.stand.truncate_body
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if self.stand.truncate_body:
            self.wfile.write(payload[: len(payload) // 2])
            self.close_connection = True
        else:
            self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def stand():
    a_stand = _Stand()
    yield a_stand
    a_stand.close()


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "keys" / "main"
    path.parent.mkdir()
    path.write_text(f"{KEY}\n")
    return path


@pytest.fixture
def project(tmp_path):
    origin = tmp_path / "origin.git"
    git_in(tmp_path, "init", "--bare", "-b", "main", str(origin))
    root = tmp_path / "project"
    git_in(tmp_path, "clone", str(origin), str(root))
    (root / "file.txt").write_text("the run's work\n")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-m", "the base")
    commit = git_in(root, "rev-parse", "HEAD").strip()
    git_in(root, "push", "origin", f"HEAD:refs/heads/main")
    return SimpleNamespace(
        root=root, commit=commit, origin=str(origin),
        tree=(Repository(directory=root, folder=None),),
    )


def _backend(stand, key_file, tree):
    return HermesAgentBackend(stand.host, stand.port, str(key_file), tree)


def _answer(content="his answer", session="echoed-session"):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_a_new_session_sends_the_instructions_and_returns_the_echoed_session(project, stand, key_file):
    stand.reply(body=_answer(), session="a-session-of-his-own")
    backend = _backend(stand, key_file, project.tree)

    text, session = backend.run(
        role=AgentRole.PLANNER, prompt="Plan the task.", cwd=str(project.root),
        instructions="the role's instructions", session_id=None,
    )

    assert text == "his answer"
    assert session == "a-session-of-his-own"
    (request,) = stand.requests
    assert request.path == "/v1/chat/completions"
    assert request.authorization == f"Bearer {KEY}"
    assert request.content_type == "application/json"
    assert request.body["model"] == "hermes-agent"
    assert [m["role"] for m in request.body["messages"]] == ["system", "user"]
    assert request.body["messages"][0]["content"] == "the role's instructions"
    user = request.body["messages"][1]["content"]
    assert user.startswith("Plan the task.\n\nChannel: ")
    assert project.origin in user and project.commit in user
    uuid.UUID(request.session)  # a new session name is a uuid


def test_a_continued_session_sends_the_user_message_alone(project, stand, key_file):
    stand.reply(body=_answer(), session="the-given-session")
    backend = _backend(stand, key_file, project.tree)

    text, session = backend.run(
        role=AgentRole.CODE_REVIEWER, prompt="Review the changes.", cwd=str(project.root),
        instructions="the role's instructions", session_id="the-given-session",
    )

    assert (text, session) == ("his answer", "the-given-session")
    (request,) = stand.requests
    assert request.session == "the-given-session"
    assert [m["role"] for m in request.body["messages"]] == ["user"]
    assert request.body["messages"][0]["content"].startswith("Review the changes.\n\nChannel: ")


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_authorization_is_a_defect_naming_the_status_and_code(project, stand, key_file, status):
    stand.reply(
        status=status,
        body={"error": {"message": "bad bearer", "type": "gateway_auth_error", "code": "gateway_auth_failed"}},
        session=None,
    )
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(RuntimeError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    message = str(caught.value)
    assert f"status: {status}" in message
    assert "code: gateway_auth_failed" in message
    assert KEY not in message
    assert len(stand.requests) == 1


def test_an_error_status_without_a_code_names_its_absence(project, stand, key_file):
    stand.reply(status=400, body={"error": {"message": "nope"}}, session=None)
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(RuntimeError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    assert "status: 400" in str(caught.value)
    assert "code: (none in the response)" in str(caught.value)


def test_a_5xx_is_retried_with_the_instructions_and_then_answers(project, stand, key_file, monkeypatch):
    monkeypatch.setattr(hermes_api, "RETRY_DELAY", 0)
    stand.reply(status=500, body={}, session=None)
    stand.reply(status=502, body={}, session=None)
    stand.reply(body=_answer("third attempt's answer"), session="echoed-session")
    backend = _backend(stand, key_file, project.tree)

    text, session = backend.run(
        role=AgentRole.PLAN_REVIEWER, prompt="p", cwd=str(project.root),
        instructions="i", session_id=None,
    )

    assert (text, session) == ("third attempt's answer", "echoed-session")
    stand.wait_for(3)
    assert [[m["role"] for m in r.body["messages"]] for r in stand.requests] == [
        ["system", "user"], ["system", "user"], ["system", "user"],
    ]
    assert all(r.body["messages"][0]["content"] == "i" for r in stand.requests)
    sessions = {r.session for r in stand.requests}
    assert len(sessions) == 1 and uuid.UUID(sessions.pop())


def test_a_continued_session_s_retries_carry_no_instructions(project, stand, key_file, monkeypatch):
    monkeypatch.setattr(hermes_api, "RETRY_DELAY", 0)
    stand.reply(status=500, body={}, session=None)
    stand.reply(body=_answer(), session="the-given-session")
    backend = _backend(stand, key_file, project.tree)

    text, session = backend.run(
        role=AgentRole.PLAN_REVIEWER, prompt="p", cwd=str(project.root),
        instructions="i", session_id="the-given-session",
    )

    assert (text, session) == ("his answer", "the-given-session")
    stand.wait_for(2)
    assert [[m["role"] for m in r.body["messages"]] for r in stand.requests] == [["user"], ["user"]]


def test_a_refused_connection_persists_into_a_network_halt_and_waits_between_attempts(
    project, stand, key_file, monkeypatch,
):
    monkeypatch.setattr(hermes_api, "RETRY_DELAY", 0)
    sleeps = []
    monkeypatch.setattr(hermes_api, "time", SimpleNamespace(sleep=sleeps.append))
    stand.close()
    backend = HermesAgentBackend(stand.host, stand.port, str(key_file), project.tree)

    with pytest.raises(NetworkError):
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    assert len(sleeps) == 2  # a wait between each of the three attempts


def test_a_timeout_persists_into_a_network_halt_the_stand_saw_every_attempt_of(
    project, stand, key_file, monkeypatch,
):
    monkeypatch.setattr(hermes_api, "RETRY_DELAY", 0)
    monkeypatch.setattr(hermes_api, "TIMEOUT_SECONDS", 0.2)
    stand.serve_sleep = 1.0
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(NetworkError):
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="the instructions")

    stand.wait_for(3)
    assert len(stand.requests) == 3
    for request in stand.requests:
        assert [m["role"] for m in request.body["messages"]] == ["system", "user"]
        assert request.body["messages"][0]["content"] == "the instructions"


def test_a_5xx_through_all_attempts_halts_as_a_network_failure(project, stand, key_file, monkeypatch):
    monkeypatch.setattr(hermes_api, "RETRY_DELAY", 0)
    for _ in range(3):
        stand.reply(status=500, body={}, session=None)
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(NetworkError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    assert "last status: 500" in str(caught.value)
    stand.wait_for(3)
    assert len(stand.requests) == 3


def test_an_incomplete_body_is_a_transport_failure(project, stand, key_file, monkeypatch):
    monkeypatch.setattr(hermes_api, "RETRY_DELAY", 0)
    stand.truncate_body = 32  # a Content-Length larger than the body, then the connection closes
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(NetworkError):
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="the instructions")

    stand.wait_for(3)
    assert len(stand.requests) == 3
    for request in stand.requests:
        assert [m["role"] for m in request.body["messages"]] == ["system", "user"]
        assert request.body["messages"][0]["content"] == "the instructions"


def test_a_damaged_error_body_still_names_its_status(project, stand, key_file):
    """A 401 whose body cannot be read — truncated past its Content-Length — surfaces
    as the defect naming the status, with no code."""
    stand.truncate_body = 32
    stand.reply(
        status=401,
        body={"error": {"message": "bad bearer", "type": "gateway_auth_error", "code": "gateway_auth_failed"}},
        session=None,
    )
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(RuntimeError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    message = str(caught.value)
    assert "status: 401" in message
    assert "code: (none in the response)" in message
    stand.wait_for(1)
    assert len(stand.requests) == 1


def test_an_undecodable_key_file_is_an_invalid_key(project, stand, key_file):
    key_file.write_bytes(b"stand-key-\xe9-stand-key\n")
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(RuntimeError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    error = caught.value
    message = str(error)
    lines = message.splitlines()
    assert lines[0] == "Hermes instance key file holds an invalid key"
    assert lines[1] == str(key_file)
    assert lines[2] == "bytes that are not UTF-8"
    assert "0xe9" not in message and "position" not in message
    assert "stand-key" not in message
    assert error.__context__ is None and error.__cause__ is None
    assert stand.requests == []


def test_the_first_attempt_refused_and_the_second_answered_carries_the_instructions(
    project, key_file, monkeypatch,
):
    """The case the every-attempt rule is about: the first attempt finds the port
    closed, the stand comes up on it from the wait between attempts, and the
    attempt it serves still carries the role's instructions."""
    monkeypatch.setattr(hermes_api, "RETRY_DELAY", 0)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    launched = {}

    def launch_stand(_seconds):
        if "stand" not in launched:
            a_stand = _Stand(port=port)
            a_stand.reply(body=_answer("answered on the retry"), session="echoed-session")
            launched["stand"] = a_stand

    monkeypatch.setattr(hermes_api, "time", SimpleNamespace(sleep=launch_stand))
    backend = HermesAgentBackend("127.0.0.1", port, str(key_file), project.tree)

    try:
        text, session = backend.run(
            role=AgentRole.PLANNER, prompt="p", cwd=str(project.root),
            instructions="the role's instructions", session_id=None,
        )

        assert text == "answered on the retry"
        stand = launched["stand"]
        stand.wait_for(1)
        (request,) = stand.requests
        assert [m["role"] for m in request.body["messages"]] == ["system", "user"]
        assert request.body["messages"][0]["content"] == "the role's instructions"
        assert request.body["messages"][1]["content"].startswith("p\n\nChannel: ")
    finally:
        if "stand" in launched:
            launched["stand"].close()


def test_a_response_with_no_echoed_session_is_a_defect(project, stand, key_file):
    stand.reply(body=_answer(), session=None)
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(RuntimeError, match="no session id"):
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")


def test_a_response_with_no_choices_is_a_defect(project, stand, key_file):
    stand.reply(body={"usage": {}}, session="echoed-session")
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(RuntimeError, match="no choices"):
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")


def test_a_response_with_no_string_content_is_a_defect(project, stand, key_file):
    stand.reply(body={"choices": [{"message": {"content": None}}]}, session="echoed-session")
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(RuntimeError, match="no message content"):
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")


def test_an_empty_answer_is_returned_as_it_is(project, stand, key_file):
    stand.reply(body=_answer(content="  \n"), session="echoed-session")
    backend = _backend(stand, key_file, project.tree)

    text, _ = backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    assert text == "  \n"


def test_an_unreadable_key_file_is_a_configuration_fault_naming_the_path(project, stand, tmp_path):
    missing = tmp_path / "keys" / "absent"
    backend = HermesAgentBackend(stand.host, stand.port, str(missing), project.tree)

    with pytest.raises(RuntimeError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    assert str(missing) in str(caught.value)
    assert stand.requests == []


@pytest.mark.parametrize("key,reason", [
    ("stand-key\na comment line\n", "interior whitespace or a line break"),
    ("stand key\n", "interior whitespace or a line break"),
    ("stand\x00key\n", "interior whitespace or a line break"),
    ("ключ-стенда\n", "a character outside latin-1"),
    ("\n", "no key"),
])
def test_an_invalid_key_is_a_fault_naming_the_path_and_reason_and_never_the_key(
    project, stand, key_file, key, reason,
):
    key_file.write_text(key)
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(RuntimeError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    message = str(caught.value)
    lines = message.splitlines()
    assert lines[0] == "Hermes instance key file holds an invalid key"
    assert lines[1] == str(key_file)
    assert lines[2] == reason
    for line in key.splitlines():
        if line.strip():
            assert line.strip() not in message
    assert stand.requests == []


def test_a_trailing_line_break_still_works(project, stand, key_file):
    stand.reply(body=_answer(), session="echoed-session")
    backend = _backend(stand, key_file, project.tree)
    key_file.write_text(f"{KEY}\n\n")

    text, _ = backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    assert text == "his answer"
    assert stand.requests[0].authorization == f"Bearer {KEY}"


def test_no_proxy_is_honoured_and_the_key_stays_with_the_instance(project, stand, key_file, monkeypatch):
    proxy = _Stand()
    try:
        monkeypatch.setenv("http_proxy", f"http://{proxy.host}:{proxy.port}")
        monkeypatch.setenv("HTTP_PROXY", f"http://{proxy.host}:{proxy.port}")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        stand.reply(body=_answer(), session="echoed-session")
        backend = _backend(stand, key_file, project.tree)

        text, _ = backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

        assert text == "his answer"
        stand.wait_for(1)
        assert proxy.requests == []
        assert stand.requests[0].authorization == f"Bearer {KEY}"
    finally:
        proxy.close()


def test_a_redirect_is_a_defect_naming_its_status_and_reaches_no_other_host(project, stand, key_file):
    elsewhere = _Stand()
    try:
        stand.reply(
            status=302, session=None,
            headers={"Location": f"http://{elsewhere.host}:{elsewhere.port}/v1/chat/completions"},
        )
        backend = _backend(stand, key_file, project.tree)

        with pytest.raises(RuntimeError) as caught:
            backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

        assert "status: 302" in str(caught.value)
        stand.wait_for(1)
        assert elsewhere.requests == []
    finally:
        elsewhere.close()


def test_a_base_commit_no_remote_branch_holds_halts(project, stand, key_file):
    (project.root / "more.txt").write_text("unpushed work\n")
    git_in(project.root, "add", "-A")
    git_in(project.root, "commit", "-m", "not pushed")
    commit = git_in(project.root, "rev-parse", "HEAD").strip()
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(BaseNotOnRemoteError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    lines = str(caught.value).splitlines()
    assert lines[0] == "The base commit is not on the remote"
    assert lines[1] == "the project's repository"
    assert lines[2] == commit
    assert stand.requests == []


def test_a_base_commit_only_another_remote_holds_halts(project, stand, key_file):
    (project.root / "elsewhere.txt").write_text("another remote's work\n")
    git_in(project.root, "add", "-A")
    git_in(project.root, "commit", "-m", "pushed to upstream only")
    commit = git_in(project.root, "rev-parse", "HEAD").strip()
    upstream = project.root.parent / "upstream.git"
    git_in(project.root.parent, "init", "--bare", "-b", "main", str(upstream))
    git_in(project.root, "remote", "add", "upstream", str(upstream))
    git_in(project.root, "push", "upstream", "HEAD:refs/heads/main")
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(BaseNotOnRemoteError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    lines = str(caught.value).splitlines()
    assert lines[0] == "The base commit is not on the remote"
    assert lines[1] == "the project's repository"
    assert lines[2] == commit
    assert stand.requests == []


def test_the_channel_names_a_url_with_credentials_without_them(project):
    git_in(project.root, "remote", "set-url", "origin", "https://user:token@example.test/r.git")
    text = hermes_api._channel_text(project.tree, (project.commit,))
    assert "https://example.test/r.git" in text
    assert "user" not in text
    assert "token" not in text
    assert "@" not in text


@pytest.mark.parametrize("url", [
    "https://example.test:8443/r.git",
    "git@example.test:org/r.git",
])
def test_the_channel_names_urls_without_a_scheme_or_credentials_as_they_are(project, url):
    git_in(project.root, "remote", "set-url", "origin", url)
    text = hermes_api._channel_text(project.tree, (project.commit,))
    assert url in text


def test_the_channel_of_a_tree_of_one_names_the_project_s_repository(project):
    """One repository in the tree: one sentence from the template, its label,
    its origin and its base, no placement sentence, and the fetch sentence."""
    text = hermes_api._channel_text(project.tree, (project.commit,))
    assert text == (
        "Channel: "
        f"This run is the project's repository at origin {project.origin}, at commit {project.commit}; "
        "your clone of it starts at that commit, the base of the work. "
        "A commit or ref this message names is on its repository's origin: "
        "fetch it before you read it."
    )


def test_the_channel_names_each_repository_of_a_tree_of_two(tmp_path):
    """Two repositories in the tree: one sentence per repository from the same
    template, in the tree's order — the project's first, each named by its
    label with its own origin and base — then the placement sentence and the
    fetch sentence."""
    project, shared, shared_base, _origin = _linked_project(tmp_path)
    project_commit = git_in(project, "rev-parse", "HEAD").strip()
    text = hermes_api._channel_text(
        (
            Repository(directory=project, folder=None),
            Repository(directory=shared, folder="ai/p1/"),
        ),
        (project_commit, shared_base),
    )
    assert text == (
        "Channel: "
        f"This run is the project's repository at origin {project.parent / 'project-origin.git'}, "
        f"at commit {project_commit}; "
        "your clone of it starts at that commit, the base of the work. "
        "This run is the repository holding `.ai-factory`, its folder `ai/p1/` "
        f"at origin {shared.parent / 'shared-origin.git'}, at commit {shared_base}; "
        "your clone of it starts at that commit, the base of the work. "
        "Put the folder of the repository holding `.ai-factory` — the whole clone "
        "where its folder is empty — where your clone of the project's repository "
        "expects `.ai-factory`, and keep that placed folder out of the commits of "
        "your clone of the project's repository. "
        "A commit or ref this message names is on its repository's origin: "
        "fetch it before you read it."
    )


def test_the_channel_of_an_ai_factory_that_is_its_own_repository_places_the_whole_clone(tmp_path):
    """The other repository's empty folder — `.ai-factory` a repository of its
    own — is still the tree's second repository: the label names its root and
    the placement sentence stays, saying the whole clone goes where the
    project expects `.ai-factory`."""
    project, shared, shared_base, _origin = _linked_project(tmp_path)
    project_commit = git_in(project, "rev-parse", "HEAD").strip()
    text = hermes_api._channel_text(
        (
            Repository(directory=project, folder=None),
            Repository(directory=shared, folder=""),
        ),
        (project_commit, shared_base),
    )
    assert text == (
        "Channel: "
        f"This run is the project's repository at origin {project.parent / 'project-origin.git'}, "
        f"at commit {project_commit}; "
        "your clone of it starts at that commit, the base of the work. "
        "This run is the repository holding `.ai-factory`, which is its root "
        f"at origin {shared.parent / 'shared-origin.git'}, at commit {shared_base}; "
        "your clone of it starts at that commit, the base of the work. "
        "Put the folder of the repository holding `.ai-factory` — the whole clone "
        "where its folder is empty — where your clone of the project's repository "
        "expects `.ai-factory`, and keep that placed folder out of the commits of "
        "your clone of the project's repository. "
        "A commit or ref this message names is on its repository's origin: "
        "fetch it before you read it."
    )


def test_the_channel_names_each_origin_of_a_tree_of_two_without_its_credentials(tmp_path):
    """Each repository's origin is stripped as a URL with a scheme is: no
    credential either URL holds reaches him."""
    project, shared, shared_base, _origin = _linked_project(tmp_path)
    git_in(project, "remote", "set-url", "origin", "https://user:token@project.example/r.git")
    git_in(shared, "remote", "set-url", "origin", "https://other:secret@shared.example/r.git")
    text = hermes_api._channel_text(
        (
            Repository(directory=project, folder=None),
            Repository(directory=shared, folder="ai/p1/"),
        ),
        (git_in(project, "rev-parse", "HEAD").strip(), shared_base),
    )
    assert "https://project.example/r.git" in text
    assert "https://shared.example/r.git" in text
    for credential in ("user", "token", "other", "secret", "@"):
        assert credential not in text


@pytest.mark.parametrize("role", list(AgentRole))
def test_every_role_s_channel_tells_him_where_his_clone_starts_and_to_fetch(
    project, stand, key_file, role,
):
    stand.reply(body=_answer(), session="echoed-session")
    backend = _backend(stand, key_file, project.tree)

    backend.run(role=role, prompt="the role's prompt", cwd=str(project.root), instructions="i")

    (request,) = stand.requests
    user = request.body["messages"][-1]["content"]
    assert user.startswith("the role's prompt\n\nChannel: ")
    assert (
        "A commit or ref this message names is on its repository's origin: fetch it before you read it."
        in user
    )
    assert "Read the project in your own clone" not in user


def test_his_assignment_names_each_repository_of_the_run_s_tree(tmp_path, stand, key_file):
    """A call over a tree of two carries each repository's sentence, in the
    tree's order, the placement sentence and the fetch sentence."""
    project, shared, shared_base, _origin = _linked_project(tmp_path)
    stand.reply(body=_answer(), session="echoed-session")
    backend = _backend(stand, key_file, (
        Repository(directory=project, folder=None),
        Repository(directory=shared, folder="ai/p1/"),
    ))

    backend.run(role=AgentRole.PLANNER, prompt="Plan the task.", cwd=str(project), instructions="i")

    (request,) = stand.requests
    user = request.body["messages"][-1]["content"]
    assert user.startswith("Plan the task.\n\nChannel: This run is the project's repository at origin ")
    assert (
        "This run is the repository holding `.ai-factory`, its folder `ai/p1/` at origin "
        in user
    )
    assert str(project.parent / "project-origin.git") in user
    assert str(shared.parent / "shared-origin.git") in user and shared_base in user
    assert (
        "Put the folder of the repository holding `.ai-factory` — the whole clone "
        "where its folder is empty — where your clone of the project's repository "
        "expects `.ai-factory`, and keep that placed folder out of the commits of "
        "your clone of the project's repository." in user
    )
    assert user.endswith(
        "A commit or ref this message names is on its repository's origin: "
        "fetch it before you read it."
    )


def test_the_second_repository_s_base_is_checked_on_its_own_origin(tmp_path, stand, key_file):
    """The run's tree is checked repository by repository: the project's base
    on its origin passes, and the other repository's unpushed base halts the
    call naming that commit, before any request leaves."""
    project, shared, _base, _origin = _linked_project(tmp_path)
    (shared / "ai" / "p1" / "late.md").write_text("unpushed\n")
    git_in(shared, "add", "-A")
    git_in(shared, "commit", "-m", "not pushed")
    commit = git_in(shared, "rev-parse", "HEAD").strip()
    backend = _backend(stand, key_file, (
        Repository(directory=project, folder=None),
        Repository(directory=shared, folder="ai/p1/"),
    ))

    with pytest.raises(BaseNotOnRemoteError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project), instructions="i")

    lines = str(caught.value).splitlines()
    assert lines[1] == "the repository holding `.ai-factory`, its folder `ai/p1/`"
    assert lines[2] == commit
    assert stand.requests == []


def test_a_tree_of_two_halts_at_the_first_repository_whose_base_is_missing(tmp_path, stand, key_file):
    """Both bases off their origins: the halt names the first repository of
    the tree, the project's."""
    project, shared, _base, _origin = _linked_project(tmp_path)
    (project / "late.txt").write_text("unpushed\n")
    git_in(project, "add", "-A")
    git_in(project, "commit", "-m", "not pushed")
    (shared / "ai" / "p1" / "late.md").write_text("unpushed\n")
    git_in(shared, "add", "-A")
    git_in(shared, "commit", "-m", "not pushed")
    commit = git_in(project, "rev-parse", "HEAD").strip()
    backend = _backend(stand, key_file, (
        Repository(directory=project, folder=None),
        Repository(directory=shared, folder="ai/p1/"),
    ))

    with pytest.raises(BaseNotOnRemoteError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project), instructions="i")

    lines = str(caught.value).splitlines()
    assert lines[1] == "the project's repository"
    assert lines[2] == commit
    assert stand.requests == []


def test_a_repository_with_no_origin_halts_at_the_first_call(tmp_path, stand, key_file):
    root = tmp_path / "lonely"
    git_in(tmp_path, "init", "-b", "main", str(root))
    (root / "file.txt").write_text("work\n")
    git_in(root, "add", "-A")
    git_in(root, "commit", "-m", "base")
    backend = _backend(stand, key_file, (Repository(directory=root, folder=None),))

    with pytest.raises(BaseNotOnRemoteError):
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(root), instructions="i")

    assert stand.requests == []


def test_a_remote_that_cannot_be_fetched_halts_as_its_own_fault(project, stand, key_file):
    import os

    vanished = f"{project.origin}-gone"
    os.rename(project.origin, vanished)
    backend = _backend(stand, key_file, project.tree)

    with pytest.raises(OriginNotFetchedError) as caught:
        backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

    lines = str(caught.value).splitlines()
    assert lines[0] == "The remote could not be fetched"
    assert lines[1] == "the project's repository"
    assert lines[2] == "origin"
    assert stand.requests == []


def test_a_fetch_that_times_out_halts_as_its_own_fault(project, stand, key_file, monkeypatch):
    hanging = _Stand()
    hanging.serve_sleep = 5.0
    try:
        monkeypatch.setattr(git_runner, "SYNC_GIT_TIMEOUT", 0.5)
        git_in(project.root, "remote", "set-url", "origin", f"http://{hanging.host}:{hanging.port}/origin.git")
        backend = _backend(stand, key_file, project.tree)

        with pytest.raises(OriginNotFetchedError) as caught:
            backend.run(role=AgentRole.PLANNER, prompt="p", cwd=str(project.root), instructions="i")

        lines = str(caught.value).splitlines()
        assert lines[0] == "The remote could not be fetched"
        assert lines[1] == "the project's repository"
        assert lines[2] == "origin"
        assert "timed out after" in lines[3]
        assert stand.requests == []
    finally:
        hanging.close()
