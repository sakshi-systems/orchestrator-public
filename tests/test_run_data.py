"""The `show` command over a built project tree: what the orchestrator holds
for a task and for a phase, printed as versioned JSON, compared byte for
byte, and the refusals' lines and status."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from orchestrator.observations import entry_id
from orchestrator.run_data import owed_phase, undisposed_entries
from tests import closure_tree
from tests.conftest import git_environment
from tests.run_tree import build

REPO = Path(__file__).resolve().parents[1]

ALPHA = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "ROADMAP.md",
  "task": {
    "address": "1.1",
    "title": "1.1 — Alpha",
    "state": "done"
  },
  "steps": [
    {
      "step": "plan",
      "rounds": [
        {
          "round": 1,
          "text": "# Plan: 1.1 — Alpha\n\nLay the greeting at the door.\n\n## Steps\n\n1. Write the greeting module.\n"
        }
      ]
    },
    {
      "step": "plan-review",
      "rounds": [
        {
          "round": 1,
          "text": "The plan names its steps and the module they touch.\n\nPLAN_REVIEW_PASS\n"
        },
        {
          "round": 2,
          "text": "An earlier attempt of this task stopped here once.\n\n## Deferred observations\n\n- Affects: unknown — The greeting module holds no word of the licence its helper wraps. [dismissed]\n- Affects: Phase 2 — The doorway's width is a later phase's own concern. [promoted → .ai-factory/ROADMAP.md]\n\n## Escalation\n\n- A leftover stop the resumed task walked past.\n\nESCALATION\n"
        }
      ]
    },
    {
      "step": "implement",
      "rounds": [
        {
          "round": 1,
          "text": "Implemented the greeting module and its test.\n\nDEVIATION: nothing — the plan held as written.\n"
        }
      ]
    },
    {
      "step": "review",
      "rounds": [
        {
          "round": 1,
          "text": "No findings.\n\n## Deferred observations\n\n- Affects: .ai-factory/specs/trickster77777/0288-deferred-observations-are-read-as-entries-with-dispositions.md — The reading joins an entry by its affects and its text only, where the skills joined them by gist.\n  A remark reworded in a later review is another entry.\n- Affects: unknown — The greeting module holds no word of the licence its helper wraps.\n\nREVIEW_PASS\n"
        }
      ]
    }
  ],
  "escalation": null,
  "observations": [
    {
      "entry": "d1d68a294e0dcd71f5634cbf2ef9006874c82ef6f471e09598ac435a0affaa9b",
      "affects": "unknown",
      "text": "The greeting module holds no word of the licence its helper wraps.",
      "sources": [
        {
          "step": "plan-review",
          "round": 2
        },
        {
          "step": "review",
          "round": 1
        }
      ],
      "disposition": {
        "kind": "dismissed"
      }
    },
    {
      "entry": "14ff5da6375ce8c769aca277560f8a6dd75ec572121edc0e2d43adc17d2e9c43",
      "affects": "Phase 2",
      "text": "The doorway's width is a later phase's own concern.",
      "sources": [
        {
          "step": "plan-review",
          "round": 2
        }
      ],
      "disposition": {
        "kind": "legacy"
      }
    },
    {
      "entry": "d15fb7d6629c8d96efd7a365939134f8df49295f57c46fd85df1c6274695746a",
      "affects": ".ai-factory/specs/trickster77777/0288-deferred-observations-are-read-as-entries-with-dispositions.md",
      "text": "The reading joins an entry by its affects and its text only, where the skills joined them by gist.\n  A remark reworded in a later review is another entry.",
      "sources": [
        {
          "step": "review",
          "round": 1
        }
      ],
      "disposition": {
        "kind": "routed",
        "target": {
          "spec": ".ai-factory/specs/trickster77777/0300-a-spec-a-routed-remark-lands-in.md"
        }
      }
    }
  ],
  "positions": []
}
"""

