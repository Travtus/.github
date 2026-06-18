from __future__ import annotations

import http.client
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError


JsonDict = dict[str, Any]
RequestFunc = Callable[[str, str, JsonDict | None], Any]


class GithubClient:
    def __init__(self, token: str, request_func: RequestFunc | None = None) -> None:
        self.token = token
        self._request_func = request_func or self._request

    def list_pull_request_files(self, owner: str, repo: str, pull_number: int) -> list[str]:
        files = self._paginate(f"/repos/{owner}/{repo}/pulls/{pull_number}/files")
        return [file["filename"] for file in files]

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        members = self._paginate(f"/orgs/{org}/teams/{team_slug}/members")
        return [member["login"] for member in members]

    def get_state_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        marker: str,
    ) -> tuple[JsonDict, int, str]:
        comments = self._paginate(f"/repos/{owner}/{repo}/issues/{issue_number}/comments")
        for comment in comments:
            body = comment.get("body", "")
            if isinstance(body, str) and marker in body:
                return _parse_state_comment_body(body, marker), int(comment["id"]), body
        raise RuntimeError(
            f"Could not find auto-reviewer state comment in {owner}/{repo}#{issue_number}.",
        )

    def update_state_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        state: JsonDict,
        previous_body: str,
        marker: str,
    ) -> None:
        next_body = _format_state_comment_body(state, marker)
        if previous_body == next_body:
            return
        self._request_func(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            {"body": next_body},
        )

    def request_reviewer(self, owner: str, repo: str, pull_number: int, reviewer: str) -> None:
        try:
            self._request_func(
                "POST",
                f"/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers",
                {"reviewers": [reviewer]},
            )
        except HTTPError as error:
            if error.code != 422:
                raise

    def list_existing_reviewers(self, owner: str, repo: str, pull_number: int) -> list[str]:
        response = self._request_func(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers",
            None,
        )
        users = response.get("users", []) if isinstance(response, dict) else []
        return [user["login"] for user in users]

    def _paginate(self, path: str) -> list[JsonDict]:
        page = 1
        results: list[JsonDict] = []
        while True:
            separator = "&" if "?" in path else "?"
            response = self._request_func("GET", f"{path}{separator}per_page=100&page={page}", None)
            if not isinstance(response, list):
                raise TypeError(f"Expected list response from {path}")
            results.extend(response)
            if len(response) < 100:
                return results
            page += 1

    def _request(self, method: str, path: str, body: JsonDict | None = None) -> JsonDict:
        data = json.dumps(body) if body is not None else None
        connection = http.client.HTTPSConnection("api.github.com", timeout=20)  # nosemgrep
        connection.request(
            method,
            path,
            body=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "travtus-auto-reviewer",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response = connection.getresponse()
        raw = response.read().decode()
        if response.status >= 400:
            raise HTTPError(
                url=path,
                code=response.status,
                msg=response.reason,
                hdrs=response.headers,
                fp=None,
            )
        return json.loads(raw) if raw else {}


class SlackClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def post_message(self, channel: str, text: str) -> None:
        response = self._request(
            "POST",
            "/api/chat.postMessage",
            {"channel": channel, "text": text},
        )
        if not response.get("ok"):
            error = response.get("error", "unknown_error")
            raise RuntimeError(f"Slack postMessage failed: {error}")

    def _request(self, method: str, path: str, body: JsonDict | None = None) -> JsonDict:
        data = json.dumps(body) if body is not None else None
        connection = http.client.HTTPSConnection("slack.com", timeout=20)  # nosemgrep
        connection.request(
            method,
            path,
            body=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        raw = response.read().decode()
        if response.status >= 400:
            raise HTTPError(
                url=path,
                code=response.status,
                msg=response.reason,
                hdrs=response.headers,
                fp=None,
            )
        return json.loads(raw) if raw else {}


@dataclass(frozen=True)
class AutomationResult:
    assigned_reviewers: dict[str, str]


class GithubApi(Protocol):
    def list_pull_request_files(self, owner: str, repo: str, pull_number: int) -> list[str]:
        pass

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        pass

    def get_state_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        marker: str,
    ) -> tuple[JsonDict, int, str]:
        pass

    def update_state_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        state: JsonDict,
        previous_body: str,
        marker: str,
    ) -> None:
        pass

    def request_reviewer(self, owner: str, repo: str, pull_number: int, reviewer: str) -> None:
        pass

    def list_existing_reviewers(self, owner: str, repo: str, pull_number: int) -> list[str]:
        pass


