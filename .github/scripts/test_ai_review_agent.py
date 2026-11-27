from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_review_agent import (
    DIFF_EXCLUDED_PATHSPECS,
    EXCLUDED_PATHSPECS,
    changed_lines,
    main,
    prepare,
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
