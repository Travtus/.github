from __future__ import annotations

import json
import re
from typing import Any

import pytest

from ai_review import (
    MAX_BODY,
    MAX_CHANGES,
    MAX_PATH,
    MAX_TEXT,
    SCHEMA,
    STICKY_MARKER,
    InvalidOutputError,
    _fit,
    find_leak,
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
        assert body.endswith("\n\n\u2026truncated\n")
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
