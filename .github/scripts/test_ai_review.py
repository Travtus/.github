from __future__ import annotations

import json
import re
from collections.abc import Callable
from http.client import IncompleteRead, RemoteDisconnected
from typing import Any
from urllib.error import HTTPError

import pytest

from ai_review import (
    MAX_BODY,
    MAX_CHANGES,
    MAX_PATH,
    MAX_TEXT,
    SCHEMA,
    STICKY_MARKER,
    TRUNCATION,
    UNAVAILABLE_TITLE,
    InvalidOutputError,
    PublishGithub,
    _fit,
    _utf16_units,
    find_leak,
    main,
    publish,
    publish_from_env,
    publishable_body,
    render_stale,
    render_sticky,
    render_unavailable,
    sanitise,
    validate,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
NEW_HEAD = "fedcba9876543210fedcba9876543210fedcba98"


def make_issue(**overrides: object) -> dict[str, Any]:
    issue = {"severity": "required", "path": "app/a.py", "line": 3, "issue": "bug", "fix": "fix it"}
    return issue | overrides


def make_review(**overrides: object) -> dict[str, Any]:
    review = {"verdict": "comment", "changes": ["Adds a retry"], "issues": [make_issue()]}
    return review | overrides


def raw(review: dict[str, Any]) -> str:
    return json.dumps(review)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "not json",
        "[]",
        "{}",
        raw(make_review(verdict="merge")),
        raw({"changes": [], "issues": []}),
        raw(make_review(changes="Adds a retry")),
        raw(make_review(changes=[1])),
        raw(make_review(issues=[make_issue(severity="blocker")])),
        raw(make_review(issues=[make_issue(line="3")])),
        raw(make_review(issues=[make_issue(line=True)])),
        raw(make_review(issues=[make_issue(line=0)])),
        raw(make_review(issues=[make_issue(issue="")])),
        raw(make_review(issues=[{k: v for k, v in make_issue().items() if k != "fix"}])),
        raw(make_review(issues=[make_issue(extra="x")])),
        raw(make_review(extra="x")),
        "[" * 100_000 + "]" * 100_000,
        raw(make_review(changes=["x"] * 300_000)),
        raw(make_review(changes=["x"] * 51)),
        raw(make_review(changes=["x" * 4001])),
        raw(make_review(issues=[make_issue()] * 51)),
        raw(make_review(issues=[make_issue(path="p" * 1001)])),
        raw(make_review(issues=[make_issue(issue="i" * 4001)])),
        raw(make_review(issues=[make_issue(fix="f" * 4001)])),
    ],
)
def test_validate_rejects_malformed_output(output: str) -> None:
    with pytest.raises(InvalidOutputError):
        validate(output)


@pytest.mark.parametrize(
    "review",
    [
        make_review(),
        make_review(verdict="approve", changes=[], issues=[]),
        make_review(issues=[make_issue(line=None, severity="note")]),
        make_review(changes=["x" * 4000] * 20),
        make_review(issues=[make_issue(path="p" * 1000, issue="i" * 4000, fix="f" * 4000)] * 15),
    ],
)
def test_validate_accepts_valid_output(review: dict[str, Any]) -> None:
    assert validate(raw(review)) == review


def keywords(schema: dict[str, Any]) -> set[str]:
    found = set(schema)
    nested = [
        *schema.get("properties", {}).values(),
        *([schema["items"]] if "items" in schema else []),
    ]
    return found.union(*map(keywords, nested))


def test_schema_uses_only_keywords_the_validator_implements() -> None:
    implemented = {"type", "enum", "required", "properties", "additionalProperties", "items"}
    implemented |= {"minLength", "maxLength", "minimum", "maxItems"}

    assert keywords(SCHEMA) <= implemented


def test_validate_caps_issues_at_15() -> None:
    notes = [make_issue(severity="note", issue=f"note {n}") for n in range(14)]
    required = [make_issue(issue=f"required {n}") for n in range(2)]

    review = validate(raw(make_review(issues=notes + required)))

    assert len(review["issues"]) == 15
    assert [i["issue"] for i in review["issues"][:2]] == ["required 0", "required 1"]


SECRET = "s3cr3t-value-" + "x" * 12


@pytest.mark.parametrize(
    "review",
    [
        make_review(issues=[make_issue(issue=f"leaks {SECRET} here")]),
        make_review(issues=[make_issue(fix=SECRET)]),
        make_review(issues=[make_issue(path=SECRET)]),
        make_review(changes=[SECRET]),
    ],
)
def test_exact_secret_scan_withholds(review: dict[str, Any]) -> None:
    assert find_leak(review, [SECRET]) is not None


TOKEN = "ghp_" + "a" * 36
INVISIBLES = ["\u200b", "\u2060", "\u00ad", "\u202e", "\ufeff"]


def split_by(text: str, invisible: str) -> str:
    return text[:5] + invisible + text[5:]


@pytest.mark.parametrize("invisible", INVISIBLES)
@pytest.mark.parametrize(
    ("secret", "secrets"), [(SECRET, [SECRET]), (TOKEN, []), ("sk-ant-" + "Zy9" * 10, [])]
)
def test_leak_scan_sees_secrets_that_rendering_would_rejoin(
    secret: str, secrets: list[str], invisible: str
) -> None:
    review = make_review(issues=[make_issue(path=split_by(secret, invisible))])

    assert find_leak(review, secrets) is not None


