"""The process groups a test run started, and how teardown ends them.

Every long-running process the harness starts (the fake core, a fake
holder, a session program, the agent's detached stand-ins) leads a process
group of its own, and whoever starts it appends one line to a ``pgroups``
file under the run's scratch root::

    <pgid> <epoch seconds when it was started>

Lines are only ever appended (``O_APPEND``, one ``write`` each). The fake-hc
scripts and the live agent write the format themselves, so they never
import this module.

Teardown reads every ``pgroups`` file under the run's roots and ends each
group with :func:`kill_groups`: ``SIGTERM`` to the whole group, a short
grace, then ``SIGKILL``. A group is signalled only while it is still the one
the run recorded: its leader, if alive, must have started when the record
says (a reused pid leads a stranger's group). While any member lives, POSIX
never reuses the group id, so a group whose leader exited is still the
run's. The harness's own group and pgid 1 are never signalled.

The process table comes from one ``ps`` (procps on Linux, BSD ``ps`` on
macOS, which has no ``/proc``), so the same code finds the same processes
on both. Only this user's processes are ever considered.
"""

from __future__ import annotations

import calendar
import glob
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

PGROUPS_FILE = "pgroups"
PGROUPS_ENV = "GC_HC_LIVE_PGROUPS"
"""Where the live agent appends the groups it starts (``<base>/pgroups``)."""

DEFAULT_TERM_GRACE_S = 2.0
MAX_TERM_GRACE_S = 5.0
REUSE_TOLERANCE_S = 2.0
"""How far a leader's start may lie from the recorded epoch (``lstart`` has
whole seconds, and the epoch is taken just after the start)."""
POLL_S = 0.1
PS_ENV = {"LC_ALL": "C", "TZ": "UTC0", "PATH": "/bin:/usr/bin"}
PS_COLUMNS = "pid=,pgid=,ppid=,uid=,stat=,lstart=,command="
PS_TIMEOUT_S = 30.0


class TableError(Exception):
    """The process table could not be read."""


@dataclass(frozen=True)
class Row:
    """One process as ``ps`` listed it."""

    pid: int
    pgid: int
    ppid: int
    uid: int
    state: str
    start: Optional[float]
    """When it started (epoch seconds, whole); None when unreadable."""
    command: str

    @property
    def zombie(self) -> bool:
        return self.state.startswith("Z")


@dataclass(frozen=True)
class Group:
    """One recorded process group."""

    pgid: int
    epoch: float
    source: str = ""
    """The ``pgroups`` file it was read from."""


@dataclass
class KillReport:
    """What :func:`kill_groups` did."""

    termed: List[int] = field(default_factory=list)
    """Groups that ``SIGTERM`` ended within the grace."""
    killed: List[int] = field(default_factory=list)
    """Groups that needed ``SIGKILL``."""
    alive: List[int] = field(default_factory=list)
    """Groups with a member still alive after ``SIGKILL``."""
    skipped: List[str] = field(default_factory=list)
    """Recorded groups left alone, and why (a reused leader pid)."""
    members: Dict[int, List[str]] = field(default_factory=dict)
    """Each signalled group's members, as ``<pid> <command>``."""

    def describe(self, pgid: int) -> str:
        return f"group {pgid}: " + "; ".join(self.members.get(pgid, [])[:4])


def parse_lstart(words: Sequence[str]) -> Optional[float]:
    """``ps``'s ``lstart`` (``Thu Nov 26 14:30:00 2026``, five words, read
    under ``TZ=UTC0``) as epoch seconds; None when it is not one."""
    if len(words) != 5:
        return None
    try:
        return float(calendar.timegm(time.strptime(" ".join(words), "%a %b %d %H:%M:%S %Y")))
    except (ValueError, OverflowError):
        return None


def parse_row(line: str) -> Optional[Row]:
    """One line of ``ps -o`` :data:`PS_COLUMNS`; None when it is not one."""
    parts = line.split()
    if len(parts) < 10 or not all(p.isdigit() for p in parts[:4]):
        return None
    return Row(
        pid=int(parts[0]),
        pgid=int(parts[1]),
        ppid=int(parts[2]),
        uid=int(parts[3]),
        state=parts[4],
        start=parse_lstart(parts[5:10]),
        command=" ".join(parts[10:]),
    )


def parse_table(text: str) -> Dict[int, Row]:
    out: Dict[int, Row] = {}
    for line in text.splitlines():
        row = parse_row(line)
        if row is not None:
            out[row.pid] = row
    return out


