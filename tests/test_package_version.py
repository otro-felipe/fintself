import tomllib
from pathlib import Path


def test_personal_finances_fork_has_a_distinct_patch_version():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert project["project"]["version"] == "1.5.0.post1"
