"""The answer command: a head deposits its answer for the run from standard input."""

from __future__ import annotations

import sys
from pathlib import Path

from .exchange import deposit


def cli() -> None:
    """Read one positional, the deposit path, and the answer from standard
    input as bytes, decoded as UTF-8 whatever the locale, so the deposit holds
    the head's bytes.

    Every refusal — empty input, input that is not valid UTF-8, a path not
    ending in `.answer`, a path whose directory does not exist, a deposit that
    exists, a deposit that cannot be written — prints one line on standard
    error, writes nothing and exits 1; a deposit exits 0.
    """
    if len(sys.argv) != 2:
        print("usage: orchestrator-answer <deposit path>", file=sys.stderr)
        sys.exit(1)
    answer_path = Path(sys.argv[1])
    raw = sys.stdin.buffer.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        print("the answer is not valid UTF-8", file=sys.stderr)
        sys.exit(1)
    if not text.strip():
        print("the answer is empty", file=sys.stderr)
        sys.exit(1)
    if answer_path.suffix != ".answer":
        print("the deposit path must end in .answer", file=sys.stderr)
        sys.exit(1)
    if not answer_path.parent.is_dir():
        print("the deposit path's directory does not exist", file=sys.stderr)
        sys.exit(1)
    try:
        deposit(answer_path, text)
    except FileExistsError:
        print("an answer is already deposited for this call", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"the deposit could not be written: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
