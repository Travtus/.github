from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, cast

from ai_review import SCHEMA, InvalidOutputError, Outcome, find_leak, validate

MAX_LINES = 1000
MAX_DIFF_BYTES = 1_000_000
AGENT_CAP_SECONDS = 720
JOB_SECONDS = 1800
RESERVE_SECONDS = 240
MIN_AGENT_SECONDS = 180
KILL_GRACE_SECONDS = 15
MAX_FINDINGS_BYTES = 120_000
DISALLOWED_TOOLS = "Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch,Task,Skill"
SETTINGS = Path(__file__).with_name("ai_review_claude_settings.json")
PROMPT = Path(__file__).with_name("ai_review_prompt.md")
LOOKUP_TOOL = Path("travtus-apm/packages/travtus-agent-pack/tools/knowledge-lookup")
LOOKUP_BUDGET_SECONDS = 180
LANGUAGE_BY_SUFFIX = {".py": "python", ".ts": "typescript", ".tsx": "typescript"}
LOOKUP_ENV_KEYS = (
    "PATH HOME LANG TMPDIR RUNNER_TEMP UV_CACHE_DIR UV_PROJECT_ENVIRONMENT UV_FROZEN UV_NO_DEV"
).split()
USAGE_LIMIT = re.compile(r"usage limit", re.IGNORECASE)

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


def lookup_queries(repo: str, changed_files: str) -> list[tuple[str, str]]:
    suffixes = {Path(name).suffix.lower() for name in changed_files.splitlines()}
    languages = sorted({LANGUAGE_BY_SUFFIX[s] for s in suffixes if s in LANGUAGE_BY_SUFFIX})
    queries = [(repo, "auto"), (f"{repo} review", "rule")]
    return queries + [(f"{language} review", "rule") for language in languages]


def lookup(
    env: Mapping[str, str],
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    now: Callable[[], float] = time.monotonic,
    budget: float = LOOKUP_BUDGET_SECONDS,
) -> dict[str, str]:
    workspace = Path(env["GITHUB_WORKSPACE"])
    temp = Path(env["RUNNER_TEMP"])
    repo = env["GITHUB_REPOSITORY"].rsplit("/", 1)[-1]
    queries = lookup_queries(
        repo, (temp / "ai-review" / "changed-files.txt").read_text(errors="replace")
    )
    knowledge, agent_io = temp / "ai-review" / "knowledge", temp / "agent-io"
    knowledge.mkdir(parents=True, exist_ok=True)
    agent_io.mkdir(exist_ok=True)
    root = workspace / "travtus-apm" / ".knowledge"
    tool_env = {key: env[key] for key in LOOKUP_ENV_KEYS if key in env}
    deadline = now() + budget
    saved = 0
    for number, (query, kind) in enumerate(queries):
        remaining = deadline - now()
        if remaining <= 0:
            break
        argv = [str(workspace / LOOKUP_TOOL), f"--query={query}", "--kind", kind]
        argv += ["--root", str(root), "--json"]
        result = knowledge / f"{number}.json"
        try:
            stderr = agent_io / f"lookup-{number}.stderr"
            code = run_process(argv, workspace, tool_env, result, stderr, remaining, popen)
        except OSError:
            code = None
        if code == 0:
            saved += 1
        else:
            result.unlink(missing_ok=True)
    print(f"knowledge lookups saved: {saved} of {len(queries)}")
    return {}


def write_outputs(env: Mapping[str, str], outputs: Mapping[str, str]) -> None:
    for key, value in outputs.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"output {key} is not a single line")
    with Path(env["GITHUB_OUTPUT"]).open("a") as sink:
        sink.writelines(f"{key}={value}\n" for key, value in outputs.items())


def agent_cap(job_started_at: float, now: float) -> float:
    return min(AGENT_CAP_SECONDS, job_started_at + JOB_SECONDS - RESERVE_SECONDS - now)


def agent_env(env: Mapping[str, str], home: Path) -> dict[str, str]:
    found = (shutil.which(tool, path=env.get("PATH")) for tool in ("node", "claude"))
    tool_dirs = dict.fromkeys(os.path.dirname(path) for path in found if path)
    return {
        "PATH": ":".join([*tool_dirs, "/usr/bin", "/bin"]),
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8",
        "CLAUDE_CODE_OAUTH_TOKEN": env.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
    }


