from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_review import SCHEMA
from ai_review_agent import (
    DIFF_EXCLUDED_PATHSPECS,
    DISALLOWED_TOOLS,
    EXCLUDED_PATHSPECS,
    SETTINGS,
    agent_cap,
    agent_env,
    changed_lines,
    classify,
    claude_argv,
    main,
    prepare,
    run_agent,
    run_process,
    write_outputs,
)

CANARY = "CANARY-7f3a91"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}
WORKFLOWS = Path(__file__).parents[1] / "workflows"


def capture(cmd: list[str], env: dict[str, str] | None = None, check: bool = True) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, check=check, env=env)  # noqa: S603
    return done.stdout.strip()


def git(cwd: Path, *args: str) -> str:
    return capture(["git", "-C", str(cwd), *args], GIT_ENV)


def commit(pr: Path, files: dict[str, str | bytes], links: dict[str, str] | None = None) -> str:
    for name, content in files.items():
        target = pr / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
    for name, destination in (links or {}).items():
        (pr / name).symlink_to(destination)
    git(pr, "add", "-A")
    git(pr, "commit", "-q", "-m", "c")
    return git(pr, "rev-parse", "HEAD")


def lines(count: int) -> str:
    return "".join(f"line {number}\n" for number in range(count))


def make_pr(
    tmp_path: Path,
    files: dict[str, str | bytes],
    links: dict[str, str] | None = None,
    head: str | None = None,
    title: str = "A title",
    setup: Callable[[Path], None] | None = None,
    base_files: dict[str, str | bytes] | None = None,
    **env: str,
) -> dict[str, str]:
    workspace = tmp_path / "work"
    pr = workspace / "pr"
    pr.mkdir(parents=True)
    git(pr, "init", "-q", "-b", "main")
    base = commit(pr, base_files or {"base.txt": "base\n"})
    git(pr, "update-ref", "refs/remotes/origin/main", base)
    if setup:
        setup(pr)
    reviewed = commit(pr, files, links)
    event = {
        "pull_request": {
            "number": 7,
            "title": title,
            "body": "a body\nsecond line",
            "base": {"ref": "main"},
            "head": {"sha": reviewed if head is None else head},
        }
    }
    (tmp_path / "event.json").write_text(json.dumps(event))
    (tmp_path / "runner").mkdir()
    return {
        "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "PATH": GIT_ENV["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        **env,
    }


def review_dir(env: dict[str, str]) -> Path:
    return Path(env["RUNNER_TEMP"]) / "ai-review"


def outputs(env: dict[str, str]) -> dict[str, str]:
    text = Path(env["GITHUB_OUTPUT"]).read_text()
    return dict(line.split("=", 1) for line in text.splitlines())


def test_exclusions_match_pr_size_check() -> None:
    workflow = (WORKFLOWS / "pr_size_check.yml").read_text()

    quoted = re.findall(r"^\s+'(:\(exclude[^']*)'", workflow, re.MULTILINE)

    assert len(quoted) > 20
    assert tuple(quoted) == EXCLUDED_PATHSPECS


def test_review_pathspecs_are_a_subset_of_the_size_exclusions() -> None:
    assert set(DIFF_EXCLUDED_PATHSPECS) < set(EXCLUDED_PATHSPECS)


def test_excluded_pathspecs_drop_lockfiles_and_markdown(tmp_path: Path) -> None:
    env = make_pr(
        tmp_path,
        {
            "uv.lock": lines(5),
            "README.MD": lines(5),
            "tests/x.py": lines(5),
            ".github/workflows/a.yml": lines(5),
            "src/a.py": lines(3),
        },
    )
    pr = Path(env["GITHUB_WORKSPACE"]) / "pr"

    assert changed_lines(pr, "origin/main", "HEAD") == 3


