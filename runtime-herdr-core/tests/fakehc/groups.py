"""fake-hc's half of the test harness's process-group registry.

A process fake-hc starts in a session of its own (a core, a session
program) is appended to ``<state>/pgroups`` as ``<pgid> <epoch>``, so the
harness can end its whole group at teardown even when fake-hc itself was
killed first (``tests/live/livekit/pgroups.py`` reads the file). Stdlib
only, and never fails the caller.
"""

from __future__ import annotations

import os
import time
from typing import Optional

PGROUPS_FILE = "pgroups"


def record_group(path: Optional[str], pid: int) -> None:
    """Append ``pid``, which leads its own process group, to ``path``."""
    if not path:
        return
    line = f"{pid} {time.time():.3f}\n".encode()
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


def record_self(path: Optional[str]) -> None:
    """Append this process, when it leads its own group."""
    if os.getpgrp() == os.getpid():
        record_group(path, os.getpid())