BETA = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "ROADMAP.md",
  "task": {
    "address": "1.2",
    "title": "1.2 — Beta",
    "state": "escalated"
  },
  "steps": [
    {
      "step": "plan",
      "rounds": [
        {
          "round": 1,
          "text": "# Plan: 1.2 — Beta\n\nChoose how the door locks.\n\n## Steps\n\n1. Add the lock helper.\n"
        }
      ]
    },
    {
      "step": "plan-review",
      "rounds": [
        {
          "round": 1,
          "text": "An earlier attempt of this task stopped here once.\n\n## Escalation\n\n- A leftover stop the resumed task walked past.\n\nESCALATION\n"
        }
      ]
    },
    {
      "step": "implement",
      "rounds": [
        {
          "round": 1,
          "text": "Added the lock helper behind the door type.\n\nDEVIATION: nothing — the plan held as written.\n"
        }
      ]
    },
    {
      "step": "review",
      "rounds": [
        {
          "round": 1,
          "text": "## Review\n\nThe lock helper is sound, but the licence of the helper it wraps is not\nsomething the reviewer can choose for the project.\n\n## Deferred observations\n\n- Affects: Phase 2 — The doorway's width belongs to a later phase's plan. [routed → .ai-factory/ROADMAP.md § Phase 2]\n- Affects: .ai-factory/specs/trickster77777/0285-the-skills-reach-a-runs-data-only-through-the-orchestrators-commands.md — The commands' deposit needs its writer spelled. [routed → .ai-factory/specs/trickster77777/0290-orchestrator-dispose-and-the-closure-deposit.md]\n- Affects: unknown — An old note whose marker no live vocabulary names. [audit-corroborated]\n\n## Escalation\n\n- The licence of the wrapped helper is a decision outside the reviewer's authority.\n- The decision belongs to whoever holds the project.\n\nESCALATION\n"
        }
      ]
    }
  ],
  "escalation": {
    "role": "code_reviewer",
    "step": "review",
    "round": 1,
    "text": "## Escalation\n\n- The licence of the wrapped helper is a decision outside the reviewer's authority.\n- The decision belongs to whoever holds the project.\n\nESCALATION"
  },
  "observations": [
    {
      "entry": "2ebb04be9f304b70aea0acabfd19e482ec451498344cd814bb558f0455db1086",
      "affects": "Phase 2",
      "text": "The doorway's width belongs to a later phase's plan.",
      "sources": [
        {
          "step": "review",
          "round": 1
        }
      ],
      "disposition": {
        "kind": "fixed"
      }
    },
    {
      "entry": "967fc759a94a680659026b874894b631ca8568d5d0c580fcbe14a769e4c813af",
      "affects": ".ai-factory/specs/trickster77777/0285-the-skills-reach-a-runs-data-only-through-the-orchestrators-commands.md",
      "text": "The commands' deposit needs its writer spelled.",
      "sources": [
        {
          "step": "review",
          "round": 1
        }
      ],
      "disposition": {
        "kind": "routed",
        "target": {
          "spec": ".ai-factory/specs/trickster77777/0290-orchestrator-dispose-and-the-closure-deposit.md"
        }
      }
    },
    {
      "entry": "e5403ac051e860900bb4655524fc856b4aa5b37831a1c9afd861ac344f954368",
      "affects": "unknown",
      "text": "An old note whose marker no live vocabulary names.",
      "sources": [
        {
          "step": "review",
          "round": 1
        }
      ],
      "disposition": {
        "kind": "legacy"
      }
    }
  ],
  "positions": [
    "start",
    "verify",
    "implement"
  ]
}
"""

GAMMA = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "ROADMAP.md",
  "task": {
    "address": "1.3",
    "title": "1.3 — Gamma",
    "state": "underway"
  },
  "steps": [
    {
      "step": "plan",
      "rounds": [
        {
          "round": 1,
          "text": "# Plan: 1.3 — Gamma\n\nWiden the doorway.\n\n## Steps\n\n1. Widen the frame constant.\n"
        }
      ]
    },
    {
      "step": "plan-review",
      "rounds": [
        {
          "round": 1,
          "text": "The plan names its steps.\n\nPLAN_REVIEW_PASS\n"
        }
      ]
    },
    {
      "step": "implement",
      "rounds": [
        {
          "round": 1,
          "text": "Widened the frame constant and its test.\n\nDEVIATION: nothing — the plan held as written.\n"
        }
      ]
    }
  ],
  "escalation": null,
  "observations": [],
  "positions": [
    "start",
    "verify",
    "implement"
  ]
}
"""