@pytest.mark.parametrize(
    ("files", "limit", "outcome", "reason"),
    [
        ({"uv.lock": lines(4)}, 5, "skip", "no reviewable changes"),
        ({"src/a.py": lines(5)}, 5, "review", None),
        ({"src/a.py": lines(6)}, 5, "skip", "over MAX_LINES"),
        ({"image.bin": b"\x00\x01\x02" * 100}, 5, "skip", "no reviewable changes"),
    ],
    ids=["nothing-countable", "at-limit", "over-limit", "binary-only"],
)
def test_skip_reason(
    tmp_path: Path, files: dict[str, str | bytes], limit: int, outcome: str, reason: str | None
) -> None:
    env = make_pr(tmp_path, files, MAX_LINES=str(limit))

    result = prepare(env)

    assert result["outcome"] == outcome
    summary = Path(env["GITHUB_STEP_SUMMARY"])
    assert (reason in summary.read_text()) if reason else not summary.exists()


def test_a_skip_writes_no_review_inputs(tmp_path: Path) -> None:
    env = make_pr(tmp_path, {"uv.lock": lines(4)})

    prepare(env)

    assert not review_dir(env).exists()


def test_review_diff_keeps_tests_and_markdown_drops_lockfiles(tmp_path: Path) -> None:
    env = make_pr(
        tmp_path,
        {
            "tests/x.py": "t\n",
            "README.md": "r\n",
            ".github/workflows/a.yml": "w\n",
            "alembic/v.py": "a\n",
            "src/a.py": "s\n",
            "uv.lock": "u\n",
            "web/package-lock.json": "p\n",
            "requirements.txt": "q\n",
            "data/a.hml": "h\n",
        },
    )

    prepare(env)

    diff = (review_dir(env) / "pr.diff").read_text()
    kept = ["tests/x.py", "README.md", ".github/workflows/a.yml", "alembic/v.py", "src/a.py"]
    dropped = ["uv.lock", "package-lock.json", "requirements.txt", "a.hml"]
    assert all(f"b/{name}" in diff for name in kept)
    assert not any(name in diff for name in dropped)
    assert (review_dir(env) / "changed-files.txt").read_text().split() == sorted(kept)


