"""Shared fixtures for the runtime-herdr-core unit tier.

The pack's package lives under ``assets/`` (pack-spec: private support files)
and the fake-hc under ``tests/fakehc``; both go on ``sys.path`` here so the
tier runs the same from the pack root (the pack's CI job) and from the
repository root (the shared pytest step).

No test in this tier builds, installs or runs a real herdr-core binary: every
hc conversation is with the in-process fake-hc, whose every result and every
client request is checked against the vendored hc schema.

Nothing a test starts may outlive the run, on Linux or macOS, whether the
test passes, fails or is interrupted: ``short_tmp`` ends the process groups
recorded under its root before removing it, ``teardown_cores`` finds a test's
standalone cores with a portable scan (``/proc`` on Linux, ``ps`` on macOS),
and two autouse safety nets close any live-harness scratch world a test left
open, the session one also at Ctrl-C.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, List, Optional, Set

import pytest

PACK_ROOT = Path(__file__).resolve().parents[2]
for _entry in (PACK_ROOT / "assets", PACK_ROOT / "tests", PACK_ROOT / "tests" / "unit", PACK_ROOT / "tests" / "live"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from fakehc import FakeHc  # noqa: E402
from gc_runtime_herdr_core.hc.client import BackendFault, CallTimeouts, CoreEndpoint, HcClient  # noqa: E402
from gc_runtime_herdr_core.hc.version import Version, VersionPolicy  # noqa: E402
from gc_runtime_herdr_core.rpp.cancel import CancelToken, Deadline  # noqa: E402
from livekit import pgroups  # noqa: E402

TEST_POLICY = VersionPolicy(min_version=Version.parse("0.1.19"), tested=(Version.parse("0.1.19"),))

ClientFactory = Callable[..., HcClient]


_SESSION_ROOTS: Set[str] = set()
"""Short temp roots made this session and not removed yet (the session net's)."""


def _end_root(path: str) -> None:
    """End every process group recorded under ``path``, then remove it."""
    try:
        pgroups.kill_groups(pgroups.read_groups([path]), term_grace_s=1.0)
    finally:
        shutil.rmtree(path, ignore_errors=True)
        _SESSION_ROOTS.discard(path)


@pytest.fixture(autouse=True)
def _close_forgotten_scratch_worlds(request: pytest.FixtureRequest) -> Iterator[None]:
    """After each test, close any live-harness scratch world it opened and
    left open (a failing body, a forgotten ``close()``), and say so."""
    from livekit.scratch import open_worlds

    before = {id(world) for world in open_worlds()}
    yield
    for world in open_worlds():
        if id(world) in before:
            continue
        problems = world.close(grace_s=5.0)
        warnings.warn(
            pytest.PytestWarning(
                f"{request.node.nodeid} left scratch world {world.base} open; closed it"
                + (": " + "; ".join(problems) if problems else "")
            ),
            stacklevel=1,
        )


@pytest.fixture(scope="session", autouse=True)
def _end_everything_at_session_end() -> Iterator[None]:
    """At session end (pytest runs this on Ctrl-C too): close every scratch
    world still open, end the groups recorded under every short root still
    on disk, and remove those roots."""
    yield
    from livekit.scratch import close_all

    close_all()
    for root in sorted(_SESSION_ROOTS):
        _end_root(root)


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short private directory: a Unix socket path must fit in sun_path."""
    path = os.path.realpath(tempfile.mkdtemp(prefix="hcrt-", dir="/tmp"))  # /tmp is a symlink on macOS
    _SESSION_ROOTS.add(path)
    try:
        yield Path(path)
    finally:
        _end_root(path)


@pytest.fixture
def fake_hc(short_tmp: Path) -> Iterator[FakeHc]:
    """A running fake-hc; the test fails if any reply or request left the schema."""
    fake = FakeHc(str(short_tmp / "hc.sock"))
    fake.start()
    try:
        yield fake
    finally:
        fake.close()
    assert fake.schema_violations == [], "fake-hc schema guard:\n" + "\n".join(fake.schema_violations)


@pytest.fixture
def gate_policy() -> VersionPolicy:
    """The gate policy matching fake-hc's default version."""
    return TEST_POLICY


@pytest.fixture
def make_client(fake_hc: FakeHc) -> ClientFactory:
    """Build an HcClient for one op against the fake (fresh endpoint per call)."""

    def factory(
        *,
        deadline_s: float = 30.0,
        read: bool = False,
        probe: bool = False,
        cancel: Optional[CancelToken] = None,
        endpoint: Optional[CoreEndpoint] = None,
        fault: Optional[BackendFault] = None,
    ) -> HcClient:
        return HcClient(
            endpoint or CoreEndpoint(fake_hc.socket_path, policy=TEST_POLICY),
            deadline=Deadline.after(deadline_s),
            cancel=cancel or CancelToken(),
            timeouts=CallTimeouts.for_op(read=read, probe=probe),
            fault=fault,
        )

    return factory


@pytest.fixture
def home_dir(tmp_path: Path) -> Path:
    """Stands in for the password-database home: tests never write the real one."""
    home = tmp_path / "pwhome"
    home.mkdir(mode=0o755)
    return home


@pytest.fixture
def city(short_tmp: Path, home_dir: Path) -> Iterator["City"]:
    """An in-process city with its fake-hc core running (see cityharness)."""
    from cityharness import make_city

    made = make_city(short_tmp, home_dir)
    made.start_core()
    try:
        yield made
    finally:
        teardown_cores(made.root / "R")
        if made.fake is not None:
            made.fake.close()
            assert made.fake.schema_violations == [], "fake-hc schema guard:\n" + "\n".join(made.fake.schema_violations)


def _processes_for(root: Path, marker: str) -> List[int]:
    """Pids of this user's live processes whose argv holds ``marker`` and
    that belong to state root ``root``: their environment points at it
    (``GC_EXEC_STATE_DIR`` or ``XDG_STATE_HOME``), or they are in a process
    group recorded under it. Portable: ``/proc`` on Linux, ``ps`` on macOS."""
    from gc_runtime_herdr_core.herdrcore.procs import ProcError, default_source

    procs = default_source()
    try:
        snapshot = procs.snapshot()
    except ProcError:
        return []
    me, uid = os.getpid(), os.getuid()
    candidates = [
        p.pid
        for p in snapshot.values()
        if p.pid != me and p.uid == uid and not p.zombie and not p.opaque and any(marker in word for word in p.argv)
    ]
    if not candidates:
        return []
    envs = procs.environs(candidates)
    wanted = str(root)
    found = {
        pid
        for pid in candidates
        if envs.get(pid, {}).get("GC_EXEC_STATE_DIR") == wanted or envs.get(pid, {}).get("XDG_STATE_HOME") == wanted
    }
    groups = pgroups.read_groups([wanted]) if os.path.isdir(wanted) else []
    if groups:
        try:
            rows = pgroups.table()
        except pgroups.TableError:
            rows = {}
        owned, _ = pgroups.owned_groups(groups, rows)
        found.update(pid for pid in candidates if pid in rows and rows[pid].pgid in owned)
    return sorted(found)


def teardown_cores(root: Path, timeout: float = 30.0) -> None:
    """Wait out every background core start a test's ops kicked, stop every
    core under ``root``, end the process groups recorded under it, and fail
    the test if a core or kick survives (killing it). The same on every
    platform."""
    import signal
    import time

    end = time.monotonic() + timeout
    while _processes_for(root, "x-kick-core") and time.monotonic() < end:
        time.sleep(0.05)
    stop_cores(root)
    while _processes_for(root, "cli.py") and time.monotonic() < end:
        time.sleep(0.05)
    leaked = _processes_for(root, "cli.py") + _processes_for(root, "x-kick-core")
    for pid in leaked:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if os.path.isdir(root):
        pgroups.kill_groups(pgroups.read_groups([str(root)]), term_grace_s=1.0)
    assert not leaked, f"standalone fake cores or kicks outlived the test: {leaked}"


def stop_cores(root: Path) -> None:
    """Stop every standalone core a test's ops started under state root ``root``."""
    from fakehc.rawclient import raw_call

    for lock in root.glob("herdr-core/*/core.lock"):
        try:
            raw_call(lock.read_text(), "server.stop", {}, timeout=10.0)
        except (OSError, ValueError):
            continue


if TYPE_CHECKING:
    from cityharness import City