class SlackApi(Protocol):
    def post_message(self, channel: str, text: str) -> None:
        pass


STATE_COMMENT_MARKER = "<!-- auto-reviewer-state -->"


def _parse_state_comment_body(body: str, marker: str) -> JsonDict:
    if marker not in body:
        raise ValueError("State comment marker not found.")
    if "```json" not in body:
        raise ValueError("State comment JSON fence not found.")

    json_start = body.index("```json") + len("```json")
    json_end = body.index("```", json_start)
    return json.loads(body[json_start:json_end].strip())


def _format_state_comment_body(state: JsonDict, marker: str) -> str:
    content = json.dumps(state, indent=2, sort_keys=True)
    return f"{marker}\n```json\n{content}\n```\n"


def classify_files(changed_files: list[str], config: JsonDict) -> list[str]:
    matched_teams = []
    for team_name, team_config in config["teams"].items():
        extensions = tuple(team_config["extensions"])
        if any(file.endswith(extensions) for file in changed_files):
            matched_teams.append(team_name)
    return matched_teams


def pick_round_robin_reviewer(
    members: list[str],
    state: JsonDict,
    pr_author: str,
) -> tuple[str | None, JsonDict]:
    ordered_members = sorted(members, key=str.lower)
    eligible_members = [member for member in ordered_members if member.lower() != pr_author.lower()]
    if not eligible_members:
        return None, state

    start_index = _start_index(ordered_members, state)

    for offset in range(1, len(ordered_members) + 1):
        next_index = (start_index + offset) % len(ordered_members)
        reviewer = ordered_members[next_index]
        if reviewer in eligible_members:
            counts = dict(state.get("counts", {}))
            counts[reviewer] = int(counts.get(reviewer, 0)) + 1
            return reviewer, {
                "cursor": next_index,
                "last_reviewer": reviewer,
                "previous_order": ordered_members,
                "counts": counts,
            }

    return None, state


def _start_index(ordered_members: list[str], state: JsonDict) -> int:
    last_reviewer = state.get("last_reviewer")
    if isinstance(last_reviewer, str) and last_reviewer in ordered_members:
        return ordered_members.index(last_reviewer)

    previous_order = state.get("previous_order")
    if not isinstance(previous_order, list) or not isinstance(last_reviewer, str):
        return -1

    previous_active_members = [member for member in previous_order if member in ordered_members]
    if last_reviewer not in previous_order or not previous_active_members:
        return -1

    previous_index = previous_order.index(last_reviewer)
    for offset in range(1, len(previous_order) + 1):
        candidate = previous_order[(previous_index + offset) % len(previous_order)]
        if candidate in ordered_members:
            return ordered_members.index(candidate) - 1

    return -1


