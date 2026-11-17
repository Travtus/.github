from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

FetchVersion = Callable[[str, str], int]

PROMPTLAYER_URL = "https://api.promptlayer.com/prompt-templates"
ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2


class LabelLookupError(Exception):
    """The version behind a prompt label could not be resolved."""


def find_problems(
    prompt_list: str, label_a: str, label_b: str, fetch_version: FetchVersion
) -> list[str]:
    """Return one message per prompt whose two labels do not resolve to the same version."""
    names = [line.strip() for line in prompt_list.splitlines()]
    names = [name for name in names if name and not name.startswith("#")]
    if not names:
        return ["The prompt list has no prompts."]

    problems: list[str] = []
    for name in names:
        if "==" in name:
            problems.append(
                f"{name}: version pinning is not supported yet; list the prompt name only."
            )
            continue

        versions: dict[str, int] = {}
        for label in (label_a, label_b):
            try:
                versions[label] = fetch_version(name, label)
            except LabelLookupError as error:
                problems.append(str(error))

        if len(versions) < len({label_a, label_b}):
            continue
        if versions[label_a] != versions[label_b]:
            problems.append(
                f"{name}: {label_a} is version {versions[label_a]}, "
                f"{label_b} is version {versions[label_b]}."
            )
        else:
            print(f"ok  {name}: {label_a} and {label_b} are both version {versions[label_a]}")
    return problems


def version_from_response(body: bytes) -> int:
    try:
        version = json.loads(body)["version"]
    except (ValueError, KeyError, TypeError):
        version = None
    if not isinstance(version, int):
        raise LabelLookupError("PromptLayer response has no version.")
    return version


def fetch_label_version(api_key: str, name: str, label: str) -> int:
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        f"{PROMPTLAYER_URL}/{urllib.parse.quote(name, safe='')}",
        data=json.dumps({"label": label}).encode(),
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    reason = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_DELAY_SECONDS)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                return version_from_response(response.read())
        except urllib.error.HTTPError as error:
            reason = f"HTTP {error.code}"
            if error.code != 429 and error.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, LabelLookupError) as error:
            reason = str(error)
    raise LabelLookupError(
        f"{name}: could not resolve label '{label}' ({reason}): "
        "missing label, unknown prompt, bad API key or unexpected response."
    )


def main() -> int:
    prompts_file = Path(os.environ["PROMPTS_FILE"])
    api_key = os.environ["PROMPTLAYER_API_KEY"]
    if not prompts_file.is_file():
        print(f"::error::Prompt list '{prompts_file}' not found.")
        return 1

    problems = find_problems(
        prompts_file.read_text(),
        os.environ["LABEL_A"],
        os.environ["LABEL_B"],
        lambda name, label: fetch_label_version(api_key, name, label),
    )
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
