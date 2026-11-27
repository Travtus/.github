from __future__ import annotations

import html
import http.client
import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from functools import partial
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.error import HTTPError

STICKY_MARKER = "<!-- ai-review:sticky -->"
UNAVAILABLE_TITLE = "## AI review unavailable"
BOT_LOGIN = "github-actions[bot]"
MAX_ISSUES = 15
MAX_CHANGES = 20
MAX_TEXT = 1000
MAX_PATH = 300
MAX_BODY = 64536  # GitHub allows 65536 UTF-16 units; the margin absorbs counting differences
MIN_SECRET_LENGTH = 8

SCHEMA = json.loads(Path(__file__).with_name("ai_review_schema.json").read_text())

VERDICT_ICONS = {"approve": "\U0001f7e2", "comment": "\U0001f7e1", "request_changes": "\U0001f534"}

CREDENTIAL_PATTERNS = {
    "GitHub token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    "AWS access key": re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "Slack token": re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "JWT": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
    "refresh token": re.compile(r'"refresh_token"\s*:\s*"[^"]+"'),
}

IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2065, 0x2065),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0xE0000, 0xE0FFF),
)
ZERO_WIDTH_BREAKS = re.compile(
    r"(?i)(?<=:)(?=//)|(?<=www)(?=\.)|(?<=gh)(?=-\d)|(?<=#)(?=\d)|(?<=@)|(?=\$)"
)
MARKDOWN_MARKERS = re.compile(r"[\\#\-+*_=~]")
ORDERED_LIST_START = re.compile(r"(?m)^([ \t]*\d{1,9})([.)])")
MARKDOWN_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
ESCAPE_OR_ENTITY = re.compile(r"\\.|&#?\w+;|.", re.DOTALL)
TRUNCATION = "\u2026truncated"
JsonDict = dict[str, Any]
JsonValue = Any
RequestFunc = Callable[[str, str, JsonDict | None], JsonValue]
HTML_TAG = re.compile(r"<[^>]*>")
REQUIRED_ENV = ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "PR_NUMBER", "REVIEWED_SHA", "REVIEW_RESULT")
JOB_FAILURES = {"failure": "review job failed", "cancelled": "review job cancelled or timed out"}
JSON_TYPES = {"object": dict, "array": list, "string": str, "null": type(None)}


class InvalidOutputError(Exception):
    """The agent output does not match the review schema."""


class Issue(TypedDict):
    severity: Literal["required", "note"]
    path: str
    line: int | None
    issue: str
    fix: str


class Review(TypedDict):
    verdict: Literal["approve", "comment", "request_changes"]
    changes: list[str]
    issues: list[Issue]


def _is_type(value: object, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, JSON_TYPES[name])


def _violation(value: object, schema: dict[str, Any], where: str) -> str | None:
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, name) for name in names):
            return f"{where}: expected {' or '.join(names)}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where}: not one of {schema['enum']}"
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        return f"{where}: too short"
    if isinstance(value, str) and len(value) > schema.get("maxLength", len(value)):
        return f"{where}: too long"
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value < schema.get("minimum", value)
    ):
        return f"{where}: below {schema['minimum']}"
    if isinstance(value, dict):
        return _object_violation(value, schema, where)
    if isinstance(value, list) and len(value) > schema.get("maxItems", len(value)):
        return f"{where}: too many items"
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            if problem := _violation(item, schema["items"], f"{where}[{index}]"):
                return problem
    return None


def _object_violation(value: dict[str, Any], schema: dict[str, Any], where: str) -> str | None:
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in value:
            return f"{where}: missing {key}"
    if schema.get("additionalProperties") is False and set(value) - set(properties):
        return f"{where}: unexpected property"
    for key, subschema in properties.items():
        if key in value and (problem := _violation(value[key], subschema, f"{where}.{key}")):
            return problem
    return None


