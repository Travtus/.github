# Standard library
from __future__ import annotations

from typing import Any

# First-party
from auto_reviewer import ReviewerAutomation

JsonDict = dict[str, Any]

CONFIG: JsonDict = {
    "teams": {
        "Platform": {
            "github_team": "Platform",
            "slack_channel": "C0A035FRREE",
            "extensions": [".py"],
        },
    },
}


class FakeGithub:
    def __init__(
        self,
        team_members: list[str],
        existing_reviewers: list[str],
        changed_files: list[str] | None = None,
        state: JsonDict | None = None,
    ) -> None:
        self.team_members = team_members
        self.existing_reviewers = existing_reviewers
        self.changed_files = changed_files or ["src/app.py"]
        self.state = state or {}
        self.requested_reviewers: list[str] = []
        self.updated_state: JsonDict | None = None

    def list_pull_request_files(self, owner: str, repo: str, pull_number: int) -> list[str]:
        return self.changed_files

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        return self.team_members

    def get_state_comment(
        self, owner: str, repo: str, issue_number: int, marker: str
    ) -> tuple[JsonDict, int, str]:
        return dict(self.state), 1, ""

    def update_state_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        state: JsonDict,
        previous_body: str,
        marker: str,
    ) -> None:
        self.updated_state = state

    def request_reviewer(self, owner: str, repo: str, pull_number: int, reviewer: str) -> bool:
        self.requested_reviewers.append(reviewer)
        return True

    def list_existing_reviewers(self, owner: str, repo: str, pull_number: int) -> list[str]:
        return self.existing_reviewers


class FakeSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def post_message(self, channel: str, text: str) -> None:
        self.messages.append((channel, text))


def _make_automation(github: FakeGithub, slack: FakeSlack) -> ReviewerAutomation:
    return ReviewerAutomation(
        github=github,
        slack=slack,
        config=CONFIG,
        state_owner="Travtus",
        state_repo=".github",
        state_issue_number=70,
    )


def test_author_self_review_does_not_block_round_robin() -> None:
    """A PR author who replies to their own PR still gets a teammate assigned.

    Regression test: list_existing_reviewers previously included the PR author's own
    review activity (e.g. replying to automated feedback), which made the script think
    the author's team already had a reviewer and skipped assignment entirely.
    """
    github = FakeGithub(
        team_members=["Jamkasz", "rangan-anand", "nikkogg", "muhammad-tkhan"],
        existing_reviewers=["Jamkasz", "copilot-pull-request-reviewer"],
    )
    slack = FakeSlack()
    automation = _make_automation(github, slack)

    result = automation.run(
        owner="Travtus",
        repo="platform-config-service",
        pull_number=19,
        pr_author="Jamkasz",
        pr_url="https://github.com/Travtus/platform-config-service/pull/19",
    )

    # Tie-break among eligible teammates (all with 0 prior assignments) is alphabetical.
    assert result.assigned_reviewers == {"Platform": "muhammad-tkhan"}
    assert github.requested_reviewers == ["muhammad-tkhan"]


def test_real_teammate_review_still_blocks_reassignment() -> None:
    """A genuine teammate review still counts and prevents a duplicate assignment."""
    github = FakeGithub(
        team_members=["Jamkasz", "rangan-anand", "nikkogg", "muhammad-tkhan"],
        existing_reviewers=["Jamkasz", "rangan-anand"],
    )
    slack = FakeSlack()
    automation = _make_automation(github, slack)

    result = automation.run(
        owner="Travtus",
        repo="platform-config-service",
        pull_number=19,
        pr_author="Jamkasz",
        pr_url="https://github.com/Travtus/platform-config-service/pull/19",
    )

    assert result.assigned_reviewers == {}
    assert github.requested_reviewers == []