IGNORABLE_MARKS = [
    0x17B4,
    0x17B5,
    0x180B,
    0x180D,
    0x180F,
    0xFE00,
    0xFE0F,
    0xFFF0,
    0xFFF8,
    0xE0000,
    0xE0101,
    0xE01EF,
    0xE0FFF,
]
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.mark.parametrize("mark", IGNORABLE_MARKS, ids=hex)
def test_default_ignorable_marks_cannot_hide_a_credential(mark: int) -> None:
    split = split_by(AWS_KEY, chr(mark))
    review = make_review(changes=[split], issues=[make_issue(fix=split)])

    assert find_leak(review, []) == "AWS access key pattern"
    withheld = render_unavailable("output withheld (AWS access key pattern)", SHA)
    assert publishable_body(review, SHA, SHA, []) == withheld


@pytest.mark.parametrize("invisible", INVISIBLES)
@pytest.mark.parametrize("field", ["path", "issue", "fix", "changes"])
def test_a_review_that_passes_the_scan_never_renders_the_secret(field: str, invisible: str) -> None:
    split = split_by(SECRET, invisible)
    if field == "changes":
        review = make_review(changes=[split])
    else:
        review = make_review(issues=[make_issue(**{field: split})])

    bodies = [render_sticky(review, SHA), render_stale(review, SHA, NEW_HEAD)]

    assert find_leak(review, [SECRET]) is not None or not any(SECRET in body for body in bodies)


@pytest.mark.parametrize("secrets", [[], [""], ["short"]])
def test_exact_secret_scan_ignores_empty_and_short_values(secrets: list[str]) -> None:
    assert find_leak(make_review(issues=[make_issue(issue="short")]), secrets) is None


@pytest.mark.parametrize(
    "text",
    [
        "ghp_" + "a" * 36,
        "github_pat_" + "A1b2" * 10,
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "sk-ant-" + "oat01-" + "Zy9" * 10,
        "xoxb-" + "123456789012-abcdefghij",
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".c2lnbmF0dXJl",
        '{"refresh_token": "abcdef"}',
        "x" + "ghp_" + "a" * 36,
        "x" + "AKIA" + "A" * 16,
        "AKIA" + "A" * 16 + "x",
        "AKIA" + "A" * 16 + "_",
        "AKIA" + "A" * 17,
        "ASIA" + "A" * 16 + "9",
        "x" + "sk-ant-" + "oat01-" + "Zy9" * 10,
        "a" + "xoxb-" + "123456789012-abcdefghij",
        "x" + "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".c2lnbmF0dXJl",
    ],
)
def test_pattern_scan_withholds(text: str) -> None:
    assert find_leak(make_review(issues=[make_issue(issue=f"see {text}")]), []) is not None


def nested(depth: int) -> list[Any]:
    value: list[Any] = []
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    ("value", "secrets"),
    [
        (make_review(issues=[make_issue(line=123456789012)]), ["123456789012"]),
        (make_review(issues=[make_issue(line=1234567.5)]), ["1234567.5"]),
        ({"ghp_" + "a" * 36: 1}, []),
        ({"outer": {SECRET: 1}}, [SECRET]),
        (nested(2000), []),
    ],
)
def test_leak_scan_covers_numbers_keys_and_unscannable_nesting(
    value: object, secrets: list[str]
) -> None:
    assert find_leak(value, secrets) is not None


@pytest.mark.parametrize("value", [True, None, 3, [True, None, 3.5]])
def test_leak_scan_ignores_bools_none_and_short_numbers(value: object) -> None:
    assert find_leak(value, [SECRET]) is None


@pytest.mark.parametrize(
    "text",
    [SHA, "a" * 64, "abc1234", "123e4567-e89b-12d3-a456-426614174000", "ghp is a prefix", "AKIA"],
)
def test_benign_sha_not_flagged(text: str) -> None:
    body = render_sticky(make_review(issues=[make_issue(issue=text)]), SHA)

    assert find_leak(body, [SECRET]) is None


def test_render_sticky() -> None:
    review = make_review(
        verdict="request_changes",
        changes=["Adds a retry"],
        issues=[
            make_issue(path="a.py", line=7, issue="off by one", fix="use <="),
            make_issue(severity="note", path="b.py", line=None, issue="naming", fix="rename"),
        ],
    )

    body = render_sticky(review, SHA)

    assert body.startswith(STICKY_MARKER)
    assert f"`{SHA}`" in body
    assert "Adds a retry" in body
    assert "`a.py:7`" in body
    assert "off by one" in body
    assert "`b.py`" in body
    assert body.index("a.py") < body.index("b.py")


@pytest.mark.parametrize(
    ("verdict", "icon"),
    [("approve", "\U0001f7e2"), ("comment", "\U0001f7e1"), ("request_changes", "\U0001f534")],
)
def test_render_sticky_verdict_icon(verdict: str, icon: str) -> None:
    assert icon in render_sticky(make_review(verdict=verdict), SHA)


def test_render_stale_and_unavailable_variants() -> None:
    review = make_review(changes=["unique change"], issues=[make_issue(issue="unique issue")])
    stale = render_stale(review, SHA, NEW_HEAD)
    unavailable = render_unavailable("reviewer busy", SHA)

    for body in (stale, unavailable):
        assert body.startswith(STICKY_MARKER)
        assert f"`{SHA}`" in body
    assert f"`{NEW_HEAD}`" in stale
    assert "stale" in stale.lower()
    assert "unique change" in stale
    assert "unique issue" in stale
    assert "unique change" not in unavailable
    assert "unique issue" not in unavailable
    assert "AI review unavailable: reviewer busy" in unavailable
    assert "will review on the next push, or re-run this job" in unavailable


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("<!-- ai-review:sticky -->", "<!--"),
        ("<img src=x onerror=1>", "<img"),
        ("![pixel](https://evil.example/p.png)", "!["),
        ("[click](https://evil.example)", "]("),
        ("ping @octocat and @Travtus/core", "@octocat"),
        ("ping @Travtus/core", "@Travtus"),
        ("see #123", "#123"),
        ("see Travtus/other#9", "other#9"),
        ("a\x00b\x1b[31mc", "\x00"),
        ("a\x1b[31mc", "\x1b"),
    ],
)
def test_sanitise_untrusted_text(text: str, forbidden: str) -> None:
    assert forbidden not in sanitise(text)