def claude_argv(prompt: str, settings: Path, apm_dir: Path, review_dir: Path) -> list[str]:
    return [
        "claude",
        "-p",
        prompt,
        "--tools",
        "Read,Grep,Glob",
        "--disallowedTools",
        DISALLOWED_TOOLS,
        "--setting-sources",
        "",
        "--settings",
        str(settings),
        "--strict-mcp-config",
        "--add-dir",
        str(apm_dir),
        "--add-dir",
        str(review_dir),
        "--max-turns",
        "40",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA, separators=(",", ":")),
    ]


def _kill_group(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, sig)


def _exit_on_sigterm(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def run_process(
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    cap: float,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> int | None:
    previous = signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                return process.wait(timeout=cap)
            except subprocess.TimeoutExpired:
                _kill_group(process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=KILL_GRACE_SECONDS)
                return None
            finally:
                _kill_group(process.pid, signal.SIGKILL)
    finally:
        signal.signal(signal.SIGTERM, previous)


def classify(returncode: int, stdout: str, stderr: str, secrets: list[str]) -> tuple[Outcome, str]:
    try:
        result = json.loads(stdout)
    except (ValueError, RecursionError):
        result = None
    if returncode != 0 or not isinstance(result, dict) or result.get("is_error"):
        usage_limit = USAGE_LIMIT.search(f"{stdout}\n{stderr}")
        return ("usage_limit" if usage_limit else "agent_failed"), ""
    try:
        review = validate(json.dumps(result.get("structured_output")))
        if find_leak(review, secrets):
            return "output_withheld", ""
        findings = json.dumps(review, ensure_ascii=False, separators=(",", ":"))
        if len(findings.encode()) > MAX_FINDINGS_BYTES:
            return "invalid_output", ""
    except (InvalidOutputError, ValueError):
        return "invalid_output", ""
    return "ok", findings


def run_agent(
    env: Mapping[str, str],
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    now: Callable[[], float] = time.time,
    prompt_file: Path = PROMPT,
) -> dict[str, str]:
    cap = agent_cap(float(env["JOB_STARTED_AT"]), now())
    if cap < MIN_AGENT_SECONDS:
        return {"outcome": "insufficient_time", "findings": ""}
    workspace = Path(env["GITHUB_WORKSPACE"])
    temp = Path(env["RUNNER_TEMP"])
    agent_io = temp / "agent-io"
    agent_io.mkdir(exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="agent-home-", dir=temp))
    (home / "tmp").mkdir()
    apm_dir, review_dir = workspace / "travtus-apm", temp / "ai-review"
    prompt = prompt_file.read_text()
    places = {
        "ai_review_dir": review_dir,
        "pr_dir": workspace / "pr",
        "apm_knowledge_dir": apm_dir / ".knowledge",
    }
    for name, path in places.items():
        prompt = prompt.replace(f"{{{name}}}", str(path))
    argv = claude_argv(prompt, SETTINGS, apm_dir, review_dir)
    stdout_path, stderr_path = agent_io / "claude.stdout", agent_io / "claude.stderr"
    try:
        code = run_process(
            argv, workspace / "pr", agent_env(env, home), stdout_path, stderr_path, cap, popen
        )
    except OSError:
        return {"outcome": "agent_failed", "findings": ""}
    outcome: Outcome = "timeout"
    findings = ""
    if code is not None:
        stdout = stdout_path.read_text(errors="replace")
        stderr = stderr_path.read_text(errors="replace")
        outcome, findings = classify(code, stdout, stderr, [env.get("CLAUDE_CODE_OAUTH_TOKEN", "")])
    print(f"agent outcome: {outcome}")
    return {"outcome": outcome, "findings": findings}


COMMANDS: dict[str, Callable[[Mapping[str, str]], dict[str, str]]] = {
    "prepare": prepare,
    "lookup": lookup,
    "run-agent": run_agent,
}


def main(argv: Sequence[str], env: Mapping[str, str] = os.environ) -> int:
    write_outputs(env, COMMANDS[argv[0]](env))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