DELTA = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "ROADMAP.md",
  "task": {
    "address": "1.4",
    "title": "1.4 — Delta",
    "state": "unstarted"
  },
  "steps": [],
  "escalation": null,
  "observations": [],
  "positions": []
}
"""

ZETA = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "ROADMAP.md",
  "task": {
    "address": "2.2",
    "title": "2.2 — Zeta",
    "state": "unstarted"
  },
  "steps": [],
  "escalation": null,
  "observations": [],
  "positions": []
}
"""

PHASE_ONE = r"""{
  "version": 1,
  "scope": "phase",
  "roadmap": "ROADMAP.md",
  "phase": 1,
  "tasks": [
    {
      "address": "1.1",
      "title": "1.1 — Alpha",
      "state": "done",
      "escalation": null,
      "observations": [
        {
          "entry": "d1d68a294e0dcd71f5634cbf2ef9006874c82ef6f471e09598ac435a0affaa9b",
          "affects": "unknown",
          "text": "The greeting module holds no word of the licence its helper wraps.",
          "sources": [
            {
              "step": "plan-review",
              "round": 2
            },
            {
              "step": "review",
              "round": 1
            }
          ],
          "disposition": {
            "kind": "dismissed"
          }
        },
        {
          "entry": "14ff5da6375ce8c769aca277560f8a6dd75ec572121edc0e2d43adc17d2e9c43",
          "affects": "Phase 2",
          "text": "The doorway's width is a later phase's own concern.",
          "sources": [
            {
              "step": "plan-review",
              "round": 2
            }
          ],
          "disposition": {
            "kind": "legacy"
          }
        },
        {
          "entry": "d15fb7d6629c8d96efd7a365939134f8df49295f57c46fd85df1c6274695746a",
          "affects": ".ai-factory/specs/trickster77777/0288-deferred-observations-are-read-as-entries-with-dispositions.md",
          "text": "The reading joins an entry by its affects and its text only, where the skills joined them by gist.\n  A remark reworded in a later review is another entry.",
          "sources": [
            {
              "step": "review",
              "round": 1
            }
          ],
          "disposition": {
            "kind": "routed",
            "target": {
              "spec": ".ai-factory/specs/trickster77777/0300-a-spec-a-routed-remark-lands-in.md"
            }
          }
        }
      ]
    },
    {
      "address": "1.1a — Refit Alpha's bell",
      "title": "1.1a — Refit Alpha's bell",
      "state": "done",
      "escalation": null,
      "observations": []
    },
    {
      "address": "1.2",
      "title": "1.2 — Beta",
      "state": "escalated",
      "escalation": {
        "role": "code_reviewer",
        "step": "review",
        "round": 1,
        "text": "## Escalation\n\n- The licence of the wrapped helper is a decision outside the reviewer's authority.\n- The decision belongs to whoever holds the project.\n\nESCALATION"
      },
      "observations": [
        {
          "entry": "2ebb04be9f304b70aea0acabfd19e482ec451498344cd814bb558f0455db1086",
          "affects": "Phase 2",
          "text": "The doorway's width belongs to a later phase's plan.",
          "sources": [
            {
              "step": "review",
              "round": 1
            }
          ],
          "disposition": {
            "kind": "fixed"
          }
        },
        {
          "entry": "967fc759a94a680659026b874894b631ca8568d5d0c580fcbe14a769e4c813af",
          "affects": ".ai-factory/specs/trickster77777/0285-the-skills-reach-a-runs-data-only-through-the-orchestrators-commands.md",
          "text": "The commands' deposit needs its writer spelled.",
          "sources": [
            {
              "step": "review",
              "round": 1
            }
          ],
          "disposition": {
            "kind": "routed",
            "target": {
              "spec": ".ai-factory/specs/trickster77777/0290-orchestrator-dispose-and-the-closure-deposit.md"
            }
          }
        },
        {
          "entry": "e5403ac051e860900bb4655524fc856b4aa5b37831a1c9afd861ac344f954368",
          "affects": "unknown",
          "text": "An old note whose marker no live vocabulary names.",
          "sources": [
            {
              "step": "review",
              "round": 1
            }
          ],
          "disposition": {
            "kind": "legacy"
          }
        }
      ]
    },
    {
      "address": "1.3",
      "title": "1.3 — Gamma",
      "state": "underway",
      "escalation": null,
      "observations": []
    },
    {
      "address": "1.4",
      "title": "1.4 — Delta",
      "state": "unstarted",
      "escalation": null,
      "observations": []
    }
  ]
}
"""