def test_sanitise_escapes_brackets_to_intact_entities() -> None:
    assert sanitise("[x]") == "&#91;x&#93;"


def test_sanitise_drops_backticks_so_escapes_never_show_inside_code_spans() -> None:
    assert sanitise("`f() -> list[str]`") == "'f() \\-&gt; list&#91;str&#93;'"


@pytest.mark.parametrize("text", ["plain text\nsecond line", "a\tb\nc"])
def test_sanitise_keeps_plain_text_tabs_and_newlines(text: str) -> None:
    assert sanitise(text) == text


HEADING = re.compile(r"^\s*(?:[-*+]\s+)?#{1,6}\s")
BLOCK_START = re.compile(r"(?:#{1,6}(?:\s|$)|[-*+>]\s|\d+[.)]\s|[-*_=~]{3,}|```|~~~)")
HOSTILE = [
    "x\n\n~~~\n",
    "x\n\n## \U0001f7e2 AI review: approve",
    "x\n| a | b |",
    "## \U0001f7e2 AI review: approve",
    "- ## x",
    "# h",
    "+ p",
    "* e",
    "> q",
    "1. n",
    "2) n",
    "---",
    "***",
    "___",
    "===",
    "~~~",
    "\\## x",
    "\\* e",
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_changes_and_reason_stay_on_one_line(hostile: str) -> None:
    sticky = render_sticky(make_review(changes=[hostile]), SHA)
    unavailable = render_unavailable(hostile, SHA)

    for body in (sticky, unavailable):
        assert not any(
            line.startswith(("~~~", "## \U0001f7e2", "| a")) for line in body.splitlines()[2:]
        )
        assert not any(HEADING.match(line) for line in body.splitlines()[2:])
    assert sum(line.startswith("## ") for line in sticky.splitlines()) == 1
    change = next(line for line in sticky.splitlines() if line.startswith("- "))
    assert not BLOCK_START.match(change[2:])


def test_agent_text_cannot_forge_the_marker_or_add_table_rows() -> None:
    hostile = make_issue(issue=f"{STICKY_MARKER}\n| a | b |", path="x|y`z\n", fix="@octocat")

    body = render_sticky(make_review(issues=[hostile]), SHA)

    assert body.count(STICKY_MARKER) == 1
    assert "@octocat" not in body
    assert len([line for line in body.splitlines() if line.startswith("|")]) == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a & b &lt;", "a &amp; b &amp;lt;"),
        ("# h", "\\# h"),
        ("- i", "\\- i"),
        ("+ i", "\\+ i"),
        ("*b* _i_", "\\*b\\* \\_i\\_"),
        ("===", "\\=\\=\\="),
        ("~~s~~", "\\~\\~s\\~\\~"),
        ("\\*", "\\\\\\*"),
        ("1. a", "1\\. a"),
        ("x\n12) a", "x\n12\\) a"),
        ("see GH-12", "see GH\u200b\\-12"),
        ("gh-7", "gh\u200b\\-7"),
        ("https://x.example", "https:\u200b//x.example"),
        ("www.x.example", "www\u200b.x.example"),
        ("WWW.x.example", "WWW\u200b.x.example"),
    ],
)
def test_sanitise_escapes_markdown_and_breaks_autolinks(text: str, expected: str) -> None:
    assert sanitise(text) == expected


INVISIBLE_CODE_POINTS = [
    0x00AD,
    0x034F,
    0x061C,
    0x115F,
    0x1160,
    0x180E,
    0x200B,
    0x200C,
    0x200D,
    0x200E,
    0x200F,
    0x202A,
    0x202E,
    0x2060,
    0x2061,
    0x2064,
    0x2065,
    0x2066,
    0x2069,
    0x3164,
    0xFEFF,
    0xFFA0,
    0xE0001,
    0xE0041,
    0xE007F,
    0x0085,
    0x009F,
    0x0000,
    0x001B,
    0x007F,
    0x000D,
]


@pytest.mark.parametrize("code_point", INVISIBLE_CODE_POINTS, ids=hex)
def test_invisible_and_bidi_characters_are_stripped(code_point: int) -> None:
    character = chr(code_point)
    review = make_review(changes=[f"a{character}b"], issues=[make_issue(path=f"p{character}q")])

    body = render_sticky(review, SHA)

    assert sanitise(f"a{character}b") == "ab"
    assert character not in body
    assert "`pq:3`" in body


def test_stripping_happens_before_our_own_zero_width_space() -> None:
    assert sanitise("https:\u200b//x\u200b") == "https:\u200b//x"


@pytest.mark.parametrize("surrogate", ["\ud800", "a\udfffb"])
def test_lone_surrogates_never_break_encoding(surrogate: str) -> None:
    review = make_review(changes=[surrogate], issues=[make_issue(path=surrogate, fix=surrogate)])

    bodies = [
        sanitise(surrogate),
        render_sticky(review, SHA),
        render_stale(review, surrogate, NEW_HEAD),
        render_unavailable(surrogate, surrogate),
    ]

    for body in bodies:
        body.encode("utf-8")


