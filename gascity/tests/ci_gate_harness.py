"""Shared harness for the build-basic CI gate tests (gascity and gitea packs).

It runs `checks/ci-gate.sh` the way gc's check sandbox does (plan RA-3): a
scrubbed environment holding only PATH, HOME=<city>, GC_HOME, GC_CITY_PATH,
GC_BEAD_ID and GC_ITERATION, plus the gate's test-only GC_CI_GATE_* timing
knobs. PATH holds only a temp bin dir and /usr/bin:/bin. The bin dir has:

- `gc`: a fake that serves `bd show --json` and `formula show --json` from
  `<city>/fake-gc.json` and applies every `bd update --set-metadata` to it;
- `gh`: a stub that replays scripted responses per subcommand from
  `<city>/gh-stub.json` and logs argv, cwd, GH_CONFIG_DIR, and whether
  GH_TOKEN matched the expected token's hash (never the value);
- `python3`: the interpreter running the tests.

Not a conftest.py: the shared CI run collects every pack's tests together.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[2]
GASCITY = REPO_ROOT / "gascity"
GATE_SH = GASCITY / "assets" / "scripts" / "checks" / "ci-gate.sh"
CI_GATE_PY = GASCITY / "assets" / "scripts" / "ci_gate.py"

ROOT_ID = "gp-root"
SUBJECT_ID = "gp-root.ci-gate.iteration"

FAKE_GC = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.path.join(os.environ["GC_CITY_PATH"], "fake-gc.json")
    with open(path) as handle:
        state = json.load(handle)
    args = sys.argv[1:]
    state.setdefault("calls", []).append(args)
    def save():
        with open(path, "w") as handle:
            json.dump(state, handle)
    if args[:2] == ["bd", "show"] and len(args) >= 3:
        bead = state["beads"].get(args[2])
        save()
        if bead is None or state.get("fail_show"):
            sys.stderr.write("Error: no issue found matching " + args[2] + "\\n")
            sys.exit(1)
        print(json.dumps([bead]))
    elif args[:2] == ["bd", "update"] and len(args) >= 3:
        bead = state["beads"].setdefault(args[2], {"id": args[2], "metadata": {}})
        rest, values = args[3:], {}
        while rest:
            flag = rest.pop(0)
            if flag != "--set-metadata" or not rest:
                sys.stderr.write("fake gc: unexpected update argument " + flag + "\\n")
                sys.exit(2)
            key, _, value = rest.pop(0).partition("=")
            values[key] = value
        bead["metadata"].update(values)
        state.setdefault("updates", []).append([args[2], values])
        save()
    elif args[:2] == ["formula", "show"]:
        save()
        print(json.dumps({"search_paths": state.get("search_paths", [])}))
    else:
        save()
        sys.stderr.write("fake gc: unsupported " + " ".join(args) + "\\n")
        sys.exit(2)
    '''
)

STUB_GH = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    import hashlib, json, os, sys
    city = os.environ["GC_CITY_PATH"]
    path = os.path.join(city, "gh-stub.json")
    with open(path) as handle:
        stub = json.load(handle)
    args = sys.argv[1:]
    key = " ".join(args[:2])
    token = os.environ.get("GH_TOKEN")
    entry = {
        "argv": args,
        "cwd": os.getcwd(),
        "gh_token_set": token is not None,
        "gh_token_ok": token is not None and hashlib.sha256(token.encode()).hexdigest() == stub.get("token_sha256"),
        "gh_config_dir": os.environ.get("GH_CONFIG_DIR"),
    }
    with open(os.path.join(city, "gh-log.jsonl"), "a") as handle:
        handle.write(json.dumps(entry) + "\\n")
    queue = stub.get("responses", {}).get(key) or [{"stderr": "gh stub: no response for " + key, "code": 99}]
    response = queue.pop(0) if len(queue) > 1 else queue[0]
    with open(path, "w") as handle:
        json.dump(stub, handle)
    out = response.get("stdout", "")
    sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
    sys.stderr.write(response.get("stderr", ""))
    sys.exit(response.get("code", 0))
    '''
)


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def json(self) -> dict:
        return json.loads(self.out.strip().splitlines()[-1])


class Harness:
    """One scratch city: fake gc state, stub gh, and runners for wait and merge."""

    def __init__(self, tmp: Path, search_paths: list[Path] | None = None) -> None:
        self.city = tmp / "city"
        self.city.mkdir()
        (self.city / ".gc").mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.rig = tmp / "rig"
        self.rig.mkdir()
        for name, text in (("gc", FAKE_GC), ("gh", STUB_GH)):
            path = self.bin / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)
        os.symlink(sys.executable, self.bin / "python3")
        self.state = {
            "beads": {
                ROOT_ID: {"id": ROOT_ID, "metadata": {
                    "gc.var.artifact_root": "plans/demo/build",
                    "gc.var.rig_name": "demo",
                    "gc.work_dir": str(self.rig),
                }},
                SUBJECT_ID: {"id": SUBJECT_ID, "metadata": {"gc.root_bead_id": ROOT_ID}},
            },
            "search_paths": [str(p) for p in (search_paths or [GASCITY / "formulas"])],
        }
        self.save()
        self.stub = {"responses": {}, "token_sha256": ""}
        self.save_stub()

    # state ------------------------------------------------------------------

    def save(self) -> None:
        (self.city / "fake-gc.json").write_text(json.dumps(self.state), encoding="utf-8")

    def load(self) -> dict:
        self.state = json.loads((self.city / "fake-gc.json").read_text(encoding="utf-8"))
        return self.state

    def root(self, **meta: str) -> None:
        self.state["beads"][ROOT_ID]["metadata"].update(meta)
        self.save()

    def subject(self, **meta: str) -> None:
        self.state["beads"][SUBJECT_ID]["metadata"].update(meta)
        self.save()

    def root_meta(self) -> dict:
        return self.load()["beads"][ROOT_ID]["metadata"]

    def updates(self) -> list:
        return self.load().get("updates", [])

    def config(self, token_files: dict[str, Path]) -> None:
        data = {"token_files": {host: str(path) for host, path in token_files.items()}}
        (self.city / ".gc" / "ci-gate.json").write_text(json.dumps(data), encoding="utf-8")

    def token(self, name: str, value: str) -> Path:
        path = self.city / f"{name}.token"
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    # gh stub ----------------------------------------------------------------

    def save_stub(self) -> None:
        (self.city / "gh-stub.json").write_text(json.dumps(self.stub), encoding="utf-8")

    def gh(self, key: str, *responses: dict) -> None:
        self.stub["responses"][key] = list(responses)
        self.save_stub()

    def expect_gh_token(self, value: str) -> None:
        self.stub["token_sha256"] = hashlib.sha256(value.encode()).hexdigest()
        self.save_stub()

    def gh_log(self) -> list[dict]:
        log = self.city / "gh-log.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    # runners ----------------------------------------------------------------

    def sandbox_env(self, attempt: int, **knobs: str) -> dict:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.city),
            "GC_HOME": str(self.city / ".gc-home"),
            "GC_CITY_PATH": str(self.city),
            "GC_BEAD_ID": SUBJECT_ID,
            "GC_ITERATION": str(attempt),
            "GC_CI_GATE_POLL_SECONDS": "0.05",
            "GC_CI_GATE_GRACE_SECONDS": "0.2",
            "GC_CI_GATE_TIMEOUT_SECONDS": "1",
        }
        env.update(knobs)
        return env

    def wait(self, attempt: int = 1, **knobs: str) -> Result:
        result = subprocess.run(
            [str(GATE_SH)], env=self.sandbox_env(attempt, **knobs), cwd=str(self.city),
            capture_output=True, text=True, timeout=120,
        )
        return Result(result.returncode, result.stdout, result.stderr)

    def merge(self, env: dict | None = None, cwd: Path | None = None) -> Result:
        run_env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.city),
            "GC_CITY_PATH": str(self.city),
            **(env or {}),
        }
        result = subprocess.run(
            [sys.executable, str(CI_GATE_PY), "merge", "--root", ROOT_ID], env=run_env,
            cwd=str(cwd or self.city), capture_output=True, text=True, timeout=120,
        )
        return Result(result.returncode, result.stdout, result.stderr)

    def everything_written(self) -> str:
        """Every byte the gate could have leaked a token into, for secrecy checks."""
        parts = [json.dumps(self.load()), json.dumps(self.gh_log())]
        for path in self.rig.rglob("*.md"):
            parts.append(path.read_text(encoding="utf-8"))
        return "\n".join(parts)