def test_prepare_removes_symlinks_without_changing_diff(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inner").symlink_to("/proc/self/environ")
    env = make_pr(
        tmp_path,
        {"src/a.py": lines(3), "src/real.txt": "real\n"},
        links={
            "to_file": "src/real.txt",
            "to_dir": "src",
            "to_proc": "/proc/self/environ",
            "dangling": "nowhere",
            "to_outside": str(outside),
            "src/nested": "../src/real.txt",
        },
    )
    pr = Path(env["GITHUB_WORKSPACE"]) / "pr"
    (pr / "src" / "a.py").write_text("changed in the worktree only\n")
    expected = git(pr, "diff", "--no-ext-diff", "--no-textconv", "origin/main", "HEAD")

    prepare(env)

    assert [p for p in pr.rglob("*") if p.is_symlink()] == []
    assert (outside / "inner").is_symlink()
    assert (review_dir(env) / "pr.diff").read_text().strip() == expected
    assert "to_proc" in expected


def test_prepare_writes_pr_json_from_event_file(tmp_path: Path) -> None:
    env = make_pr(tmp_path, {"src/a.py": lines(2)}, title="$(touch pwned) `x`")

    result = prepare(env)

    data = json.loads((review_dir(env) / "pr.json").read_text())
    assert data["title"] == "$(touch pwned) `x`"
    assert data["body"] == "a body\nsecond line"
    assert data["number"] == 7
    assert result == {"outcome": "review", "reviewed_sha": data["head_sha"]}
    assert result["reviewed_sha"] == git(Path(env["GITHUB_WORKSPACE"]) / "pr", "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "event_head",
    ["f" * 40, "not-a-sha", "", "HEAD"],
    ids=["other-sha", "non-hex", "empty", "ref-name"],
)
def test_prepare_rejects_a_checkout_that_is_not_the_event_head(
    tmp_path: Path, event_head: str
) -> None:
    env = make_pr(tmp_path, {"src/a.py": lines(2)}, head=event_head)

    with pytest.raises(SystemExit):
        prepare(env)

    assert not review_dir(env).exists()


def test_main_writes_prepare_outputs_to_the_output_file(tmp_path: Path) -> None:
    env = make_pr(tmp_path, {"src/a.py": lines(2)})

    assert main(["prepare"], env) == 0

    assert outputs(env) == {
        "outcome": "review",
        "reviewed_sha": git(Path(env["GITHUB_WORKSPACE"]) / "pr", "rev-parse", "HEAD"),
    }


def test_prepare_does_not_echo_git_output(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    env = make_pr(tmp_path, {f"{CANARY}.py": lines(2)})
    git(Path(env["GITHUB_WORKSPACE"]) / "pr", "config", "core.abbrev", CANARY)

    with pytest.raises(subprocess.CalledProcessError) as raised:
        prepare(env)

    seen = capfd.readouterr()
    assert CANARY not in seen.out + seen.err + str(raised.value)


def test_prepare_does_not_echo_diff_output(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    env = make_pr(tmp_path, {f"{CANARY}.py": lines(2)})

    prepare(env)

    seen = capfd.readouterr()
    assert CANARY not in seen.out + seen.err
    assert CANARY in (review_dir(env) / "pr.diff").read_text()


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\r\nb"])
def test_write_outputs_rejects_a_multi_line_value(tmp_path: Path, value: str) -> None:
    env = {"GITHUB_OUTPUT": str(tmp_path / "output")}

    with pytest.raises(ValueError, match="single line"):
        write_outputs(env, {"outcome": value})

    assert not Path(env["GITHUB_OUTPUT"]).exists()


@pytest.mark.parametrize("attributes", ["* -diff", "* binary", "*.py -diff"])
def test_the_prs_own_gitattributes_cannot_hide_the_diff(tmp_path: Path, attributes: str) -> None:
    env = make_pr(tmp_path, {".gitattributes": f"{attributes}\n", "src/a.py": lines(3)})
    pr = Path(env["GITHUB_WORKSPACE"]) / "pr"

    assert changed_lines(pr, "origin/main", "HEAD") == 4
    assert prepare(env)["outcome"] == "review"
    assert "+line 2" in (review_dir(env) / "pr.diff").read_text()


def add_submodule(pr: Path) -> None:
    sub = pr / "sub"
    sub.mkdir()
    git(sub, "init", "-q", "-b", "main")
    commit(sub, {"x": "1\n"})


def test_a_submodule_bump_is_counted_even_if_gitmodules_ignores_it(tmp_path: Path) -> None:
    modules = '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n\tignore = all\n'
    env = make_pr(tmp_path, {".gitmodules": modules, "src/a.py": lines(3)}, setup=add_submodule)

    prepare(env)

    assert "Subproject commit" in (review_dir(env) / "pr.diff").read_text()


def test_an_oversized_diff_is_skipped_with_a_fixed_reason(tmp_path: Path) -> None:
    env = make_pr(tmp_path, {"src/a.py": lines(2), "tests/big.py": lines(200_000)})

    assert prepare(env)["outcome"] == "skip"

    assert "diff too large" in Path(env["GITHUB_STEP_SUMMARY"]).read_text()
    assert not review_dir(env).exists()


@pytest.mark.parametrize(("delta", "outcome"), [(0, "review"), (-1, "skip")])
def test_the_diff_size_limit_is_inclusive(tmp_path: Path, delta: int, outcome: str) -> None:
    env = make_pr(tmp_path, {"src/a.py": lines(3)})
    prepare(env)
    size = (review_dir(env) / "pr.diff").stat().st_size

    assert prepare(env | {"MAX_DIFF_BYTES": str(size + delta)})["outcome"] == outcome


def test_repo_config_cannot_run_textconv_or_external_diff_drivers(tmp_path: Path) -> None:
    env = make_pr(tmp_path, {"src/a.py": lines(3)}, base_files={".gitattributes": "*.py diff=up\n"})
    pr = Path(env["GITHUB_WORKSPACE"]) / "pr"
    git(pr, "config", "diff.up.textconv", "tr a-z A-Z")
    git(pr, "config", "diff.external", "echo EXTERNAL")

    prepare(env)

    diff = (review_dir(env) / "pr.diff").read_text()
    assert "+line 2" in diff
    assert "EXTERNAL" not in diff


NOW = 1_000_000.0
TOKEN = "oauth-token-0123456789"  # noqa: S105
REVIEW = {
    "verdict": "comment",
    "changes": ["Adds a retry"],
    "issues": [
        {"severity": "note", "path": "app/a.py", "line": 3, "issue": "bug", "fix": "fix it"}
    ],
}


def result_json(structured: object = REVIEW, **fields: object) -> str:
    return json.dumps(
        {"type": "result", "is_error": False, "structured_output": structured} | fields
    )


def findings_size(review: dict[str, Any]) -> int:
    return len(json.dumps(review, ensure_ascii=False, separators=(",", ":")).encode())


def review_with_size(target: int) -> dict[str, Any]:
    review: dict[str, Any] = {
        "verdict": "comment",
        "changes": ["\u20ac" * 4000] * 9 + ["", "", ""],
        "issues": [],
    }
    gap = target - findings_size(review)
    sizes = [min(4000, gap), min(4000, max(0, gap - 4000)), max(0, gap - 8000)]
    review["changes"][-3:] = ["a" * size for size in sizes]
    assert findings_size(review) == target
    return review


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(0, 720), (500, 720), (900, 660), (1_320, 240), (1_380, 180), (1_381, 179), (1_700, -140)],
)
def test_agent_cap_from_deadlines(elapsed: int, expected: int) -> None:
    assert agent_cap(NOW, NOW + elapsed) == expected


def make_tools(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for name in ("node", "claude"):
        (tools / name).write_text("#!/bin/sh\n")
        (tools / name).chmod(0o755)
    return tools


def agent_inputs(tmp_path: Path, **env: str) -> dict[str, str]:
    workspace = tmp_path / "work"
    (workspace / "pr").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runner").mkdir(exist_ok=True)
    return {
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "JOB_STARTED_AT": str(NOW),
        "CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
        "PATH": f"{make_tools(tmp_path)}:/usr/bin",
        "GITHUB_TOKEN": "ghs_step_token",
        "ACTIONS_RUNTIME_TOKEN": "runtime",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "idtoken",
        "AWS_ACCESS_KEY_ID": "AKIAXXXXXXXXXXXXXXXX",
        "ANTHROPIC_BASE_URL": "https://evil.example",
        "HOME": "/home/runner",
        **env,
    }


class Recorder:
    def __init__(self, script: str) -> None:
        self.script = script
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        self.calls.append((argv, kwargs))
        return subprocess.Popen([sys.executable, "-c", self.script], **kwargs)  # noqa: S603


class HungProcess:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.pid = process.pid
        self.expired = False

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None and not self.expired:
            self.expired = True
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.process.wait(timeout)


def printing(stdout: str = "", stderr: str = "", code: int = 0) -> str:
    return (
        f"import sys; sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); sys.exit({code})"
    )


def run(tmp_path: Path, popen: Callable[..., Any], elapsed: int = 0, **env: str) -> dict[str, str]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review it")
    inputs = agent_inputs(tmp_path, **env)
    return run_agent(inputs, popen=popen, now=lambda: NOW + elapsed, prompt_file=prompt)


def test_prompt_placeholders_are_replaced_with_absolute_paths(tmp_path: Path) -> None:
    template = "{ai_review_dir} {pr_dir} {apm_knowledge_dir} {pr_dir}/x {not_ours}"
    recorder = Recorder(printing(result_json()))
    prompt = tmp_path / "prompt.md"
    prompt.write_text(template)

    run_agent(agent_inputs(tmp_path), popen=recorder, now=lambda: NOW, prompt_file=prompt)

    work = tmp_path / "work"
    expected = f"{tmp_path}/runner/ai-review {work}/pr {work}/travtus-apm/.knowledge {work}/pr/x"
    assert recorder.calls[0][0][2] == f"{expected} {{not_ours}}"


def test_agent_env_is_exact_allowlist(tmp_path: Path) -> None:
    recorder = Recorder(printing(result_json()))

    run(tmp_path, recorder)

    [(argv, kwargs)] = recorder.calls
    child_env = kwargs["env"]
    assert child_env.keys() == {"PATH", "HOME", "TMPDIR", "LANG", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert child_env["PATH"] == f"{tmp_path / 'tools'}:/usr/bin:/bin"
    assert child_env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert child_env["LANG"] == "C.UTF-8"
    runner = tmp_path / "runner"
    assert Path(child_env["HOME"]).parent == runner
    assert Path(child_env["TMPDIR"]).parent == Path(child_env["HOME"])
    assert Path(child_env["TMPDIR"]).is_dir()
    assert kwargs["cwd"] == tmp_path / "work" / "pr"
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert argv == claude_argv(
        "review it", SETTINGS, tmp_path / "work" / "travtus-apm", runner / "ai-review"
    )


def test_agent_env_omits_missing_tools(tmp_path: Path) -> None:
    assert agent_env({"PATH": str(tmp_path)}, tmp_path)["PATH"] == "/usr/bin:/bin"


def test_agent_output_files_outside_add_dir_roots(tmp_path: Path) -> None:
    recorder = Recorder(printing(result_json()))

    run(tmp_path, recorder)

    [(argv, kwargs)] = recorder.calls
    roots = [Path(argv[i + 1]) for i, arg in enumerate(argv) if arg == "--add-dir"]
    roots.append(kwargs["cwd"])
    for stream in (kwargs["stdout"], kwargs["stderr"]):
        path = Path(stream.name)
        assert path.parent == tmp_path / "runner" / "agent-io"
        assert not any(root in path.parents for root in roots)


def test_claude_argv() -> None:
    argv = claude_argv("PROMPT", Path("/s/settings.json"), Path("/w/apm"), Path("/t/ai-review"))

    assert argv == [
        "claude",
        "-p",
        "PROMPT",
        "--tools",
        "Read,Grep,Glob",
        "--disallowedTools",
        "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch,Task,Skill",
        "--setting-sources",
        "",
        "--settings",
        "/s/settings.json",
        "--strict-mcp-config",
        "--add-dir",
        "/w/apm",
        "--add-dir",
        "/t/ai-review",
        "--max-turns",
        "40",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA, separators=(",", ":")),
    ]


def test_trusted_settings_deny_every_disallowed_tool() -> None:
    deny = DISALLOWED_TOOLS.split(",")

    assert json.loads(SETTINGS.read_text()) == {
        "permissions": {
            "defaultMode": "dontAsk",
            "deny": deny,
            "blockReadsOutsideWorkingDirectories": True,
        },
        "disableAllHooks": True,
    }


@pytest.mark.parametrize(
    ("elapsed", "launched"), [(1_380, True), (1_381, False)], ids=["exactly-180s", "179s"]
)
def test_not_enough_time_launches_nothing(tmp_path: Path, elapsed: int, launched: bool) -> None:
    recorder = Recorder(printing(result_json()))

    outputs = run(tmp_path, recorder, elapsed=elapsed)

    assert bool(recorder.calls) == launched
    assert (outputs["outcome"] == "ok") == launched
    if not launched:
        assert outputs == {"outcome": "insufficient_time", "findings": ""}


def test_main_runs_the_agent_and_writes_one_line_outputs(tmp_path: Path) -> None:
    env = agent_inputs(tmp_path, JOB_STARTED_AT="0")

    assert main(["run-agent"], env) == 0

    assert Path(env["GITHUB_OUTPUT"]).read_text() == "outcome=insufficient_time\nfindings=\n"


@pytest.mark.parametrize(
    ("script", "outcome"),
    [
        (printing(result_json()), "ok"),
        (printing(result_json(), code=1), "agent_failed"),
        (printing("not json"), "agent_failed"),
        (printing("Claude usage limit reached|123", code=1), "usage_limit"),
        (
            printing(result_json(None, is_error=True, result="Claude usage limit reached")),
            "usage_limit",
        ),
        (printing(result_json(None, is_error=True, result="auth failed")), "agent_failed"),
        (printing(result_json({"verdict": "meh"})), "invalid_output"),
        (printing(result_json(None)), "invalid_output"),
        (printing(result_json(REVIEW, result="this mentions a usage limit")), "ok"),
    ],
    ids=[
        "ok",
        "non-zero-exit",
        "not-json",
        "usage-limit-text",
        "usage-limit-error",
        "other-error",
        "schema-violation",
        "no-structured-output",
        "success-mentioning-usage-limit",
    ],
)
def test_run_agent_outcomes(tmp_path: Path, script: str, outcome: str) -> None:
    result = run(tmp_path, Recorder(script))

    assert result["outcome"] == outcome
    assert (result["findings"] != "") == (outcome == "ok")


def test_a_hung_agent_is_a_timeout(tmp_path: Path) -> None:
    def popen(argv: list[str], **kwargs: Any) -> HungProcess:  # noqa: ANN401
        return HungProcess(
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        )  # noqa: S603

    assert run(tmp_path, popen) == {"outcome": "timeout", "findings": ""}


def test_a_missing_agent_binary_is_agent_failed(tmp_path: Path) -> None:
    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:  # noqa: ANN401
        return subprocess.Popen([str(tmp_path / "absent")], **kwargs)  # noqa: S603

    assert run(tmp_path, popen) == {"outcome": "agent_failed", "findings": ""}


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "[]",
        '"text"',
        "null",
        "{",
        "[" * 100_000,
        '{"structured_output": ' + "[" * 100_000,
        '{"structured_output": {"verdict": "comment", "changes": ["\\ud800"], "issues": []}}',
        '{"structured_output": {"verdict": "comment", "changes": ["\\u0000"], "issues": []}}',
    ],
    ids=[
        "empty",
        "array",
        "string",
        "null",
        "truncated",
        "deep-nesting",
        "deep-structured-output",
        "lone-surrogate",
        "nul-character",
    ],
)
def test_hostile_output_never_raises(stdout: str) -> None:
    outcome, findings = classify(0, stdout, "", [TOKEN])

    assert outcome in {"agent_failed", "invalid_output", "ok"}
    assert (findings != "") == (outcome == "ok")
    findings.encode()