def test_sanitise_breaks_dollars_so_math_never_renders() -> None:
    assert sanitise("$x$ and $$x$$") == "\u200b$x\u200b$ and \u200b$\u200b$x\u200b$\u200b$"


def test_sanitise_bounds_length_without_cutting_an_escape_or_entity() -> None:
    assert sanitise("#" * 100, limit=51) == "\\#" * 25
    assert sanitise("<" * 100, limit=50) == "&lt;" * 12
    assert sanitise("a" * (MAX_TEXT + 5)) == "a" * MAX_TEXT


def issue_row(**overrides: object) -> str:
    body = render_sticky(make_review(issues=[make_issue(**overrides)]), SHA)
    return body.splitlines()[-1]


@pytest.mark.parametrize("field", ["issue", "fix"])
@pytest.mark.parametrize(
    ("text", "cell"),
    [("a|b", "a&#124;b"), ("a\nb", "a<br>b"), ("a\nb|c", "a<br>b&#124;c")],
)
def test_table_cells_escape_pipes_and_newlines(field: str, text: str, cell: str) -> None:
    row = issue_row(**{field: text})

    assert cell in row
    assert row.count("|") == 5
    assert "\n" not in row


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("a`b", "`a'b:3`"),
        ("a|b", "`a\\|b:3`"),
        ("a\x00b\x1bc", "`abc:3`"),
        ("a \n\t  b", "`a b:3`"),
        ("p" * (MAX_PATH + 50), "`" + "p" * MAX_PATH + "`"),
    ],
)
def test_location_is_a_single_safe_code_span(path: str, code: str) -> None:
    assert f"| {code} |" in issue_row(path=path)


@pytest.mark.parametrize("severity", ["required", "note"])
def test_issue_row_shows_severity_location_issue_and_fix(severity: str) -> None:
    row = issue_row(severity=severity, path="app/a.py", line=3, issue="the issue", fix="the fix")

    assert row == f"| {severity} | `app/a.py:3` | the issue | the fix |"


def test_changes_are_capped() -> None:
    changes = [f"change {n}" for n in range(MAX_CHANGES + 5)]

    body = render_sticky(validate(raw(make_review(changes=changes))), SHA)

    assert sum(line.startswith("- ") for line in body.splitlines()) == MAX_CHANGES
    assert f"change {MAX_CHANGES - 1}" in body
    assert f"change {MAX_CHANGES}" not in body


@pytest.mark.parametrize(("issues", "present"), [([], True), ([make_issue()], False)])
def test_no_issues_found_only_when_there_are_none(issues: list[Any], present: bool) -> None:
    assert ("No issues found." in render_sticky(make_review(issues=issues), SHA)) is present


@pytest.mark.parametrize("fill", ["|", "||&<[", "||&<", "\U0001f7e2"])
def test_oversized_body_is_trimmed_at_a_line_boundary(fill: str) -> None:
    long_issue = make_issue(path=fill * MAX_PATH, issue=fill * MAX_TEXT, fix=fill * MAX_TEXT)
    review = make_review(changes=[fill * MAX_TEXT] * 20, issues=[long_issue] * 15)

    bodies = [render_sticky(review, SHA), render_stale(review, SHA, NEW_HEAD)]

    for body in bodies:
        assert len(body) <= 64_536
        assert len(body.encode("utf-16-le")) // 2 <= 65536
        assert body.startswith(STICKY_MARKER)
        assert all(line.endswith("|") for line in body.splitlines() if line.startswith("|"))


def test_fit_budgets_the_suffix_when_the_last_newline_is_near_the_limit() -> None:
    first = "a" * 100 + "\n"
    second = "b" * (MAX_BODY - len(first) - 6) + "\n"
    body = first + second + "c" * 100

    fitted = _fit(body)

    assert fitted == first + "\n\u2026truncated\n"
    assert len(fitted) <= MAX_BODY


def test_fit_keeps_a_margin_below_githubs_limit() -> None:
    fitted = _fit("line\n" * 20_000)

    assert 64_000 < len(fitted) <= 64_536


def test_fit_never_splits_a_surrogate_pair() -> None:
    body = "x\ny" + "\U0001f7e2" * MAX_BODY

    assert _fit(body) == "x\n\n\u2026truncated\n"


def test_short_bodies_are_not_trimmed() -> None:
    bodies = [
        render_sticky(make_review(), SHA),
        render_stale(make_review(), SHA, NEW_HEAD),
        render_unavailable("busy", SHA),
    ]

    assert not any("truncated" in body for body in bodies)


def test_unavailable_body_is_bounded() -> None:
    assert len(render_unavailable("|&<[" * 5000, SHA)) <= MAX_BODY


