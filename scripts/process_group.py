# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Safe process-group termination and signal guards."""

from __future__ import annotations

import contextlib
import os
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess


def kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a process group safely, guarding against invalid PIDs."""
    # HISS-08: never signal pid 0/1 or our own process group.
    if (
        not isinstance(process.pid, int)
        or isinstance(process.pid, bool)
        or process.pid <= 1
        or process.pid == os.getpgrp()
    ):
        with contextlib.suppress(ProcessLookupError):
            # pid 0/1/bool: Popen.kill() would reach os.kill(0|1); signal nothing.
            if (
                isinstance(process.pid, int)
                and not isinstance(process.pid, bool)
                and process.pid > 1
            ):
                process.kill()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
