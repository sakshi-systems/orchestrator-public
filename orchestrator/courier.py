"""The orchestrator's one peer send: a head's address read fresh, the text sent
by the runtime the address names, and a typed result either way.

A send never raises its own fault: everything a call can meet — a locator that
cannot be read, an absent executable, a non-zero exit, a timeout, output that
cannot be decoded, evidence that does not prove a delivery — is a non-send
whose reason is the whole account.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import NamedTuple

from .claude_cli import _resolve_claude
from .locator import HeadIdentity, LocatorFault, read_address

SEND_TIMEOUT = 30  # seconds

_CLAUDE_PROMPT = (
    'Make exactly one SendMessage call and nothing else, to "{session_name}", '
    "with the text between the marker lines below as its message, copied exactly: "
    "every character as it stands, nothing added, removed or reworded. Then stop.\n"
    "<<<TEXT\n"
    "{text}\n"
    "TEXT>>>"
)


class SendResult(NamedTuple):
    """A send or a non-send: `reason` is None for a send, the whole account for a non-send."""

    sent: bool
    reason: str | None


def send_codex(thread_id: str, text: str) -> SendResult:
    """Send `text` to a Codex thread by its id, `codex` unresolved on PATH."""
    try:
        subprocess.run(
            ["codex", "queue", "--thread", thread_id, "--message", text],
            timeout=SEND_TIMEOUT, check=True,
            stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired:
        return SendResult(sent=False, reason=f"codex send timed out after {SEND_TIMEOUT} seconds")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return SendResult(sent=False, reason=str(exc))
    return SendResult(sent=True, reason=None)


def send(identity: HeadIdentity, text: str) -> SendResult:
    """Read a head's address fresh and send `text` by the runtime it names.

    A locator that cannot be read is a non-send whose reason is the fault's
    text. The runtime is chosen once, at one dispatch point, from the address
    alone; the text is passed verbatim.
    """
    try:
        address = read_address(identity)
    except LocatorFault as fault:
        return SendResult(sent=False, reason=str(fault))
    if address.runtime == "codex":
        return send_codex(address.session_id, text)
    return _send_claude(identity, address, text)


def _send_claude(identity: HeadIdentity, address, text: str) -> SendResult:
    """Send `text` to a Claude head by one one-shot call, judged by its evidence alone."""
    if not address.session_name:
        return SendResult(sent=False, reason=(
            f"claude address for repository {identity.repository}, owner {identity.owner!r}, "
            f"folder {identity.folder!r} has no session-name field"
        ))
    prompt = _CLAUDE_PROMPT.replace("{session_name}", address.session_name).replace("{text}", text)
    try:
        argv = [
            str(_resolve_claude()), "-p", prompt,
            "--tools", "SendMessage", "--allowedTools", "SendMessage",
            "--model", "haiku",
            "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
        ]
        if address.permission_mode is not None:
            argv += ["--permission-mode", address.permission_mode]
        completed = subprocess.run(
            argv, timeout=SEND_TIMEOUT, check=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=sys.stderr,
            text=True, encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return SendResult(sent=False, reason=f"claude send timed out after {SEND_TIMEOUT} seconds")
    except (OSError, ValueError) as exc:  # an absent claude, output that cannot be decoded
        return SendResult(sent=False, reason=str(exc))
    return _evidence_of(completed.stdout, address.session_name, text)


def _json_object(text) -> dict | None:
    """The object a text parses to, or None when it parses to no object."""
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _evidence_of(output: str, session_name: str, text: str) -> SendResult:
    """Read the send from the call's captured output, never from its exit status.

    Each line that is a JSON object is an event; an event whose message holds a
    content list contributes its blocks. A block is the call when it is a
    SendMessage tool_use whose input holds the session name and the text, both
    exact; a later block is its result when its tool_use_id is a kept id, and a
    text block of its content that parses to a JSON object with success true
    makes the send. Anything else is a non-send naming what was missing.
    """
    kept_ids: set = set()
    saw_result = False
    saw_unparsed_result = False
    failure_message: str | None = None
    for line in output.splitlines():
        event = _json_object(line)
        if event is None:
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "SendMessage":
                tool_input = block.get("input")
                call_id = block.get("id")
                if (isinstance(tool_input, dict)
                        and tool_input.get("to") == session_name
                        and tool_input.get("message") == text
                        and isinstance(call_id, str) and call_id):
                    kept_ids.add(call_id)
            elif (block.get("type") == "tool_result"
                    and isinstance(block.get("tool_use_id"), str)
                    and block.get("tool_use_id") in kept_ids):
                inner = block.get("content")
                if not isinstance(inner, list):
                    continue
                for item in inner:
                    if not isinstance(item, dict) or item.get("type") != "text":
                        continue
                    payload = _json_object(item.get("text"))
                    if payload is None:
                        saw_unparsed_result = True
                        continue
                    saw_result = True
                    if payload.get("success") is True:
                        return SendResult(sent=True, reason=None)
                    if failure_message is None and isinstance(payload.get("message"), str):
                        failure_message = payload["message"]
    if not kept_ids:
        return SendResult(sent=False, reason=(
            f"no SendMessage call to {session_name!r} with the exact text"
        ))
    if not saw_result:
        if saw_unparsed_result:
            return SendResult(sent=False, reason="the SendMessage result held no JSON object")
        return SendResult(sent=False, reason="the SendMessage call had no result")
    reason = "the SendMessage result reports no delivery"
    if failure_message is not None:
        reason = f"{reason}: {failure_message}"
    return SendResult(sent=False, reason=reason)