@pytest.mark.parametrize(
    ("review", "secrets", "secret"),
    [
        (
            make_review(issues=[make_issue(path="synthetic-secret", line=12345678)]),
            ["synthetic-secret:12345678"],
            "synthetic-secret:12345678",
        ),
        (make_review(changes=["alpha\t\nbeta-token"]), ["alpha beta-token"], "alpha beta-token"),
        (make_review(changes=["alpha\t\nbetatoken"]), ["alpha betatoken"], "alpha betatoken"),
        (make_review(changes=["synthetic`secret"]), ["synthetic'secret"], "synthetic'secret"),
        (make_review(changes=["a&b\n[c]xyz"]), ["a&b [c]xyz"], "a&b [c]xyz"),
        (make_review(changes=["a&b[c]d-secret"]), ["a&b[c]d-secret"], "a&b[c]d-secret"),
        (make_review(changes=["a" * 1100 + SECRET]), [SECRET], SECRET),
        (make_review(changes=[f"see {TOKEN}"]), [], TOKEN),
        (
            make_review(issues=[make_issue(path="alpha&amp;\n beta-token", line=None)]),
            ["alpha&amp; beta-token"],
            "alpha&amp; beta-token",
        ),
        (
            make_review(issues=[make_issue(path="alpha\\*\n beta-token", line=None)]),
            ["alpha\\* beta-token"],
            "alpha\\* beta-token",
        ),
    ],
)
@pytest.mark.parametrize("head_sha", [SHA, NEW_HEAD])
def test_publishable_body_withholds_anything_the_scans_can_see(
    review: dict[str, Any], secrets: list[str], secret: str, head_sha: str
) -> None:
    body = publishable_body(review, SHA, head_sha, secrets)

    assert body.startswith(STICKY_MARKER)
    assert "AI review unavailable: output withheld (" in body
    assert secret not in body


@pytest.mark.parametrize(
    ("head_sha", "render"),
    [
        (SHA, lambda review: render_sticky(review, SHA)),
        (NEW_HEAD, lambda r: render_stale(r, SHA, NEW_HEAD)),
    ],
    ids=["current", "stale"],
)
def test_publishable_body_renders_a_clean_review_unchanged(head_sha: str, render: Any) -> None:
    review = make_review()

    assert publishable_body(review, SHA, head_sha, [SECRET]) == render(review)


def test_unavailable_body_has_the_title_on_line_two() -> None:
    assert render_unavailable("busy", SHA).splitlines()[:2] == [STICKY_MARKER, UNAVAILABLE_TITLE]


RENDERS = [
    pytest.param(lambda review: render_sticky(review, SHA), id="current"),
    pytest.param(lambda review: render_stale(review, SHA, NEW_HEAD), id="stale"),
]


@pytest.mark.parametrize("render", RENDERS)
@pytest.mark.parametrize(("change_count", "issue_count"), [(20, 15), (0, 60), (20, 60)])
def test_issue_rows_get_budget_before_changes(
    render: Any, change_count: int, issue_count: int
) -> None:
    changes = ["\U0001f7e2" * MAX_TEXT] * change_count
    issues = [
        make_issue(issue=f"issue {n} " + "x" * 900, fix="y" * 900) for n in range(issue_count)
    ]

    body = render(make_review(changes=changes, issues=issues))

    rows = [line for line in body.splitlines() if line.startswith("| required")]
    omitted = re.search(r"(\d+) more issues omitted", body)
    assert _utf16_units(body) <= MAX_BODY
    assert TRUNCATION not in body
    assert all(f"issue {n} " in row for n, row in enumerate(rows))
    assert len(rows) + (int(omitted[1]) if omitted else 0) == issue_count
    assert (omitted is None) == (issue_count == 15)
    assert (
        sum(line.startswith("- ") for line in body.splitlines()) < change_count or not change_count
    )


@pytest.mark.parametrize(
    ("stale", "fix_length"), [(False, 896), (True, 891)], ids=["current", "stale"]
)
def test_the_omitted_note_is_budgeted_with_the_rows(stale: bool, fix_length: int) -> None:
    issues = [make_issue(issue=f"issue {n} " + "x" * 900, fix="y" * fix_length) for n in range(60)]
    review = make_review(changes=[], issues=issues)

    body = render_stale(review, SHA, NEW_HEAD) if stale else render_sticky(review, SHA)

    assert TRUNCATION not in body
    assert body.endswith(" more issues omitted\n")


def test_render_sticky_never_exceeds_the_body_limit() -> None:
    body = render_sticky(make_review(), SHA, "n" * (2 * MAX_BODY))

    assert _utf16_units(body) <= MAX_BODY
    assert body.endswith(TRUNCATION + "\n")


def test_issue_rows_that_fit_without_the_head_are_still_budgeted_with_it() -> None:
    issues = [make_issue(issue=f"issue {n} " + "x" * 900, fix="y" * 900) for n in range(34)]

    body = render_sticky(make_review(changes=[], issues=issues), SHA, "n" * 2000)

    assert TRUNCATION not in body
    assert body.endswith("1 more issues omitted\n")


REPO = "Travtus/example"
PR = 7


class FakeConnection:
    def __init__(self, status: int, payload: str) -> None:
        self.status = status
        self.reason = "reason"
        self.headers: dict[str, str] = {}
        self.payload = payload
        self.sent: list[tuple[str, str, str | None, dict[str, str]]] = []

    def request(self, method: str, path: str, body: str | None, headers: dict[str, str]) -> None:
        self.sent.append((method, path, body, headers))

    def getresponse(self) -> FakeConnection:
        return self

    def read(self) -> bytes:
        return self.payload.encode()


def transport(status: int, payload: str) -> tuple[PublishGithub, FakeConnection]:
    connection = FakeConnection(status, payload)
    return PublishGithub("tok", connection_factory=lambda *args, **kwargs: connection), connection


def test_markdown_transport_returns_html_text() -> None:
    github, connection = transport(200, "<h1>hi</h1>")

    assert github.render_markdown(REPO, "# hi") == "<h1>hi</h1>"

    [(method, path, body, headers)] = connection.sent
    assert (method, path) == ("POST", "/markdown")
    assert json.loads(body or "") == {"text": "# hi", "mode": "gfm", "context": REPO}
    assert headers["Accept"] == "text/html"
    assert headers["Authorization"] == "Bearer tok"


