"""Nothing a live-harness world starts outlives its teardown.

These run in the default unit tier, on Linux and macOS, without
``GC_HC_LIVE``: fake-hc stands in for hc, and every process is one these
tests start. A world here starts the fake core, the live agent and the
agent's detached infrastructure (the watchdog with its store, the proxy, an
orphan ``sleep`` and a fenced proxy), the processes that used to survive a
run on macOS, where teardown found nothing to signal.
"""

from __future__ import annotations

import calendar
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List

import pytest

from fakehc.cli import write_launcher
from livekit import guard, pgroups
from livekit.hcrpc import HcRpc
from livekit.scratch import STANDIN, Scratch, open_scratch, scratch_world

PACK_ROOT = Path(__file__).resolve().parents[2]
AGENT = PACK_ROOT / "tests" / "live" / "agent.py"
CI = {"GITHUB_ACTIONS": "true"}
GONE_WITHIN_S = 2.0


@pytest.fixture
def fake_bin(short_tmp: Path) -> Path:
    path = short_tmp / "bin" / "herdr-core"
    path.parent.mkdir()
    write_launcher(str(path))
    return path


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # a stranger now has the pid: the one started here is gone, but say not
    try:
        row = pgroups.table().get(pid)
    except pgroups.TableError:
        return False
    return row is None or row.zombie


def _wait_gone(pids: List[int], timeout_s: float) -> List[int]:
    """The pids still alive after up to ``timeout_s``."""
    end = time.monotonic() + timeout_s
    while True:
        alive = [pid for pid in pids if not _gone(pid)]
        if not alive or time.monotonic() >= end:
            return alive
        time.sleep(0.05)


def _start_core(made: Scratch) -> int:
    paths = made.paths()
    os.makedirs(os.path.dirname(paths.socket), mode=0o700, exist_ok=True)
    env = dict(os.environ)
    env.update(made.hc_cli_env(paths.session, paths.socket))
    done = subprocess.run([made.hc_bin, "server", "start"], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return HcRpc(paths.socket).status().pid


def _start_agent(made: Scratch, name: str) -> "subprocess.Popen[bytes]":
    env = dict(made.agent_env(name, GC_HC_LIVE_SPAWN="infra,orphan"), PATH=os.environ.get("PATH", "/usr/bin:/bin"))
    return subprocess.Popen([sys.executable, str(AGENT)], env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)


def _populate(made: Scratch, pids: List[int]) -> "subprocess.Popen[bytes]":
    """Start the fake core, the agent and its infrastructure in ``made``;
    every pid goes to ``pids`` as soon as it is known."""
    pids.append(_start_core(made))
    agent = _start_agent(made, "a1")
    pids.append(agent.pid)
    report = made.agent_report("a1", timeout_s=30, want=("infra", "orphan"))
    pids.extend(report.infra[role] for role in ("watchdog", "dolt", "proxy"))
    pids.extend(report.orphan[role] for role in ("sleep", "fenced"))
    assert made.ping_store(report.store_socket)
    return agent


def _assert_nothing_left(base: str, pids: List[int]) -> None:
    alive = _wait_gone(pids, 5.0)
    assert not alive, f"processes of the world outlived its teardown: {alive}"
    assert pgroups.scan(base) == [], "the portable scan still finds the world's processes"
    assert not os.path.exists(base), f"{base} outlived teardown"


def test_teardown_leaves_no_process_and_no_root(fake_bin: Path) -> None:
    made = open_scratch(guard.clearance(CI), "plain", str(fake_bin), python=sys.executable)
    pids: List[int] = []
    try:
        agent = _populate(made, pids)
        assert len(pids) == 7, pids
        agent.kill()  # the agent crashes: its detached infrastructure is left behind
        agent.wait(timeout=10)
        problems = made.close()
    finally:
        made.close()
    assert problems == [], problems
    _assert_nothing_left(made.base, pids)


def test_teardown_after_a_failing_body_leaves_nothing(fake_bin: Path) -> None:
    pids: List[int] = []
    bases: List[str] = []
    agents: List["subprocess.Popen[bytes]"] = []
    with pytest.raises(RuntimeError, match="the body failed"):
        with scratch_world(guard.clearance(CI), "plain", str(fake_bin), python=sys.executable) as made:
            bases.append(made.base)
            agents.append(_populate(made, pids))
            raise RuntimeError("the body failed")
    for agent in agents:  # still running when the body raised: teardown ended it
        assert agent.wait(timeout=10) in (-signal.SIGTERM, -signal.SIGKILL)
    _assert_nothing_left(bases[0], pids)


def test_fakehc_serve_exits_within_two_seconds_of_sigterm(short_tmp: Path, fake_bin: Path) -> None:
    state = short_tmp / "state"
    state.mkdir(mode=0o700)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(short_tmp),
        "HC_SESSION": "t",
        "HC_SOCKET": str(short_tmp / "hc.sock"),
        "XDG_STATE_HOME": str(state),
    }
    done = subprocess.run([str(fake_bin), "server", "start"], env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    pid = int(json.loads(done.stdout)["pid"])
    assert [g.pgid for g in pgroups.read_groups([str(state)])] == [pid], "the core's group was not recorded"
    os.kill(pid, signal.SIGTERM)
    assert _wait_gone([pid], GONE_WITHIN_S) == [], "fake-hc serve outlived SIGTERM by more than 2 s"


def test_watchdog_stand_in_dies_on_sigterm_even_if_the_parent_ignored_it(short_tmp: Path) -> None:
    standins = short_tmp / "standin"
    standins.mkdir()
    for name in ("__gc-managed-dolt-scope-watchdog", "dolt"):
        shutil.copyfile(str(STANDIN), str(standins / name))
    sock = short_tmp / "store.sock"
    pid_file = short_tmp / "dolt.pid"

    def ignore_sigterm() -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)

    watchdog = subprocess.Popen(
        [sys.executable, "__gc-managed-dolt-scope-watchdog", str(sock), str(pid_file)],
        cwd=str(standins),
        stdin=subprocess.DEVNULL,
        preexec_fn=ignore_sigterm,
        start_new_session=True,
    )
    pgroups.record(str(short_tmp / pgroups.PGROUPS_FILE), watchdog.pid)  # short_tmp ends it if this fails
    end = time.monotonic() + 15
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < end, "the watchdog never started its store"
        time.sleep(0.05)
    store = int(pid_file.read_text())
    os.killpg(watchdog.pid, signal.SIGTERM)
    try:
        assert watchdog.wait(timeout=GONE_WITHIN_S) == -signal.SIGTERM
    except subprocess.TimeoutExpired:
        pytest.fail("the watchdog stand-in outlived SIGTERM by more than 2 s")
    assert _wait_gone([store], GONE_WITHIN_S) == [], "the store stand-in outlived SIGTERM"


