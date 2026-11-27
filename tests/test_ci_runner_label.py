"""Guard: every Blacksmith runner label goes through the CI_RUNNER variable.

A fork without Blacksmith runners sets the ``CI_RUNNER`` repository variable
(for example to ``ubuntu-latest``); with it unset or empty each job falls back
to its own Blacksmith label. A bare Blacksmith label, or a ``CI_RUNNER``
expression without a Blacksmith fallback, breaks one side of that contract.

``GITHUB_HOSTED_FALLBACKS`` names the jobs that fall back to a GitHub-hosted
label instead: each must use ``${{ vars.CI_RUNNER || '<that label>' }}`` and
nothing else. Only listed jobs may use a non-Blacksmith ``CI_RUNNER`` fallback.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

CI_RUNNER_PATTERN = re.compile(r"^\$\{\{ vars\.CI_RUNNER \|\| '(blacksmith-[0-9]+vcpu-ubuntu-2404)' \}\}$")

# (workflow file, job id) -> the GitHub-hosted label its CI_RUNNER expression falls back to.
GITHUB_HOSTED_FALLBACKS = {("runtime-herdr-core-live.yml", "live"): "ubuntu-24.04"}


def _hosted_expression(label: str) -> str:
    return f"${{{{ vars.CI_RUNNER || '{label}' }}}}"


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.y*ml"))


def _label_problem(value: object, workflow: str = "", job_id: str = "") -> str | None:
    """Return why a ``runs-on`` value is rejected, or None when it is allowed."""
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            problem = _label_problem(item, workflow, job_id)
            if problem:
                return problem
        return None
    if not isinstance(value, str):
        return f"unexpected runs-on type {type(value).__name__}"
    hosted = GITHUB_HOSTED_FALLBACKS.get((workflow, job_id))
    if hosted is not None:
        if value == _hosted_expression(hosted):
            return None
        if "vars.CI_RUNNER" in value:
            return f"CI_RUNNER must fall back to {hosted!r}"
        return f"must go through CI_RUNNER with fallback {hosted!r}"
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
        if _label_problem(value, name, job_id):
            rejected.append(f"{name}:{job_id}: {value!r}")
    return rejected


def _load_all() -> dict[str, object]:
    return {path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in _workflow_files()}


def test_workflow_runner_labels_use_ci_runner_with_blacksmith_default() -> None:
    rejected = [entry for name, data in _load_all().items() for entry in _rejections(name, data)]
    assert not rejected, "runs-on must be ${{ vars.CI_RUNNER || 'blacksmith-…' }}:\n" + "\n".join(rejected)


def _uses_ci_runner(workflow: str, job_id: str, value: str) -> bool:
    hosted = GITHUB_HOSTED_FALLBACKS.get((workflow, job_id))
    if hosted is not None:
        return value == _hosted_expression(hosted)
    return bool(CI_RUNNER_PATTERN.fullmatch(value))


def test_guard_sees_workflows_and_ci_runner_jobs() -> None:
    workflows = _load_all()
    assert workflows, "no workflow files found"
    matched = [
        f"{name}:{job_id}"
        for name, data in workflows.items()
        for job_id, job in _jobs(data).items()
        if isinstance(job, dict) and isinstance(job.get("runs-on"), str) and _uses_ci_runner(name, job_id, job["runs-on"])
    ]
    assert matched, "no job uses the CI_RUNNER expression; the guard walked nothing"
    for workflow, job_id in GITHUB_HOSTED_FALLBACKS:
        assert f"{workflow}:{job_id}" in matched, f"{workflow}:{job_id} is allowlisted but does not use its expression"


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


@pytest.mark.parametrize(
    ("workflow", "job_id", "runs_on", "rejected"),
    [
        ("runtime-herdr-core-live.yml", "live", "${{ vars.CI_RUNNER || 'ubuntu-24.04' }}", False),
        ("runtime-herdr-core-live.yml", "live", "ubuntu-24.04", True),
        ("runtime-herdr-core-live.yml", "live", "${{ vars.CI_RUNNER || 'blacksmith-2vcpu-ubuntu-2404' }}", True),
        ("example.yml", "job", "${{ vars.CI_RUNNER || 'ubuntu-24.04' }}", True),
    ],
)
def test_guard_applies_github_hosted_fallback_allowlist(workflow: str, job_id: str, runs_on: str, rejected: bool) -> None:
    data = yaml.safe_load(f"jobs:\n  {job_id}:\n    runs-on: {runs_on}\n")
    assert data["jobs"][job_id]["runs-on"] == runs_on
    assert bool(_rejections(workflow, data)) is rejected


def test_guard_rejects_unexpected_runs_on_type() -> None:
    data = yaml.safe_load("jobs:\n  job:\n    runs-on: 42\n")
    assert _rejections("example.yml", data) == ["example.yml:job: 42"]
