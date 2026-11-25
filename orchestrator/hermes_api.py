"""The Hermes agent backend: a role's turn as one call to a Hermes instance's API."""

from __future__ import annotations

import contextlib
import http.client
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import git_runner
from .errors import BaseNotOnRemoteError, HaltError, NetworkError, OriginNotFetchedError
from .git_runner import run_git_sync
from .ports import Repository

MAX_ATTEMPTS = 3
RETRY_DELAY = 30  # seconds
TIMEOUT_SECONDS = 3600

_SESSION_HEADER = "X-Hermes-Session-Id"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that follows nothing, so the key reaches no host but the instance's."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    """Build the opener of a call: no proxy of the environment, no redirect followed."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())


def _base_on_remote(repository: Repository, base: str) -> str:
    """Return the recorded base commit once origin is known to hold it.

    The commit is the one the task's record holds for the repository, handed
    by the caller — it is checked, never re-read from the repository's `HEAD`.
    A repository with no `origin` remote halts as BaseNotOnRemoteError, as does
    a fetched origin none of whose remote-tracking refs holds the commit — a
    commit only another remote's refs hold does not pass; a remote that cannot
    be fetched — a failed or timed-out `git fetch origin` — halts as
    OriginNotFetchedError. Each halt names the repository by its label on a
    line of its own after the first, so a stand without access to one of the
    tree's origins reads which repository the halt is of.
    """
    cwd = str(repository.directory)
    label = repository.label
    commit = base
    if run_git_sync(["remote", "get-url", "origin"], cwd=cwd).returncode != 0:
        raise BaseNotOnRemoteError(
            f"The base commit is not on the remote\n{label}\n{commit}"
        )
    try:
        fetched = run_git_sync(["fetch", "origin"], cwd=cwd)
    except subprocess.TimeoutExpired:
        raise OriginNotFetchedError(
            f"The remote could not be fetched\n{label}\norigin\n"
            f"timed out after {git_runner.SYNC_GIT_TIMEOUT} seconds"
        )
    if fetched.returncode != 0:
        raise OriginNotFetchedError(
            f"The remote could not be fetched\n{label}\norigin\n{fetched.stderr.strip()[:500]}"
        )
    contains = run_git_sync(
        ["for-each-ref", "--contains", commit, "refs/remotes/origin/"], cwd=cwd
    )
    if contains.returncode != 0 or not contains.stdout.strip():
        raise BaseNotOnRemoteError(
            f"The base commit is not on the remote\n{label}\n{commit}"
        )
    return commit


def _channel_text(tree: tuple[Repository, ...], commits: tuple[str, ...]) -> str:
    """The channel text naming each repository of the run's tree, its origin
    URL and the base commit his clone of it starts at.

    Each repository is named by its label, one sentence from one template
    whether the tree holds one repository or two, with `commits` holding the
    recorded base commit the call checked for each, in the tree's order. A URL
    with a scheme is named without its userinfo — everything before the last
    `@` of the authority — so no credential the URL holds reaches him as
    conversation content; a URL without a scheme, a local path or an scp form,
    is named as it is. Where the tree holds the repository `.ai-factory` lies
    in, a further sentence tells him where its folder goes in his clone of
    the project's repository and to keep that placed folder out of the
    commits of that clone.
    """
    sentences = []
    for repository, commit in zip(tree, commits):
        resolved = run_git_sync(
            ["remote", "get-url", "origin"], cwd=str(repository.directory)
        )
        if resolved.returncode != 0:
            raise BaseNotOnRemoteError(
                f"The base commit is not on the remote\n{repository.label}\n{commit}"
            )
        url = resolved.stdout.strip()
        if "://" in url:
            scheme, netloc, path, query, fragment = urllib.parse.urlsplit(url)
            url = urllib.parse.urlunsplit(
                (scheme, netloc.rpartition("@")[2], path, query, fragment)
            )
        sentences.append(
            f"This run is {repository.label} at origin {url}, at commit {commit}; "
            "your clone of it starts at that commit, the base of the work."
        )
    parts = ["Channel: " + " ".join(sentences)]
    if any(repository.folder is not None for repository in tree):
        parts.append(
            "Put the folder of the repository holding `.ai-factory` — the whole clone "
            "where its folder is empty — where your clone of the project's repository "
            "expects `.ai-factory`, and keep that placed folder out of the commits of "
            "your clone of the project's repository."
        )
    parts.append(
        "A commit or ref this message names is on its repository's origin: "
        "fetch it before you read it."
    )
    return " ".join(parts)