def validate(raw: str) -> Review:
    try:
        review = json.loads(raw)
    except (ValueError, RecursionError):
        raise InvalidOutputError("output is not JSON") from None
    if problem := _violation(review, SCHEMA, "output"):
        raise InvalidOutputError(problem)
    issues = sorted(review["issues"], key=lambda issue: issue["severity"] != "required")
    return {**review, "changes": review["changes"][:MAX_CHANGES], "issues": issues[:MAX_ISSUES]}


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def find_leak(value: object, secrets: list[str]) -> str | None:
    """Name the first credential-shaped or exact-secret match in any string, never its value."""
    exact = [secret for secret in secrets if len(secret) >= MIN_SECRET_LENGTH]
    try:
        for original in _strings(value):
            for text in (original, _clean(original)):
                if any(secret in text for secret in exact):
                    return "exact secret value"
                for name, pattern in CREDENTIAL_PATTERNS.items():
                    if pattern.search(text):
                        return f"{name} pattern"
    except RecursionError:
        return "unscannable nesting"
    return None


def _is_invisible(character: str) -> bool:
    category = unicodedata.category(character)
    return (
        category == "Cf"
        or any(low <= ord(character) <= high for low, high in IGNORABLE_RANGES)
        or (category == "Cc" and character not in "\n\t")
    )


def _clean(text: str) -> str:
    text = text.encode("utf-8", "replace").decode()
    return "".join(character for character in text if not _is_invisible(character))


def sanitise(text: str, limit: int = MAX_TEXT) -> str:
    text = ZERO_WIDTH_BREAKS.sub("\u200b", _clean(text))
    text = ORDERED_LIST_START.sub(r"\1\\\2", MARKDOWN_MARKERS.sub(r"\\\g<0>", text))
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("[", "&#91;").replace("]", "&#93;").replace("`", "'")
    end = 0
    for token in ESCAPE_OR_ENTITY.finditer(text):
        if token.end() > limit:
            break
        end = token.end()
    return text[:end]


def _code(text: str) -> str:
    text = _clean(text).replace("`", "'").replace("|", "\\|")
    return f"`{' '.join(text.split())[:MAX_PATH]}`"


def _line(text: str) -> str:
    return " ".join(sanitise(text).split())


def _cell(text: str) -> str:
    return sanitise(text).replace("|", "&#124;").replace("\n", "<br>")


def _issue_row(issue: Issue) -> str:
    location = issue["path"] if issue["line"] is None else f"{issue['path']}:{issue['line']}"
    cells = [issue["severity"], _code(location), _cell(issue["issue"]), _cell(issue["fix"])]
    return "| " + " | ".join(cells) + " |"


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fit(body: str) -> str:
    suffix = f"\n{TRUNCATION}\n"
    if _utf16_units(body) <= MAX_BODY:
        return body
    budget = 2 * (MAX_BODY - _utf16_units(suffix))
    head = body.encode("utf-16-le")[:budget].decode("utf-16-le", "ignore")
    return head[: head.rfind("\n") + 1] + suffix


def _within_limit(lines: list[str]) -> bool:
    return _utf16_units("\n".join(lines) + "\n") <= MAX_BODY


def _longest_prefix(before: list[str], items: list[str], after: list[str]) -> int:
    kept = 0
    while kept < len(items) and _within_limit(before + items[: kept + 1] + after):
        kept += 1
    return kept


def _omitted_note(count: int) -> list[str]:
    return ["", f"{count} more issues omitted"]


def _issue_lines(head: list[str], issues: list[Issue]) -> list[str]:
    if not issues:
        return ["No issues found."]
    table = ["**Issues**", "", "| Severity | Location | Issue | Fix |", "|---|---|---|---|"]
    rows = [_issue_row(issue) for issue in issues]
    kept = _longest_prefix(head + table, rows, [])
    if kept < len(rows):
        kept = _longest_prefix(head + table, rows, _omitted_note(len(rows)))
    omitted = len(rows) - kept
    return table + rows[:kept] + (_omitted_note(omitted) if omitted else [])


def _change_lines(head: list[str], changes: list[str], issue_lines: list[str]) -> list[str]:
    items = [f"- {_line(change)}" for change in changes]
    kept = _longest_prefix(head + ["**Changes**", ""], items, [""] + issue_lines)
    return ["**Changes**", "", *items[:kept], ""] if kept else []