class ReviewerAutomation:
    def __init__(
        self,
        github: GithubApi,
        slack: SlackApi,
        config: JsonDict,
        state_owner: str,
        state_repo: str,
        state_issue_number: int,
        reviewer_slack_ids: dict[str, str] | None = None,
    ) -> None:
        self.github = github
        self.slack = slack
        self.config = config
        self.state_owner = state_owner
        self.state_repo = state_repo
        self.state_issue_number = state_issue_number
        self.reviewer_slack_ids = {
            login.lower(): slack_id for login, slack_id in (reviewer_slack_ids or {}).items()
        }

    def run(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        pr_author: str,
        pr_url: str,
    ) -> AutomationResult:
        changed_files = self.github.list_pull_request_files(owner, repo, pull_number)
        matched_teams = classify_files(changed_files, self.config)
        if not matched_teams:
            default_team = self.config.get("default_team", "Platform")
            print(f"No reviewer team matched changed files; defaulting to {default_team}.")
            matched_teams = [default_team]

        existing_reviewers = self.github.list_existing_reviewers(owner, repo, pull_number)
        state, comment_id, previous_body = self.github.get_state_comment(
            self.state_owner,
            self.state_repo,
            self.state_issue_number,
            STATE_COMMENT_MARKER,
        )
        assigned_reviewers, next_state = self._select_reviewers(
            owner,
            matched_teams,
            state,
            pr_author,
            existing_reviewers,
        )
        if not assigned_reviewers:
            print("Every matched team already has a requested reviewer.")
            return AutomationResult(assigned_reviewers={})

        self.github.update_state_comment(
            self.state_owner,
            self.state_repo,
            comment_id,
            next_state,
            previous_body,
            STATE_COMMENT_MARKER,
        )
        self._request_reviews_and_notify(
            owner,
            repo,
            pull_number,
            pr_author,
            pr_url,
            assigned_reviewers,
        )
        return AutomationResult(assigned_reviewers=assigned_reviewers)

    def _select_reviewers(
        self,
        owner: str,
        matched_teams: list[str],
        state: JsonDict,
        pr_author: str,
        existing_reviewers: list[str],
    ) -> tuple[dict[str, str], JsonDict]:
        existing_lower = {reviewer.lower() for reviewer in existing_reviewers}
        next_state = json.loads(json.dumps(state))
        assigned_reviewers = {}
        for team_name in matched_teams:
            team_config = self.config["teams"][team_name]
            members = self.github.list_team_members(owner, team_config["github_team"])
            if any(member.lower() in existing_lower for member in members):
                continue
            team_state = next_state.setdefault(
                team_name,
                {"cursor": -1, "last_reviewer": None, "previous_order": [], "counts": {}},
            )
            reviewer, updated_team_state = pick_round_robin_reviewer(members, team_state, pr_author)
            if reviewer is None:
                raise RuntimeError(f"No eligible reviewer found for {team_name}.")
            assigned_reviewers[team_name] = reviewer
            next_state[team_name] = updated_team_state
        return assigned_reviewers, next_state

    def _request_reviews_and_notify(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        pr_author: str,
        pr_url: str,
        assigned_reviewers: dict[str, str],
    ) -> None:
        for team_name, reviewer in assigned_reviewers.items():
            self.github.request_reviewer(owner, repo, pull_number, reviewer)
            self._notify_slack(team_name, reviewer, owner, repo, pull_number, pr_author, pr_url)

    def _notify_slack(
        self,
        team_name: str,
        reviewer: str,
        owner: str,
        repo: str,
        pull_number: int,
        pr_author: str,
        pr_url: str,
    ) -> None:
        slack_user_id = self.reviewer_slack_ids.get(reviewer.lower())
        if slack_user_id is None:
            print(f"Skipping Slack mention for {reviewer}: no Slack id mapping.")
            return

        channel = self.config["teams"][team_name]["slack_channel"]
        text = (
            f"<@{slack_user_id}> you have been selected for review.\n\n"
            f"PR: <{pr_url}|{owner}/{repo}#{pull_number}>\n"
            f"Author: @{pr_author}\n"
            f"Team: {team_name}"
        )
        self.slack.post_message(channel, text)


def load_json(path: Path) -> JsonDict:
    return json.loads(path.read_text())


def main() -> int:
    required_env_vars = [
        "GITHUB_TOKEN",
        "SLACK_BOT_TOKEN",
        "GITHUB_REPOSITORY",
        "PR_NUMBER",
        "PR_AUTHOR",
        "PR_URL",
    ]
    missing_env_vars = [name for name in required_env_vars if not os.environ.get(name)]
    if missing_env_vars:
        print(
            f"Missing required environment variables: {', '.join(missing_env_vars)}",
            file=sys.stderr,
        )
        return 1

    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", maxsplit=1)
    script_dir = Path(__file__).resolve().parent
    config = load_json(script_dir.parent / "auto_reviewer_config.json")
    state_repository = config.get("state_repository", "Travtus/.github")
    state_owner, state_repo = state_repository.split("/", maxsplit=1)
    state_issue_number = int(config["state_issue_number"])
    reviewer_slack_ids = json.loads(os.environ.get("REVIEWER_SLACK_MAP") or "{}")

    automation = ReviewerAutomation(
        github=GithubClient(os.environ["GITHUB_TOKEN"]),
        slack=SlackClient(os.environ["SLACK_BOT_TOKEN"]),
        config=config,
        state_owner=state_owner,
        state_repo=state_repo,
        state_issue_number=state_issue_number,
        reviewer_slack_ids=reviewer_slack_ids,
    )
    result = automation.run(
        owner=owner,
        repo=repo,
        pull_number=int(os.environ["PR_NUMBER"]),
        pr_author=os.environ["PR_AUTHOR"],
        pr_url=os.environ["PR_URL"],
    )
    print(f"Assigned reviewers: {json.dumps(result.assigned_reviewers, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