PHASE_TWO = r"""{
  "version": 1,
  "scope": "phase",
  "roadmap": "ROADMAP.md",
  "phase": 2,
  "tasks": [
    {
      "address": "2.1",
      "title": "2.1 — Epsilon",
      "state": "unstarted",
      "escalation": null,
      "observations": []
    },
    {
      "address": "2.2",
      "title": "2.2 — Zeta",
      "state": "unstarted",
      "escalation": null,
      "observations": []
    }
  ]
}
"""

COVER = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "ROADMAP_TESTS.md",
  "task": {
    "address": "Cover Alpha",
    "title": "Cover Alpha",
    "state": "underway"
  },
  "steps": [
    {
      "step": "plan",
      "rounds": [
        {
          "round": 1,
          "text": "# Plan: Cover Alpha\n\nCover the greeting module with tests.\n\n## Test Command\n\n`python -m pytest tests/test_greeting.py`\n\n## Steps\n\n1. Write the cases.\n"
        }
      ]
    },
    {
      "step": "plan-review",
      "rounds": [
        {
          "round": 1,
          "text": "The cases miss the empty input.\n\n"
        },
        {
          "round": 2,
          "text": "The cases cover the empty input now.\n\nPLAN_REVIEW_PASS\n"
        }
      ]
    },
    {
      "step": "implement",
      "rounds": [
        {
          "round": 1,
          "text": "Wrote the cases, the empty input included.\n\nDEVIATION: nothing — the plan held as written.\n"
        }
      ]
    },
    {
      "step": "test-run",
      "rounds": [
        {
          "round": 1,
          "text": "$ python -m pytest tests/test_greeting.py\nexit code: 1\nelapsed: 0.2s\n\n1 failed, 2 passed\n"
        }
      ]
    }
  ],
  "escalation": null,
  "observations": [],
  "positions": [
    "start",
    "verify",
    "implement"
  ]
}
"""

ETA = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "roadmaps/named.md",
  "task": {
    "address": "7.1",
    "title": "7.1 — Eta",
    "state": "underway"
  },
  "steps": [
    {
      "step": "plan",
      "rounds": [
        {
          "round": 1,
          "text": "# Plan: 7.1 — Eta\n\nPaint the named door.\n\n## Steps\n\n1. Paint the door.\n"
        }
      ]
    },
    {
      "step": "plan-review",
      "rounds": [
        {
          "round": 1,
          "text": "The plan names its steps.\n\n## Deferred observations\n\n- Affects: Phase 2 — the doorway's width belongs to a later phase's plan.\n\nPLAN_REVIEW_PASS\n"
        }
      ]
    },
    {
      "step": "implement",
      "rounds": [
        {
          "round": 1,
          "text": "Painted the door.\n\nDEVIATION: nothing — the plan held as written.\n"
        }
      ]
    }
  ],
  "escalation": null,
  "observations": [
    {
      "entry": "4148e4ae8a7aab4a1f4babe8074e7261c097dc11bda32c31a9e04c56eeebc23c",
      "affects": "Phase 2",
      "text": "the doorway's width belongs to a later phase's plan.",
      "sources": [
        {
          "step": "plan-review",
          "round": 1
        }
      ],
      "disposition": null
    }
  ],
  "positions": [
    "start",
    "verify",
    "implement"
  ]
}
"""