def _reviewed_line(sha: str) -> str:
    return f"Reviewed commit {_code(sha)}"


def render_sticky(review: Review, reviewed_sha: str, notice: str = "") -> str:
    icon = VERDICT_ICONS[review["verdict"]]
    head = [STICKY_MARKER, f"## {icon} AI review: {review['verdict']}", ""]
    head += [notice, ""] if notice else []
    head += [_reviewed_line(reviewed_sha), ""]
    issue_lines = _issue_lines(head, review["issues"])
    changes = _change_lines(head, review["changes"], issue_lines)
    return _fit("\n".join(head + changes + issue_lines) + "\n")


def render_stale(review: Review, reviewed_sha: str, head_sha: str) -> str:
    notice = (
        f"> **Stale:** this review covers {_code(reviewed_sha)} but the PR head is now "
        f"{_code(head_sha)}. A newer review will replace it."
    )
    return render_sticky(review, reviewed_sha, notice)


def _unavailable_commit_line(sha: str) -> str:
    return f"Commit {_code(sha)}: the reviewer will review on the next push, or re-run this job."


def render_unavailable(reason: str, reviewed_sha: str) -> str:
    return (
        f"{STICKY_MARKER}\n"
        f"{UNAVAILABLE_TITLE}\n\n"
        f"AI review unavailable: {_line(reason)}\n\n"
        f"{_unavailable_commit_line(reviewed_sha)}\n"
    )


def _find_body_leak(body: str, secrets: list[str]) -> str | None:
    displayed = html.unescape(MARKDOWN_ESCAPE.sub(r"\1", body))
    return find_leak(body, secrets) or find_leak(displayed, secrets)


def _withheld(reason: str, reviewed_sha: str) -> str:
    return render_unavailable(f"output withheld ({reason})", reviewed_sha)


def publishable_body(review: Review, reviewed_sha: str, head_sha: str, secrets: list[str]) -> str:
    """Render the review, or an unavailable notice when a leak scan of it or its body fires."""
    reason = find_leak(review, secrets)
    if reason is None:
        stale = head_sha != reviewed_sha
        body = (
            render_stale(review, reviewed_sha, head_sha)
            if stale
            else render_sticky(review, reviewed_sha)
        )
        reason = _find_body_leak(body, secrets)
        if reason is None:
            return body
    return _withheld(reason, reviewed_sha)