def test_kill_groups_never_signals_a_reused_leader() -> None:
    # A recorded group whose leader started long after the record names
    # another process: its pid was reused, so it is a stranger's group.
    stranger = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        report = pgroups.kill_groups([pgroups.Group(stranger.pid, time.time() - 100)], term_grace_s=0.3)
        assert report.skipped and "reused" in report.skipped[0], report
        assert report.termed == [] and report.killed == [] and report.alive == []
        time.sleep(0.2)
        assert stranger.poll() is None, "a reused leader's group was signalled"
    finally:
        stranger.kill()
        stranger.wait(timeout=10)


def test_kill_groups_escalates_to_sigkill() -> None:
    stubborn = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(15, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert stubborn.stdout is not None and stubborn.stdout.readline().strip() == b"ready"
        started = time.monotonic()
        report = pgroups.kill_groups([pgroups.Group(stubborn.pid, time.time())], term_grace_s=0.3)
        assert report.killed == [stubborn.pid] and report.alive == [] and report.termed == [], report
        assert stubborn.wait(timeout=5) == -signal.SIGKILL
        assert time.monotonic() - started < 5
    finally:
        if stubborn.poll() is None:
            stubborn.kill()
            stubborn.wait(timeout=10)


def test_kill_groups_never_signals_the_harness_own_group() -> None:
    mine = os.getpgrp()
    report = pgroups.kill_groups([pgroups.Group(mine, time.time()), pgroups.Group(1, time.time())], term_grace_s=0.1)
    assert report.termed == [] and report.killed == [] and report.members == {}


@pytest.mark.parametrize(
    "line",
    [
        # procps (Linux): right-aligned numbers, a space-padded day.
        "  4242  4242     1  1000 Ss   Mon Nov  9 08:00:00 2026 /usr/bin/python3 db-proxy-child",
        # BSD ps (macOS): the same lstart under LC_ALL=C TZ=UTC0.
        "4242 4242 1 501 S    Mon Nov  9 08:00:00 2026     /usr/bin/python3 db-proxy-child",
    ],
)
def test_the_process_table_reads_procps_and_bsd_lstart(line: str) -> None:
    row = pgroups.parse_row(line)
    assert row is not None
    assert (row.pid, row.pgid, row.ppid) == (4242, 4242, 1)
    assert row.start == float(calendar.timegm((2026, 11, 9, 8, 0, 0, 0, 0, 0)))
    assert row.command == "/usr/bin/python3 db-proxy-child"
    assert not row.zombie


def test_the_process_table_skips_what_is_not_a_row() -> None:
    assert pgroups.parse_row("  PID  PGID") is None
    assert pgroups.parse_row("4242 4242 1 501 Z Mon Nov 9 08:00:00 2026 <defunct>") is not None
    assert pgroups.parse_lstart(["Mon", "Nov", "9", "08:00:00"]) is None


def test_the_registry_reads_back_what_was_recorded(tmp_path: Path) -> None:
    nested = tmp_path / "state" / "herdr-core" / "s"
    nested.mkdir(parents=True)
    pgroups.record(str(tmp_path / "pgroups"), 101, epoch=1000.0)
    pgroups.record(str(nested / "pgroups"), 202, epoch=2000.5)
    with (nested / "pgroups").open("a") as handle:
        handle.write("garbage\n")
    found: Dict[int, float] = {g.pgid: g.epoch for g in pgroups.read_groups([str(tmp_path)])}
    assert found == {101: 1000.0, 202: 2000.5}
