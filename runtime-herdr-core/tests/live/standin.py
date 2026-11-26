"""City infrastructure stand-ins for the live tier. The harness copies this
file to names gc and bd give their processes, and runs each copy as
``<python> <name> ...`` from that directory, so ``argv[1]`` is exactly the
verb gc's ``IsCityInfrastructureArgv`` recognises:

* ``__gc-managed-dolt-scope-watchdog <socket> <pid file>``: starts the store
  (``dolt sql-server <socket>``) as its child, writes the store's pid, and
  waits on it, like gc's managed-store watchdog;
* ``dolt sql-server <socket>``: answers ``pong`` to every connection on a
  Unix socket, standing in for the store the city depends on. With
  ``GC_HC_LIVE_STORE_DELAY`` (seconds) it waits that long before it binds,
  as a store slow to start does;
* ``db-proxy-child``: sleeps, standing in for bd's proxy.

Every role ends on ``SIGTERM`` (the default action), even when the parent
that started it ignored the signal: :func:`main` restores the default first.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from typing import List


def _store(path: str) -> int:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    delay = os.environ.get("GC_HC_LIVE_STORE_DELAY", "")
    if delay:
        time.sleep(float(delay))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(16)
    while True:
        conn, _ = server.accept()
        with conn:
            try:
                conn.sendall(b"pong\n")
            except OSError:
                pass  # the client gave up waiting; it must not take the store down


def _watchdog(sock: str, pid_file: str) -> int:
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    child = subprocess.Popen([sys.executable, "dolt", "sql-server", sock], cwd=here)
    tmp = f"{pid_file}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(str(child.pid))
    os.replace(tmp, pid_file)
    child.wait()
    while True:  # a watchdog outlives its store (the tests read both); SIGTERM still ends it
        time.sleep(3600)


def main(argv: List[str]) -> int:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)  # an inherited SIG_IGN must not make it immortal
    role = os.path.basename(argv[0])
    if role == "__gc-managed-dolt-scope-watchdog":
        return _watchdog(argv[1], argv[2])
    if role == "dolt":
        return _store(argv[2])
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