class PublishGithub:
    def __init__(
        self,
        token: str,
        request_func: RequestFunc | None = None,
        connection_factory: Callable[..., Any] = http.client.HTTPSConnection,
    ) -> None:
        self.token = token
        self._connection_factory = connection_factory
        self._request_func = request_func or self._request
        self._markdown_func = request_func or partial(
            self._request, accept="text/html", decode=False
        )

    def pull_head_sha(self, repo: str, number: int) -> str:
        return self._request_func("GET", f"/repos/{repo}/pulls/{number}", None)["head"]["sha"]

    def list_comments(self, repo: str, number: int) -> list[JsonDict]:
        return self._paginate(f"/repos/{repo}/issues/{number}/comments")

    def create_comment(self, repo: str, number: int, body: str) -> None:
        self._request_func("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        self._request_func("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", {"body": body})

    def render_markdown(self, repo: str, text: str) -> str:
        payload = {"text": text, "mode": "gfm", "context": repo}
        return self._markdown_func("POST", "/markdown", payload)

    def _paginate(self, path: str) -> list[JsonDict]:
        page = 1
        results: list[JsonDict] = []
        while True:
            response = self._request_func("GET", f"{path}?per_page=100&page={page}", None)
            if not isinstance(response, list):
                raise TypeError(f"Expected list response from {path}")
            results.extend(response)
            if len(response) < 100:
                return results
            page += 1

    def _request(
        self,
        method: str,
        path: str,
        body: JsonDict | None = None,
        accept: str = "application/vnd.github+json",
        decode: bool = True,
    ) -> JsonValue:
        connection = self._connection_factory("api.github.com", timeout=20)
        connection.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "travtus-ai-review",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response = connection.getresponse()
        text = response.read().decode()
        if response.status >= 400:
            raise HTTPError(path, response.status, response.reason, response.headers, None)
        return (json.loads(text) if text else {}) if decode else text


def safe_body(github: PublishGithub, repo: str, body: str, reviewed_sha: str) -> str:
    reason = _find_body_leak(body, [])
    if reason is None:
        rendered = github.render_markdown(repo, body)
        visible = html.unescape(HTML_TAG.sub("", rendered))
        if leak := find_leak([rendered, visible], []):
            reason = f"rendered {leak}"
    if reason is None:
        return body
    return _withheld(reason, reviewed_sha)


def _is_own_sticky(comment: JsonDict) -> bool:
    user = comment.get("user") or {}
    return comment["body"].splitlines()[:1] == [STICKY_MARKER] and user.get("login") == BOT_LOGIN


def _own_stickies(github: PublishGithub, repo: str, number: int) -> list[JsonDict]:
    return [c for c in github.list_comments(repo, number) if _is_own_sticky(c)]


def upsert_sticky(github: PublishGithub, repo: str, number: int, body: str) -> None:
    for attempt in range(2):
        stickies = _own_stickies(github, repo, number)
        outdated = [c for c in stickies if c["body"] != body]
        try:
            if not stickies:
                github.create_comment(repo, number, body)
            for sticky in outdated:
                github.update_comment(repo, sticky["id"], body)
            return
        except (HTTPError, OSError, http.client.HTTPException) as error:
            if attempt or (isinstance(error, HTTPError) and error.code < 500):
                raise


def _upsert_review(
    github: PublishGithub, repo: str, number: int, review: Review, reviewed_sha: str, head_sha: str
) -> str:
    body = publishable_body(review, reviewed_sha, head_sha, [])
    body = safe_body(github, repo, body, reviewed_sha)
    upsert_sticky(github, repo, number, body)
    return body


def publish(
    github: PublishGithub, repo: str, number: int, reviewed_sha: str, result: Review | str
) -> None:
    head = github.pull_head_sha(repo, number)
    if isinstance(result, str):
        if head == reviewed_sha:
            body = safe_body(github, repo, render_unavailable(result, reviewed_sha), reviewed_sha)
            upsert_sticky(github, repo, number, body)
        return
    if head != reviewed_sha:
        head_lines = {_reviewed_line(head), _unavailable_commit_line(head)}
        stickies = _own_stickies(github, repo, number)
        if not any(head_lines.intersection(sticky["body"].splitlines()) for sticky in stickies):
            _upsert_review(github, repo, number, result, reviewed_sha, head)
        return
    body = _upsert_review(github, repo, number, result, reviewed_sha, head)
    if body != render_sticky(result, reviewed_sha):
        return
    latest = github.pull_head_sha(repo, number)
    if latest != reviewed_sha:
        _upsert_review(github, repo, number, result, reviewed_sha, latest)


def _result_from_env(env: Mapping[str, str]) -> Review | str:
    status = env["REVIEW_RESULT"]
    if status in JOB_FAILURES:
        return JOB_FAILURES[status]
    if status != "success":
        raise SystemExit(f"Unexpected REVIEW_RESULT: {status}")
    outcome = env.get("OUTCOME", "")
    if outcome != "ok":
        return outcome or "no review outcome"
    try:
        return validate(env.get("FINDINGS", ""))
    except InvalidOutputError:
        return "invalid agent output"


def publish_from_env(env: Mapping[str, str], github: PublishGithub) -> None:
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    if env["REVIEW_RESULT"] == "skipped":
        return
    try:
        number = int(env["PR_NUMBER"])
    except ValueError:
        raise SystemExit(f"PR_NUMBER is not a number: {env['PR_NUMBER']!r}") from None
    result = _result_from_env(env)
    publish(github, env["GITHUB_REPOSITORY"], number, env["REVIEWED_SHA"], result)


def main(env: Mapping[str, str] = os.environ, github: PublishGithub | None = None) -> int:
    publish_from_env(env, github or PublishGithub(env.get("GITHUB_TOKEN", "")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
