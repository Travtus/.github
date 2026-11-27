from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, cast

MAX_LINES = 1000
MAX_DIFF_BYTES = 1_000_000

CASE_FOLDED = ("*.md", "test/**", "tests/**", "unit_test/**", "unit_tests/**", "alembic*/**")
LOCKFILE_NAMES = (
    "open_api.yaml requirements*.txt uv.lock poetry.lock Pipfile.lock package-lock.json "
    "npm-shrinkwrap.json yarn.lock pnpm-lock.yaml bun.lock bun.lockb Cargo.lock Gemfile.lock "
    "go.sum composer.lock"
).split()
GENERATED_PATHS = ("*.hml", "connectors/**/schema.graphql", "*-supergraph/**/schema.graphql")
LOCKFILES = tuple(f":(exclude,glob)**/{name}" for name in LOCKFILE_NAMES)
GENERATED = tuple(f":(exclude,glob)**/{path}" for path in GENERATED_PATHS)
EXCLUDED_PATHSPECS = (
    *(f":(exclude,icase,glob)**/{path}" for path in CASE_FOLDED),
    *LOCKFILES,
    ":(exclude,glob)pyproject.toml",
    ":(exclude,glob).github/workflows/**",
    *GENERATED,
)
DIFF_EXCLUDED_PATHSPECS = LOCKFILES + GENERATED


class OutputTooLargeError(Exception):
    """A git command printed more than the review can take."""


def _git(pr: Path, *args: str, limit: int = MAX_DIFF_BYTES) -> bytes:
    command = ["git", "-C", str(pr), *args]
    with subprocess.Popen(  # noqa: S603
        command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as process:
        output = cast(IO[bytes], process.stdout).read(limit + 1)
        if len(output) > limit:
            process.kill()
            raise OutputTooLargeError
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)
    return output


def _diff(
    pr: Path,
    base: str,
    head: str,
    pathspecs: Sequence[str],
    *flags: str,
    limit: int = MAX_DIFF_BYTES,
) -> bytes:
    # The base supplies .gitattributes so the PR cannot mark its own files -diff or binary.
    options = ["--no-ext-diff", "--no-textconv", "--ignore-submodules=none", *flags]
    command = [f"--attr-source={base}", "diff", *options, base, head, "--", ".", *pathspecs]
    return _git(pr, *command, limit=limit)


def changed_lines(pr: Path, base: str, head: str) -> int:
    numstat = _diff(pr, base, head, EXCLUDED_PATHSPECS, "--numstat").decode(errors="replace")
    total = 0
    for line in numstat.split("\n"):
        fields = line.split("\t", 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            total += int(fields[0]) + int(fields[1])
    return total


def remove_symlinks(root: Path) -> None:
    for directory, names, files in os.walk(root):
        for name in names + files:
            path = Path(directory, name)
            if path.is_symlink():
                path.unlink()


def _skip(env: Mapping[str, str], head: str, reason: str) -> dict[str, str]:
    with Path(env["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"AI review skipped: {reason}\n")
    return {"outcome": "skip", "reviewed_sha": head}


def prepare(env: Mapping[str, str]) -> dict[str, str]:
    pr = Path(env["GITHUB_WORKSPACE"]) / "pr"
    review_dir = Path(env["RUNNER_TEMP"]) / "ai-review"
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())["pull_request"]
    head = _git(pr, "rev-parse", "HEAD").decode().strip()
    if head != event["head"]["sha"]:
        raise SystemExit("checked-out commit is not the event head")
    base = _git(pr, "merge-base", f"origin/{event['base']['ref']}", head).decode().strip()
    limit = int(env.get("MAX_DIFF_BYTES", MAX_DIFF_BYTES))
    try:
        total = changed_lines(pr, base, head)
        print(f"changed lines: {total}")
        if total == 0:
            return _skip(env, head, "no reviewable changes")
        if total > int(env.get("MAX_LINES", MAX_LINES)):
            return _skip(env, head, "over MAX_LINES")
        diff = _diff(pr, base, head, DIFF_EXCLUDED_PATHSPECS, limit=limit)
        names = _diff(pr, base, head, DIFF_EXCLUDED_PATHSPECS, "--name-only", limit=limit)
    except OutputTooLargeError:
        return _skip(env, head, "diff too large")
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "pr.diff").write_bytes(diff)
    (review_dir / "changed-files.txt").write_bytes(names)
    details = {
        "number": event["number"],
        "title": event["title"],
        "body": event["body"] or "",
        "base_ref": event["base"]["ref"],
        "head_sha": head,
    }
    (review_dir / "pr.json").write_text(json.dumps(details))
    remove_symlinks(pr)
    return {"outcome": "review", "reviewed_sha": head}


def write_outputs(env: Mapping[str, str], outputs: Mapping[str, str]) -> None:
    for key, value in outputs.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"output {key} is not a single line")
    with Path(env["GITHUB_OUTPUT"]).open("a") as sink:
        sink.writelines(f"{key}={value}\n" for key, value in outputs.items())


COMMANDS: dict[str, Callable[[Mapping[str, str]], dict[str, str]]] = {"prepare": prepare}


def main(argv: Sequence[str], env: Mapping[str, str] = os.environ) -> int:
    write_outputs(env, COMMANDS[argv[0]](env))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
