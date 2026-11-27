"""build-basic's CI gate and merge, GitHub path plus forge-independent logic.

Every gate scenario runs `checks/ci-gate.sh` under gc's scrubbed check
environment (plan RA-3) against a fake `gc` and a stub `gh`; see
ci_gate_harness.py. Requirements: REQ-001..REQ-013 and REQ-015 of the
ci-gate-merge build, plus plan amendments RA-1 (stale head) and RA-2 (merge
false failure).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets" / "scripts"))

import ci_gate  # noqa: E402
from ci_gate_harness import GATE_SH, ROOT_ID, Harness  # noqa: E402


TOKEN = "SENTINEL-gh-7f3a9c-do-not-leak"
PR_URL = "https://github.com/acme/demo-app/pull/12"
OLD_SHA = "1111111111111111111111111111111111111111"
NEW_SHA = "2222222222222222222222222222222222222222"
REQ013_CI = {"green", "red", "timeout", "none"}


def view(state: str = "OPEN", sha: str = NEW_SHA) -> dict:
    return {"stdout": {"state": state, "headRefOid": sha}}


def checks(*items: tuple[str, str], code: int = 0) -> dict:
    payload = [
        {"name": name, "bucket": bucket, "state": bucket.upper(), "workflow": "ci",
         "link": f"https://github.com/acme/demo-app/actions/runs/11/job/{index + 20}"}
        for index, (name, bucket) in enumerate(items)
    ]
    return {"stdout": payload, "code": code}


NO_CHECKS = {"stdout": "", "stderr": "no checks reported on the 'build/x' branch\n", "code": 1}


@pytest.fixture
def h(tmp_path) -> Harness:
    harness = Harness(tmp_path)
    harness.root(**{"gc.build.pr_url": PR_URL, "gc.build.pr_number": "12", "gc.build.publish_forge": "github"})
    harness.config({"github.com": harness.token("github", TOKEN)})
    harness.expect_gh_token(TOKEN)
    return harness


def assert_secret_kept(h: Harness, *results) -> None:
    for result in results:
        assert TOKEN not in result.out and TOKEN not in result.err
    assert TOKEN not in h.everything_written()
    for call in h.gh_log():
        assert TOKEN not in json.dumps(call["argv"])


def assert_req013(meta: dict) -> None:
    assert meta["gc.build.ci_status"] in REQ013_CI
    assert meta["gc.build.ci_rounds"].isdigit()
    status = meta["gc.build.merge_status"]
    assert status == "merged" or status.startswith(("skipped:", "failed:")), status


def gh_calls(h: Harness, key: str) -> list[dict]:
    return [call for call in h.gh_log() if " ".join(call["argv"][:2]) == key]


# --------------------------------------------------------------------------
# the gate


def test_green_records_verdict_and_uses_the_token_only_in_gh_env(h) -> None:
    h.gh("pr view", view())
    h.gh("pr checks", checks(("lint", "pass"), ("codeql", "skipping")))
    result = h.wait()
    assert result.code == 0, result.err
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "green"
    assert meta["gc.build.ci_rounds"] == "0"
    assert meta["gc.build.ci_head_sha"] == NEW_SHA
    assert "gc.build.merge_status" not in meta
    calls = h.gh_log()
    assert [c["argv"][:2] for c in calls] == [["pr", "view"], ["pr", "checks"]]
    for call in calls:
        assert call["gh_token_ok"], "gh did not get the configured token as GH_TOKEN"
        config_dir = call["gh_config_dir"]
        assert config_dir and not config_dir.startswith(str(h.city)), "GH_CONFIG_DIR must be a per-run temp dir"
        assert not os.path.exists(config_dir), "the per-run GH_CONFIG_DIR is removed afterwards"
    assert len(result.err.strip().splitlines()) == 1
    assert_secret_kept(h, result)


def test_red_with_rounds_left_writes_capped_redacted_findings_and_exits_1(h) -> None:
    log = "\n".join([f"step line {i}" for i in range(300)] + [
        f"leaked {TOKEN}", "Authorization: token abcdefabcdefabcdef", "ghs_" + "a" * 36, "password=hunter2",
        "FAILED tests/test_x.py::test_y",
    ])
    h.gh("pr view", view())
    h.gh("pr checks", checks(("lint", "pass"), ("test", "fail")))
    h.gh("run view", {"stdout": log})
    result = h.wait()
    assert result.code == 1, result.err
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "red"
    assert meta["gc.build.ci_rounds"] == "0"
    assert meta["gc.build.ci_reason"] == "test"
    assert "gc.build.merge_status" not in meta
    findings = Path(meta["gc.build.ci_findings_path"])
    assert findings == h.rig / "plans" / "demo" / "build" / "ci" / "round-1-findings.md"
    text = findings.read_text(encoding="utf-8")
    assert "## test" in text and "## lint" not in text
    assert "FAILED tests/test_x.py::test_y" in text
    assert "step line 0\n" not in text, "the tail keeps only the last 80 lines"
    tail = text.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert len(tail.splitlines()) <= 80 and len(tail.encode()) <= 8 * 1024
    for secret in (TOKEN, "abcdefabcdefabcdef", "ghs_" + "a" * 36, "hunter2"):
        assert secret not in text
    run_view = gh_calls(h, "run view")
    assert run_view and run_view[0]["argv"] == ["run", "view", "--job", "21", "--repo", "acme/demo-app", "--log-failed"]
    assert not any(c["argv"][:2] in (["run", "rerun"], ["run", "cancel"]) for c in h.gh_log())
    assert "red" in result.err and len(result.err.strip().splitlines()) == 1
    assert_secret_kept(h, result)


def test_red_at_the_cap_ends_the_loop_and_merge_fails_ci_red(h) -> None:
    h.root(**{"gc.var.ci_fix_rounds": "3", "gc.build.ci_status": "red", "gc.build.ci_head_sha": OLD_SHA})
    h.subject(**{"ci_fix.action": "pushed", "ci_fix.pushed_sha": NEW_SHA})
    h.gh("pr view", view(sha=NEW_SHA))
    h.gh("pr checks", checks(("test", "fail")))
    h.gh("run view", {"stdout": "boom"})
    result = h.wait(attempt=4)
    assert result.code == 0, result.err
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "red"
    assert meta["gc.build.ci_rounds"] == "3"
    assert meta["gc.build.merge_status"] == "skipped:ci_red"
    assert_req013(meta)
    merged = h.merge()
    assert merged.json["outcome"] == "fail" and merged.json["failure_class"] == "ci_red"
    assert "test" in merged.json["failure_reason"]
    assert not gh_calls(h, "pr merge")


def test_always_red_schedules_exactly_cap_fix_rounds(h) -> None:
    """AC-5: with the cap at 3, attempts 1..3 exit 1 (three fix rounds), attempt 4 ends it."""
    h.root(**{"gc.var.ci_fix_rounds": "3"})
    h.gh("pr checks", checks(("test", "fail")))
    codes = []
    for attempt in range(1, 5):
        sha = f"{attempt}" * 40
        h.subject(**{"ci_fix.action": "pushed", "ci_fix.pushed_sha": sha})
        h.gh("pr view", view(sha=sha))
        codes.append(h.wait(attempt=attempt).code)
    assert codes == [1, 1, 1, 0]
    assert h.root_meta()["gc.build.merge_status"] == "skipped:ci_red"


def test_gave_up_ends_the_loop_red_without_a_forge_call(h) -> None:
    h.root(**{"gc.build.ci_status": "red", "gc.build.ci_head_sha": OLD_SHA})
    h.subject(**{"ci_fix.action": "gave_up", "ci_fix.reason": "runner outage"})
    result = h.wait(attempt=2)
    assert result.code == 0
    meta = h.root_meta()
    assert meta["gc.build.merge_status"] == "skipped:ci_red"
    assert "gave_up" in meta["gc.build.ci_reason"] and "runner outage" in meta["gc.build.ci_reason"]
    assert meta["gc.build.ci_rounds"] == "1"
    assert h.gh_log() == []


def test_pending_then_green_keeps_waiting_without_extra_exits(h) -> None:
    h.gh("pr view", view())
    h.gh("pr checks", checks(("test", "pending")), checks(("test", "pending")), checks(("test", "pass")))
    result = h.wait(GC_CI_GATE_TIMEOUT_SECONDS="20")
    assert result.code == 0, result.err
    assert h.root_meta()["gc.build.ci_status"] == "green"
    assert len(gh_calls(h, "pr checks")) == 3


def test_timeout_leaves_the_pr_open_and_merge_fails_ci_timeout(h) -> None:
    h.gh("pr view", view())
    h.gh("pr checks", checks(("test", "pending")))
    started = time.monotonic()
    result = h.wait(GC_CI_GATE_TIMEOUT_SECONDS="0.5")
    assert time.monotonic() - started < 15
    assert result.code == 0, result.err
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "timeout"
    assert meta["gc.build.merge_status"] == "skipped:ci_timeout"
    assert meta["gc.build.ci_reason"].startswith("timeout_after_90m")
    assert_req013(meta)
    merged = h.merge()
    assert merged.json["outcome"] == "fail" and merged.json["failure_class"] == "ci_timeout"
    assert not gh_calls(h, "pr merge")


def test_a_rerun_resumes_this_rounds_deadline(h) -> None:
    h.root(**{"gc.build.ci_round_started": f"1:{int(time.time()) - 100}"})
    h.gh("pr view", view())
    h.gh("pr checks", checks(("test", "pending")))
    started = time.monotonic()
    result = h.wait(GC_CI_GATE_TIMEOUT_SECONDS="102", GC_CI_GATE_POLL_SECONDS="0.5")
    assert time.monotonic() - started < 60, "a resumed round must not restart its timer"
    assert result.code == 0
    assert h.root_meta()["gc.build.ci_status"] == "timeout"


def test_a_stamp_from_another_attempt_is_replaced(h) -> None:
    h.root(**{"gc.build.ci_round_started": "1:5"})
    h.gh("pr view", view())
    h.gh("pr checks", checks(("test", "pass")))
    h.subject(**{"ci_fix.action": "pushed", "ci_fix.pushed_sha": NEW_SHA})
    assert h.wait(attempt=2).code == 0
    assert h.root_meta()["gc.build.ci_round_started"].startswith("2:")


def test_no_ci_on_the_first_round_is_none_and_merges(h) -> None:
    h.gh("pr view", view())
    h.gh("pr checks", NO_CHECKS)
    result = h.wait()
    assert result.code == 0, result.err
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "none"
    assert meta["gc.build.ci_rounds"] == "0"
    assert meta["gc.build.ci_reason"].startswith("no_ci_within_")
    h.gh("pr merge", {"stdout": ""})
    merged = h.merge()
    assert merged.json["merge_status"] == "merged" and merged.json["outcome"] == "pass"
    assert_req013(h.root_meta())


def test_no_checks_after_a_fix_keeps_waiting_until_timeout(h) -> None:
    h.root(**{"gc.build.ci_status": "red", "gc.build.ci_head_sha": OLD_SHA})
    h.subject(**{"ci_fix.action": "pushed", "ci_fix.pushed_sha": NEW_SHA})
    h.gh("pr view", view(sha=NEW_SHA))
    h.gh("pr checks", NO_CHECKS)
    result = h.wait(attempt=2, GC_CI_GATE_TIMEOUT_SECONDS="4")
    assert result.code == 0
    assert h.root_meta()["gc.build.ci_status"] == "timeout"
    assert len(gh_calls(h, "pr checks")) > 1


def test_stale_head_after_a_fix_is_pending_until_the_pushed_sha_is_green(h) -> None:
    """RA-1: the forge still shows the red sha for a moment after the fix push."""
    h.root(**{"gc.build.ci_status": "red", "gc.build.ci_head_sha": OLD_SHA})
    h.subject(**{"ci_fix.action": "pushed", "ci_fix.pushed_sha": NEW_SHA})
    h.gh("pr view", view(sha=OLD_SHA), view(sha=NEW_SHA))
    h.gh("pr checks", checks(("test", "fail")), checks(("test", "pass")))
    result = h.wait(attempt=2, GC_CI_GATE_TIMEOUT_SECONDS="20")
    assert result.code == 0, result.err
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "green"
    assert meta["gc.build.ci_rounds"] == "1"
    assert meta["gc.build.ci_head_sha"] == NEW_SHA


def test_no_fix_pushed_and_head_unchanged_ends_red(h) -> None:
    h.root(**{"gc.build.ci_status": "red", "gc.build.ci_head_sha": OLD_SHA})
    h.subject(**{"ci_fix.action": "noop"})
    h.gh("pr view", view(sha=OLD_SHA))
    h.gh("pr checks", checks(("test", "fail")))
    result = h.wait(attempt=2)
    assert result.code == 0
    meta = h.root_meta()
    assert meta["gc.build.ci_reason"] == "no_fix_pushed"
    assert meta["gc.build.merge_status"] == "skipped:ci_red"


@pytest.mark.parametrize(("state", "merge_status"), [("MERGED", "skipped:already_merged"), ("CLOSED", "skipped:pr_closed")])
def test_a_pr_merged_or_closed_by_someone_else_settles_all_three_keys(h, state, merge_status) -> None:
    h.gh("pr view", view(state=state))
    h.gh("pr checks", checks(("test", "pending")))
    assert h.wait().code == 0
    meta = h.root_meta()
    assert meta["gc.build.merge_status"] == merge_status
    assert meta["gc.build.ci_status"] == "none"
    assert_req013(meta)
    assert h.merge().json["outcome"] == "pass"


def test_no_pr_makes_no_forge_call_and_reads_no_config(h) -> None:
    h.root(**{"gc.build.pr_url": ""})
    (h.city / ".gc" / "ci-gate.json").unlink()
    result = h.wait()
    assert result.code == 0
    meta = h.root_meta()
    assert meta["gc.build.merge_status"] == "skipped:no_pr"
    assert "gc.build.ci_status" not in meta
    assert h.gh_log() == []
    assert h.merge().json == {**h.merge().json, "merge_status": "skipped:no_pr", "outcome": "pass"}


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("no_config", "config_missing"),
        ("bad_config", "config_invalid"),
        ("other_host", "no_token_for_host"),
        ("missing_token_file", "token_unreadable"),
        ("http_401", "http_401"),
    ],
)
def test_unreadable_ci_fails_fast(h, setup, code) -> None:
    config = h.city / ".gc" / "ci-gate.json"
    if setup == "no_config":
        config.unlink()
    elif setup == "bad_config":
        config.write_text("{not json", encoding="utf-8")
    elif setup == "other_host":
        h.config({"gitea.example": h.token("other", "x")})
    elif setup == "missing_token_file":
        h.config({"github.com": h.city / "absent.token"})
    else:
        h.gh("pr view", {"stderr": "HTTP 401: Bad credentials (https://api.github.com/graphql)\n", "code": 1})
    started = time.monotonic()
    result = h.wait(GC_CI_GATE_TIMEOUT_SECONDS="30")
    assert time.monotonic() - started < 15, "unreadable must not wait out the timeout"
    assert result.code == 0, result.err
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "timeout"
    assert meta["gc.build.ci_reason"] == f"unreadable:{code}"
    assert meta["gc.build.merge_status"] == "skipped:ci_unreadable"
    merged = h.merge()
    assert merged.json["outcome"] == "fail" and merged.json["failure_class"] == "ci_unreadable"
    assert_secret_kept(h, result, merged)


def test_gh_missing_from_the_gate_path_is_unreadable(h) -> None:
    (h.bin / "gh").unlink()
    result = h.wait()
    assert result.code == 0
    assert h.root_meta()["gc.build.ci_reason"] == "unreadable:gh_missing"


def test_startup_bead_failure_is_an_attempt_free_rerun(h) -> None:
    h.state["fail_show"] = True
    h.save()
    assert h.wait().code == 75


def test_stderr_summary_is_one_capped_line(h) -> None:
    name = "evil\nname " + "x" * 600
    h.gh("pr view", view())
    h.gh("pr checks", {"stdout": [{"name": name, "bucket": "fail", "link": ""}]})
    result = h.wait()
    assert result.code == 1
    lines = result.err.strip().splitlines()
    assert len(lines) == 1
    assert len(lines[0]) < 1600
    assert "x" * 201 not in lines[0]


def test_unknown_gh_bucket_fails_closed(h) -> None:
    h.gh("pr view", view())
    h.gh("pr checks", checks(("weird", "brand-new-bucket")))
    assert h.wait().code == 1
    assert h.root_meta()["gc.build.ci_status"] == "red"


def test_clamps(h) -> None:
    assert ci_gate.clamp_int("99", 3, 0, 10) == 10
    assert ci_gate.clamp_int("0", 90, 1, 240) == 1
    assert ci_gate.clamp_int("junk", 3, 0, 10) == 3
    assert ci_gate.clamp_int(None, 90, 1, 240) == 90
    h.root(**{"gc.var.ci_fix_rounds": "99", "gc.var.ci_timeout_minutes": "0"})
    h.subject(**{"ci_fix.action": "pushed", "ci_fix.pushed_sha": NEW_SHA})
    h.gh("pr view", view())
    h.gh("pr checks", checks(("test", "fail")))
    result = h.wait(attempt=11)
    assert result.code == 0, "round 11 is past the clamped cap of 10"
    assert h.root_meta()["gc.build.ci_rounds"] == "10"
    h.gh("pr checks", checks(("test", "pending")))
    h.wait(attempt=11, GC_CI_GATE_TIMEOUT_SECONDS="0.3")
    assert h.root_meta()["gc.build.ci_reason"].startswith("timeout_after_1m")


def test_gate_wrapper_is_executable_and_needs_no_model() -> None:
    assert os.access(GATE_SH, os.X_OK)
    text = GATE_SH.read_text(encoding="utf-8")
    assert "ci_gate.py\" wait" in text
    for word in ("claude", "gc session", "sleep"):
        assert word not in text


# --------------------------------------------------------------------------
# the merge


def green_root(h: Harness, **extra: str) -> None:
    h.root(**{"gc.build.ci_status": "green", "gc.build.ci_rounds": "0", "gc.build.ci_head_sha": NEW_SHA, **extra})


def test_merge_argv_is_squash_delete_pinned_and_runs_outside_any_checkout(h) -> None:
    green_root(h)
    h.gh("pr merge", {"stdout": ""})
    result = h.merge(env={"GH_TOKEN_UNUSED": "1"})
    assert result.code == 0, result.err
    assert result.json["merge_status"] == "merged" and result.json["outcome"] == "pass"
    calls = gh_calls(h, "pr merge")
    assert len(calls) == 1
    assert calls[0]["argv"] == ["pr", "merge", PR_URL, "--squash", "--delete-branch", "--match-head-commit", NEW_SHA]
    cwd = calls[0]["cwd"]
    assert not cwd.startswith(str(h.city)) and not cwd.startswith(str(h.rig))
    assert not os.path.exists(cwd), "the merge's temp cwd is removed afterwards"
    assert h.root_meta()["gc.build.merge_status"] == "merged"


def test_merge_false_never_calls_merge(h) -> None:
    green_root(h, **{"gc.var.merge": "false"})
    result = h.merge()
    assert result.json["merge_status"] == "skipped:merge_false" and result.json["outcome"] == "pass"
    assert not gh_calls(h, "pr merge")
    assert_req013(h.root_meta())


def test_merge_failure_records_failed_gh_exit(h) -> None:
    green_root(h)
    h.gh("pr merge", {"stderr": "Pull request is not mergeable\n", "code": 1})
    h.gh("pr view", view(state="OPEN"))
    result = h.merge()
    assert result.json["merge_status"] == "failed:gh_exit_1"
    assert result.json["outcome"] == "fail" and result.json["failure_class"] == "merge_failed"
    assert PR_URL in result.json["failure_reason"]
    assert h.root_meta()["gc.build.merge_status"] == "failed:gh_exit_1"


def test_merge_exit_1_after_a_remote_merge_is_merged(h) -> None:
    """RA-2: gh exits nonzero after merging (local branch delete), the PR says MERGED."""
    green_root(h)
    h.gh("pr merge", {"stderr": "failed to delete local branch\n", "code": 1})
    h.gh("pr view", {"stdout": {"state": "MERGED"}})
    result = h.merge()
    assert result.json["merge_status"] == "merged" and result.json["outcome"] == "pass"


def test_merge_without_a_verdict_fails(h) -> None:
    result = h.merge()
    assert result.json["merge_status"] == "failed:no_ci_verdict" and result.json["outcome"] == "fail"
    assert not gh_calls(h, "pr merge")


def test_merge_is_idempotent_once_merged(h) -> None:
    green_root(h, **{"gc.build.merge_status": "merged"})
    result = h.merge()
    assert result.json["outcome"] == "pass"
    assert not gh_calls(h, "pr merge")
