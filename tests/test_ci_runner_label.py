"""Guard: every Blacksmith runner label goes through the CI_RUNNER variable.

A fork without Blacksmith runners sets the ``CI_RUNNER`` repository variable
(for example to ``ubuntu-latest``); with it unset or empty each job falls back
to its own Blacksmith label. A bare Blacksmith label, or a ``CI_RUNNER``
expression without a Blacksmith fallback, breaks one side of that contract.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

CI_RUNNER_PATTERN = re.compile(r"^\$\{\{ vars\.CI_RUNNER \|\| '(blacksmith-[0-9]+vcpu-ubuntu-2404)' \}\}$")


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.y*ml"))


def _label_problem(value: object) -> str | None:
    """Return why a ``runs-on`` value is rejected, or None when it is allowed."""
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            problem = _label_problem(item)
            if problem:
                return problem
        return None
    if not isinstance(value, str):
        return f"unexpected runs-on type {type(value).__name__}"
    if CI_RUNNER_PATTERN.fullmatch(value):
        return None
    if "blacksmith-" in value:
        return "Blacksmith label must be the CI_RUNNER fallback"
    if "vars.CI_RUNNER" in value:
        return "CI_RUNNER must fall back to a Blacksmith label"
    return None


def _jobs(data: object) -> dict:
    if not isinstance(data, dict):
        return {}
    jobs = data.get("jobs") or {}
    return jobs if isinstance(jobs, dict) else {}


def _rejections(name: str, data: object) -> list[str]:
    rejected = []
    for job_id, job in _jobs(data).items():
        value = job.get("runs-on") if isinstance(job, dict) else None
        if _label_problem(value):
            rejected.append(f"{name}:{job_id}: {value!r}")
    return rejected


def _load_all() -> dict[str, object]:
    return {path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in _workflow_files()}


def test_workflow_runner_labels_use_ci_runner_with_blacksmith_default() -> None:
    rejected = [entry for name, data in _load_all().items() for entry in _rejections(name, data)]
    assert not rejected, "runs-on must be ${{ vars.CI_RUNNER || 'blacksmith-…' }}:\n" + "\n".join(rejected)


def test_guard_sees_workflows_and_ci_runner_jobs() -> None:
    workflows = _load_all()
    assert workflows, "no workflow files found"
    matched = [
        f"{name}:{job_id}"
        for name, data in workflows.items()
        for job_id, job in _jobs(data).items()
        if isinstance(job, dict) and isinstance(job.get("runs-on"), str) and CI_RUNNER_PATTERN.fullmatch(job["runs-on"])
    ]
    assert matched, "no job uses the CI_RUNNER expression; the guard walked nothing"


@pytest.mark.parametrize(
    ("runs_on", "rejected"),
    [
        ("blacksmith-2vcpu-ubuntu-2404", True),
        ("${{ vars.CI_RUNNER }}", True),
        ("${{ vars.CI_RUNNER || 'ubuntu-latest' }}", True),
        ("ubuntu-24.04", False),
        ("${{ vars.CI_RUNNER || 'blacksmith-32vcpu-ubuntu-2404' }}", False),
        ("${{ vars.CI_RUNNER || 'blacksmith-2vcpu-ubuntu-2404' }}", False),
    ],
)
def test_guard_classifies_runner_labels(runs_on: str, rejected: bool) -> None:
    data = yaml.safe_load(f"jobs:\n  job:\n    runs-on: {runs_on}\n")
    assert data["jobs"]["job"]["runs-on"] == runs_on
    assert bool(_rejections("example.yml", data)) is rejected


def test_guard_rejects_unexpected_runs_on_type() -> None:
    data = yaml.safe_load("jobs:\n  job:\n    runs-on: 42\n")
    assert _rejections("example.yml", data) == ["example.yml:job: 42"]