MIGRATION = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "ROADMAP_TESTS.md",
  "task": {
    "address": "2.0 migration notes",
    "title": "2.0 migration notes",
    "state": "unstarted"
  },
  "steps": [],
  "escalation": null,
  "observations": [],
  "positions": []
}
"""


GAMMA_AGAIN = r"""{
  "version": 1,
  "scope": "task",
  "roadmap": "ROADMAP.md",
  "task": {
    "address": "1.3",
    "title": "1.3 — Gamma",
    "state": "underway"
  },
  "steps": [
    {
      "step": "plan",
      "rounds": [
        {
          "round": 1,
          "text": "# Plan: 1.3 — Gamma\n\nThe second attempt's plan.\n"
        }
      ]
    }
  ],
  "escalation": null,
  "observations": [],
  "positions": [
    "start",
    "implement"
  ]
}
"""


def _show(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Run the command as a client runs it, against the tree built under
    `root`, with the machine's base the tree carries and git's
    configuration files silenced, so the command reads no config of the
    machine it runs on."""
    environment = git_environment()
    environment["ORCHESTRATOR_CONFIG"] = str(root / "base.json")
    return subprocess.run(
        [sys.executable, "-m", "orchestrator.main", "show", *arguments],
        cwd=REPO, env=environment, capture_output=True,
    )


def _project(tmp_path: Path) -> Path:
    return build(tmp_path)


def _assert_shown(project: Path, expected: str, *arguments: str) -> None:
    result = _show(project.parent, *arguments)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout == expected.encode("utf-8")


def test_the_done_task_shows_every_step_and_its_rounds(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, ALPHA, "--project-dir", str(project), "1.1")


def test_the_escalated_task_shows_its_escalation(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, BETA, "--project-dir", str(project), "1.2")


def test_the_underway_task_shows_the_steps_it_reached(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, GAMMA, "--project-dir", str(project), "1.3")


def test_the_unstarted_task_shows_where_it_stands_and_no_steps(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, DELTA, "--project-dir", str(project), "1.4")


def test_a_task_past_the_breakpoint_is_addressed(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, ZETA, "--project-dir", str(project), "2.2")


def test_a_phase_lists_a_letter_closed_done_line_by_its_whole_title(tmp_path):
    """A done line a letter closes carries no number, so the phase lists
    it by its whole title as its address, as a flat task."""
    project = _project(tmp_path)
    result = _show(project.parent, "--project-dir", str(project), "--phase", "1")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    shown = json.loads(result.stdout)
    listed = [task for task in shown["tasks"] if task["address"] == "1.1a — Refit Alpha's bell"]
    assert listed == [
        {
            "address": "1.1a — Refit Alpha's bell",
            "title": "1.1a — Refit Alpha's bell",
            "state": "done",
            "escalation": None,
            "observations": [],
        }
    ]


def test_a_phase_shows_each_task_s_standing(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, PHASE_ONE, "--project-dir", str(project), "--phase", "1")


def test_a_phase_lists_the_tasks_past_the_breakpoint(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, PHASE_TWO, "--project-dir", str(project), "--phase", "2")


def test_where_every_group_is_finished_the_highest_numbered_one_answers(tmp_path):
    project = _project(tmp_path)
    plans = project / ".ai-factory" / "plans"
    (plans / "03-1-3-gamma.json").write_text('{"step": "done"}', encoding="utf-8")
    (plans / "05-1-3-gamma.md").write_text("# Plan: 1.3 — Gamma\n\nThe second attempt's plan.\n", encoding="utf-8")
    (plans / "05-1-3-gamma.json").write_text('{"step": "done"}', encoding="utf-8")
    _assert_shown(project, GAMMA_AGAIN, "--project-dir", str(project), "1.3")


def test_the_flat_task_of_the_test_sibling_shows_its_test_run(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, COVER, "--project-dir", str(project), "--roadmap", "ROADMAP_TESTS.md", "Cover Alpha")


