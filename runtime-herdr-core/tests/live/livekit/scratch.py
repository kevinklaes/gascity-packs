"""One live run's scratch world, and its teardown.

Every run gets a fresh ``mktemp`` directory under ``/tmp`` (short, so hc's
socket fits in ``sun_path``) holding the scratch ``GC_EXEC_STATE_DIR`` (the
state root ``R``), the spawn-file directory, the plain lane's runtime
directory, the agents' working directory and the infrastructure stand-ins.

Teardown stops every core it finds under ``R`` (``R/herdr-core/*/core.lock``:
the city key, ``unscoped`` and gc's conformance city keys each start one),
then requires that no hc runtime process of ``R`` and no hc unit those
processes ran in is left, ends the run's own survivors (the infrastructure
stand-ins the suites expect to outlive a crash), and removes the directory
last.

Ending survivors escalates and works the same on Linux and macOS: every
process group the run recorded (``pgroups`` files under the root, see
:mod:`.pgroups`) gets ``SIGTERM``, a short grace (:attr:`Scratch.term_grace_s`,
at most 5 s), then ``SIGKILL``; then whatever :class:`~.owned.Ownership` or
the portable scan still finds gets the same, one process at a time, through
:meth:`~.owned.Ownership.signal`. A process that needed ``SIGKILL`` is a
teardown note, not a problem.

Every world :func:`open_scratch` makes is registered until it is closed, so
a test that forgets ``close()`` (or dies before it) is closed by a safety net
(:func:`open_worlds`, :func:`close_all`); :func:`scratch_world` opens one
that always closes.
"""

from __future__ import annotations

import atexit
import glob
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Set

from gc_runtime_herdr_core.hc.paths import HcPaths, resolve_paths
from gc_runtime_herdr_core.herdrcore.procs import default_source
from gc_runtime_herdr_core.rpp.jsonval import JsonObject, loads

from . import pgroups
from .cases import Lane
from .guard import Clearance
from .owned import RUN_MARK_ENV, NotOwned, Ownership

LIVE_DIR = Path(__file__).resolve().parents[1]
AGENT = LIVE_DIR / "agent.py"
STANDIN = LIVE_DIR / "standin.py"
STANDIN_NAMES = ("__gc-managed-dolt-scope-watchdog", "db-proxy-child", "dolt")
STOP_TIMEOUT_S = 60.0
CITY_NAME = "hclive"
"""The scratch city's name, as gc names a city to its runtime."""


@dataclass(frozen=True)
class AgentReport:
    pid: int
    infra: Dict[str, int]
    orphan: Dict[str, int]
    store_socket: str
    errors: List[str]
    witness: Optional[int] = None
    """A sleep the agent started without ``HC_SCOPE`` (``witness`` asked for)."""


@dataclass(frozen=True)
class HcProbe:
    argv0: str
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class InputLine:
    at: float
    text: str
    phase: str = "prompt"
    """``dialog`` while the agent's startup dialog showed, ``prompt`` after."""


def _private_dir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _remove(path: str) -> None:
    """Remove a scratch root, read-only directories in it included."""
    for here, dirs, _ in os.walk(path):
        for name in dirs:
            full = os.path.join(here, name)
            if not os.path.islink(full):
                os.chmod(full, 0o700)
    shutil.rmtree(path)