def test_invalid_utf8_output_is_agent_failed(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.buffer.write(b'\\xff\\xfe{')"

    assert run(tmp_path, Recorder(script)) == {"outcome": "agent_failed", "findings": ""}


@pytest.mark.parametrize(
    ("target", "outcome"),
    [(120_000, "ok"), (120_001, "invalid_output")],
)
def test_findings_size_guard(target: int, outcome: str) -> None:
    review = review_with_size(target)

    result, findings = classify(0, result_json(review), "", [TOKEN])

    assert result == outcome
    assert (len(findings.encode()) if findings else 0) == (target if outcome == "ok" else 0)


@pytest.mark.parametrize(
    "leaked",
    [
        TOKEN,
        TOKEN[:8] + "\u200b" + TOKEN[8:],
        TOKEN[:8] + "\u2060" + TOKEN[8:],
        "AKIA" + "A" * 16,
        "ghp_" + "a" * 36,
    ],
    ids=["plain", "zero-width-space", "word-joiner", "aws-pattern", "github-pattern"],
)
def test_leak_in_output_withheld(leaked: str) -> None:
    review = {**REVIEW, "changes": [f"saw {leaked} here"]}

    assert classify(0, result_json(review), "", [TOKEN]) == ("output_withheld", "")


def test_outputs_are_single_line(tmp_path: Path) -> None:
    review = {**REVIEW, "changes": ["a\nb\r\nc\noutcome=ok\nEOF\nfindings=x\u2028y"]}
    env = {"GITHUB_OUTPUT": str(tmp_path / "output")}
    outcome, findings = classify(0, result_json(review), "", [TOKEN])

    write_outputs(env, {"outcome": outcome, "findings": findings})

    [first, second] = Path(env["GITHUB_OUTPUT"]).read_text().split("\n")[:-1]
    assert first == "outcome=ok"
    assert json.loads(second.removeprefix("findings="))["changes"] == review["changes"]


@pytest.mark.parametrize(
    ("script", "elapsed"),
    [
        (printing(CANARY + " out", CANARY + " err", 1), 0),
        (printing(result_json({**REVIEW, "changes": [CANARY]}), CANARY), 0),
        (printing(CANARY, CANARY), 0),
    ],
    ids=["failure", "success", "not-json"],
)
def test_run_agent_does_not_echo_subprocess_output(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], script: str, elapsed: int
) -> None:
    run(tmp_path, Recorder(script), elapsed)

    seen = capfd.readouterr()
    assert CANARY not in seen.out + seen.err


