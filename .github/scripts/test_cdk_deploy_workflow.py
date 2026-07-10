from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO_ROOT / ".github/workflows/cdk-deploy.yml").read_text()
README = (REPO_ROOT / "README.md").read_text()
CDK_README = README.split("### `cdk-deploy.yml`", maxsplit=1)[1].split("\n### ", maxsplit=1)[0]


def test_private_dependencies_use_scoped_github_app_auth() -> None:
    assert "PAT_GITHUB" not in WORKFLOW
    assert "PLATFORM_ADMIN_APP_PRIVATE_KEY" in WORKFLOW
    assert "PRIVATE_DEPENDENCY_REPOSITORIES" in WORKFLOW
    assert "actions/create-github-app-token@v3" in WORKFLOW
    assert "client-id: ${{ vars.PLATFORM_ADMIN_APP_ID }}" in WORKFLOW
    assert "permission-contents: read" in WORKFLOW


def test_private_dependency_credentials_are_ephemeral_and_verified() -> None:
    assert "persist-credentials: false" in WORKFLOW
    assert "trap cleanup_git_auth EXIT" in WORKFLOW
    assert "git ls-remote --exit-code" in WORKFLOW
    assert "uv sync --all-groups --frozen" in WORKFLOW


def test_image_tag_is_validated_and_passed_to_synth_and_deploy() -> None:
    assert "if: ${{ inputs.IMAGE_TAG != '' }}" in WORKFLOW
    assert '[[ ! "$IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]]' in WORKFLOW
    assert WORKFLOW.count('args+=(-c "imageTag=$IMAGE_TAG")') == 2
    assert "IMAGE_TAG: ${{ github.sha }}" in CDK_README


def test_readme_documents_the_caller_contract() -> None:
    assert "`PAT_GITHUB`" not in CDK_README
    assert "`PLATFORM_ADMIN_APP_ID`" in CDK_README
    assert "`PLATFORM_ADMIN_APP_PRIVATE_KEY`" in CDK_README
    assert "`PRIVATE_DEPENDENCY_REPOSITORIES`" in CDK_README


if __name__ == "__main__":
    test_private_dependencies_use_scoped_github_app_auth()
    test_private_dependency_credentials_are_ephemeral_and_verified()
    test_image_tag_is_validated_and_passed_to_synth_and_deploy()
    test_readme_documents_the_caller_contract()
