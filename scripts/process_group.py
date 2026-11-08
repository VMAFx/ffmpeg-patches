# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Safe process-group and process-tree termination guards."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess  # nosec B404 - safe process tree termination on Windows.
import sys

TASKKILL_TIMEOUT_S: float = 10.0


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


def kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate a process and its descendant tree across supported platforms."""
    if os.name == "posix":
        kill_process_group(process)
        return
    if not isinstance(process.pid, int) or isinstance(process.pid, bool) or process.pid <= 1:
        return
    if os.name == "nt":
        cmd = ["taskkill", "/T", "/F", "/PID", str(process.pid)]
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=TASKKILL_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            print(
                f"kill_process_tree: taskkill unavailable or timed out ({exc}); "
                "falling back to process.kill()",
                file=sys.stderr,
            )
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