GRANDCHILD = (
    "import os, pathlib, signal, sys, time;"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
    "time.sleep(60)"
)
LEADER = textwrap.dedent(f"""
    import pathlib, subprocess, sys, time
    marker = pathlib.Path(sys.argv[1])
    subprocess.Popen([sys.executable, "-c", {GRANDCHILD!r}, sys.argv[1]])
    while not marker.exists() or not marker.read_text():
        time.sleep(0.01)
    if sys.argv[2] == "hang":
        time.sleep(60)
    """)


LEADER_BODY = LEADER.replace("sys.argv[1]", '"grandchild.pid"').replace(
    'sys.argv[2] == "hang"', "True"
)


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = capture(["ps", "-o", "stat=", "-p", str(pid)], check=False)
    return bool(state) and not state.startswith("Z")


def wait_until_gone(pid: int) -> bool:
    for _ in range(50):
        if not is_alive(pid):
            return True
        time.sleep(0.1)
    return False


def spawn(tmp_path: Path, script: str, *args: str, cap: float = 30) -> int | None:
    argv = [sys.executable, "-c", script, *args]
    out, err = tmp_path / "out", tmp_path / "err"
    return run_process(argv, tmp_path, dict(os.environ), out, err, cap)


@pytest.mark.parametrize(
    ("mode", "cap", "returncode"),
    [("hang", 1, None), ("exit", 30, 0)],
    ids=["timeout-kills-a-term-ignoring-grandchild", "normal-exit-kills-a-lingering-grandchild"],
)
def test_run_agent_kills_process_group(
    tmp_path: Path, mode: str, cap: float, returncode: int | None
) -> None:
    marker = tmp_path / "grandchild.pid"

    assert spawn(tmp_path, LEADER, str(marker), mode, cap=cap) == returncode

    assert wait_until_gone(int(marker.read_text()))


