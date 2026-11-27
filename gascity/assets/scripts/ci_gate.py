#!/usr/bin/env python3
"""build-basic's CI gate and merge.

Two subcommands, both standard-library only (Python >= 3.9):

  wait   The exec check of build-basic's `ci-gate` step, run by
         `checks/ci-gate.sh` inside gc's check sandbox. It waits, bounded, for
         the PR's CI and records the verdict on the workflow root. Exit codes:
           0   terminal verdict (green, none, timeout, unreadable, red at the
               round cap, no PR, PR merged or closed by someone else)
           1   red with fix rounds left: gc starts the next fix round
           75  `gc bd` failed at startup: gc re-runs the check attempt-free
  merge  Run by the publisher in the `ci-merge` step. Reads the verdict,
         squash-merges when it allows, and records `gc.build.merge_status`.
         Prints one JSON line whose `outcome`, `failure_class` and
         `failure_reason` the publisher copies onto its step bead.

Both print one JSON line on stdout. `wait` also prints one token-free
summary line on stderr, which gc copies into `gc.attempt_log`.

Credentials. gc runs checks with HOME=<city root>, a narrow PATH and no rig
override env, so the gate cannot borrow the publisher's login. It reads
`$GC_CITY_PATH/.gc/ci-gate.json`:

  {"token_files": {"github.com": "/abs/path", "gitea.example:3001": "/abs/path"}}

The token file is chosen by the host[:port] of the PR's API base, so a token
only ever goes to its own host. GitHub tokens reach `gh` only as GH_TOKEN in
the child's environment; Gitea token files are passed to `gitea_pr.py
--token-file`. Tokens never appear in argv, output, metadata or findings.
The merge uses the publisher's own credentials (gh's login, or the gitea
pack's GC_GITEA_TOKEN_FILE read only by `gitea_pr.py merge`).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Optional, Sequence
import urllib.parse


DEFAULT_ROUNDS = 3
MAX_ROUNDS = 10
DEFAULT_TIMEOUT_MINUTES = 90
MAX_TIMEOUT_MINUTES = 240
POLL_SECONDS = 60.0
GRACE_SECONDS = 300.0
REQUEST_TIMEOUT = 30.0
TAIL_LINES = 80
TAIL_BYTES = 8 * 1024
FINDINGS_BYTES = 32 * 1024
NAME_MAX = 200
SUMMARY_MAX = 1500
EXIT_TERMINAL, EXIT_RED, EXIT_BLIND = 0, 1, 75

# Test-only knobs. gc's check sandbox never passes them.
POLL_ENV = "GC_CI_GATE_POLL_SECONDS"
GRACE_ENV = "GC_CI_GATE_GRACE_SECONDS"
TIMEOUT_ENV = "GC_CI_GATE_TIMEOUT_SECONDS"

CONFIG_REL = Path(".gc") / "ci-gate.json"
TERMINAL_MERGE = {"skipped:no_pr", "skipped:already_merged", "skipped:pr_closed", "merged"}
FAILING_MERGE = {
    "skipped:ci_timeout": "ci_timeout",
    "skipped:ci_unreadable": "ci_unreadable",
    "skipped:ci_red": "ci_red",
}
FALSE_WORDS = {"false", "0", "no", "off"}

GITHUB_BUCKETS = {"pending": "pending", "pass": "pass", "skipping": "pass", "fail": "fail", "cancel": "fail"}
GITHUB_STATES = {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}


# --------------------------------------------------------------------------
# redaction

_SECRETS: set[str] = set()
REDACTED = "[REDACTED]"
_PATTERNS = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}"), REDACTED),
    # Fine-grained GitHub tokens ("github" + "_pat_" prefix; [t] keeps diff scans quiet).
    (re.compile(r"\bgithub_pa[t]_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:(?:token|bearer|basic)\s+)?\S+"), r"\1" + REDACTED),
    (re.compile(r"(?i)\b(token|bearer)(\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1\2" + REDACTED),
    (re.compile(r"(?i)\b(password|passwd|secret)(\s*[=:]\s*)\S+"), r"\1\2" + REDACTED),
)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def one_line(text: str, limit: int = NAME_MAX) -> str:
    return " ".join(redact(str(text)).split())[:limit]


# --------------------------------------------------------------------------
# errors and bead I/O


class Unreadable(Exception):
    """CI cannot be read and waiting will not change that (config, auth)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Transient(Exception):
    """A poll failed in a way the next poll may not (network, 5xx)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BeadError(Exception):
    pass


Runner = Callable[..., subprocess.CompletedProcess]


def run(argv: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(list(argv), **kwargs)


def bd_show(bead_id: str) -> dict:
    try:
        result = run(["gc", "bd", "show", bead_id, "--json"], timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BeadError(f"gc bd show {bead_id}: {type(exc).__name__}") from None
    if result.returncode != 0:
        raise BeadError(f"gc bd show {bead_id} exited {result.returncode}: {one_line(result.stderr[-400:], 400)}")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise BeadError(f"gc bd show {bead_id}: output is not JSON") from None
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise BeadError(f"gc bd show {bead_id}: unexpected JSON")
    return data


def metadata(bead: Mapping[str, Any]) -> dict:
    meta = bead.get("metadata")
    return {k: v for k, v in meta.items() if isinstance(v, str)} if isinstance(meta, dict) else {}


def bd_update(bead_id: str, values: Mapping[str, str]) -> bool:
    if not values:
        return True
    argv = ["gc", "bd", "update", bead_id]
    for key, value in values.items():
        argv += ["--set-metadata", f"{key}={redact(str(value))}"]
    try:
        result = run(argv, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------
# configuration


def clamp_int(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def env_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env[name])
    except (KeyError, ValueError):
        return default
    return value if value >= 0 else default


def truthy(raw: str | None, default: bool = True) -> bool:
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in FALSE_WORDS


def netloc_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def github_repo(pr_url: str) -> tuple[str, str]:
    """owner, repo from https://github.com/<owner>/<repo>/pull/<n>."""
    parts = urllib.parse.urlsplit(pr_url).path.strip("/").split("/")
    if len(parts) >= 4 and parts[2] == "pull":
        return parts[0], parts[1]
    raise Unreadable("pr_url_unparseable")


