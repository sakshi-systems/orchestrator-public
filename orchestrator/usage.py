"""Selected usage-threshold policies for Claude output and Codex account windows."""

from __future__ import annotations

import math
import re
from pathlib import Path

from .claude_cli import _run_usage_probe
from .codex_cli import _read_rate_limits
from .config import OrchestratorConfig
from .errors import HaltError

SESSION_PATTERN = r"Current session:\s+(\d+(?:\.\d+)?)%"
WEEKLY_PATTERN = r"Current week \(all models\):\s+(\d+(?:\.\d+)?)%"


def _is_quota_number(value: object) -> bool:
    """Accept finite JSON numbers without treating booleans as usage."""
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _check_codex_usage_limits(config: OrchestratorConfig, project_dir: Path) -> None:
    """Check each supported observed Codex account window before a task."""
    unavailable = "  [usage check: Codex quota precheck unavailable; continuing without quota protection]"
    try:
        snapshot = _read_rate_limits(str(project_dir))
    except Exception:
        print(unavailable)
        return

    bucket = None
    if isinstance(snapshot, dict):
        buckets = snapshot.get("rateLimitsByLimitId")
        if buckets is None:
            bucket = snapshot.get("rateLimits")
        elif isinstance(buckets, dict):
            bucket = buckets.get("codex")

    observations = []
    if isinstance(bucket, dict) and bucket.get("limitId") in (None, "codex"):
        for slot in ("primary", "secondary"):
            window = bucket.get(slot)
            if not isinstance(window, dict):
                continue
            percentage = window.get("usedPercent")
            duration = window.get("windowDurationMins")
            if not _is_quota_number(percentage) or percentage < 0:
                continue
            if not _is_quota_number(duration):
                continue
            if duration == 300:
                observations.append(("five-hour", percentage, config.usage_threshold_5h))
            elif duration == 10080:
                observations.append(("weekly", percentage, config.usage_threshold_weekly))

    if not observations:
        print(unavailable)
        return

    parts = [f"{name} {pct if type(pct) is int else f'{pct:.0f}'}%" for name, pct, _ in observations]
    print(f"  [Codex usage: {' · '.join(parts)}]")
    for name, percentage, threshold in observations:
        if percentage >= threshold:
            raise HaltError(
                "Codex usage threshold reached\n"
                f"{name} usage at {percentage}% (threshold: {threshold}%)."
            )


def _parse_pct(text: str, pattern: str) -> float | None:
    """Return the first captured group of pattern as float, or None if no match."""
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def _check_usage_limits(config: OrchestratorConfig) -> None:
    """Run `claude /usage`, log current usage, and stop if either threshold is breached."""
    try:
        output = _run_usage_probe()
    except Exception:
        print("  [usage check: could not parse output, continuing]")
        return

    session_pct = _parse_pct(output, SESSION_PATTERN)
    weekly_pct = _parse_pct(output, WEEKLY_PATTERN)

    parts = []
    if session_pct is not None:
        parts.append(f"session {session_pct:.0f}%")
    if weekly_pct is not None:
        parts.append(f"week {weekly_pct:.0f}%")
    if parts:
        print(f"  [usage: {' · '.join(parts)}]")
    else:
        print("  [usage check: could not parse output, continuing]")
        return

    session_threshold = config.usage_threshold_5h
    weekly_threshold = config.usage_threshold_weekly

    if session_pct is not None and session_pct >= session_threshold:
        raise HaltError(
            f"Session usage at {session_pct:.0f}% — stopping (threshold: {session_threshold:.0f}%)."
        )
    if weekly_pct is not None and weekly_pct >= weekly_threshold:
        raise HaltError(
            f"Weekly usage at {weekly_pct:.0f}% — stopping (threshold: {weekly_threshold:.0f}%)."
        )