def test_a_named_roadmap_s_task_shows_its_own_artifacts(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, ETA, "--project-dir", str(project), "--roadmap", "roadmaps/named.md", "7.1")


def test_the_dotted_alias_of_the_default_roadmap_reaches_its_run_data(tmp_path):
    """The alias './ROADMAP.md' answers with the object the plain spelling
    answers with: the default roadmap's flat namespace, the 'roadmap' field
    itself the normalized spelling."""
    project = _project(tmp_path)
    _assert_shown(project, ALPHA, "--project-dir", str(project), "--roadmap", "./ROADMAP.md", "1.1")


def test_a_flat_title_that_begins_like_a_number_is_addressed_by_its_title(tmp_path):
    project = _project(tmp_path)
    _assert_shown(project, MIGRATION, "--project-dir", str(project), "--roadmap", "ROADMAP_TESTS.md", "2.0 migration notes")


def test_a_leftover_escalating_artifact_is_not_the_task_s_escalation(tmp_path):
    """Pinned by the done task's bytes above: its second plan review once
    escalated, the record is closed, and `escalation` is null — the artifact
    stays a round of its step and nothing else."""
    project = _project(tmp_path)
    result = _show(project.parent, "--project-dir", str(project), "1.1")
    shown = json.loads(result.stdout)
    assert shown["task"]["state"] == "done"
    assert [round_["round"] for round_ in shown["steps"][1]["rounds"]] == [1, 2]
    assert shown["escalation"] is None


def test_an_older_escalating_artifact_does_not_displace_the_recorded_stop(tmp_path):
    """Pinned by the escalated task's bytes above: the record names the
    review, and a plan review of an earlier attempt carries the marker as
    well — the reported stop is the reviewer's, not the plan reviewer's."""
    project = _project(tmp_path)
    result = _show(project.parent, "--project-dir", str(project), "1.2")
    shown = json.loads(result.stdout)
    assert shown["escalation"]["role"] == "code_reviewer"
    assert shown["escalation"]["step"] == "review"
    assert shown["escalation"]["round"] == 1


def test_a_record_that_names_no_path_falls_back_to_the_scan(tmp_path):
    project = _project(tmp_path)
    # The group's earlier plan review no longer escalates, so the first round
    # in run order whose last lines carry the marker is the review.
    (project / ".ai-factory" / "plan-reviews" / "02-1-2-beta-plan-review-1.md").write_text(
        "The plan names its steps.\n\nPLAN_REVIEW_PASS\n", encoding="utf-8",
    )
    sidecar = project / ".ai-factory" / "plans" / "02-1-2-beta.json"
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    record["escalation"] = "reviewer: a record a hand wrote without its artifact"
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    result = _show(project.parent, "--project-dir", str(project), "1.2")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    shown = json.loads(result.stdout)
    assert shown["escalation"]["step"] == "review"
    assert shown["escalation"]["round"] == 1


def test_a_record_that_names_no_round_of_the_group_reports_no_escalation(tmp_path):
    project = _project(tmp_path)
    sidecar = project / ".ai-factory" / "plans" / "02-1-2-beta.json"
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    record["escalation"] = "reviewer: gone (reviews/09-gone-review-1.md)"
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    result = _show(project.parent, "--project-dir", str(project), "1.2")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    shown = json.loads(result.stdout)
    assert shown["task"]["state"] == "escalated"
    assert shown["escalation"] is None


def _refuses(project: Path, line: str, *arguments: str) -> None:
    result = _show(project.parent, *arguments)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == f"{line}\n".encode("utf-8")


def test_a_missing_selector_refuses(tmp_path):
    project = _project(tmp_path)
    _refuses(project, "A selector is missing: give a task or --phase", "--project-dir", str(project))


def test_a_doubled_selector_refuses(tmp_path):
    project = _project(tmp_path)
    _refuses(
        project, "A selector is doubled: give a task or --phase, not both",
        "--project-dir", str(project), "1.1", "--phase", "1",
    )