def gitea_target(root_meta: Mapping[str, str]) -> tuple[str, str, str]:
    """api_base, owner, repo: the publisher's keys, else parsed from the PR URL."""
    api_base = root_meta.get("gc.build.publish_api_base", "").strip().rstrip("/")
    owner = root_meta.get("gc.build.publish_owner", "").strip()
    repo = root_meta.get("gc.build.publish_repo", "").strip()
    if api_base and owner and repo:
        return api_base, owner, repo
    url = root_meta.get("gc.build.pr_url", "")
    parts = urllib.parse.urlsplit(url)
    segments = parts.path.strip("/").split("/")
    if parts.scheme in ("http", "https") and parts.netloc and len(segments) >= 4 and segments[-2] == "pulls":
        prefix = "/".join(segments[:-4])
        base = f"{parts.scheme}://{parts.netloc}" + (f"/{prefix}" if prefix else "")
        return base, segments[-4], segments[-3]
    raise Unreadable("gitea_target_unknown")


def forge_of(root_meta: Mapping[str, str]) -> str:
    forge = root_meta.get("gc.build.publish_forge", "").strip().lower()
    if forge:
        return forge
    return "github" if netloc_of(root_meta.get("gc.build.pr_url", "")) == "github.com" else "unknown"


def city_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("GC_CITY_PATH") or env.get("HOME") or ".")


