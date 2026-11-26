"""Find and stop the real `gc`, `dolt` and `bd` processes a scratch city left.

A live-gc test (`gc_live_city.write_city` and a real `GC_TEST_BIN`) runs gc
commands against a throwaway city under pytest's tmp dir. Some of them start
city infrastructure that outlives the command: on gc 1.5.1 a single
`gc doctor` leaves a `gc __gc-managed-dolt-scope-watchdog` and its
`dolt sql-server`, detached and reparented to PID 1, running until something
signals them. This module finds those processes by the city's root and ends
them.

How a process is tied to a root (any one is enough; only this user's `gc`,
`dolt` and `bd` processes are ever considered):

- argv: the command names the root. gc writes paths in its own form, which on
  macOS drops the `/private` prefix of `/private/var/...`, so both forms of
  the root are matched;
- environment: `GC_PACKS_TEST_ROOT`, `GC_CITY`, `GC_CITY_PATH`,
  `GC_CITY_ROOT`, `GC_HOME` or `HOME` lies under the root (`/proc` on Linux,
  one `ps -E` on macOS);
- working directory under the root (`/proc/<pid>/cwd` on Linux, `lsof` for
  the candidates on macOS).

Which gc command stops a scratch city's managed Dolt (checked on gc 1.5.1,
no supervisor running): `gc stop` with the city's environment stops both the
watchdog and `dolt sql-server`, but takes about 15 s per city; `gc dolt stop`
does not exist there (`unknown command "dolt"`). A plain SIGTERM ends both at
once, so `stop` signals by default (watchdogs first, so none restarts its
store), and runs `gc stop` first only when asked (`gc_bin`).

Standard library only; it imports nothing from any pack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal
import subprocess
import sys
import time
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

ROOT_ENV = "GC_PACKS_TEST_ROOT"
ENV_KEYS = (ROOT_ENV, "GC_CITY", "GC_CITY_PATH", "GC_CITY_ROOT", "GC_HOME", "HOME")
NAMES = frozenset({"gc", "dolt", "bd"})
WATCHDOG_VERB = "__gc-managed-dolt-scope-watchdog"
PS_ENV = {"LC_ALL": "C", "PATH": "/bin:/usr/bin:/usr/sbin:/sbin"}
LINUX = sys.platform.startswith("linux") and os.path.isdir("/proc/self")


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    uid: int
    state: str
    command: str
    why: str = ""
    """How it was tied to a root (argv, env or cwd)."""

    @property
    def zombie(self) -> bool:
        return self.state.startswith("Z")

    @property
    def watchdog(self) -> bool:
        return WATCHDOG_VERB in self.command


@dataclass
class StopReport:
    found: List[Proc] = field(default_factory=list)
    termed: List[int] = field(default_factory=list)
    """Ended by SIGTERM (or by `gc stop`)."""
    killed: List[int] = field(default_factory=list)
    """Needed SIGKILL."""
    alive: List[int] = field(default_factory=list)
    """Still alive after SIGKILL."""
    gc_stop: str = ""
    """What `gc stop` said, when it ran."""

    def describe(self, pids: Iterable[int]) -> List[str]:
        by_pid = {p.pid: p for p in self.found}
        return [f"{pid} {by_pid[pid].command[:200] if pid in by_pid else '?'}" for pid in pids]


def root_forms(root: os.PathLike[str] | str) -> List[str]:
    """The spellings a process may use for `root`: as given, resolved, and
    without macOS's `/private` prefix (how gc writes `/private/var/...`)."""
    forms: List[str] = []
    for form in (os.fspath(root), os.path.realpath(root)):
        form = form.rstrip("/") or "/"
        forms.append(form)
        if form.startswith("/private/"):
            forms.append(form[len("/private"):])
    return sorted(set(forms), key=forms.index)


def _under(value: str, forms: Sequence[str]) -> bool:
    return any(value == form or value.startswith(form + "/") for form in forms)


def _names(command: str) -> bool:
    words = command.split()
    if not words:
        return False
    first = os.path.basename(words[0])
    if first in NAMES:
        return True
    # An interpreter running a script named like the binary (a test stand-in).
    return first.startswith("python") and len(words) > 1 and os.path.basename(words[1]) in NAMES


def table() -> List[Proc]:
    """This host's processes, one `ps`."""
    done = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=,ppid=,uid=,stat=,command="],
        env=dict(PS_ENV),
        capture_output=True,
        text=True,
        errors="surrogateescape",
        timeout=30,
        check=False,
    )
    out: List[Proc] = []
    for line in done.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            out.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4]))
    return out


