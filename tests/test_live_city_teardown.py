"""A scratch city's gc and dolt processes do not outlive its test.

A real gc leaves city infrastructure running for a throwaway city: on gc
1.5.1 one `gc doctor` starts a managed Dolt (a scope watchdog and
`dolt sql-server`) that reparents to PID 1 and runs until signalled. The
live-gc tests stop it through `live_city_reaper` (wired into
`gc_live_city`'s fixtures). The first case proves that against a real gc
(`GC_TEST_BIN`, skipped without it); the others check, without gc, that the
reaper ties processes to a root by argv, environment and working directory,
and never touches one that names no root of its own.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

import live_city_reaper
from gc_live_city import (
    gc_test_bin,  # noqa: F401 -- pytest fixture, used by name
    reap_live_cities,  # noqa: F401 -- autouse fixture: stops what gc left per test
    reap_live_cities_at_session_end,  # noqa: F401 -- autouse fixture: and at session end
    run_gc,
    write_city,
)

SLEEP = shutil.which("sleep") or "/bin/sleep"
STAND_IN = "import time\ntime.sleep(60)\n"


def test_nothing_gc_starts_for_a_scratch_city_outlives_its_stop(tmp_path: Path, gc_test_bin: Path) -> None:  # noqa: F811
    workspace = write_city(tmp_path / "city-root", {})
    run_gc(gc_test_bin, workspace, "doctor")  # the command that left a managed Dolt behind
    root = tmp_path / "city-root"
    before = live_city_reaper.find([root])
    report = live_city_reaper.stop([root])
    assert live_city_reaper.find([root]) == [], report
    assert report.killed == [], f"needed SIGKILL: {report.describe(report.killed)}"
    # What doctor started, for the log: on gc 1.5.1 a watchdog and dolt sql-server.
    print(f"gc left {len(before)} process(es) for the city: {[p.command[:120] for p in before]}")


def _start(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, start_new_session=True)


def _wait_listed(pids: list[int]) -> None:
    end = time.monotonic() + 10
    while time.monotonic() < end:
        listed = {p.pid for p in live_city_reaper.table()}
        if set(pids) <= listed:
            return
        time.sleep(0.05)


def test_the_reaper_ties_processes_to_a_root_and_spares_the_rest(tmp_path: Path) -> None:
    root = tmp_path / "city-root"
    elsewhere = tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    (root / "dolt").symlink_to(SLEEP)  # argv[0] is <root>/dolt
    (elsewhere / "dolt").symlink_to(SLEEP)
    (elsewhere / "gc").write_text(STAND_IN)
    clean_env = {"PATH": "/usr/bin:/bin"}
    by_argv = _start([str(root / "dolt"), "60"], cwd=elsewhere, env=clean_env)
    by_cwd = _start([str(elsewhere / "dolt"), "61"], cwd=root, env=clean_env)
    by_env = _start(
        [sys.executable, str(elsewhere / "gc")],
        cwd=elsewhere,
        env=dict(clean_env, **{live_city_reaper.ROOT_ENV: str(root)}),
    )
    stranger = _start([str(elsewhere / "dolt"), "62"], cwd=elsewhere, env=clean_env)  # a dolt, but not this root's
    plain = _start([SLEEP, "63"], cwd=root, env=clean_env)  # this root's cwd, but no gc, dolt or bd
    started = [by_argv, by_cwd, by_env, stranger, plain]
    try:
        _wait_listed([p.pid for p in started])
        found = {p.pid: p.why for p in live_city_reaper.find([root])}
        assert found == {by_argv.pid: "argv", by_cwd.pid: "cwd", by_env.pid: "env"}, found
        report = live_city_reaper.stop([root], grace_s=5.0)
        assert sorted(report.termed) == sorted(found) and report.killed == [] and report.alive == [], report
        for proc in (by_argv, by_cwd, by_env):
            assert proc.wait(timeout=5) == -signal.SIGTERM
        assert stranger.poll() is None and plain.poll() is None, "the reaper signalled a process of no root of its own"
    finally:
        for proc in started:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def test_the_reaper_matches_both_spellings_of_a_macos_temp_root() -> None:
    forms = live_city_reaper.root_forms("/private/var/folders/x/T/pytest-of-u/pytest-1/t0")
    assert "/private/var/folders/x/T/pytest-of-u/pytest-1/t0" in forms
    assert "/var/folders/x/T/pytest-of-u/pytest-1/t0" in forms  # how gc writes it
    procs = [
        live_city_reaper.Proc(10, 1, os.getuid(), "S", "gc __gc-managed-dolt-scope-watchdog /var/folders/x/T/pytest-of-u/pytest-1/t0/city/.gc/x.yaml"),
        live_city_reaper.Proc(11, 10, os.getuid(), "S", "dolt sql-server --config /var/folders/x/T/pytest-of-u/pytest-1/t0/city/c.yaml"),
        live_city_reaper.Proc(12, 1, os.getuid(), "S", "dolt sql-server --config /var/folders/x/T/pytest-of-u/pytest-1/t01/city/c.yaml"),
        live_city_reaper.Proc(13, 1, os.getuid() + 1, "S", "dolt sql-server --config /var/folders/x/T/pytest-of-u/pytest-1/t0/c.yaml"),
        live_city_reaper.Proc(14, 1, os.getuid(), "S", "bd db-proxy-child --root /elsewhere/.beads/dolt"),
    ]
    found = live_city_reaper.find(["/private/var/folders/x/T/pytest-of-u/pytest-1/t0"], procs=procs)
    assert [p.pid for p in found if p.why == "argv"] == [10, 11]  # the watchdog first; t01 and a stranger's uid never
