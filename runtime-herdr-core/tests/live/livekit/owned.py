"""What one live run owns, and the only way it signals a process.

A process belongs to the run when

* the run started it and recorded its start time (:meth:`Ownership.record`);
* its environment carries the run's mark (``GC_HC_LIVE_RUN=<run id>``): every
  agent and stand-in the run starts gets it, and whatever they start inherits
  it; or
* it is hc runtime of the run's scratch state root (its ``XDG_STATE_HOME``
  is that root, a fresh ``mktemp`` directory nothing else uses): the cores
  and holders the run's own ops started; or
* its command line names the run's scratch root (``base``, that same fresh
  directory): what a stand-in or a session runs from there. This is the key
  that still works where an environment cannot be read (macOS ``ps -E``
  shows none for a platform binary).

The process table is any :class:`ProcSource`: ``/proc`` on Linux, ``ps`` on
macOS (``default_source()``). A scan reads every candidate's environment in
one batch (:meth:`ProcSource.environs`), not one ``ps`` per process.

:meth:`Ownership.signal` opens a pidfd first and then checks ownership, so a
pid reused in between can never be signalled: the pidfd still names the
process that was judged, or a dead one.
"""

from __future__ import annotations

import os
import signal as signals
from typing import Dict, Iterable, List, Mapping, Optional, Set

from gc_runtime_herdr_core.herdrcore.procs import Proc, ProcError, ProcSource

from .units import unit_of

RUN_MARK_ENV = "GC_HC_LIVE_RUN"
HC_RUNTIME_ENV = "HC_RUNTIME"
"""hc marks its own cores and holders with ``HC_RUNTIME=1``."""
HC_BINARIES = frozenset({"hc", "herdr-core"})


class NotOwned(Exception):
    """The run does not own that process, or it is gone."""


class Ownership:
    def __init__(self, run_id: str, state_root: str, procs: ProcSource, *, base: str = "") -> None:
        self.run_id = run_id
        self.state_root = state_root
        self.procs = procs
        self.base = base
        """The run's scratch root: a process whose argv names it is the run's."""
        self._started: Dict[int, str] = {}
        self.units: Set[str] = set()
        """hc units the run's cores and holders ran in (teardown checks them)."""

    def _proc(self, pid: int) -> Optional[Proc]:
        try:
            return self.procs.process(pid)
        except ProcError:
            return None

    def record(self, pid: int) -> None:
        """Remember a process the run started itself."""
        proc = self._proc(pid)
        if proc is None:
            raise NotOwned(f"pid {pid} is gone before it could be recorded")
        self._started[pid] = proc.start

    def _owns(self, proc: Proc, runtime_only: bool) -> bool:
        if proc.uid != os.getuid() or proc.opaque:
            return False
        try:
            env = self.procs.environ(proc.pid) or {}
        except ProcError:
            env = None
        return self._judge(proc, env, runtime_only)

    def _judge(self, proc: Proc, env: Optional[Mapping[str, str]], runtime_only: bool) -> bool:
        """Whether the run owns ``proc``; ``env`` None when it could not be read."""
        if env is not None and env.get("XDG_STATE_HOME") == self.state_root:
            return True
        if runtime_only:
            return False
        if env is not None and env.get(RUN_MARK_ENV) == self.run_id:
            return True
        if self.base and any(self.base in word for word in proc.argv):
            return True
        return self._started.get(proc.pid) == proc.start and proc.start != ""

    def owns(self, pid: int, *, runtime_only: bool = False) -> bool:
        """Whether the run owns ``pid`` (only hc runtime of its root with ``runtime_only``)."""
        proc = self._proc(pid)
        return proc is not None and not proc.zombie and self._owns(proc, runtime_only)

    def runtime(self) -> List[Proc]:
        """The run's hc cores and holders that are still running."""
        return self._scan(runtime_only=True)

    def marked(self) -> List[Proc]:
        """The run's own marked processes (agents, stand-ins), hc runtime excluded."""
        runtime = {p.pid for p in self.runtime()}
        return [p for p in self._scan(runtime_only=False) if p.pid not in runtime]

    def runtime_from_agents(self) -> List[Proc]:
        """hc cores and holders that carry the run's mark, so were started from
        an agent's environment: hc runtime of the scratch root, or a process hc
        marks as runtime (or an hc binary) wherever its state lives. The pack
        starts its core with an allowlisted environment, and a holder restarts
        its core with its own (the core's), so neither ever carries the mark."""
        found: List[Proc] = []
        procs = self._scan(runtime_only=False)
        envs = self._environs(p.pid for p in procs)
        for proc in procs:
            env = envs.get(proc.pid)
            if env is None or env.get(RUN_MARK_ENV) != self.run_id:
                continue
            binary = os.path.basename(proc.argv[0]) if proc.argv else ""
            if env.get("XDG_STATE_HOME") == self.state_root or env.get(HC_RUNTIME_ENV) == "1" or binary in HC_BINARIES:
                found.append(proc)
        return found

    def _environs(self, pids: Iterable[int]) -> Dict[int, Dict[str, str]]:
        try:
            return self.procs.environs(pids)
        except ProcError:
            return {}

    def _scan(self, *, runtime_only: bool) -> List[Proc]:
        try:
            snapshot = self.procs.snapshot()
        except ProcError:
            return []
        me, uid = os.getpid(), os.getuid()
        candidates = [p for p in snapshot.values() if p.pid != me and not p.zombie and p.uid == uid and not p.opaque]
        envs = self._environs(p.pid for p in candidates)
        return sorted((p for p in candidates if self._judge(p, envs.get(p.pid), runtime_only)), key=lambda p: p.pid)

    def alive(self, pid: int) -> bool:
        """Whether ``pid`` runs and is not a zombie (and, when the run recorded
        it, is still the process recorded)."""
        proc = self._proc(pid)
        if proc is None or proc.zombie:
            return False
        recorded = self._started.get(pid)
        return recorded is None or recorded == proc.start

    def signal(self, pid: int, sig: int) -> None:
        """Signal a process the run owns; :class:`NotOwned` for any other.

        With a pidfd (Linux 5.3 and a Python built with it) the process is
        pinned before it is judged, so a reused pid can never be signalled.
        Without one, the check and the signal are one ``/proc`` read apart."""
        pidfd_open = getattr(os, "pidfd_open", None)
        if pidfd_open is None:
            if not self.owns(pid):
                raise NotOwned(f"pid {pid} does not belong to this run, or is gone")
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            return
        try:
            fd: int = pidfd_open(pid)
        except ProcessLookupError:
            raise NotOwned(f"pid {pid} is gone") from None
        try:
            if not self.owns(pid):
                raise NotOwned(f"pid {pid} does not belong to this run")
            try:
                signals.pidfd_send_signal(fd, sig)
            except ProcessLookupError:
                pass
        finally:
            os.close(fd)

    def note_unit(self, pid: int) -> Optional[str]:
        """Record the hc unit an owned runtime process runs in, if any."""
        if not self.owns(pid, runtime_only=True):
            return None
        cgroup = self.procs.cgroup(pid)
        unit = None if cgroup is None else unit_of(cgroup)
        if unit is not None and unit.startswith(("hc-serve-", "hc-hold-")):
            self.units.add(unit)
        return unit
