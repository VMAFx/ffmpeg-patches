# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Bounded pipe reads and safe process-group and process-tree termination guards."""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess  # nosec B404 - safe process tree termination on Windows.
import sys
import time
from collections.abc import Sequence
from typing import BinaryIO

TASKKILL_TIMEOUT_S: float = 10.0


def read_bounded(
    process: subprocess.Popen[bytes],
    streams: Sequence[BinaryIO],
    deadline: float,
    limit: int,
    timeout: float,
) -> list[bytes]:
    """Read pipes within a shared byte limit and deadline, then reap the process."""
    output = [bytearray() for _ in streams]
    total = 0
    with selectors.DefaultSelector() as selector:
        for index, stream in enumerate(streams):
            selector.register(stream, selectors.EVENT_READ, index)
        for _ in range(limit + 2 * len(streams) + 2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # semgrep-reason (expires 2026-11-21): category C; exception only, runs nothing
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit  # noqa: E501
                raise subprocess.TimeoutExpired(process.args, timeout)
            if not selector.get_map():
                process.wait(timeout=remaining)
                return [bytes(data) for data in output]
            ready = selector.select(remaining)
            if not ready:
                # semgrep-reason (expires 2026-11-21): category C; exception only, runs nothing
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit  # noqa: E501
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in ready:
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > limit:
                    raise ValueError(f"output exceeds {limit} bytes")
                output[key.data].extend(chunk)
    raise ValueError(f"output exceeds {limit} bytes")


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
            subprocess.run(  # nosec B603 B607 - fixed taskkill argv with explicit timeout.
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