def test_json_transport_decodes_the_response() -> None:
    github, connection = transport(200, json.dumps({"head": {"sha": SHA}}))

    assert github.pull_head_sha(REPO, PR) == SHA

    [(method, path, body, headers)] = connection.sent
    assert (method, path, body) == ("GET", f"/repos/{REPO}/pulls/{PR}", None)
    assert headers["Accept"] == "application/vnd.github+json"


@pytest.mark.parametrize("status", [400, 404, 502])
@pytest.mark.parametrize(
    "call",
    [
        lambda github: github.render_markdown(REPO, "x"),
        lambda github: github.pull_head_sha(REPO, PR),
    ],
    ids=["markdown", "json"],
)
def test_transport_raises_http_error_on_a_failure_status(
    call: Callable[[PublishGithub], object], status: int
) -> None:
    github, _ = transport(status, "")

    with pytest.raises(HTTPError) as error:
        call(github)

    assert error.value.code == status


BOT = "github-actions[bot]"
COMMENTS = f"/repos/{REPO}/issues/{PR}/comments"
HEAD = f"/repos/{REPO}/pulls/{PR}"


def comment(body: str, comment_id: int, login: str = BOT) -> dict[str, Any]:
    return {"id": comment_id, "body": body, "user": {"login": login}}


class FakeApi:
    def __init__(
        self,
        comments: list[dict[str, Any]] | None = None,
        heads: list[str] | None = None,
        render: Callable[[str], str] | None = None,
    ) -> None:
        self.comments = list(comments or [])
        self.heads = heads or [SHA]
        self.render = render or (lambda text: re.sub(r"\\(.)", r"\1", text))
        self.faults: list[tuple[BaseException, str]] = []
        self.late: list[Callable[[], None]] = []
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        self.calls.append((method, path, body))
        if path == "/markdown":
            return self.render(body["text"])
        if method != "GET":
            return self.write(method, path, body)
        if path == HEAD:
            return {"head": {"sha": self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]}}
        page = int(path.rsplit("page=", 1)[1])
        listed = self.comments[(page - 1) * 100 : page * 100]
        while self.late:
            self.late.pop()()
        return listed

    def write(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        def apply() -> None:
            if method == "POST":
                next_id = max((c["id"] for c in self.comments), default=0) + 1
                self.comments.append(comment(body["body"], next_id))
                return
            target = int(path.rsplit("/", 1)[1])
            next(c for c in self.comments if c["id"] == target)["body"] = body["body"]

        if self.faults:
            error, mode = self.faults.pop(0)
            if mode == "applied":
                apply()
            elif mode == "late":
                self.late.append(apply)
            raise error
        apply()
        return {}

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.calls if m != "GET" and p != "/markdown"]

    def stickies(self) -> list[str]:
        return [c["body"] for c in self.comments if c["body"].startswith(STICKY_MARKER)]


def publish_to(api: FakeApi, result: dict[str, Any] | str) -> None:
    publish(PublishGithub("tok", request_func=api), REPO, PR, SHA, result)


OLD_STICKY = render_sticky(make_review(verdict="approve"), SHA)
NEWER_STICKY = render_sticky(make_review(verdict="approve"), NEW_HEAD)
UNAVAILABLE_FOR_HEAD = render_unavailable("busy", NEW_HEAD)
WITHHELD_FOR_HEAD = render_unavailable("output withheld (GitHub token pattern)", NEW_HEAD)
OLDER_STICKY = render_sticky(make_review(verdict="approve"), "b" * 40)
NEWER_STALE = render_stale(make_review(verdict="approve"), NEW_HEAD, "c" * 40)
PATCH_FIRST = [("PATCH", f"/repos/{REPO}/issues/comments/1")]


@pytest.mark.parametrize(
    ("comments", "expected"),
    [
        ([], [("POST", COMMENTS)]),
        ([comment(OLD_STICKY, 1)], PATCH_FIRST),
        ([comment(OLDER_STICKY, 1)], PATCH_FIRST),
        ([comment(NEWER_STICKY, 1)], []),
        ([comment(NEWER_STALE, 1)], []),
        ([comment(UNAVAILABLE_FOR_HEAD, 1)], []),
        ([comment(WITHHELD_FOR_HEAD, 1)], []),
        ([comment(OLD_STICKY, 1), comment(NEWER_STICKY, 2)], []),
    ],
    ids=[
        "no-sticky",
        "sticky-for-this-commit",
        "sticky-for-an-older-commit",
        "sticky-for-the-head",
        "stale-sticky-for-the-head",
        "unavailable-sticky-for-the-head",
        "withheld-sticky-for-the-head",
        "mixed",
    ],
)
def test_publish_on_a_moved_head_never_overwrites_a_newer_review(
    comments: list[dict[str, Any]], expected: list[tuple[str, str]]
) -> None:
    api = FakeApi(comments=comments, heads=[NEW_HEAD])

    publish_to(api, make_review())

    assert api.writes() == expected
    assert (render_stale(make_review(), SHA, NEW_HEAD) in api.stickies()) is bool(expected)
    assert [path for _, path, _ in api.calls].count(HEAD) == 1


def test_publish_on_a_moved_head_skips_an_unavailable_notice() -> None:
    api = FakeApi(heads=[NEW_HEAD])

    publish_to(api, "busy")

    assert api.calls == [("GET", HEAD, None)]