def _environs(pids: Set[int]) -> Dict[int, Dict[str, str]]:
    out: Dict[int, Dict[str, str]] = {}
    if LINUX:
        for pid in pids:
            try:
                with open(f"/proc/{pid}/environ", "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            env: Dict[str, str] = {}
            for item in raw.split(b"\0"):
                key, sep, value = item.partition(b"=")
                if sep:
                    env[key.decode("utf-8", "surrogateescape")] = value.decode("utf-8", "surrogateescape")
            out[pid] = env
        return out
    done = subprocess.run(
        ["ps", "-E", "-A", "-ww", "-o", "pid=,command="],
        env=dict(PS_ENV),
        capture_output=True,
        text=True,
        errors="surrogateescape",
        timeout=30,
        check=False,
    )
    for line in done.stdout.splitlines():
        words = line.split()
        if not words or not words[0].isdigit() or int(words[0]) not in pids:
            continue
        env = {}
        for word in words[1:]:  # lossy: ps -E cuts a value at whitespace; paths survive
            key, sep, value = word.partition("=")
            if sep and key in ENV_KEYS:
                env[key] = value
        out[int(words[0])] = env
    return out


def _cwds(pids: Set[int]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    if not pids:
        return out
    if LINUX:
        for pid in pids:
            try:
                out[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
        return out
    try:
        done = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fn", "-p", ",".join(str(p) for p in sorted(pids))],
            env=dict(PS_ENV),
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return out
    pid: Optional[int] = None
    for line in done.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def find(roots: Iterable[os.PathLike[str] | str], *, procs: Optional[List[Proc]] = None) -> List[Proc]:
    """This user's live `gc`, `dolt` and `bd` processes tied to any of `roots`."""
    forms = [form for root in roots for form in root_forms(root)]
    if not forms:
        return []
    me, uid = os.getpid(), os.getuid()
    candidates = {
        p.pid: p
        for p in (table() if procs is None else procs)
        if p.uid == uid and p.pid != me and not p.zombie and _names(p.command)
    }
    found: Dict[int, Proc] = {}
    for proc in candidates.values():
        if any(form + "/" in proc.command or proc.command.endswith(form) or f"{form} " in proc.command for form in forms):
            found[proc.pid] = Proc(proc.pid, proc.ppid, proc.uid, proc.state, proc.command, "argv")
    rest = set(candidates) - set(found)
    for pid, env in _environs(rest).items():
        if any(_under(env.get(key, ""), forms) for key in ENV_KEYS):
            proc = candidates[pid]
            found[pid] = Proc(proc.pid, proc.ppid, proc.uid, proc.state, proc.command, "env")
    rest -= set(found)
    for pid, cwd in _cwds(rest).items():
        if _under(cwd, forms) or _under(os.path.realpath(cwd), forms):
            proc = candidates[pid]
            found[pid] = Proc(proc.pid, proc.ppid, proc.uid, proc.state, proc.command, "cwd")
    return sorted(found.values(), key=lambda p: (not p.watchdog, p.pid))


def _alive(pids: Iterable[int], roots: Sequence[os.PathLike[str] | str]) -> List[int]:
    """Which of `pids` are still live processes tied to `roots` (a pid now
    naming some other process is not ours, and is never signalled)."""
    wanted = set(pids)
    return sorted(p.pid for p in find(roots) if p.pid in wanted)


def _wait(pids: List[int], roots: Sequence[os.PathLike[str] | str], timeout_s: float) -> List[int]:
    end = time.monotonic() + timeout_s
    while True:
        live = _alive(pids, roots)
        if not live or time.monotonic() >= end:
            return live
        time.sleep(0.1)


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop(
    roots: Sequence[os.PathLike[str] | str],
    *,
    grace_s: float = 5.0,
    gc_bin: Optional[os.PathLike[str] | str] = None,
    env_by_root: Optional[Mapping[str, Mapping[str, str]]] = None,
    gc_stop_timeout_s: float = 30.0,
) -> StopReport:
    """End every process tied to `roots`: with `gc_bin`, `gc stop` in each
    city first (bounded); then SIGTERM (watchdogs first), up to `grace_s`,
    then SIGKILL for what is left. Only processes `find` still ties to a root
    are ever signalled."""
    report = StopReport()
    roots = [os.fspath(root) for root in roots]
    report.found = find(roots)
    if not report.found:
        return report
    if gc_bin is not None:
        outputs = []
        for root, env in (env_by_root or {}).items():
            city = env.get("GC_CITY", root)
            try:
                done = subprocess.run(
                    [os.fspath(gc_bin), "stop"],
                    cwd=city,
                    env=dict(env),
                    capture_output=True,
                    text=True,
                    timeout=gc_stop_timeout_s,
                    check=False,
                )
                outputs.append(f"{root}: exit {done.returncode}")
            except (OSError, subprocess.TimeoutExpired) as err:
                outputs.append(f"{root}: {err}")
        report.gc_stop = "; ".join(outputs)
    pids = [p.pid for p in report.found]
    live = _alive(pids, roots)
    watchdogs = [p.pid for p in report.found if p.watchdog and p.pid in live]
    for pid in watchdogs:
        _signal(pid, signal.SIGTERM)
    for pid in live:
        if pid not in watchdogs:
            _signal(pid, signal.SIGTERM)
    left = _wait(pids, roots, grace_s)
    report.termed = sorted(set(pids) - set(left))
    for pid in left:
        _signal(pid, signal.SIGKILL)
    report.killed = left
    report.alive = _wait(left, roots, 2.0) if left else []
    return report