def table() -> Dict[int, Row]:
    """Every process on the host, one ``ps`` (``-A``: every process, on
    procps and BSD alike)."""
    try:
        done = subprocess.run(
            ["ps", "-A", "-ww", "-o", PS_COLUMNS],
            env=dict(PS_ENV),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise TableError(f"ps: {err}") from None
    rows = parse_table(done.stdout.decode("utf-8", "surrogateescape"))
    if not rows:
        raise TableError(f"ps listed no process (exit {done.returncode})")
    return rows


def record(path: str, pid: int, *, epoch: Optional[float] = None) -> None:
    """Append ``pid`` (a group leader) to the registry at ``path``; never raises."""
    line = f"{pid} {time.time() if epoch is None else epoch:.3f}\n".encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        os.write(fd, line)
    except OSError:
        pass
    finally:
        os.close(fd)


def parse_groups(text: str, source: str = "") -> List[Group]:
    out: List[Group] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        try:
            out.append(Group(pgid=int(parts[0]), epoch=float(parts[1]), source=source))
        except ValueError:
            continue
    return out


def registry_files(roots: Iterable[str]) -> List[str]:
    found: Set[str] = set()
    for root in roots:
        if os.path.isfile(root) and os.path.basename(root) == PGROUPS_FILE:
            found.add(root)
            continue
        found.update(glob.glob(os.path.join(glob.escape(root), "**", PGROUPS_FILE), recursive=True))
    return sorted(found)


def read_groups(roots: Iterable[str]) -> List[Group]:
    """Every group recorded in a ``pgroups`` file under ``roots`` (a root
    may also name such a file)."""
    out: List[Group] = []
    for path in registry_files(roots):
        try:
            with open(path, encoding="utf-8") as handle:
                out.extend(parse_groups(handle.read(), path))
        except OSError:
            continue
    return out


def _members(rows: Dict[int, Row], pgid: int, uid: int) -> List[Row]:
    return [r for r in rows.values() if r.pgid == pgid and r.uid == uid and not r.zombie]


def owned_groups(groups: Iterable[Group], rows: Dict[int, Row]) -> Tuple[Dict[int, List[Row]], List[str]]:
    """The recorded groups that still have members and are still the run's,
    with their members; and the groups left alone, with why."""
    uid = os.getuid()
    mine = os.getpgrp()
    out: Dict[int, List[Row]] = {}
    skipped: List[str] = []
    seen: Set[int] = set()
    for group in groups:
        if group.pgid in seen or group.pgid <= 1 or group.pgid == mine:
            continue
        seen.add(group.pgid)
        members = _members(rows, group.pgid, uid)
        if not members:
            continue
        leader = rows.get(group.pgid)
        if leader is not None and leader.pgid == group.pgid:
            if leader.start is None:
                skipped.append(f"group {group.pgid}: its leader's start time cannot be read")
                continue
            if abs(leader.start - group.epoch) > REUSE_TOLERANCE_S:
                skipped.append(
                    f"group {group.pgid}: its leader started at {leader.start:.0f}, not {group.epoch:.0f} as recorded"
                    " (a reused pid); left alone"
                )
                continue
        out[group.pgid] = members
    return out, skipped


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_groups(
    groups: Iterable[Group],
    *,
    term_grace_s: float = DEFAULT_TERM_GRACE_S,
    kill_wait_s: float = 2.0,
) -> KillReport:
    """End every recorded group that is still the run's: ``SIGTERM``, up to
    ``term_grace_s`` (capped at :data:`MAX_TERM_GRACE_S`) for it to go,
    then ``SIGKILL`` and up to ``kill_wait_s``."""
    grace = min(max(term_grace_s, 0.0), MAX_TERM_GRACE_S)
    report = KillReport()
    groups = list(groups)
    if not groups:
        return report
    rows = table()
    targets, report.skipped = owned_groups(groups, rows)
    if not targets:
        return report
    uid = os.getuid()
    for pgid, members in targets.items():
        report.members[pgid] = [f"{m.pid} {m.command[:160]}" for m in members]
        _signal_group(pgid, signal.SIGTERM)
    live = _wait_gone(set(targets), uid, grace)
    report.termed = sorted(set(targets) - live)
    if not live:
        return report
    for pgid in live:
        _signal_group(pgid, signal.SIGKILL)
    report.killed = sorted(live)
    report.alive = sorted(_wait_gone(live, uid, kill_wait_s))
    return report


def _wait_gone(pgids: Set[int], uid: int, timeout_s: float) -> Set[int]:
    """The groups of ``pgids`` that still have a live member after up to ``timeout_s``."""
    end = time.monotonic() + timeout_s
    live = set(pgids)
    while True:
        try:
            rows = table()
        except TableError:
            return live
        live = {pgid for pgid in live if _members(rows, pgid, uid)}
        if not live or time.monotonic() >= end:
            return live
        time.sleep(POLL_S)


def scan(base: str, *, roots: Sequence[str] = (), rows: Optional[Dict[int, Row]] = None) -> List[Row]:
    """This user's live processes that belong to the run rooted at ``base``:
    the scratch path is in their command, or they are in a group the run
    recorded under ``base`` (or ``roots``) that is still the run's. Never
    this process or its parent."""
    rows = table() if rows is None else rows
    uid = os.getuid()
    me = {os.getpid(), os.getppid()}
    owned, _ = owned_groups(read_groups([base, *roots]), rows)
    return sorted(
        (
            r
            for r in rows.values()
            if r.uid == uid
            and not r.zombie
            and r.pid not in me
            and (base in r.command or r.pgid in owned)
        ),
        key=lambda r: r.pid,
    )