class HermesAgentBackend:
    """Hermes implementation of the agent-runtime port, for one instance.

    Carries out a role's turn as one synchronous call to the instance's API
    through the stand's tunnel. The instance's key is read from its key file
    at call time and never appears in any message, error or evidence text. He
    works in his own clone, built from each repository of the run's tree the
    backend is handed, so every call first checks the task's recorded base
    commit of each repository — the one the call is handed, the same on every
    call of the task — on its own origin, the first failure halting, and then
    names each repository, its origin URL without any credential it holds
    and that commit in the assignment's channel text. A call handed no
    recorded bases halts as a task with no record to work from, and the
    bases pair with the tree strictly, so a tuple that does not cover the
    whole tree fails rather than naming part of it.
    """

    def __init__(
        self, server: str, port: int, key_file: str, tree: tuple[Repository, ...]
    ) -> None:
        self.server = server
        self.port = port
        self.key_file = Path(key_file)
        self.tree = tree

    def _read_key(self) -> str:
        """Read the instance's key at call time and validate it.

        A key file that cannot be read is a configuration fault naming the path;
        a key that could not be sent — bytes that are not UTF-8, empty, with
        interior whitespace, a line break or an ASCII control character, or with
        a character outside latin-1 — is a fault naming the path and a fixed
        reason, never the key. The invalid-key fault is raised outside any
        exception handler, so it carries no context and no cause.
        """
        try:
            raw = self.key_file.read_bytes()
        except OSError as e:
            raise RuntimeError(
                "Hermes instance key file could not be read\n"
                f"{self.key_file}\n{e}"
            ) from e
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            reason = "bytes that are not UTF-8"
        else:
            key = text.strip()
            if not key:
                reason = "no key"
            elif any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in key):
                reason = "interior whitespace or a line break"
            else:
                try:
                    key.encode("latin-1")
                except UnicodeEncodeError:
                    reason = "a character outside latin-1"
                else:
                    return key
        raise RuntimeError(
            "Hermes instance key file holds an invalid key\n"
            f"{self.key_file}\n{reason}"
        )

    def run(
        self,
        *,
        role,
        prompt: str,
        cwd: str,
        instructions: str,
        session_id: str | None = None,
        exchange: str | None = None,
        bases: tuple[str, ...] | None = None,
    ) -> tuple[str, str]:
        key = self._read_key()
        opener = _build_opener()
        # Each repository's recorded base is checked on its own origin, in the
        # tree's order, before any attempt runs: the first failure halts the
        # call, naming the repository it failed on. The commits come from the
        # record, paired with the tree in order, so every call of the task
        # names the same base; a call that carries no record halts here — the
        # run writes the bases at the task's start — and the pairing is
        # strict, so a bases that does not cover the whole tree fails loudly
        # instead of silently naming only part of it.
        if bases is None:
            raise HaltError("The task's base is not recorded")
        commits = tuple(
            _base_on_remote(repository, base)
            for repository, base in zip(self.tree, bases, strict=True)
        )
        assignment = f"{prompt}\n\n{_channel_text(self.tree, commits)}"
        session = session_id if session_id is not None else str(uuid.uuid4())

        for attempt in range(1, MAX_ATTEMPTS + 1):
            # The system message precedes the user message on every attempt of a
            # new session: an attempt that failed by a refused connection, a
            # timeout or a 5xx may never have reached his server, and a 200 is
            # the only confirmation that the session carries the instructions.
            # A continued session already carries them.
            if session_id is None:
                messages = [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": assignment},
                ]
            else:
                messages = [{"role": "user", "content": assignment}]

            body = json.dumps({"model": "hermes-agent", "messages": messages}).encode("utf-8")
            request = urllib.request.Request(
                f"http://{self.server}:{self.port}/v1/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    _SESSION_HEADER: session,
                    "Content-Type": "application/json",
                },
                method="POST",
            )

            try:
                with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                    echoed = response.headers.get(_SESSION_HEADER)
                    raw = response.read()
                    return self._answer(raw, echoed), echoed
            except urllib.error.HTTPError as e:
                status = e.code
                if status >= 500:
                    if attempt < MAX_ATTEMPTS:
                        self._wait(attempt)
                        continue
                    raise NetworkError(
                        "Hermes transport failure persisted through retries\n"
                        f"instance at {self.server}:{self.port}\n"
                        f"last status: {status}"
                    )
                raw = None
                with contextlib.suppress(OSError, ValueError, http.client.HTTPException):
                    raw = e.read()
                self._defect_status(status, raw)
            except (OSError, http.client.HTTPException) as e:
                # A refused connection, a timeout, a reset or an incomplete read
                # of the body — a transport failure with no answer.
                if attempt < MAX_ATTEMPTS:
                    self._wait(attempt)
                    continue
                raise NetworkError(
                    "Hermes transport failure persisted through retries\n"
                    f"instance at {self.server}:{self.port}\n"
                    f"last fault: {type(e).__name__}: {e}"
                )

        raise NetworkError(
            "Hermes transport failure persisted through retries\n"
            f"instance at {self.server}:{self.port}"
        )

    def _wait(self, attempt: int) -> None:
        print(f">>> Hermes transport failure, retrying in {RETRY_DELAY}s (attempt {attempt}/{MAX_ATTEMPTS})...")
        time.sleep(RETRY_DELAY)

    def _answer(self, raw: bytes, echoed_session: str | None) -> str:
        """Read a 200 answer: the first choice's message content, under the echoed session id."""
        if not echoed_session:
            raise RuntimeError(
                "Hermes response carries no session id\n"
                f"instance at {self.server}:{self.port}"
            )
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as e:
            raise RuntimeError(
                "Hermes response is not valid JSON\n"
                f"instance at {self.server}:{self.port}"
            ) from e
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RuntimeError(
                "Hermes response carries no choices\n"
                f"instance at {self.server}:{self.port}"
            )
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise RuntimeError(
                "Hermes response carries no message content\n"
                f"instance at {self.server}:{self.port}"
            )
        return content

    def _defect_status(self, status: int, raw: bytes | None) -> None:
        """Surface an error status as a defect naming the status and the error's code, never the key."""
        code = None
        if raw:
            with contextlib.suppress(OSError, ValueError):
                payload = json.loads(raw.decode("utf-8", "replace"))
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = error["code"]
        detail = f"code: {code}" if code is not None else "code: (none in the response)"
        raise RuntimeError(
            "Hermes instance answered with an error status\n"
            f"status: {status}\n"
            f"{detail}"
        )