@pytest.mark.parametrize(
    "review",
    [make_review(), make_review(verdict="approve", changes=[], issues=[])],
    ids=["issues", "clean"],
)
def test_post_check_marks_sticky_stale(review: dict[str, Any]) -> None:
    api = FakeApi(heads=[SHA, NEW_HEAD])

    publish_to(api, review)

    assert api.stickies() == [render_stale(review, SHA, NEW_HEAD)]
    assert api.writes() == [("POST", COMMENTS), ("PATCH", f"/repos/{REPO}/issues/comments/1")]


@pytest.mark.parametrize(
    "result",
    ["busy", make_review(changes=[f"see {TOKEN}"])],
    ids=["unavailable", "withheld"],
)
def test_post_check_is_skipped_when_the_sticky_is_not_a_review(
    result: dict[str, Any] | str,
) -> None:
    api = FakeApi(heads=[SHA, NEW_HEAD])

    publish_to(api, result)

    assert api.writes() == [("POST", COMMENTS)]
    assert [path for _, path, _ in api.calls].count(HEAD) == 1
    assert "stale" not in api.stickies()[0].lower()


BOT_STICKY = render_sticky(make_review(), SHA)
NOISE = [comment("noise", n, "someone") for n in range(100)]


@pytest.mark.parametrize(
    ("comments", "expected"),
    [
        ([], [("POST", COMMENTS)]),
        ([comment(f"{STICKY_MARKER}\nhuman", 1, "someone")], [("POST", COMMENTS)]),
        ([comment(f"hello\n{STICKY_MARKER}", 1)], [("POST", COMMENTS)]),
        ([comment(f"{STICKY_MARKER} extra", 1)], [("POST", COMMENTS)]),
        (
            [comment(f"{STICKY_MARKER}\nhuman", 1, "someone"), comment(f"{STICKY_MARKER}\nold", 2)],
            [("PATCH", f"/repos/{REPO}/issues/comments/2")],
        ),
        (
            [comment(f"{STICKY_MARKER}\nold", 1), comment(f"{STICKY_MARKER}\nolder", 2)],
            [
                ("PATCH", f"/repos/{REPO}/issues/comments/1"),
                ("PATCH", f"/repos/{REPO}/issues/comments/2"),
            ],
        ),
        (
            [comment(BOT_STICKY, 1), comment(f"{STICKY_MARKER}\nold", 2)],
            [("PATCH", f"/repos/{REPO}/issues/comments/2")],
        ),
        ([{"id": 1, "body": BOT_STICKY, "user": None}], [("POST", COMMENTS)]),
        ([comment(BOT_STICKY, 1)], []),
        (
            [*NOISE, comment(f"{STICKY_MARKER}\nold", 500)],
            [("PATCH", f"/repos/{REPO}/issues/comments/500")],
        ),
    ],
    ids=[
        "none",
        "human-marker-on-line-one",
        "bot-marker-mid-body",
        "bot-marker-not-first-line",
        "skips-human-patches-bot",
        "patches-every-bot-sticky",
        "patches-only-outdated-copies",
        "deleted-user",
        "unchanged",
        "bot-sticky-on-page-two",
    ],
)
def test_publish_patches_own_sticky_not_foreign_marker(
    comments: list[dict[str, Any]], expected: list[tuple[str, str]]
) -> None:
    api = FakeApi(comments=comments)

    publish_to(api, make_review())

    assert api.writes() == expected


ERRORS = [
    pytest.param(TimeoutError(), id="timeout"),
    pytest.param(HTTPError("u", 502, "bad gateway", None, None), id="502"),
    pytest.param(RemoteDisconnected(), id="remote-disconnected"),
    pytest.param(IncompleteRead(b""), id="incomplete-read"),
]


@pytest.mark.parametrize("existing", [False, True], ids=["post", "patch"])
@pytest.mark.parametrize("error", ERRORS)
def test_lost_response_reconciles_without_duplicate(error: BaseException, existing: bool) -> None:
    api = FakeApi(comments=[comment(render_unavailable("old", SHA), 1)] if existing else [])
    api.faults = [(error, "applied")]

    publish_to(api, make_review())

    assert api.stickies() == [BOT_STICKY]


@pytest.mark.parametrize("existing", [False, True], ids=["post", "patch"])
@pytest.mark.parametrize("error", ERRORS)
def test_absent_after_timeout_retries_once(error: BaseException, existing: bool) -> None:
    api = FakeApi(comments=[comment(render_unavailable("old", SHA), 1)] if existing else [])
    api.faults = [(error, "lost")]

    publish_to(api, make_review())

    assert api.stickies() == [BOT_STICKY]
    assert len(api.writes()) == 2

    failing = FakeApi(comments=[comment(render_unavailable("old", SHA), 1)] if existing else [])
    failing.faults = [(error, "lost"), (error, "lost")]
    with pytest.raises(type(error)):
        publish_to(failing, make_review())
    assert len(failing.writes()) == 2


def test_late_completing_post_duplicates_once() -> None:
    api = FakeApi()
    api.faults = [(TimeoutError(), "late")]

    publish_to(api, make_review())
    assert api.stickies() == [BOT_STICKY, BOT_STICKY]

    newer = make_review(verdict="approve", changes=[], issues=[])
    publish_to(api, newer)

    assert api.stickies() == [render_sticky(newer, SHA)] * 2


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_client_errors_are_not_retried(status: int) -> None:
    api = FakeApi()
    api.faults = [(HTTPError("u", status, "no", None, None), "lost")]

    with pytest.raises(HTTPError):
        publish_to(api, make_review())

    assert api.writes() == [("POST", COMMENTS)]


