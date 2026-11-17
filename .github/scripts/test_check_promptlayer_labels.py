# Standard library
from __future__ import annotations

# Third-party
import pytest

# First-party
from check_promptlayer_labels import LabelLookupError, find_problems, version_from_response

VERSIONS = {
    ("aligned", "uat"): 3,
    ("aligned", "prod"): 3,
    ("SQL Selection - Warehouse", "uat"): 2,
    ("SQL Selection - Warehouse", "prod"): 2,
    ("drifted", "uat"): 50,
    ("drifted", "prod"): 47,
    ("uat_only", "uat"): 1,
}


def fake_fetch(name: str, label: str) -> int:
    try:
        return VERSIONS[(name, label)]
    except KeyError:
        raise LabelLookupError(f"{name}: could not resolve label '{label}' (HTTP 404).") from None


def problems(prompt_list: str) -> list[str]:
    return find_problems(prompt_list, "uat", "prod", fake_fetch)


def test_aligned_prompts_have_no_problems() -> None:
    assert problems("aligned\nSQL Selection - Warehouse\n") == []


def test_comments_blank_lines_and_surrounding_whitespace_are_ignored() -> None:
    assert problems("# prompts\n\n  aligned  \r\n\t\naligned") == []


def test_mismatch_reports_both_versions() -> None:
    assert problems("drifted") == ["drifted: uat is version 50, prod is version 47."]


def test_every_prompt_is_checked_after_a_failure() -> None:
    found = problems("unknown\ndrifted\naligned\nuat_only")

    assert found == [
        "unknown: could not resolve label 'uat' (HTTP 404).",
        "unknown: could not resolve label 'prod' (HTTP 404).",
        "drifted: uat is version 50, prod is version 47.",
        "uat_only: could not resolve label 'prod' (HTTP 404).",
    ]


def test_version_pin_is_rejected_without_a_lookup() -> None:
    assert problems("aligned==3") == [
        "aligned==3: version pinning is not supported yet; list the prompt name only."
    ]


@pytest.mark.parametrize("prompt_list", ["", "\n\n", "# only a comment\n"])
def test_list_without_prompts_is_a_problem(prompt_list: str) -> None:
    assert problems(prompt_list) == ["The prompt list has no prompts."]


def test_version_from_response_reads_the_version() -> None:
    assert version_from_response(b'{"version": 7, "prompt_template": {}}') == 7


@pytest.mark.parametrize(
    "body",
    [b"<html>oops</html>", b"", b'{"prompt_template": {}}', b'{"version": null}', b"[1]"],
)
def test_version_from_response_rejects_bodies_without_a_version(body: bytes) -> None:
    with pytest.raises(LabelLookupError):
        version_from_response(body)
