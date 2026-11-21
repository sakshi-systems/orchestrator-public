"""The Hermes agent backend: a role's turn as one call to a Hermes instance's API."""

from __future__ import annotations

import contextlib
import http.client
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import git_runner
from .errors import BaseNotOnRemoteError, NetworkError, OriginNotFetchedError
from .git_runner import run_git_sync

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


def _base_on_remote(cwd: str) -> str:
    """Return the commit the run is at, once a remote branch is known to hold it.

    A repository with no `origin` remote halts as BaseNotOnRemoteError, as does a
    fetched remote none of whose branches holds the commit; a remote that cannot
    be fetched — a failed or timed-out `git fetch origin` — halts as
    OriginNotFetchedError, and a repository with no commit at all is a defect.
    """
    resolved = run_git_sync(["rev-parse", "HEAD"], cwd=cwd)
    if resolved.returncode != 0:
        raise RuntimeError(
            "Hermes call could not resolve the project's base commit\n"
            f"{resolved.stderr.strip()[:500]}"
        )
    commit = resolved.stdout.strip()
    if run_git_sync(["remote", "get-url", "origin"], cwd=cwd).returncode != 0:
        raise BaseNotOnRemoteError(f"The base commit is not on the remote\n{commit}")
    try:
        fetched = run_git_sync(["fetch", "origin"], cwd=cwd)
    except subprocess.TimeoutExpired:
        raise OriginNotFetchedError(
            f"The remote could not be fetched\norigin\ntimed out after {git_runner.SYNC_GIT_TIMEOUT} seconds"
        )
    if fetched.returncode != 0:
        raise OriginNotFetchedError(
            f"The remote could not be fetched\norigin\n{fetched.stderr.strip()[:500]}"
        )
    contains = run_git_sync(["branch", "-r", "--contains", commit], cwd=cwd)
    if contains.returncode != 0 or not contains.stdout.strip():
        raise BaseNotOnRemoteError(f"The base commit is not on the remote\n{commit}")
    return commit


def _channel_text(cwd: str, commit: str) -> str:
    """The channel text naming the origin URL and the base commit he reads the project at."""
    resolved = run_git_sync(["remote", "get-url", "origin"], cwd=cwd)
    if resolved.returncode != 0:
        raise BaseNotOnRemoteError(f"The base commit is not on the remote\n{commit}")
    url = resolved.stdout.strip()
    return (
        f"Channel: this run is the repository at origin {url}, at commit {commit}. "
        "Read the project in your own clone at that commit — it is the base of your work."
    )


class HermesAgentBackend:
    """Hermes implementation of the agent-runtime port, for one instance.

    Carries out a role's turn as one synchronous call to the instance's API
    through the stand's tunnel. The instance's key is read from its key file
    at call time and never appears in any message, error or evidence text. He
    works in his own clone, so every call first checks that the project's base
    commit is on the remote and then names the origin URL and that commit in
    the assignment's channel text.
    """

    def __init__(self, server: str, port: int, key_file: str) -> None:
        self.server = server
        self.port = port
        self.key_file = Path(key_file)

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
    ) -> tuple[str, str]:
        key = self._read_key()
        opener = _build_opener()
        commit = _base_on_remote(cwd)
        assignment = f"{prompt}\n\n{_channel_text(cwd, commit)}"
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
