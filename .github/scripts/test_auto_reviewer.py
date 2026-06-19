from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from auto_reviewer import (
    AutomationResult,
    classify_files,
    pick_round_robin_reviewer,
)


# ---------------------------------------------------------------------------
# Draft-guard tests (main() early-return when PR_DRAFT=true)
# ---------------------------------------------------------------------------

def _base_env(overrides: dict | None = None) -> dict:
    base = {
        "GITHUB_TOKEN": "tok",
        "SLACK_BOT_TOKEN": "stok",
        "REVIEWER_SLACK_MAP": "{}",
        "GITHUB_REPOSITORY": "Travtus/some-repo",
        "PR_NUMBER": "42",
        "PR_AUTHOR": "alice",
        "PR_URL": "https://github.com/Travtus/some-repo/pull/42",
        "PR_DRAFT": "false",
    }
    if overrides:
        base.update(overrides)
    return base


def test_main_returns_zero_and_skips_for_draft_pr(monkeypatch):
    """main() must exit 0 without calling the GitHub API when PR_DRAFT=true."""
    env = _base_env({"PR_DRAFT": "true"})
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    with patch("auto_reviewer.GithubClient") as mock_gh_cls, \
         patch("auto_reviewer.SlackClient"), \
         patch("auto_reviewer.load_json", return_value={
             "state_repository": "Travtus/.github",
             "state_issue_number": 70,
             "default_team": "Platform",
             "teams": {
                 "Platform": {
                     "github_team": "Platform",
                     "slack_channel": "C123",
                     "extensions": [".py"],
                 }
             },
         }):
        mock_gh_cls.return_value.list_pull_request_files.side_effect = AssertionError(
            "API must not be called for draft PRs"
        )

        from auto_reviewer import main
        result = main()

    assert result == 0


def test_main_proceeds_for_non_draft_pr(monkeypatch):
    """main() must call the GitHub API when PR_DRAFT=false."""
    env = _base_env({"PR_DRAFT": "false"})
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    automation_mock = MagicMock()
    automation_mock.run.return_value = AutomationResult(assigned_reviewers={})

    with patch("auto_reviewer.GithubClient"), \
         patch("auto_reviewer.SlackClient"), \
         patch("auto_reviewer.load_json", return_value={
             "state_repository": "Travtus/.github",
             "state_issue_number": 70,
             "default_team": "Platform",
             "teams": {
                 "Platform": {
                     "github_team": "Platform",
                     "slack_channel": "C123",
                     "extensions": [".py"],
                 }
             },
         }), \
         patch("auto_reviewer.ReviewerAutomation", return_value=automation_mock):

        from auto_reviewer import main
        result = main()

    automation_mock.run.assert_called_once()
    assert result == 0


# ---------------------------------------------------------------------------
# classify_files tests
# ---------------------------------------------------------------------------

SAMPLE_CONFIG = {
    "teams": {
        "Platform": {"extensions": [".py", ".yaml", ".yml"], "github_team": "Platform", "slack_channel": "C1"},
        "frontend": {"extensions": [".ts", ".tsx", ".js"], "github_team": "frontend", "slack_channel": "C2"},
    },
    "default_team": "Platform",
}


def test_classify_files_matches_python_to_platform():
    assert classify_files(["src/foo.py", "README.md"], SAMPLE_CONFIG) == ["Platform"]


def test_classify_files_matches_ts_to_frontend():
    assert classify_files(["app/component.tsx"], SAMPLE_CONFIG) == ["frontend"]


def test_classify_files_returns_empty_for_no_match():
    assert classify_files(["README.md", "LICENSE"], SAMPLE_CONFIG) == []


def test_classify_files_matches_multiple_teams():
    matched = classify_files(["app/api.py", "app/component.ts"], SAMPLE_CONFIG)
    assert set(matched) == {"Platform", "frontend"}


# ---------------------------------------------------------------------------
# pick_round_robin_reviewer tests
# ---------------------------------------------------------------------------

def test_pick_round_robin_skips_pr_author():
    members = ["alice", "bob", "carol"]
    reviewer, _ = pick_round_robin_reviewer(members, {}, pr_author="alice")
    assert reviewer != "alice"


def test_pick_round_robin_picks_least_assigned():
    members = ["alice", "bob", "carol"]
    state = {"counts": {"alice": 3, "bob": 1, "carol": 2}, "last_reviewer": None}
    reviewer, _ = pick_round_robin_reviewer(members, state, pr_author="dave")
    assert reviewer == "bob"


def test_pick_round_robin_returns_none_when_only_author():
    reviewer, state = pick_round_robin_reviewer(["alice"], {}, pr_author="alice")
    assert reviewer is None


def test_pick_round_robin_increments_count():
    members = ["alice", "bob"]
    _, new_state = pick_round_robin_reviewer(members, {}, pr_author="carol")
    picked = new_state["last_reviewer"]
    assert new_state["counts"][picked] == 1