def test_an_absolute_roadmap_value_refuses(tmp_path):
    project = _project(tmp_path)
    _refuses(
        project, "Invalid '--roadmap' value: '/etc/passwd' (must be a relative path with no '..' segments)",
        "--project-dir", str(project), "--roadmap", "/etc/passwd", "1.1",
    )


def test_a_roadmap_value_with_a_dotdot_segment_refuses(tmp_path):
    project = _project(tmp_path)
    _refuses(
        project, "Invalid '--roadmap' value: '../ROADMAP.md' (must be a relative path with no '..' segments)",
        "--project-dir", str(project), "--roadmap", "../ROADMAP.md", "1.1",
    )


def test_a_roadmap_that_does_not_exist_refuses(tmp_path):
    project = _project(tmp_path)
    _refuses(
        project, "The roadmap does not exist: .ai-factory/roadmaps/absent.md",
        "--project-dir", str(project), "--roadmap", "roadmaps/absent.md", "7.1",
    )


def test_a_task_the_roadmap_does_not_hold_refuses(tmp_path):
    project = _project(tmp_path)
    _refuses(project, "No task '9.9' in the roadmap", "--project-dir", str(project), "9.9")


def test_a_flat_title_standing_on_two_lines_refuses_naming_them(tmp_path):
    project = _project(tmp_path)
    _refuses(
        project, "Task 'Cover the parser' stands on more than one line of the roadmap: lines 6, 7",
        "--project-dir", str(project), "--roadmap", "ROADMAP_TESTS.md", "Cover the parser",
    )


def test_a_phase_the_roadmap_does_not_hold_refuses(tmp_path):
    project = _project(tmp_path)
    _refuses(project, "No phase 9 in the roadmap", "--project-dir", str(project), "--phase", "9")


def test_a_malformed_phase_selector_refuses(tmp_path):
    project = _project(tmp_path)
    _refuses(project, "No phase 'x' in the roadmap", "--project-dir", str(project), "--phase", "x")


def test_a_missing_option_value_refuses_as_one_line(tmp_path):
    project = _project(tmp_path)
    result = _show(project.parent, "--project-dir", str(project), "--phase")
    assert result.returncode == 1
    assert result.stdout == b""
    stderr = result.stderr.decode("utf-8")
    assert len(stderr.splitlines()) == 1
    assert stderr == "argument --phase: expected one argument\n"


def test_a_second_task_selector_refuses_as_one_line(tmp_path):
    project = _project(tmp_path)
    _refuses(project, "unrecognized arguments: extra", "--project-dir", str(project), "1.1", "extra")


def test_an_unknown_option_refuses_as_one_line(tmp_path):
    project = _project(tmp_path)
    _refuses(project, "unrecognized arguments: --bogus", "--project-dir", str(project), "--bogus", "1.1")


def test_a_refusal_of_the_configuration_prints_its_first_line(tmp_path):
    project = _project(tmp_path)
    (tmp_path / "base.json").write_text('{"max_iterations": 3}', encoding="utf-8")
    result = _show(project.parent, "--project-dir", str(project), "1.1")
    assert result.returncode == 1
    assert result.stdout == b""
    stderr = result.stderr.decode("utf-8")
    assert len(stderr.splitlines()) == 1
    assert stderr.startswith("Invalid 'max_iterations' in ")
    assert "(a value that shapes a run; it belongs in " in stderr


def test_an_escalation_without_its_heading_gives_the_whole_answer(tmp_path):
    project = _project(tmp_path)
    review = project / ".ai-factory" / "reviews" / "02-1-2-beta-review-1.md"
    review.write_text(
        "The licence of the wrapped helper is a decision outside the reviewer's authority.\n\nESCALATION\n",
        encoding="utf-8",
    )
    result = _show(project.parent, "--project-dir", str(project), "1.2")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    shown = json.loads(result.stdout)
    assert shown["escalation"]["text"] == (
        "The licence of the wrapped helper is a decision outside the reviewer's authority.\n\nESCALATION\n"
    )


