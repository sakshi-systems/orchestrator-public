"""The run's tree found from the tree on disk, against real git."""

from pathlib import Path

import pytest

from orchestrator.iteration_ref import ref_of
from orchestrator.ports import Repository
from orchestrator.repositories import find_run_tree
from tests.conftest import git_in


def _project(tmp_path: Path, name: str) -> Path:
    """A project repository at a base commit, no `.ai-factory` yet."""
    project = tmp_path / name
    project.mkdir()
    (project / "code.txt").write_text("base\n", encoding="utf-8")
    git_in(project, "init", "-q", "-b", "main", ".")
    git_in(project, "add", "-A")
    git_in(project, "commit", "-q", "-m", "the base")
    return project


def _shared_repository(tmp_path: Path) -> Path:
    """A repository built to hold several projects' artifact folders."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "README.md").write_text("shared\n", encoding="utf-8")
    git_in(shared, "init", "-q", "-b", "main", ".")
    git_in(shared, "add", "-A")
    git_in(shared, "commit", "-q", "-m", "the base")
    return shared


def test_a_link_finds_the_repository_ai_factory_leads_to(tmp_path):
    project = _project(tmp_path, "project")
    shared = _shared_repository(tmp_path)
    folder = shared / "ai-factory" / "project-a"
    folder.mkdir(parents=True)
    (folder / "plans").mkdir()
    (folder / "plans" / "01-slug.md").write_text("# Plan\n", encoding="utf-8")
    project.joinpath(".ai-factory").symlink_to(folder)

    tree = find_run_tree(project)

    assert len(tree) == 2
    project_repository, other = tree
    assert project_repository == Repository(directory=project, folder=None)
    assert other == Repository(directory=project / ".ai-factory",
                               folder="ai-factory/project-a/")
    assert project_repository.label == "the project's repository"
    assert other.label == (
        "the repository holding `.ai-factory`, its folder `ai-factory/project-a/`"
    )
    assert project_repository.stage == ("add", "-A")
    assert other.stage == ("add", "-A", ".")
    # The other repository's index is shared with the other projects it
    # holds, so the two repositories never take one scope.
    assert project_repository.scope == ()
    assert other.scope == ("--", ".")
    assert project_repository.scope != other.scope
    assert project_repository.named("01-slug") == "01-slug"
    assert other.named("01-slug") == "ai-factory/project-a/01-slug"


def test_an_ai_factory_repository_of_its_own_is_found_with_an_empty_folder(
        tmp_path):
    project = _project(tmp_path, "project")
    factory = project / ".ai-factory"
    factory.mkdir()
    git_in(factory, "init", "-q", "-b", "main", ".")

    tree = find_run_tree(project)

    assert len(tree) == 2
    other = tree[1]
    assert other == Repository(directory=factory, folder="")
    assert other.label == "the repository holding `.ai-factory`, which is its root"
    assert other.stage == ("add", "-A", ".")
    assert other.scope == ("--", ".")
    # The repository is the project's alone, so its names are the project's
    # repository's.
    assert other.named("01-slug") == "01-slug"


@pytest.mark.parametrize("kind", ["ignored", "tracked", "untracked"])
def test_a_plain_folder_is_the_project_s_repository_alone(tmp_path, kind):
    project = _project(tmp_path, "project")
    factory = project / ".ai-factory"
    (factory / "plans").mkdir(parents=True)
    (factory / "plans" / "01-slug.md").write_text("# Plan\n", encoding="utf-8")
    if kind == "ignored":
        (project / ".gitignore").write_text(".ai-factory/\n", encoding="utf-8")
        git_in(project, "add", ".gitignore")
        git_in(project, "commit", "-q", "-m", "ignore the factory")
    elif kind == "tracked":
        git_in(project, "add", ".ai-factory")
        git_in(project, "commit", "-q", "-m", "track the factory")

    tree = find_run_tree(project)

    assert tree == (Repository(directory=project, folder=None),)


def test_an_ai_factory_in_no_repository_is_the_project_s_alone(tmp_path):
    project = _project(tmp_path, "project")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "artifacts").mkdir(parents=True)
    project.joinpath(".ai-factory").symlink_to(elsewhere / "artifacts")

    tree = find_run_tree(project)

    assert tree == (Repository(directory=project, folder=None),)


def test_a_project_in_no_repository_is_its_repository_alone(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".ai-factory").mkdir()

    tree = find_run_tree(project)

    assert tree == (Repository(directory=project, folder=None),)


def test_a_missing_ai_factory_is_the_project_s_alone(tmp_path):
    project = _project(tmp_path, "project")

    tree = find_run_tree(project)

    assert tree == (Repository(directory=project, folder=None),)


def test_two_folders_of_one_repository_never_share_a_name(tmp_path):
    shared = _shared_repository(tmp_path)
    names = []
    for project_name, lab in (("first", "project-a"),
                              ("second", "project-b")):
        project = _project(tmp_path, project_name)
        folder = shared / "ai-factory" / lab
        folder.mkdir(parents=True)
        project.joinpath(".ai-factory").symlink_to(folder)
        tree = find_run_tree(project)
        assert len(tree) == 2
        names.append(tree[1].named("01-slug"))

    assert names == ["ai-factory/project-a/01-slug",
                     "ai-factory/project-b/01-slug"]
    assert ref_of(names[0]) == (
        "refs/orchestrator-iteration/ai-factory/project-a/01-slug"
    )