@dataclass
class Scratch:
    clearance: Clearance
    lane: Lane
    hc_bin: str
    python: str
    run_id: str
    base: str
    ownership: Ownership
    created_units: Set[str] = field(default_factory=set)
    notes: Dict[str, object] = field(default_factory=dict)
    teardown_notes: List[str] = field(default_factory=list)
    closed: bool = False
    term_grace_s: float = pgroups.DEFAULT_TERM_GRACE_S
    """How long a survivor gets after ``SIGTERM`` before ``SIGKILL`` (capped at 5 s)."""

    # --- layout -----------------------------------------------------------------------

    @property
    def state_root(self) -> str:
        return os.path.join(self.base, "state")

    @property
    def spawn_root(self) -> str:
        """Where the pack writes the scratch city's spawn files. The plain lane
        points the pack at a scratch directory (``GC_HC_TEST_SPAWN_ROOT``: it
        has no ``/run/user`` of its own); the systemd lane leaves the pack's
        production location, the tmpfs ``/run/user/<uid>/gc-herdr-core/<city
        id>/``, which is per city id and so the run's alone."""
        if self.lane == "plain":
            return os.path.join(self.base, "spawn")
        return os.path.join(self.runtime_dir, "gc-herdr-core", self.paths().city_id)

    @property
    def work(self) -> str:
        return os.path.join(self.base, "work")

    @property
    def city_path(self) -> str:
        return os.path.join(self.base, "city")

    @property
    def standin_dir(self) -> str:
        return os.path.join(self.base, "standin")

    @property
    def pgroups_path(self) -> str:
        """The registry the run's agents append their detached groups to."""
        return os.path.join(self.base, pgroups.PGROUPS_FILE)

    @property
    def runtime_dir(self) -> str:
        """The core's ``XDG_RUNTIME_DIR``: a private scratch directory on the
        plain lane (no systemd placement), the user's own on the systemd lane."""
        if self.lane == "plain":
            return os.path.join(self.base, "run")
        return f"/run/user/{os.getuid()}"

    # --- environments ------------------------------------------------------------------

    def pack_env(self, *, rescope: bool = False) -> Dict[str, str]:
        """The environment the pack's ops run with: the scratch state root,
        this lane's placement, and the pinned hc as the binary to drive."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/"),
            "LANG": "C.UTF-8",
            "GC_HC_TEST": "1",
            "GC_HC_TEST_BIN": self.hc_bin,
            "GC_EXEC_STATE_DIR": self.state_root,
            "GC_EXEC_CITY_PATH": self.city_path,
        }
        if self.lane == "plain":
            env["GC_HC_TEST_RUNTIME_DIR"] = self.runtime_dir
            env["GC_HC_TEST_SPAWN_ROOT"] = os.path.join(self.base, "spawn")
        else:
            env["XDG_RUNTIME_DIR"] = self.runtime_dir
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={self.runtime_dir}/bus"
            if rescope:
                env["GC_HC_TEST_USERSCOPE"] = "1"
        return env

    def gc_env(self, *, rescope: bool = False) -> Dict[str, str]:
        """The environment gc's own checks (``gc runtime check`` and
        ``gc runtime conformance``) run with: the pack's, in the scratch city
        named as gc names a city. The pack starts no core for an op that
        names no city; gc's kit names its own cities only for the checks
        that compare two."""
        env = self.pack_env(rescope=rescope)
        env["GC_EXEC_CITY_NAME"] = CITY_NAME
        return env

    def paths(self) -> HcPaths:
        """Where the scratch city's core lives, as the pack resolves it."""
        return resolve_paths(self.pack_env())

    def hc_cli_env(self, session: str, sock: str) -> Dict[str, str]:
        """hc's CLI pointed at one core of the scratch root."""
        env = {
            "PATH": os.path.dirname(self.hc_bin) + ":/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/"),
            "HC_SESSION": session,
            "HC_SOCKET": sock,
            "XDG_STATE_HOME": self.state_root,
            "XDG_CONFIG_HOME": os.path.join(self.state_root, "hc-config"),
            "XDG_RUNTIME_DIR": self.runtime_dir,
        }
        if self.lane == "systemd":
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={self.runtime_dir}/bus"
        return env

    def systemd_env(self) -> Dict[str, str]:
        return dict(os.environ, XDG_RUNTIME_DIR=self.runtime_dir, DBUS_SESSION_BUS_ADDRESS=f"unix:path={self.runtime_dir}/bus")

    def agent_env(self, name: str, **extra: str) -> Dict[str, str]:
        """A session's environment for the live agent (gc's session env
        always carries its own ``XDG_STATE_HOME``/``XDG_CONFIG_HOME``)."""
        env = {
            RUN_MARK_ENV: self.run_id,
            "GC_HC_LIVE_DIR": self.work,
            "GC_HC_LIVE_AGENT": name,
            "GC_HC_LIVE_STANDINS": self.standin_dir,
            "GC_HC_LIVE_PYTHON": self.python,
            pgroups.PGROUPS_ENV: self.pgroups_path,
            "GC_INSTANCE_TOKEN": f"{self.run_id}-{name}-{uuid.uuid4().hex[:8]}",
            "XDG_STATE_HOME": os.path.join(self.base, "agent-state"),
            "XDG_CONFIG_HOME": os.path.join(self.base, "agent-config"),
        }
        env.update(extra)
        return env

    def infra_unit(self, label: str) -> str:
        """A name for a stand-in infrastructure scope this run creates (and stops)."""
        from .units import infra_unit

        unit = infra_unit(self.run_id, label)
        self.created_units.add(unit)
        return unit

    def agent_command(self) -> str:
        return f"exec {self.python} {AGENT}"

    # --- the agent's files ----------------------------------------------------------------

    def _agent_file(self, name: str, leaf: str) -> str:
        return os.path.join(self.work, f"{name}{leaf}")

    def forget_agent(self, name: str) -> None:
        for leaf in (".json", ".hc.json", ".run-hc"):
            try:
                os.unlink(self._agent_file(name, leaf))
            except FileNotFoundError:
                pass

    def agent_report(self, name: str, *, timeout_s: float, want: Sequence[str] = ()) -> AgentReport:
        path = self._agent_file(name, ".json")
        end = time.monotonic() + timeout_s
        while True:
            try:
                with open(path, "rb") as handle:
                    doc = JsonObject.of(loads(handle.read()))
                break
            except (OSError, ValueError):
                if time.monotonic() > end:
                    raise AssertionError(f"agent {name} never reported ({path})") from None
                time.sleep(0.1)
        infra = doc.opt_obj("infra")
        orphan = doc.opt_obj("orphan")
        report = AgentReport(
            pid=doc.req_int("pid"),
            infra={} if infra is None else {r: infra.req_int(r) for r in ("watchdog", "dolt", "proxy") if infra.has(r)},
            orphan={} if orphan is None else {r: orphan.req_int(r) for r in ("sleep", "fenced")},
            store_socket="" if infra is None else infra.req_str("store_socket"),
            errors=list(doc.str_tuple("errors")),
            witness=doc.opt_int("witness"),
        )
        if report.errors:
            raise AssertionError(f"agent {name}: {'; '.join(report.errors)}")
        started = {"infra": bool(report.infra), "orphan": bool(report.orphan), "witness": report.witness is not None}
        for key in want:
            if not started[key]:
                raise AssertionError(f"agent {name} did not start its {key}")
        extra = [] if report.witness is None else [report.witness]
        for pid in [*report.infra.values(), *report.orphan.values(), *extra]:
            self.ownership.record(pid)
        return report

    def input_lines(self, name: str) -> List[InputLine]:
        try:
            with open(self._agent_file(name, ".input.log"), "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return []
        out = []
        for line in raw.splitlines():
            doc = JsonObject.of(loads(line))
            at = doc.raw("at")
            out.append(
                InputLine(
                    at=float(at) if isinstance(at, (int, float)) else 0.0,
                    text=doc.req_str("text"),
                    phase=doc.str_or("phase", "prompt"),
                )
            )
        return out

    def input_text(self, name: str) -> str:
        return "\n".join(line.text for line in self.input_lines(name))

    def run_hc_in_agent(self, name: str, *, timeout_s: float) -> HcProbe:
        """Make the agent run ``hc ls`` from its own PATH and return the outcome."""
        result = self._agent_file(name, ".hc.json")
        Path(self._agent_file(name, ".run-hc")).touch()
        end = time.monotonic() + timeout_s
        while not os.path.exists(result):
            if time.monotonic() > end:
                raise AssertionError(f"agent {name} never ran hc")
            time.sleep(0.1)
        with open(result, "rb") as handle:
            doc = JsonObject.of(loads(handle.read()))
        return HcProbe(argv0=doc.req_str("argv0"), code=doc.req_int("code"), stdout=doc.req_str("stdout"), stderr=doc.req_str("stderr"))

    def ping_store(self, path: str, *, timeout_s: float = 2.0) -> bool:
        """Whether the store stand-in answers on its socket."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(timeout_s)
                conn.connect(path)
                return conn.recv(16).startswith(b"pong")
        except OSError:
            return False

    def find_bytes(self, needle: bytes, roots: Sequence[str] = ()) -> List[str]:
        """Every file under the state root, the spawn root and the runtime
        directory (plain lane), plus ``roots``, that holds ``needle``."""
        where = [self.state_root, self.spawn_root, *roots]
        if self.lane == "plain":
            where.append(self.runtime_dir)
        hits: List[str] = []
        for top in where:
            for here, _, files in os.walk(top):
                for name in files:
                    path = os.path.join(here, name)
                    try:
                        if not stat.S_ISREG(os.lstat(path).st_mode):
                            continue
                        with open(path, "rb") as handle:
                            if needle in handle.read():
                                hits.append(path)
                    except OSError:
                        continue
        return sorted(hits)

    def note(self, key: str, value: object) -> None:
        """Record a measurement for the job summary."""
        self.notes[key] = value

    # --- teardown ---------------------------------------------------------------------------

    def core_locks(self) -> List[str]:
        return sorted(glob.glob(os.path.join(self.state_root, "herdr-core", "*", "core.lock")))

    def _systemctl(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["systemctl", "--user", *args], env=self.systemd_env(), capture_output=True, text=True, timeout=30, check=False
        )

    def close(self, *, grace_s: float = 15.0) -> List[str]:
        """Stop everything the run started and remove the scratch root;
        returns what went wrong (empty when the run left nothing behind).
        Each step runs even when an earlier one failed."""
        if self.closed:
            return []
        self.closed = True
        problems: List[str] = []
        try:
            for step in (self._stop_cores, lambda p: self._reap(p, grace_s), self._stop_units):
                try:
                    step(problems)
                except Exception as err:  # noqa: BLE001 - teardown must reach the removal
                    problems.append(f"teardown step failed: {err!r}")
            if self.lane == "systemd" and os.path.isdir(self.spawn_root):
                shutil.rmtree(self.spawn_root)  # the scratch city's own spawn directory
        finally:
            _OPEN.pop(id(self), None)
            _remove(self.base)
        return problems

    def _stop_cores(self, problems: List[str]) -> None:
        for proc in self.ownership.runtime():
            self.ownership.note_unit(proc.pid)
        for lock in self.core_locks():
            state = os.path.dirname(lock)
            session = os.path.basename(state)
            try:
                with open(lock, encoding="utf-8") as handle:
                    sock = handle.read().strip()
            except OSError:
                continue
            try:
                done = subprocess.run(
                    [self.hc_bin, "server", "stop"],
                    env=self.hc_cli_env(session, sock),
                    capture_output=True,
                    text=True,
                    timeout=STOP_TIMEOUT_S,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                problems.append(f"hc server stop ({session}) hung for {STOP_TIMEOUT_S:.0f} s")
                continue
            if done.returncode != 0:
                self.teardown_notes.append(f"hc server stop ({session}) exited {done.returncode}: {done.stderr.strip()[-300:]}")

    def _reap(self, problems: List[str], grace_s: float) -> None:
        end = time.monotonic() + grace_s
        while self.ownership.runtime() and time.monotonic() < end:
            time.sleep(0.2)
        for proc in self.ownership.runtime():
            problems.append(f"hc runtime of the scratch root outlived teardown: pid {proc.pid} {' '.join(proc.argv)[:200]}")
        grace = min(max(self.term_grace_s, 0.0), pgroups.MAX_TERM_GRACE_S)
        report = pgroups.kill_groups(pgroups.read_groups([self.base]), term_grace_s=grace)
        self.teardown_notes.extend(report.skipped)
        for pgid in report.killed:
            self.teardown_notes.append(f"needed SIGKILL after SIGTERM: {report.describe(pgid)}")
        for pgid in report.alive:
            problems.append(f"a recorded process group survived SIGKILL: {report.describe(pgid)}")
        leftover = {p.pid for p in self.ownership.marked()} | {p.pid for p in self.ownership.runtime()}
        leftover |= {row.pid for row in pgroups.scan(self.base)}
        self._terminate(sorted(leftover), grace, problems)

    def _terminate(self, pids: Sequence[int], grace_s: float, problems: List[str]) -> None:
        """``SIGTERM`` each owned pid, up to ``grace_s`` for it to go, then ``SIGKILL``."""
        sent = [pid for pid in pids if self._send(pid, signal.SIGTERM)]
        end = time.monotonic() + grace_s
        while time.monotonic() < end and any(self.ownership.alive(pid) for pid in sent):
            time.sleep(0.1)
        for pid in sent:
            if self.ownership.alive(pid) and self._send(pid, signal.SIGKILL):
                self.teardown_notes.append(f"needed SIGKILL after SIGTERM: pid {pid}")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and any(self.ownership.alive(pid) for pid in sent):
            time.sleep(0.1)
        for pid in sent:
            if self.ownership.alive(pid):
                problems.append(f"pid {pid} of the run survived SIGKILL")

    def _send(self, pid: int, sig: int) -> bool:
        try:
            self.ownership.signal(pid, sig)
        except NotOwned:
            return False
        return True

    def _stop_units(self, problems: List[str]) -> None:
        if self.lane != "systemd":
            return
        for unit in sorted(self.ownership.units):
            active = self._systemctl("is-active", "--", unit).stdout.strip()
            if active in ("active", "activating", "deactivating", "reloading"):
                problems.append(f"hc unit {unit} outlived teardown ({active})")
                self._systemctl("stop", "--", unit)
        for unit in sorted(self.created_units):
            self._systemctl("stop", "--", unit)


_OPEN: Dict[int, Scratch] = {}
"""Every world :func:`open_scratch` made that is not closed yet."""


def open_worlds() -> List[Scratch]:
    """The scratch worlds opened and not closed yet, oldest first."""
    return list(_OPEN.values())


def close_all(*, grace_s: float = 5.0) -> List[str]:
    """Close every open world (a safety net); what went wrong, per world."""
    problems: List[str] = []
    for made in open_worlds():
        try:
            problems.extend(f"{made.base}: {line}" for line in made.close(grace_s=grace_s))
        except Exception as err:  # noqa: BLE001 - a net closes every world it can
            problems.append(f"{made.base}: close failed: {err!r}")
    return problems


def _kill_open_groups() -> None:
    """At interpreter exit: end the groups every still-open world recorded
    and remove its root. Cheap insurance for a run that died before its
    fixtures' teardown; never runs hc."""
    for made in open_worlds():
        try:
            pgroups.kill_groups(pgroups.read_groups([made.base]), term_grace_s=1.0)
        except Exception:  # noqa: BLE001 - exit must go on
            pass
        shutil.rmtree(made.base, ignore_errors=True)


atexit.register(_kill_open_groups)


def open_scratch(clearance: Clearance, lane: Lane, hc_bin: str, *, python: Optional[str] = None) -> Scratch:
    """A fresh scratch world for one run (needs a :class:`~.guard.Clearance`)."""
    if not isinstance(clearance, Clearance):
        raise TypeError("a scratch root needs the host's clearance")
    base = os.path.realpath(tempfile.mkdtemp(prefix="hclive-", dir="/tmp"))
    run_id = uuid.uuid4().hex[:12]
    scratch = Scratch(
        clearance=clearance,
        lane=lane,
        hc_bin=hc_bin,
        python=python or sys.executable,
        run_id=run_id,
        base=base,
        ownership=Ownership(run_id, os.path.join(base, "state"), default_source(), base=base),
    )
    _OPEN[id(scratch)] = scratch
    for path in (scratch.state_root, scratch.work, scratch.city_path, scratch.standin_dir):
        _private_dir(path)
    if lane == "plain":
        _private_dir(scratch.runtime_dir)
        _private_dir(scratch.spawn_root)
    for name in STANDIN_NAMES:
        target = os.path.join(scratch.standin_dir, name)
        shutil.copyfile(str(STANDIN), target)
        os.chmod(target, 0o600)
    return scratch


@contextmanager
def scratch_world(
    clearance: Clearance,
    lane: Lane,
    hc_bin: str,
    *,
    python: Optional[str] = None,
    problems: Optional[List[str]] = None,
) -> Iterator[Scratch]:
    """:func:`open_scratch` that always closes, even when the body raises;
    the close's problems go to ``problems`` when given."""
    made = open_scratch(clearance, lane, hc_bin, python=python)
    try:
        yield made
    finally:
        found = made.close()
        if problems is not None:
            problems.extend(found)


def write_summary(lines: Mapping[str, object], path: Optional[str]) -> None:
    """Append the run's notes to the job summary, when there is one."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in lines.items():
            handle.write(f"- **{key}**: `{json.dumps(value)}`\n")