def test_a_process_group_that_is_already_gone_is_not_an_error(tmp_path: Path) -> None:
    original = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        assert spawn(tmp_path, "pass") == 0
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, original)


def test_a_timed_out_agent_gets_sigterm_before_sigkill(tmp_path: Path) -> None:
    marker = tmp_path / "terminated"
    script = textwrap.dedent("""
        import pathlib, signal, sys, time
        def stop(*_):
            pathlib.Path(sys.argv[1]).write_text("x")
            sys.exit(0)
        signal.signal(signal.SIGTERM, stop)
        time.sleep(60)
        """)

    assert spawn(tmp_path, script, str(marker), cap=2) is None

    assert marker.exists()


RUN_AGENT = textwrap.dedent(f"""
    import os, sys
    from pathlib import Path
    sys.path.insert(0, {str(Path(__file__).parent)!r})
    from ai_review_agent import run_agent
    run_agent(dict(os.environ), prompt_file=Path(sys.argv[1]))
    """)


def test_sigterm_to_run_agent_kills_the_agent_group(tmp_path: Path) -> None:
    env = agent_inputs(tmp_path, JOB_STARTED_AT=str(time.time()))
    claude = Path(env["PATH"].split(":")[0]) / "claude"
    claude.write_text(f"#!{sys.executable}\n" + LEADER_BODY)
    claude.chmod(0o755)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review it")
    marker = tmp_path / "work" / "pr" / "grandchild.pid"
    child = subprocess.Popen([sys.executable, "-c", RUN_AGENT, str(prompt)], env=env)  # noqa: S603
    try:
        for _ in range(200):
            if marker.exists() and marker.read_text():
                break
            time.sleep(0.1)
        child.send_signal(signal.SIGTERM)
        child.wait(timeout=30)
    finally:
        child.kill()

    assert wait_until_gone(int(marker.read_text()))