def test_a_halt_over_the_run_s_data_prints_its_first_line(tmp_path):
    project = _project(tmp_path)
    (project / ".ai-factory" / "plans" / "03-1-3-gamma.json").write_text("not json", encoding="utf-8")
    result = _show(project.parent, "--project-dir", str(project), "1.3")
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"Sidecar is not valid JSON\n"


# The owed rule: a phase owes a closure when every task of it is done and an
# entry of its deferred observations has no disposition.


def _owed_project(tmp_path, *phases):
    return closure_tree.build(tmp_path, roadmap=closure_tree.phase_roadmap(*phases))


def test_a_phase_half_run_to_the_breakpoint_owes_nothing(tmp_path):
    """The tasks past `---STOP---` are open, so the phase is not wholly done;
    a predicate that read the default parse would hide them and return 1."""
    project = _owed_project(tmp_path, (1, (
        "- [x] **1.1 — Alpha** — the done task",
        "",
        "---STOP---",
        "",
        "- [ ] **1.2 — Beta** — the open task past the marker",
    )))
    closure_tree.lay_done_task(project, "1.1 — Alpha")

    assert owed_phase(project, "ROADMAP.md") is None


def test_a_task_under_no_phase_heading_never_owes(tmp_path):
    """The section that names no phase comes first and holds a done task with
    an undisposed entry; were its task grouped under no phase it would owe
    first, and the result would not be the Phase 2 below it."""
    roadmap = "\n".join([
        "# Roadmap",
        "",
        "## Tests",
        "",
        "- [x] **Cover Alpha** — a flat task, done",
        "",
        "## Phase 2 — the phase 2",
        "",
        "- [x] **2.1 — Beta** — the done task",
        "",
    ])
    project = closure_tree.build(tmp_path, roadmap=roadmap)
    closure_tree.lay_done_task(project, "Cover Alpha", seq=1)
    beta = closure_tree.lay_done_task(project, "2.1 — Beta", seq=2)

    assert owed_phase(project, "ROADMAP.md") == 2
    assert undisposed_entries(project, "ROADMAP.md", 2) == [beta.entry]


def test_the_oldest_wholly_done_phase_owes_first(tmp_path):
    project = _owed_project(
        tmp_path,
        (1, ("- [x] **1.1 — Alpha** — done",)),
        (2, ("- [x] **2.1 — Beta** — done",)),
        (3, ("- [ ] **3.1 — Gamma** — open",)),
    )
    closure_tree.lay_done_task(project, "1.1 — Alpha", seq=1)
    closure_tree.lay_done_task(project, "2.1 — Beta", seq=2)

    assert owed_phase(project, "ROADMAP.md") == 1


def test_an_entry_disposed_by_its_marker_alone_is_disposed(tmp_path):
    project = _owed_project(tmp_path, (1, ("- [x] **1.1 — Alpha** — done",)))
    closure_tree.lay_done_task(project, "1.1 — Alpha", variant=closure_tree.MARKER)

    assert owed_phase(project, "ROADMAP.md") is None
    assert undisposed_entries(project, "ROADMAP.md", 1) == []


def test_an_entry_disposed_by_its_record_alone_is_disposed(tmp_path):
    project = _owed_project(tmp_path, (1, ("- [x] **1.1 — Alpha** — done",)))
    closure_tree.lay_done_task(project, "1.1 — Alpha", variant=closure_tree.RECORD)

    assert owed_phase(project, "ROADMAP.md") is None
    assert undisposed_entries(project, "ROADMAP.md", 1) == []


def test_an_entry_with_no_marker_and_no_record_is_listed_by_its_id(tmp_path):
    project = _owed_project(tmp_path, (1, ("- [x] **1.1 — Alpha** — done",)))
    alpha = closure_tree.lay_done_task(project, "1.1 — Alpha")

    assert alpha.entry == entry_id(None, alpha.stem, alpha.affects, alpha.text)
    assert undisposed_entries(project, "ROADMAP.md", 1) == [alpha.entry]
    assert owed_phase(project, "ROADMAP.md") == 1
