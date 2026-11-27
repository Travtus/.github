from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, TypedDict

STICKY_MARKER = "<!-- ai-review:sticky -->"
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

INVISIBLE_FILLERS = frozenset("\u034f\u115f\u1160\u3164\uffa0\u2065")
ZERO_WIDTH_BREAKS = re.compile(
    r"(?i)(?<=:)(?=//)|(?<=www)(?=\.)|(?<=gh)(?=-\d)|(?<=#)(?=\d)|(?<=@)|(?=\$)"
)
MARKDOWN_MARKERS = re.compile(r"[\\#\-+*_=~]")
ORDERED_LIST_START = re.compile(r"(?m)^([ \t]*\d{1,9})([.)])")
MARKDOWN_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
ESCAPE_OR_ENTITY = re.compile(r"\\.|&#?\w+;|.", re.DOTALL)
TRUNCATION = "\u2026truncated"
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
        or character in INVISIBLE_FILLERS
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


def render_sticky(review: Review, reviewed_sha: str, notice: str = "") -> str:
    icon = VERDICT_ICONS[review["verdict"]]
    lines = [STICKY_MARKER, f"## {icon} AI review: {review['verdict']}", ""]
    lines += [notice, ""] if notice else []
    lines += [f"Reviewed commit {_code(reviewed_sha)}", ""]
    if review["changes"]:
        lines += ["**Changes**", ""]
        lines += [f"- {_line(change)}" for change in review["changes"]] + [""]
    if review["issues"]:
        lines += ["**Issues**", "", "| Severity | Location | Issue | Fix |", "|---|---|---|---|"]
        lines += [_issue_row(issue) for issue in review["issues"]]
    else:
        lines.append("No issues found.")
    return _fit("\n".join(lines) + "\n")


def render_stale(review: Review, reviewed_sha: str, head_sha: str) -> str:
    notice = (
        f"> **Stale:** this review covers {_code(reviewed_sha)} but the PR head is now "
        f"{_code(head_sha)}. A newer review will replace it."
    )
    return render_sticky(review, reviewed_sha, notice)


def render_unavailable(reason: str, reviewed_sha: str) -> str:
    return (
        f"{STICKY_MARKER}\n"
        "## AI review unavailable\n\n"
        f"AI review unavailable: {_line(reason)}\n\n"
        f"Commit {_code(reviewed_sha)}: the reviewer will review on the next push, "
        "or re-run this job.\n"
    )


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
        displayed = html.unescape(MARKDOWN_ESCAPE.sub(r"\1", body))
        reason = find_leak(body, secrets) or find_leak(displayed, secrets)
        if reason is None:
            return body
    return render_unavailable(f"output withheld ({reason})", reviewed_sha)