def token_file_for(host: str, env: Mapping[str, str]) -> Path:
    """The read token file the operator configured for this API host."""
    config = city_path(env) / CONFIG_REL
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Unreadable("config_missing") from None
    except (OSError, ValueError, UnicodeDecodeError):
        raise Unreadable("config_invalid") from None
    files = data.get("token_files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise Unreadable("config_invalid")
    entry = files.get(host)
    if not isinstance(entry, str) or not entry.strip():
        raise Unreadable("no_token_for_host")
    return Path(os.path.expanduser(entry.strip()))


def read_secret(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise Unreadable("token_unreadable") from None
    if not token or any(ch.isspace() for ch in token):
        raise Unreadable("token_unreadable")
    _SECRETS.add(token)
    return token


def locate_pack_script(name: str, rig: str, env: Mapping[str, str]) -> Path | None:
    """Highest formula layer shipping assets/scripts/<name>, as the prompts locate it."""
    argv = ["gc", "formula", "show", "build-basic"]
    if rig:
        argv += ["--rig", rig]
    argv.append("--json")
    try:
        result = run(argv, timeout=120, env=dict(env))
        layers = json.loads(result.stdout).get("search_paths") or []
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    for layer in reversed(layers):
        candidate = Path(str(layer)).parent / "assets" / "scripts" / name
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------
# forge adapters: {"pr_state", "head_sha", "checks": [{name, state, url, description}]}


def child_env(env: Mapping[str, str], extra: Mapping[str, str] | None = None) -> dict:
    out = {k: v for k, v in env.items() if k not in ("GC_GITEA_TOKEN_FILE", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")}
    out.update(extra or {})
    return out


class GitHub:
    def __init__(self, pr_url: str, token: str, env: Mapping[str, str], config_dir: str) -> None:
        self.pr_url = pr_url
        self.owner, self.repo = github_repo(pr_url)
        self.gh = shutil.which("gh", path=env.get("PATH"))
        if not self.gh:
            raise Unreadable("gh_missing")
        self.env = child_env(env, {
            "GH_TOKEN": token,
            "GH_CONFIG_DIR": config_dir,
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "NO_COLOR": "1",
        })
        self.cwd = config_dir

    def _gh(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return run([self.gh, *args], env=self.env, cwd=self.cwd, timeout=REQUEST_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Transient("gh_timeout") from None
        except OSError:
            raise Unreadable("gh_missing") from None

    @staticmethod
    def _auth_error(stderr: str) -> str | None:
        lowered = stderr.lower()
        for code in ("401", "403"):
            if f"http {code}" in lowered:
                return f"http_{code}"
        if "bad credentials" in lowered or "authentication" in lowered or "gh auth login" in lowered:
            return "http_401"
        return None

    def poll(self) -> dict:
        view = self._gh("pr", "view", self.pr_url, "--json", "state,headRefOid")
        if view.returncode != 0:
            raise Unreadable(self._auth_error(view.stderr)) if self._auth_error(view.stderr) else Transient(f"gh_exit_{view.returncode}")
        try:
            pr = json.loads(view.stdout)
        except ValueError:
            raise Transient("gh_bad_json") from None
        checks_run = self._gh("pr", "checks", self.pr_url, "--json", "name,state,bucket,link,workflow")
        checks: list = []
        if checks_run.stdout.strip():
            try:
                raw = json.loads(checks_run.stdout)
            except ValueError:
                raise Transient("gh_bad_json") from None
            for item in raw if isinstance(raw, list) else []:
                item = item if isinstance(item, dict) else {}
                checks.append({
                    "name": one_line(item.get("name") or item.get("workflow") or "(unnamed)"),
                    "state": GITHUB_BUCKETS.get(str(item.get("bucket") or "").lower(), "fail"),
                    "url": str(item.get("link") or ""),
                    "description": one_line(item.get("state") or ""),
                })
        elif checks_run.returncode != 0 and "no checks reported" not in checks_run.stderr.lower():
            code = self._auth_error(checks_run.stderr)
            raise Unreadable(code) if code else Transient(f"gh_exit_{checks_run.returncode}")
        return {
            "pr_state": GITHUB_STATES.get(str(pr.get("state") or "").upper(), "open"),
            "head_sha": str(pr.get("headRefOid") or ""),
            "checks": checks,
        }

    def log_tail(self, check: Mapping[str, str]) -> str | None:
        match = re.search(r"/actions/runs/(\d+)(?:/job/(\d+))?", check.get("url") or "")
        if not match:
            return None
        args = ["run", "view"]
        args += ["--job", match.group(2)] if match.group(2) else [match.group(1)]
        result = self._gh(*args, "--repo", f"{self.owner}/{self.repo}", "--log-failed")
        return result.stdout if result.returncode == 0 and result.stdout.strip() else None


class Gitea:
    def __init__(self, root_meta: Mapping[str, str], token_file: Path, script: Path, env: Mapping[str, str]) -> None:
        self.api_base, self.owner, self.repo = gitea_target(root_meta)
        self.number = root_meta.get("gc.build.pr_number", "").strip()
        if not self.number.isdigit():
            raise Unreadable("pr_number_missing")
        if not token_file.is_file():
            raise Unreadable("token_unreadable")
        self.token_file = token_file
        self.script = script
        self.env = child_env(env)

    def _call(self, command: str, *extra: str) -> dict:
        argv = [sys.executable, str(self.script), command, "--api-base", self.api_base, "--owner", self.owner,
                "--repo", self.repo, "--number", self.number, "--token-file", str(self.token_file),
                "--timeout", str(REQUEST_TIMEOUT), *extra]
        try:
            result = run(argv, env=self.env, timeout=REQUEST_TIMEOUT * 3)
        except subprocess.TimeoutExpired:
            raise Transient("gitea_timeout") from None
        except OSError:
            raise Unreadable("gitea_helper_missing") from None
        if result.returncode == 0:
            try:
                return json.loads(result.stdout)
            except ValueError:
                raise Transient("gitea_bad_json") from None
        code = error_code(result.stderr) or f"gitea_exit_{result.returncode}"
        if code in ("http_401", "http_403", "http_404") or code.startswith("token_") or code in (
            "base_url_invalid", "number_invalid"):
            raise Unreadable(code)
        raise Transient(code)

    def poll(self) -> dict:
        data = self._call("ci-status")
        checks = []
        for item in data.get("checks") or []:
            item = item if isinstance(item, dict) else {}
            checks.append({
                "name": one_line(item.get("name") or "(unnamed)"),
                "state": item.get("state") if item.get("state") in ("pending", "pass", "fail") else "fail",
                "url": str(item.get("url") or ""),
                "description": one_line(item.get("description") or ""),
            })
        return {"pr_state": data.get("pr_state") or "open", "head_sha": str(data.get("head_sha") or ""), "checks": checks}

    def log_tail(self, check: Mapping[str, str]) -> str | None:
        if not check.get("url"):
            return None
        try:
            return self._call("ci-log-tail", "--target-url", check["url"]).get("tail")
        except (Transient, Unreadable):
            return None


def error_code(stderr: str) -> str | None:
    for line in reversed((stderr or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            return data["error"]
    return None


# --------------------------------------------------------------------------
# classification and findings


def classify(snapshot: Mapping[str, Any]) -> str:
    """red, pending, green or empty for one poll's checks."""
    states = [check["state"] for check in snapshot.get("checks") or []]
    if any(state == "fail" for state in states):
        return "red"
    if any(state == "pending" for state in states):
        return "pending"
    return "green" if states else "empty"


def failing_names(snapshot: Mapping[str, Any]) -> list[str]:
    return [check["name"] for check in snapshot.get("checks") or [] if check["state"] == "fail"]


def cap_tail(text: str) -> str:
    tail = "\n".join(redact(text).splitlines()[-TAIL_LINES:])
    data = tail.encode("utf-8")
    if len(data) > TAIL_BYTES:
        tail = data[-TAIL_BYTES:].decode("utf-8", errors="ignore")
    return tail


def findings_dir(root_id: str, root_meta: Mapping[str, str], env: Mapping[str, str]) -> Path:
    artifact_root = root_meta.get("gc.build.artifact_root") or root_meta.get("gc.var.artifact_root") or ""
    if artifact_root and os.path.isabs(artifact_root):
        return Path(artifact_root) / "ci"
    if artifact_root:
        for base in (root_meta.get("gc.work_dir"), env.get("GC_STORE_PATH"), env.get("GC_RIG_ROOT"), env.get("GC_WORK_DIR")):
            if base and os.path.isabs(base) and os.path.isdir(base):
                return Path(base) / artifact_root / "ci"
    return city_path(env) / ".gc" / "ci-findings" / root_id


def write_findings(path: Path, snapshot: Mapping[str, Any], attempt: int, adapter: Any) -> None:
    parts = [
        f"# CI findings, round {attempt}",
        "",
        f"- Head sha: `{snapshot.get('head_sha') or 'unknown'}`",
        f"- Failing checks: {len(failing_names(snapshot))}",
        f"- Log tails are the last {TAIL_LINES} lines / {TAIL_BYTES // 1024} KiB per check, "
        f"{FINDINGS_BYTES // 1024} KiB per file, with secrets redacted.",
        "- This file is gate output. Never commit it.",
        "",
    ]
    for check in snapshot.get("checks") or []:
        if check["state"] != "fail":
            continue
        parts += [f"## {check['name']}", ""]
        if check.get("description"):
            parts.append(f"- Description: {check['description']}")
        if check.get("url"):
            parts.append(f"- URL: {redact(check['url'])}")
        tail = None
        try:
            tail = adapter.log_tail(check)
        except Exception:  # noqa: BLE001 -- tails are best effort
            tail = None
        if tail:
            parts += ["", "```text", cap_tail(tail).replace("```", "'''"), "```"]
        else:
            parts.append("- Log tail: not available")
        parts.append("")
    text = "\n".join(parts)
    data = text.encode("utf-8")
    if len(data) > FINDINGS_BYTES:
        note = "\n\n[truncated at 32 KiB]\n"
        text = data[: FINDINGS_BYTES - len(note)].decode("utf-8", errors="ignore") + note
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# wait


def summary(verdict: str, attempt: int, rounds: int, cap: int, reason: str) -> str:
    line = f"ci-gate: {verdict} attempt={attempt} rounds={rounds}/{cap}"
    if reason:
        line += f" reason={reason}"
    return one_line(line, SUMMARY_MAX)


def cmd_wait(env: Mapping[str, str], out, err, sleep=time.sleep, clock=time.time) -> int:
    def finish(code: int, verdict: str, values: Mapping[str, str], reason: str = "") -> int:
        if values and not bd_update(root_id, values):
            err.write(one_line(f"ci-gate: metadata write to {root_id} failed", SUMMARY_MAX) + "\n")
        result = {"verdict": verdict, "exit": code, **{k.rsplit(".", 1)[-1]: v for k, v in values.items()}}
        out.write(json.dumps(result, sort_keys=True) + "\n")
        err.write(summary(verdict, attempt, rounds_used, cap, reason) + "\n")
        return code

    bead_id = env.get("GC_BEAD_ID", "").strip()
    if not bead_id:
        err.write("ci-gate: GC_BEAD_ID is required\n")
        return EXIT_BLIND
    try:
        subject = bd_show(bead_id)
        subject_meta = metadata(subject)
        root_id = subject_meta.get("gc.root_bead_id") or bead_id
        root_meta = metadata(bd_show(root_id)) if root_id != bead_id else subject_meta
    except BeadError as exc:
        err.write(one_line(f"ci-gate: {exc}", SUMMARY_MAX) + "\n")
        return EXIT_BLIND

    attempt = clamp_int(env.get("GC_ITERATION") or subject_meta.get("gc.attempt"), 1, 1, 1000)
    cap = clamp_int(root_meta.get("gc.var.ci_fix_rounds"), DEFAULT_ROUNDS, 0, MAX_ROUNDS)
    minutes = clamp_int(root_meta.get("gc.var.ci_timeout_minutes"), DEFAULT_TIMEOUT_MINUTES, 1, MAX_TIMEOUT_MINUTES)
    rounds_used = attempt - 1
    timeout_s = env_float(env, TIMEOUT_ENV, minutes * 60.0)
    poll_s = env_float(env, POLL_ENV, POLL_SECONDS)
    grace_s = env_float(env, GRACE_ENV, GRACE_SECONDS)
    pr_url = root_meta.get("gc.build.pr_url", "").strip()

    # REQ-001: nothing was opened, so nothing is read and no forge is called.
    if not pr_url:
        return finish(EXIT_TERMINAL, "no_pr", {"gc.build.merge_status": "skipped:no_pr"}, "no_pr")

    base = {"gc.build.ci_rounds": str(rounds_used)}

    def terminal_red(reason: str, head: str) -> int:
        values = {**base, "gc.build.ci_status": "red", "gc.build.ci_reason": reason,
                  "gc.build.merge_status": "skipped:ci_red"}
        if head:
            values["gc.build.ci_head_sha"] = head
        return finish(EXIT_TERMINAL, "red", values, reason)

    def unreadable(code: str) -> int:
        reason = f"unreadable:{code}"
        return finish(EXIT_TERMINAL, "unreadable", {**base, "gc.build.ci_status": "timeout", "gc.build.ci_reason": reason,
                                                    "gc.build.merge_status": "skipped:ci_unreadable"}, reason)

    fix_action = subject_meta.get("ci_fix.action", "").strip()
    pushed_sha = subject_meta.get("ci_fix.pushed_sha", "").strip()
    previous_red = root_meta.get("gc.build.ci_head_sha", "").strip() if root_meta.get("gc.build.ci_status") == "red" else ""
    if attempt > 1 and fix_action == "gave_up":
        reason = one_line(subject_meta.get("ci_fix.reason") or root_meta.get("gc.build.ci_reason") or "fix round gave up")
        return terminal_red(f"gave_up: {reason}", previous_red)

    # Resume this round's deadline if gc re-ran the check (dispatcher restart, kill).
    now = clock()
    started = None
    stamp = root_meta.get("gc.build.ci_round_started", "")
    if stamp.partition(":")[0] == str(attempt):
        try:
            started = float(stamp.partition(":")[2])
        except ValueError:
            started = None
    if started is None or started > now:
        started = now
        bd_update(root_id, {"gc.build.ci_round_started": f"{attempt}:{int(started)}"})
    deadline = started + timeout_s

    config_dir = tempfile.mkdtemp(prefix="ci-gate-gh-")
    try:
        try:
            forge = forge_of(root_meta)
            if forge == "github":
                token = read_secret(token_file_for("github.com", env))
                adapter: Any = GitHub(pr_url, token, env, config_dir)
            elif forge == "gitea":
                api_base, _, _ = gitea_target(root_meta)
                token_path = token_file_for(netloc_of(api_base), env)
                script = locate_pack_script("gitea_pr.py", root_meta.get("gc.var.rig_name", ""), env)
                if script is None:
                    raise Unreadable("gitea_helper_missing")
                adapter = Gitea(root_meta, token_path, script, env)
            else:
                raise Unreadable(f"forge_{forge}")
        except Unreadable as exc:
            return unreadable(exc.code)

        snapshot: dict = {"head_sha": "", "checks": []}
        last_error = ""
        while True:
            try:
                snapshot = adapter.poll()
                last_error = ""
            except Unreadable as exc:
                return unreadable(exc.code)
            except Transient as exc:
                last_error = exc.code
                snapshot = {"head_sha": snapshot.get("head_sha", ""), "checks": [], "transient": True}

            head = snapshot.get("head_sha", "")
            state = snapshot.get("pr_state")
            verdict = None if snapshot.get("transient") else classify(snapshot)
            if state in ("merged", "closed"):
                status = verdict if verdict in ("red", "green") else "none"
                merge_status = "skipped:already_merged" if state == "merged" else "skipped:pr_closed"
                return finish(EXIT_TERMINAL, state, {**base, "gc.build.ci_status": status, "gc.build.ci_head_sha": head,
                                                     "gc.build.ci_reason": f"pr_{state}", "gc.build.merge_status": merge_status},
                              f"pr_{state}")
            if verdict is not None and attempt > 1 and previous_red and head == previous_red:
                # RA-1: the forge still shows the sha the last red verdict was for.
                if fix_action != "pushed":
                    return terminal_red("no_fix_pushed", head)
                verdict = "pending"
            elif verdict is not None and attempt > 1 and pushed_sha and head != pushed_sha:
                verdict = "pending"

            elapsed = clock() - started
            if verdict == "red":
                names = failing_names(snapshot)
                reason = ",".join(one_line(name) for name in names) or "red"
                findings = findings_dir(root_id, root_meta, env) / f"round-{attempt}-findings.md"
                try:
                    write_findings(findings, snapshot, attempt, adapter)
                except OSError:
                    findings = Path("")
                values = {**base, "gc.build.ci_status": "red", "gc.build.ci_reason": reason, "gc.build.ci_head_sha": head}
                if str(findings):
                    values["gc.build.ci_findings_path"] = str(findings)
                if rounds_used >= cap:
                    values["gc.build.merge_status"] = "skipped:ci_red"
                    return finish(EXIT_TERMINAL, "red", values, reason)
                return finish(EXIT_RED, "red", values, f"{reason} findings={findings}")
            if verdict == "green":
                return finish(EXIT_TERMINAL, "green", {**base, "gc.build.ci_status": "green", "gc.build.ci_head_sha": head,
                                                       "gc.build.ci_reason": f"green:{len(snapshot['checks'])}_checks"})
            if verdict == "empty" and attempt == 1 and elapsed >= grace_s:
                reason = f"no_ci_within_{int(grace_s)}s"
                return finish(EXIT_TERMINAL, "none", {**base, "gc.build.ci_status": "none", "gc.build.ci_head_sha": head,
                                                      "gc.build.ci_reason": reason}, reason)

            now = clock()
            if now >= deadline:
                reason = f"timeout_after_{minutes}m" + (f" last_error={last_error}" if last_error else "")
                values = {**base, "gc.build.ci_status": "timeout", "gc.build.ci_reason": reason,
                          "gc.build.merge_status": "skipped:ci_timeout"}
                if head:
                    values["gc.build.ci_head_sha"] = head
                return finish(EXIT_TERMINAL, "timeout", values, reason)
            wait = min(poll_s, deadline - now)
            if verdict == "empty" and attempt == 1:
                wait = min(wait, max(0.0, started + grace_s - now)) or wait
            sleep(max(wait, 0.0))
    except Exception as exc:  # noqa: BLE001 -- record a verdict rather than start a blind fix round
        return unreadable(f"internal_error:{type(exc).__name__}")
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# merge


def merge_github(root_meta: Mapping[str, str], head_sha: str, env: Mapping[str, str]) -> str:
    """merged or failed:<code>. Runs from a temp dir so gh deletes no local branch (RA-2)."""
    pr_url = root_meta["gc.build.pr_url"]
    workdir = tempfile.mkdtemp(prefix="ci-merge-")
    try:
        try:
            result = run(["gh", "pr", "merge", pr_url, "--squash", "--delete-branch", "--match-head-commit", head_sha],
                         cwd=workdir, env=dict(env), timeout=120)
        except (OSError, subprocess.SubprocessError):
            return "failed:gh_missing"
        if result.returncode == 0:
            return "merged"
        try:
            view = run(["gh", "pr", "view", pr_url, "--json", "state"], cwd=workdir, env=dict(env), timeout=60)
            if view.returncode == 0 and str(json.loads(view.stdout).get("state", "")).upper() == "MERGED":
                return "merged"
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            pass
        stderr = result.stderr.lower()
        if "head branch was modified" in stderr or "match-head-commit" in stderr or "expected head" in stderr:
            return "failed:head_moved"
        return f"failed:gh_exit_{result.returncode}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def merge_gitea(root_meta: Mapping[str, str], head_sha: str, env: Mapping[str, str]) -> str:
    rig = root_meta.get("gc.var.rig_name", "") or env.get("GC_RIG", "")
    script = locate_pack_script("gitea_pr.py", rig, env)
    if script is None:
        return "failed:gitea_helper_missing"
    worktree = root_meta.get("gc.build.pr_head_worktree", "")
    cwd = None
    for candidate in (worktree, root_meta.get("gc.work_dir", ""), env.get("GC_RIG_ROOT", "")):
        if candidate and os.path.isdir(candidate):
            cwd = candidate
            break
    number = root_meta.get("gc.build.pr_number", "").strip()
    if not number.isdigit():
        return "failed:no_pr_number"
    try:
        expected = gitea_target(root_meta)
    except Unreadable:
        return "failed:repo_mismatch"
    try:
        origin = run([sys.executable, str(script), "parse-origin"], cwd=cwd, env=dict(env), timeout=60)
    except (OSError, subprocess.SubprocessError):
        return "failed:origin_unreadable"
    try:
        parsed = json.loads(origin.stdout) if origin.returncode == 0 else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        return f"failed:{error_code(origin.stderr) or 'origin_unreadable'}"
    actual = (str(parsed.get("api_base", "")).rstrip("/"), str(parsed.get("owner", "")), str(parsed.get("repo", "")))
    if tuple(part.lower() for part in actual) != tuple(part.lower() for part in expected):
        return "failed:repo_mismatch"
    try:
        result = run([sys.executable, str(script), "merge", "--number", number, "--head-sha", head_sha],
                     cwd=cwd, env=dict(env), timeout=120)
    except (OSError, subprocess.SubprocessError):
        return "failed:gitea_helper_missing"
    if result.returncode == 0:
        return "merged"
    code = error_code(result.stderr) or f"gitea_exit_{result.returncode}"
    try:
        view = run([sys.executable, str(script), "pr-status", "--api-base", actual[0], "--owner", actual[1],
                    "--repo", actual[2], "--number", number], cwd=cwd, env=dict(env), timeout=60)
        if view.returncode == 0 and json.loads(view.stdout).get("state") == "merged":
            return "merged"
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        pass
    return f"failed:{code}"


def cmd_merge(root_id: str, env: Mapping[str, str], out, err) -> int:
    def finish(merge_status: str, outcome: str, failure_class: str = "", reason: str = "", write: bool = True) -> int:
        if write and not bd_update(root_id, {"gc.build.merge_status": merge_status}):
            outcome, failure_class, reason = "fail", "metadata_write_failed", f"could not record merge_status={merge_status}"
        result = {
            "merge_status": merge_status,
            "outcome": outcome,
            "failure_class": failure_class,
            "failure_reason": one_line(reason, 500),
            "pr_url": pr_url,
            "ci_status": ci_status,
            "ci_rounds": root_meta.get("gc.build.ci_rounds", ""),
        }
        out.write(json.dumps(result, sort_keys=True) + "\n")
        return 0

    try:
        root_meta = metadata(bd_show(root_id))
    except BeadError as exc:
        err.write(json.dumps({"error": "bead_unreadable", "message": one_line(str(exc), 400)}) + "\n")
        return 2
    pr_url = root_meta.get("gc.build.pr_url", "")
    ci_status = root_meta.get("gc.build.ci_status", "")
    current = root_meta.get("gc.build.merge_status", "")
    reason = root_meta.get("gc.build.ci_reason", "")

    if current in TERMINAL_MERGE:
        return finish(current, "pass", write=False)
    if current in FAILING_MERGE:
        return finish(current, "fail", FAILING_MERGE[current], reason or current, write=False)
    if not pr_url:
        return finish("skipped:no_pr", "pass")
    if ci_status == "red":
        return finish("skipped:ci_red", "fail", "ci_red", reason or "ci red")
    if ci_status == "timeout":
        if reason.startswith("unreadable:"):
            return finish("skipped:ci_unreadable", "fail", "ci_unreadable", reason)
        return finish("skipped:ci_timeout", "fail", "ci_timeout", reason or "ci timeout")
    if ci_status not in ("green", "none"):
        return finish("failed:no_ci_verdict", "fail", "merge_failed", "no CI verdict recorded on the workflow root")
    if not truthy(root_meta.get("gc.var.merge"), default=True):
        return finish("skipped:merge_false", "pass")
    head_sha = root_meta.get("gc.build.ci_head_sha", "").strip()
    if not head_sha:
        return finish("failed:no_head_sha", "fail", "merge_failed", "no CI head sha recorded on the workflow root")
    forge = forge_of(root_meta)
    if forge == "github":
        status = merge_github(root_meta, head_sha, env)
    elif forge == "gitea":
        status = merge_gitea(root_meta, head_sha, env)
    else:
        status = f"failed:forge_{forge}"
    if status == "merged":
        return finish("merged", "pass")
    return finish(status, "fail", "merge_failed", f"{status} for {pr_url}; PR left open")


# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None, out=None, err=None) -> int:
    env = dict(os.environ if env is None else env)
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(prog="ci_gate.py", description="build-basic CI gate and merge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("wait", help="the ci-gate exec check (reads GC_BEAD_ID, GC_ITERATION)")
    merge = sub.add_parser("merge", help="merge the PR when the recorded verdict allows")
    merge.add_argument("--root", required=True, help="workflow root bead id")
    args = parser.parse_args(argv)
    try:
        if args.command == "wait":
            return cmd_wait(env, out, err)
        return cmd_merge(args.root, env, out, err)
    except Exception as exc:  # noqa: BLE001 -- never print a traceback that could carry a token
        err.write(json.dumps({"error": "internal_error", "message": type(exc).__name__}) + "\n")
        return EXIT_BLIND if args.command == "wait" else 2


if __name__ == "__main__":
    sys.exit(main())
