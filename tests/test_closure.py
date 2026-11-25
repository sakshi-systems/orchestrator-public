"""The close step over the tests' own tree and a head backend whose send and
sleep are stand-ins: what it raises, what it returns and where its call's
files lie. No real codex or claude is ever reached."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import exchange
from orchestrator.closure import CloseStep
from orchestrator.courier import SendResult
from orchestrator.errors import ClosureLeftUndisposedError, EscalationError, HaltError
from orchestrator.exchange import exchange_paths
from orchestrator.head_backend import HeadAgentBackend
from orchestrator.locator import HeadIdentity
from orchestrator.scenario_lookup import TEMPLATES_DIR
from tests import closure_tree

BUILDER = HeadIdentity(repository=Path("/repos/the-project"), owner="max", folder="builder-head")
RAZOR = HeadIdentity(repository=Path("/repos/the-project"), owner="max", folder="razor-head")
PLAIN = "I reconciled my reading against the razor's.\n"
ESCALATED = "## Escalation\n\n- a decision outside the builder's authority\n\nESCALATION\n"


class Rig:
    """The step's stand-ins: a send that records the message path its cover
    sentence carries, and a sleep that deposits the scripted answer on its
    first call — after `before` ran, where the head disposes and then answers —
    and fails the test after a fixed number of calls, so a wait that never
    ends fails and never hangs."""

    def __init__(self, project: Path, directory: str, phase: int, answer: str, before=None, limit: int = 5):
        self.paths = exchange_paths(project, directory, str(phase))
        self.answer = answer
        self.before = before
        self.limit = limit
        self.calls: list[tuple[HeadIdentity, str]] = []
        self.sent_messages: list[Path] = []
        self.sleeps = 0

    def send(self, identity, text):
        self.calls.append((identity, text))
        # The message path stands in the cover sentence; the call's files are
        # removed once its answer is read, so it is recorded during the send.
        path = Path(text.split("read ", 1)[1].split(" and follow", 1)[0])
        assert path.is_file()
        self.sent_messages.append(path)
        return SendResult(sent=True, reason=None)

    def sleep(self, seconds):
        self.sleeps += 1
        if self.sleeps == 1:
            if self.before is not None:
                self.before()
            exchange.deposit(self.paths.answer, self.answer)
        elif self.sleeps > self.limit:
            raise AssertionError("the wait never found its answer")


def _step(project: Path, relpath: str, rig: Rig) -> CloseStep:
    backend = HeadAgentBackend(BUILDER, send=rig.send, sleep=rig.sleep)
    unused = HeadAgentBackend(RAZOR, send=rig.send, sleep=rig.sleep)
    return CloseStep(
        project_dir=project,
        relpath=relpath,
        template=TEMPLATES_DIR / "closure.md",
        heads={"builder": (BUILDER, backend), "razor": (RAZOR, unused)},
    )


def _owing_project(tmp_path, relpath="ROADMAP.md"):
    roadmap = closure_tree.phase_roadmap((3, ("- [x] **3.1 — Alpha** — done",)))
    project = closure_tree.build(tmp_path, roadmap=roadmap, relpath=relpath)
    placed = closure_tree.lay_done_task(project, "3.1 — Alpha", relpath=relpath)
    return project, placed


def test_a_deposit_ending_in_escalation_raises_the_escalation_naming_the_closure(tmp_path):
    """The entry stays undisposed too; the escalation is raised before the
    undisposed halt, so the stop the head asked for is the one reported."""
    project, _ = _owing_project(tmp_path)
    rig = Rig(project, "closures", 3, ESCALATED)

    with pytest.raises(EscalationError) as info:
        _step(project, "ROADMAP.md", rig)("builder", {"peer": "razor"})

    assert not isinstance(info.value, ClosureLeftUndisposedError)
    assert "closure of phase 3" in str(info.value)
    assert "ROADMAP.md" in str(info.value)


def test_an_entry_left_undisposed_halts_naming_the_phase_and_the_entry(tmp_path):
    project, placed = _owing_project(tmp_path)
    rig = Rig(project, "closures", 3, PLAIN)

    with pytest.raises(ClosureLeftUndisposedError) as info:
        _step(project, "ROADMAP.md", rig)("builder", {"peer": "razor"})

    assert isinstance(info.value, HaltError)
    lines = str(info.value).splitlines()
    assert lines[0] == "A closure left deferred observations undisposed"
    assert "phase 3" in lines[1:]
    assert placed.entry in lines[1:]


def test_an_entry_the_head_disposes_before_it_answers_ends_done(tmp_path):
    """The check after the deposit reads the state the head left, not the
    state the step began in."""
    project, placed = _owing_project(tmp_path)
    rig = Rig(project, "closures", 3, PLAIN, before=lambda: closure_tree.dispose(project, placed))

    assert _step(project, "ROADMAP.md", rig)("builder", {"peer": "razor"}) == "done"


def test_the_default_roadmap_s_call_lies_under_closures_and_the_phase(tmp_path):
    project, _ = _owing_project(tmp_path)
    rig = Rig(project, "closures", 3, PLAIN)

    with pytest.raises(ClosureLeftUndisposedError):
        _step(project, "ROADMAP.md", rig)("builder", {"peer": "razor"})

    (message,) = rig.sent_messages
    assert message == exchange_paths(project, "closures", "3").message
    assert message.parent == exchange._ROOT / exchange._digest_of(project) / "closures"
    assert message.name == "3.message.md"
    (identity, _), = rig.calls
    assert identity == BUILDER


def test_a_named_roadmap_s_call_lies_under_closures_its_stem_and_the_phase(tmp_path):
    project, _ = _owing_project(tmp_path, relpath="roadmaps/named.md")
    rig = Rig(project, "closures/named", 3, PLAIN)

    with pytest.raises(ClosureLeftUndisposedError):
        _step(project, "roadmaps/named.md", rig)("builder", {"peer": "razor"})

    (message,) = rig.sent_messages
    assert message == exchange_paths(project, "closures/named", "3").message
    assert message.parent == exchange._ROOT / exchange._digest_of(project) / "closures" / "named"


def test_a_roadmap_with_nothing_owed_ends_done_with_no_send(tmp_path):
    roadmap = closure_tree.phase_roadmap((3, ("- [x] **3.1 — Alpha** — done",)))
    project = closure_tree.build(tmp_path, roadmap=roadmap)
    closure_tree.lay_done_task(project, "3.1 — Alpha", variant=closure_tree.MARKER)
    rig = Rig(project, "closures", 3, PLAIN)

    assert _step(project, "ROADMAP.md", rig)("builder", {"peer": "razor"}) == "done"
    assert rig.calls == []
