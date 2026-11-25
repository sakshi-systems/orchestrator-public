"""The close step of a scenario: it closes the first phase that owes a closure
through the carrier head and its peer.

The behaviour is docs/features/phase-closure.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .errors import ClosureLeftUndisposedError, EscalationError
from .exchange import ExchangePaths
from .head_backend import HeadAgentBackend, how_to_answer
from .layout import _artifact_subdir
from .locator import HeadIdentity
from .protocol import _escalation_excerpt, _has_escalation
from .run_data import owed_phase, undisposed_entries
from .scenario_lookup import fill_template

DONE = "done"


class CloseStep:
    """The step kind `close`: sends the carrier head the filled closure
    template for the first phase that owes a closure, waits with no bound for
    the deposit and ends by what the deposit and the dispositions show."""

    def __init__(
        self,
        *,
        project_dir: Path,
        relpath: str,
        template: Path,
        heads: Mapping[str, tuple[HeadIdentity, HeadAgentBackend]],
    ) -> None:
        self._project_dir = project_dir
        self._relpath = relpath
        self._template = template
        self._heads = heads

    def __call__(self, value: str, options: Mapping[str, str]) -> str:
        phase = owed_phase(self._project_dir, self._relpath)
        if phase is None:
            return DONE
        _, carrier_backend = self._heads[value]
        peer, _ = self._heads[options["peer"]]

        segment = _artifact_subdir(self._relpath)
        directory = "closures" if segment is None else f"closures/{segment}"

        def compose(paths: ExchangePaths) -> str:
            filled = fill_template(self._template, {
                "phase": str(phase),
                "roadmap": self._relpath,
                "project": str(self._project_dir),
                "peer": f"repository: {peer.repository}, owner: {peer.owner}, folder: {peer.folder}",
            })
            return "\n".join([
                f"This is the closure of phase {phase} of the project at {self._project_dir}.",
                "",
                filled,
                "",
                how_to_answer(paths),
            ])

        text, _ = carrier_backend.call(self._project_dir, directory, str(phase), compose)
        if _has_escalation(text):
            raise EscalationError(
                f"closure of phase {phase} ({self._relpath}): {_escalation_excerpt(text)}"
            )
        left = undisposed_entries(self._project_dir, self._relpath, phase)
        if left:
            raise ClosureLeftUndisposedError(
                "\n".join(["A closure left deferred observations undisposed", f"phase {phase}", *left])
            )
        return DONE