LEAKY_HTML = {
    "split-by-tag": f"<p>{TOKEN[:6]}<em>{TOKEN[6:]}</em></p>",
    "entity": f"<p>{TOKEN[:3]}&#95;{TOKEN[4:]}</p>",
    "href": f'<p><a href="{TOKEN}">link</a></p>',
}


@pytest.mark.parametrize("rendered", LEAKY_HTML.values(), ids=LEAKY_HTML.keys())
@pytest.mark.parametrize("result", [make_review(), "busy"], ids=["review", "unavailable"])
def test_rendered_html_leak_withholds_everything(
    rendered: str, result: dict[str, Any] | str
) -> None:
    api = FakeApi(render=lambda text: rendered)

    publish_to(api, result)

    [body] = api.stickies()
    assert render_unavailable("output withheld (rendered GitHub token pattern)", SHA) == body
    assert api.writes() == [("POST", COMMENTS)]


@pytest.mark.parametrize(
    ("reason", "withheld"),
    [
        (TOKEN, "GitHub token pattern"),
        ("AKIA" + "A" * 16, "AWS access key pattern"),
    ],
    ids=["escaped", "raw"],
)
def test_leaky_outcome_reason_is_withheld(reason: str, withheld: str) -> None:
    api = FakeApi()

    publish_to(api, reason)

    assert api.stickies() == [render_unavailable(f"output withheld ({withheld})", SHA)]
    assert ("POST", "/markdown") not in [(m, p) for m, p, _ in api.calls]


def test_the_stale_sticky_goes_through_the_leak_gate() -> None:
    api = FakeApi(
        heads=[SHA, NEW_HEAD], render=lambda text: LEAKY_HTML["href"] if "Stale" in text else "ok"
    )

    publish_to(api, make_review())

    withheld = "output withheld (rendered GitHub token pattern)"
    assert api.stickies() == [render_unavailable(withheld, SHA)]


@pytest.mark.parametrize("error", [HTTPError("u", 500, "boom", None, None), TimeoutError()])
@pytest.mark.parametrize("result", [make_review(), "busy"], ids=["review", "unavailable"])
def test_render_failure_makes_no_public_writes(
    error: Exception, result: dict[str, Any] | str
) -> None:
    def fail(text: str) -> str:
        raise error

    api = FakeApi(render=fail)

    with pytest.raises(type(error)):
        publish_to(api, result)

    assert api.writes() == []


ENV = {
    "GITHUB_TOKEN": "tok",
    "GITHUB_REPOSITORY": REPO,
    "PR_NUMBER": str(PR),
    "REVIEWED_SHA": SHA,
    "REVIEW_RESULT": "success",
}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"OUTCOME": "ok", "FINDINGS": raw(make_review())}, render_sticky(make_review(), SHA)),
        ({"OUTCOME": "agent timed out"}, render_unavailable("agent timed out", SHA)),
        (
            {"OUTCOME": "ok", "FINDINGS": "not json"},
            render_unavailable("invalid agent output", SHA),
        ),
        ({"OUTCOME": "ok"}, render_unavailable("invalid agent output", SHA)),
        ({}, render_unavailable("no review outcome", SHA)),
        ({"REVIEW_RESULT": "failure"}, render_unavailable("review job failed", SHA)),
        (
            {"REVIEW_RESULT": "cancelled"},
            render_unavailable("review job cancelled or timed out", SHA),
        ),
    ],
    ids=["ok", "reason", "invalid-json", "no-findings", "no-outcome", "failure", "cancelled"],
)
def test_publish_from_env_maps_review_result(overrides: dict[str, str], expected: str) -> None:
    api = FakeApi()

    publish_from_env(ENV | overrides, PublishGithub("tok", request_func=api))

    assert api.stickies() == [expected]


@pytest.mark.parametrize(
    "overrides",
    [
        {"REVIEWED_SHA": ""},
        {"GITHUB_TOKEN": ""},
        {"GITHUB_REPOSITORY": ""},
        {"PR_NUMBER": ""},
        {"REVIEW_RESULT": ""},
        {"REVIEW_RESULT": "unexpected"},
        {"REVIEW_RESULT": "skipped", "REVIEWED_SHA": ""},
    ],
)
def test_publish_from_env_fails_loudly_on_bad_input(overrides: dict[str, str]) -> None:
    api = FakeApi()

    with pytest.raises(SystemExit):
        publish_from_env(ENV | overrides, PublishGithub("tok", request_func=api))

    assert api.calls == []


def test_publish_from_env_rejects_a_non_numeric_pr_number() -> None:
    api = FakeApi()

    with pytest.raises(SystemExit, match="PR_NUMBER"):
        publish_from_env(ENV | {"PR_NUMBER": "abc"}, PublishGithub("tok", request_func=api))

    assert api.calls == []


def test_publish_from_env_is_silent_when_the_review_was_skipped() -> None:
    api = FakeApi()

    publish_from_env(ENV | {"REVIEW_RESULT": "skipped"}, PublishGithub("tok", request_func=api))

    assert api.calls == []


def test_main_returns_zero_after_publishing() -> None:
    api = FakeApi()

    status = main(ENV | {"OUTCOME": "agent timed out"}, PublishGithub("tok", request_func=api))

    assert status == 0
    assert api.stickies() == [render_unavailable("agent timed out", SHA)]


def test_main_lets_a_missing_environment_variable_exit() -> None:
    with pytest.raises(SystemExit):
        main({}, PublishGithub("tok", request_func=FakeApi()))
