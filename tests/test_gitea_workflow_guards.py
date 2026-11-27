"""Guard: every workflow job has a decision for Gitea Actions.

On Gitea each job either runs on the repository's own runner or skips with the
job-level guard ``if: github.server_url == 'https://github.com'``, which is
true on github.com and false on Gitea. A job missing from both sets below (a
new upstream job, for example) fails until someone decides which it is.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

GITHUB_ONLY = "github.server_url == 'https://github.com'"

SKIPS_ON_GITEA = {
    ("codeql.yml", "analyze"),
    ("runtime-herdr-core-live.yml", "live"),
    ("supported-pack-nightly.yml", "inference"),
    ("gascity-pack-inference.yml", "supported-pack-formulas"),
}

RUNS_ON_GITEA = {
    ("ci.yml", "check"),
    ("ci.yml", "runtime-herdr-core"),
    ("ci.yml", "runtime-herdr-core-result"),
    ("supported-pack-nightly.yml", "static-contracts"),
    ("pack-release-compatibility.yml", "latest-pack-releases"),
}

RIPGREP_STEP = "Ensure ripgrep for Gastown shell tests"
SHELL_TESTS_STEP = "Run Gastown shell tests"


def _workflows() -> dict[str, dict]:
    return {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for path in sorted(WORKFLOWS_DIR.glob("*.y*ml"))
    }


def _jobs(data: object) -> dict:
    if not isinstance(data, dict):
        return {}
    jobs = data.get("jobs") or {}
    return jobs if isinstance(jobs, dict) else {}


def _job(workflow: str, job_id: str) -> dict:
    jobs = _jobs(_workflows().get(workflow))
    assert job_id in jobs, f"{workflow}:{job_id} not found"
    return jobs[job_id]


def _normalize(expression: object) -> str:
    text = str(expression).strip()
    wrapped = re.fullmatch(r"\$\{\{(.*)\}\}", text, flags=re.DOTALL)
    if wrapped:
        text = wrapped.group(1)
    return " ".join(text.split())


def _active(run: str) -> list[str]:
    return [line.strip() for line in run.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_every_job_has_a_gitea_decision() -> None:
    assert not SKIPS_ON_GITEA & RUNS_ON_GITEA, f"jobs in both sets: {sorted(SKIPS_ON_GITEA & RUNS_ON_GITEA)}"
    found = {(name, job_id) for name, data in _workflows().items() for job_id in _jobs(data)}
    assert found, "no workflow jobs found"
    decided = SKIPS_ON_GITEA | RUNS_ON_GITEA
    problems = [f"{workflow}:{job_id} has no Gitea decision" for workflow, job_id in sorted(found - decided)]
    problems += [f"{workflow}:{job_id} is listed but does not exist" for workflow, job_id in sorted(decided - found)]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(("workflow", "job_id"), sorted(SKIPS_ON_GITEA))
def test_github_only_jobs_carry_the_guard(workflow: str, job_id: str) -> None:
    condition = _job(workflow, job_id).get("if")
    assert condition is not None, f"{workflow}:{job_id} must skip on Gitea with `if: {GITHUB_ONLY}`"
    assert _normalize(condition) == GITHUB_ONLY, (
        f"{workflow}:{job_id} has `if: {condition}`; it must be exactly `if: {GITHUB_ONLY}`"
    )


@pytest.mark.parametrize(("workflow", "job_id"), sorted(RUNS_ON_GITEA))
def test_gitea_jobs_are_not_skipped_on_gitea(workflow: str, job_id: str) -> None:
    condition = _job(workflow, job_id).get("if")
    if condition is None:
        return
    assert "server_url" not in _normalize(condition), (
        f"{workflow}:{job_id} runs on Gitea but has `if: {condition}`"
    )


def test_check_installs_ripgrep_before_gastown_shell_tests() -> None:
    steps = _job("ci.yml", "check")["steps"]
    names = [step.get("name") for step in steps]
    assert RIPGREP_STEP in names, f"ci.yml:check has no step {RIPGREP_STEP!r}; found: {names}"
    assert SHELL_TESTS_STEP in names, f"ci.yml:check has no step {SHELL_TESTS_STEP!r}; found: {names}"
    assert names.index(RIPGREP_STEP) < names.index(SHELL_TESTS_STEP), (
        f"ci.yml:check must run {RIPGREP_STEP!r} before {SHELL_TESTS_STEP!r}"
    )
    step = steps[names.index(RIPGREP_STEP)]
    assert "if" not in step, f"ci.yml:check step {RIPGREP_STEP!r} must not have a step-level if"
    lines = _active(step["run"])
    assert "if command -v rg >/dev/null 2>&1; then" in lines, lines
    assert 'if [ "$(id -u)" -eq 0 ]; then' in lines, lines
    assert "elif command -v sudo >/dev/null 2>&1; then" in lines, lines
    installs = [line for line in lines if "apt-get install" in line and "ripgrep" in line]
    assert len(installs) == 2 and sum(line.startswith("sudo ") for line in installs) == 1, installs
    assert "exit 1" in lines, lines
    assert not [line for line in lines if "|| true" in line], f"ci.yml:check step {RIPGREP_STEP!r} must not mask failures"
