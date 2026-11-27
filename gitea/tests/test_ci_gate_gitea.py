"""build-basic's CI gate and merge on a Gitea rig, end to end (ci-gate-merge REQ-015).

gascity's `checks/ci-gate.sh` runs under gc's scrubbed check environment (plan
RA-3) with a fake `gc` whose `formula show` search paths include this pack, so
the gate finds the real `gitea_pr.py` the way it does in a city. Every forge
call lands on a local fake Gitea (127.0.0.1, port 0) whose request log is the
evidence. A stub `gh` sits on PATH and must never be called (the gitea path
never falls back to GitHub).

Token placement: the gate reads only the read token named in the city's
`.gc/ci-gate.json`; the merge reads only the publisher's GC_GITEA_TOKEN_FILE.
Each must appear in the Authorization header of its own requests and nowhere
else: not in paths, queries, bodies, other headers, output, metadata or
findings.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT / "gascity" / "tests"))

from ci_gate_harness import Harness  # noqa: E402
from test_gitea_pr import FakeGitea, fake_gitea  # noqa: E402


READ_TOKEN = "SENTINEL-read-5b1e-do-not-leak"
WRITE_TOKEN = "SENTINEL-write-9c2d-do-not-leak"
OWNER, REPO = "acme", "demo-app"
API = f"/api/v1/repos/{OWNER}/{REPO}"
PR = f"{API}/pulls/7"
MERGE = f"{PR}/merge"
OLD_SHA = "1111111111111111111111111111111111111111"
NEW_SHA = "2222222222222222222222222222222222222222"


def status_path(sha: str) -> str:
    return f"{API}/commits/{sha}/status"


def pr_json(sha: str = NEW_SHA, state: str = "open", merged: bool = False) -> dict:
    return {"number": 7, "state": state, "merged": merged, "html_url": f"http://gitea.test/{OWNER}/{REPO}/pulls/7",
            "head": {"ref": "build/gp-root", "sha": sha}}


def statuses(*items: tuple[str, str], combined: str = "", url: str = "") -> dict:
    data = [{"context": name, "status": state, "description": f"{name} {state}", "target_url": url} for name, state in items]
    return {"state": combined, "statuses": data, "total_count": len(data)}


class Server:
    """Routes for one scenario; list values are popped in order, the last one sticks."""

    def __init__(self) -> None:
        self.table: dict = {}

    def set(self, method: str, path: str, *responses) -> None:
        self.table[(method, path)] = list(responses)

    def __call__(self, method, path, query, body):
        entry = self.table.get((method, path))
        if not entry:
            return 404, {"message": "not found"}, {}
        response = entry.pop(0) if len(entry) > 1 else entry[0]
        return response[0], response[1], {}


@pytest.fixture
def env(tmp_path):
    server = Server()
    with fake_gitea(server) as fake:
        h = Harness(tmp_path, search_paths=[REPO_ROOT / "gascity" / "formulas", REPO_ROOT / "gitea" / "formulas"])
        h.root(**{
            "gc.build.pr_url": f"{fake.url}/{OWNER}/{REPO}/pulls/7",
            "gc.build.pr_number": "7",
            "gc.build.publish_forge": "gitea",
            "gc.build.publish_api_base": fake.url,
            "gc.build.publish_owner": OWNER,
            "gc.build.publish_repo": REPO,
            "gc.build.pr_head_branch": "build/gp-root",
            "gc.build.pr_head_worktree": str(h.rig),
        })
        h.config({fake.url.split("://", 1)[1]: h.token("read", READ_TOKEN)})
        write = h.token("write", WRITE_TOKEN)
        subprocess.run(["git", "init", "-q", str(h.rig)], check=True)
        subprocess.run(["git", "-C", str(h.rig), "remote", "add", "origin", f"ssh://git@gitea.test/{OWNER}/{REPO}.git"], check=True)
        merge_env = {"GC_GITEA_BASE_URL": fake.url, "GC_GITEA_TOKEN_FILE": str(write)}
        yield h, server, fake, merge_env


def assert_tokens_placed(h: Harness, fake: FakeGitea, *results, merge_from: int | None = None) -> None:
    """Requests before index `merge_from` are the gate's (read token); from it on, the merge's (write token)."""
    merge_from = len(fake.log) if merge_from is None else merge_from
    for index, entry in enumerate(fake.log):
        expected = WRITE_TOKEN if index >= merge_from else READ_TOKEN
        other = READ_TOKEN if expected == WRITE_TOKEN else WRITE_TOKEN
        assert entry.headers.get("Authorization") == f"token {expected}", (entry.method, entry.path)
        assert other not in json.dumps(entry.headers)
        for token in (READ_TOKEN, WRITE_TOKEN):
            assert token not in entry.path
            assert token not in json.dumps(entry.query)
            assert token not in json.dumps(entry.body)
            assert token not in json.dumps({k: v for k, v in entry.headers.items() if k.lower() != "authorization"})
    written = h.everything_written()
    for token in (READ_TOKEN, WRITE_TOKEN):
        assert token not in written
        for result in results:
            assert token not in result.out and token not in result.err
    assert h.gh_log() == [], "the gitea path must never call gh"


def merges(fake: FakeGitea) -> list:
    return [entry for entry in fake.log if entry.method == "POST"]


def test_green_then_merge_posts_squash_delete_once_with_the_write_token(env) -> None:
    h, server, fake, merge_env = env
    server.set("GET", PR, (200, pr_json()))
    server.set("GET", status_path(NEW_SHA), (200, statuses(("lint", "success"), ("test", "success"), combined="success")))
    server.set("POST", MERGE, (200, None))
    gate = h.wait()
    assert gate.code == 0, gate.err
    meta = h.root_meta()
    assert (meta["gc.build.ci_status"], meta["gc.build.ci_rounds"], meta["gc.build.ci_head_sha"]) == ("green", "0", NEW_SHA)
    assert merges(fake) == []
    merge_from = len(fake.log)
    merged = h.merge(env=merge_env)
    assert merged.code == 0, merged.err
    assert merged.json["merge_status"] == "merged" and merged.json["outcome"] == "pass"
    posts = merges(fake)
    assert len(posts) == 1 and posts[0].path == MERGE
    assert posts[0].body == {"Do": "squash", "delete_branch_after_merge": True, "head_commit_id": NEW_SHA}
    assert h.root_meta()["gc.build.merge_status"] == "merged"
    assert_tokens_placed(h, fake, gate, merged, merge_from=merge_from)


def test_red_writes_findings_with_the_job_log_tail(env) -> None:
    h, server, fake, merge_env = env
    job_url = f"{fake.url}/{OWNER}/{REPO}/actions/runs/5/jobs/0"
    server.set("GET", PR, (200, pr_json()))
    server.set("GET", status_path(NEW_SHA), (200, statuses(("lint", "success"), ("test", "failure"), combined="failure", url=job_url)))
    server.set("GET", f"{API}/actions/runs/5/jobs", (200, {"jobs": [{"id": 77}]}))
    log = "\n".join([f"noise {i}" for i in range(150)] + ["FAILED tests/test_x.py::test_y"])
    server.set("GET", f"{API}/actions/jobs/77/logs", (200, log.encode()))
    gate = h.wait()
    assert gate.code == 1, gate.err
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "red" and meta["gc.build.ci_reason"] == "test"
    text = Path(meta["gc.build.ci_findings_path"]).read_text(encoding="utf-8")
    assert "## test" in text and "FAILED tests/test_x.py::test_y" in text and "noise 0\n" not in text
    assert job_url in text
    assert merges(fake) == []
    assert_tokens_placed(h, fake, gate)


def test_pending_then_green(env) -> None:
    h, server, fake, _ = env
    server.set("GET", PR, (200, pr_json()))
    server.set("GET", status_path(NEW_SHA),
               (200, statuses(("test", "pending"), combined="pending")),
               (200, statuses(("test", "pending"), combined="pending")),
               (200, statuses(("test", "success"), combined="success")))
    gate = h.wait(GC_CI_GATE_TIMEOUT_SECONDS="20")
    assert gate.code == 0, gate.err
    assert h.root_meta()["gc.build.ci_status"] == "green"
    assert len([e for e in fake.log if e.path == status_path(NEW_SHA)]) == 3
    assert_tokens_placed(h, fake, gate)


def test_timeout_then_merge_fails_without_a_post(env) -> None:
    h, server, fake, merge_env = env
    server.set("GET", PR, (200, pr_json()))
    server.set("GET", status_path(NEW_SHA), (200, statuses(("test", "pending"), combined="pending")))
    gate = h.wait(GC_CI_GATE_TIMEOUT_SECONDS="1")
    assert gate.code == 0
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "timeout" and meta["gc.build.merge_status"] == "skipped:ci_timeout"
    merged = h.merge(env=merge_env)
    assert merged.json["outcome"] == "fail" and merged.json["failure_class"] == "ci_timeout"
    assert merges(fake) == []
    assert_tokens_placed(h, fake, gate, merged)


def test_no_ci_is_none_and_merges(env) -> None:
    h, server, fake, merge_env = env
    server.set("GET", PR, (200, pr_json()))
    server.set("GET", status_path(NEW_SHA), (200, statuses()))
    server.set("POST", MERGE, (200, None))
    gate = h.wait()
    assert gate.code == 0, gate.err
    assert h.root_meta()["gc.build.ci_status"] == "none"
    merge_from = len(fake.log)
    merged = h.merge(env=merge_env)
    assert merged.json["merge_status"] == "merged"
    assert len(merges(fake)) == 1
    assert_tokens_placed(h, fake, gate, merged, merge_from=merge_from)


def test_merge_false_never_posts(env) -> None:
    h, server, fake, merge_env = env
    h.root(**{"gc.var.merge": "false"})
    server.set("GET", PR, (200, pr_json()))
    server.set("GET", status_path(NEW_SHA), (200, statuses(("test", "success"), combined="success")))
    server.set("POST", MERGE, (200, None))
    assert h.wait().code == 0
    merged = h.merge(env=merge_env)
    assert merged.json["merge_status"] == "skipped:merge_false" and merged.json["outcome"] == "pass"
    assert merges(fake) == []
    meta = h.root_meta()
    assert meta["gc.build.ci_status"] == "green" and meta["gc.build.ci_rounds"] == "0"


def test_all_skipped_with_combined_pending_is_green(env) -> None:
    h, server, fake, _ = env
    server.set("GET", PR, (200, pr_json()))
    server.set("GET", status_path(NEW_SHA), (200, statuses(("docs", "skipped"), ("deploy", "skipped"), combined="pending")))
    assert h.wait().code == 0
    assert h.root_meta()["gc.build.ci_status"] == "green"


def test_merge_refused_405_is_failed_and_leaves_the_pr_open(env) -> None:
    h, server, fake, merge_env = env
    h.root(**{"gc.build.ci_status": "green", "gc.build.ci_rounds": "0", "gc.build.ci_head_sha": NEW_SHA})
    server.set("POST", MERGE, (405, {"message": "not allowed to merge"}))
    server.set("GET", PR, (200, pr_json()))
    merged = h.merge(env=merge_env)
    assert merged.json["merge_status"] == "failed:http_405"
    assert merged.json["outcome"] == "fail" and merged.json["failure_class"] == "merge_failed"
    assert len(merges(fake)) == 1
    assert h.root_meta()["gc.build.merge_status"] == "failed:http_405"
    assert_tokens_placed(h, fake, merged, merge_from=0)


def test_merge_error_after_a_remote_merge_is_merged(env) -> None:
    """RA-2 for Gitea: re-read the PR after a nonzero merge exit."""
    h, server, fake, merge_env = env
    h.root(**{"gc.build.ci_status": "green", "gc.build.ci_rounds": "0", "gc.build.ci_head_sha": NEW_SHA})
    server.set("POST", MERGE, (500, {"message": "hook failed"}))
    server.set("GET", PR, (200, pr_json(state="closed", merged=True)))
    merged = h.merge(env=merge_env)
    assert merged.json["merge_status"] == "merged"


def test_merge_uses_the_rig_root_when_the_pr_worktree_is_gone(env) -> None:
    """RA-4: the rig root has the same origin; repo_mismatch still applies."""
    h, server, fake, merge_env = env
    h.root(**{"gc.build.ci_status": "green", "gc.build.ci_rounds": "0", "gc.build.ci_head_sha": NEW_SHA,
              "gc.build.pr_head_worktree": str(h.rig / "gone")})
    server.set("POST", MERGE, (200, None))
    merged = h.merge(env=merge_env)
    assert merged.json["merge_status"] == "merged", merged.out + merged.err


def test_merge_refuses_a_checkout_whose_origin_is_another_repo(env) -> None:
    h, server, fake, merge_env = env
    h.root(**{"gc.build.ci_status": "green", "gc.build.ci_rounds": "0", "gc.build.ci_head_sha": NEW_SHA,
              "gc.build.publish_repo": "other-repo"})
    server.set("POST", MERGE, (200, None))
    merged = h.merge(env=merge_env)
    assert merged.json["merge_status"] == "failed:repo_mismatch"
    assert merges(fake) == []


def test_gate_never_reads_the_publisher_token_file(env) -> None:
    """Even if GC_GITEA_TOKEN_FILE leaked into the check env, the gate uses ci-gate.json."""
    h, server, fake, merge_env = env
    server.set("GET", PR, (200, pr_json()))
    server.set("GET", status_path(NEW_SHA), (200, statuses(("test", "success"), combined="success")))
    gate = h.wait(GC_GITEA_TOKEN_FILE=merge_env["GC_GITEA_TOKEN_FILE"])
    assert gate.code == 0, gate.err
    assert fake.log and all(e.headers["Authorization"] == f"token {READ_TOKEN}" for e in fake.log)


def test_read_token_rejected_is_unreadable(env) -> None:
    h, server, fake, _ = env
    server.set("GET", PR, (401, {"message": "token is required"}))
    gate = h.wait(GC_CI_GATE_TIMEOUT_SECONDS="30")
    assert gate.code == 0
    meta = h.root_meta()
    assert meta["gc.build.ci_reason"] == "unreadable:http_401"
    assert meta["gc.build.merge_status"] == "skipped:ci_unreadable"


def test_gitea_helper_not_on_search_paths_is_unreadable(env) -> None:
    h, server, fake, _ = env
    h.state["search_paths"] = [str(REPO_ROOT / "gascity" / "formulas")]
    h.save()
    gate = h.wait()
    assert gate.code == 0
    assert h.root_meta()["gc.build.ci_reason"] == "unreadable:gitea_helper_missing"
    assert fake.log == []
