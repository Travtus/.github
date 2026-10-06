# Standard library
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

# Third-party
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO_ROOT / ".github/workflows/release_source_check.yml").read_text()
README = (REPO_ROOT / "README.md").read_text()

# The branch layouts the product repos actually have.
THREE_STAGE = ["develop", "uat", "main"]  # gateway, studio, conversations
TWO_STAGE = ["develop", "main"]  # aperture, gateway-react-native, demo-system
FOUR_STAGE = ["develop", "qa", "uat", "main"]  # adam, adam-script, adam-sandbox


def _script() -> str:
    block = WORKFLOW.split("        run: |\n", maxsplit=1)[1]
    return textwrap.dedent(block)


def _run(
    tmp_path: Path,
    *,
    branches: list[str],
    base: str,
    head: str,
    head_repository: str = "Travtus/product-example",
    gh_fails: bool = False,
) -> subprocess.CompletedProcess[str]:
    fake_gh = tmp_path / "gh"
    if gh_fails:
        fake_gh.write_text("#!/bin/sh\necho 'HTTP 502' >&2\nexit 1\n")
    else:
        fake_gh.write_text("#!/bin/sh\nprintf '%s\\n' " + " ".join(branches) + "\n")
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "REPOSITORY": "Travtus/product-example",
        "BASE_REF": base,
        "HEAD_REF": head,
        "HEAD_REPOSITORY": head_repository,
    }
    return subprocess.run(
        ["bash", "-c", _script()], env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    ("branches", "base", "head"),
    [
        (THREE_STAGE, "uat", "develop"),
        (THREE_STAGE, "main", "uat"),
        (TWO_STAGE, "main", "develop"),
        (FOUR_STAGE, "qa", "develop"),
        (FOUR_STAGE, "uat", "qa"),
        (FOUR_STAGE, "main", "uat"),
        (THREE_STAGE, "main", "hotfix/login-loop"),
        (TWO_STAGE, "main", "hotfix/cors"),
        (FOUR_STAGE, "main", "hotfix/x"),
    ],
)
def test_allows_the_previous_stage_and_hotfixes_into_main(
    tmp_path: Path, branches: list[str], base: str, head: str
) -> None:
    result = _run(tmp_path, branches=branches, base=base, head=head)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("branches", "base", "head"),
    [
        (THREE_STAGE, "uat", "feat/insights-shared-dashboards"),
        (THREE_STAGE, "main", "develop"),
        (THREE_STAGE, "main", "feat/x"),
        (TWO_STAGE, "main", "feat/x"),
        (FOUR_STAGE, "uat", "develop"),
        (FOUR_STAGE, "main", "qa"),
        (FOUR_STAGE, "qa", "feat/x"),
        (THREE_STAGE, "uat", "hotfix/x"),
    ],
)
def test_rejects_skipping_a_stage(
    tmp_path: Path, branches: list[str], base: str, head: str
) -> None:
    result = _run(tmp_path, branches=branches, base=base, head=head)
    assert result.returncode == 1
    assert "::error::" in result.stdout


def test_rejects_a_fork_even_with_the_right_branch_name(tmp_path: Path) -> None:
    result = _run(
        tmp_path, branches=THREE_STAGE, base="uat", head="develop", head_repository="someone/fork"
    )
    assert result.returncode == 1
    assert "fork" in result.stdout


@pytest.mark.parametrize("base", ["develop", "feat/x", "poc/dashboards"])
def test_ignores_pull_requests_into_non_release_branches(tmp_path: Path, base: str) -> None:
    result = _run(tmp_path, branches=THREE_STAGE, base=base, head="feat/y", gh_fails=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_fails_closed_when_branches_cannot_be_listed(tmp_path: Path) -> None:
    result = _run(tmp_path, branches=[], base="main", head="develop", gh_fails=True)
    assert result.returncode != 0


def test_a_crafted_branch_name_is_data_not_code(tmp_path: Path) -> None:
    result = _run(tmp_path, branches=THREE_STAGE, base="uat", head='x";exit${IFS}0;#')
    assert result.returncode == 1
    assert "${{" not in _script()


def test_rechecks_when_a_pull_request_is_retargeted() -> None:
    assert "types: [opened, synchronize, reopened, edited]" in WORKFLOW
    assert "permissions:\n  contents: read\n" in WORKFLOW


def test_readme_documents_the_ruleset_setup() -> None:
    section = README.split("### `release_source_check.yml`", maxsplit=1)[1].split("\n### ")[0]
    assert "Require workflows to pass before merging" in section
    assert "product-sanity-cms" in section
    assert "product-gateway-embedded-script" in section
