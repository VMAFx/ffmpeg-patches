# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Replay sources relative to an executed script's own directory without running fixtures."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - only the fixed guard runs.
from pathlib import Path
from typing import NamedTuple

from tests.raw_configure_support import RawConfigureGuardBase

UNKNOWN_SOURCE = "relative script path after a source the guard cannot read; use an absolute path"
AMBIGUOUS = "assigned in a sourced file and elsewhere"
UNRESOLVED = "own-directory name assigned in a function, branch or loop"
BENIGN_NEXT = {"tools/next.sh": "echo benign\n"}
DEFAULT = 'HERE=${LANE_TEST_DIR:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")}\n'
DOLLAR0 = 'HERE="$(dirname "$(readlink -f "$0")")"\n'
REALPATH = 'HERE=$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")\n'
SRC = '. "$HERE/lib.sh"\n'
UNREAD = '. "$UNSET_DIR/x.sh"\n'
RUN = "bash inner.sh\n"
CFG = "<cfg>\n"
LINK = "-> ../tools/run.sh"


class Case(NamedTuple):
    """One tools/run.sh body, its extra files and the expected guard verdict."""

    body: str
    code: int
    reason: str | None = None
    files: dict[str, str] | None = None
    env: dict[str, str] | None = None
    command: str = "bash tools/run.sh"


SCRIPT_DIR = {
    "default-form": Case(DEFAULT + SRC + RUN, 0),
    "dollar0-quoted": Case(DOLLAR0 + SRC + RUN, 0),
    "realpath-dashdash": Case(REALPATH + SRC + RUN, 0),
    "program-form": Case(DEFAULT + SRC + RUN, 0, command="./tools/run.sh"),
    "lib-runs-configure": Case(DEFAULT + SRC, 2, "tree/configure", {"tools/lib.sh": CFG}),
    "nested-program-configure": Case(
        DEFAULT + '"$HERE/next.sh" check\n', 2, "tools/next.sh", {"tools/next.sh": CFG}
    ),
    "lib-cd-then-relative": Case(DEFAULT + SRC + RUN, 2, "inner.sh", {"tools/lib.sh": "cd sub\n"}),
    "symlink-reads-target-dir": Case(
        DOLLAR0 + SRC,
        2,
        "tree/configure",
        {"link/run.sh": LINK, "tools/lib.sh": CFG},
        command="bash link/run.sh",
    ),
    "symlink-skips-link-dir": Case(
        DOLLAR0 + SRC,
        0,
        None,
        {"link/run.sh": LINK, "link/lib.sh": CFG},
        command="bash link/run.sh",
    ),
    "override-in-env": Case(DEFAULT + SRC + RUN, 2, UNKNOWN_SOURCE, env={"LANE_TEST_DIR": "/x"}),
    "override-assigned": Case("LANE_TEST_DIR=/x\n" + DEFAULT + SRC + RUN, 2, UNKNOWN_SOURCE),
    "override-assigned-lib-unresolved": Case(
        "LANE_TEST_DIR=/x\n" + DEFAULT + SRC, 2, UNKNOWN_SOURCE, {"tools/lib.sh": CFG}
    ),
    "override-sourced-before": Case(
        ". <case>/tools/pre.sh\n" + DEFAULT + SRC + RUN,
        2,
        UNKNOWN_SOURCE,
        {"tools/pre.sh": "LANE_TEST_DIR=/x\n"},
    ),
    "cd-before-assignment": Case("cd <case>\n" + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE),
    "cd-before-assignment-lib-unresolved": Case(
        "cd <case>\n" + DOLLAR0 + SRC, 2, UNKNOWN_SOURCE, {"tools/lib.sh": CFG}
    ),
    "unread-source-before-assignment": Case(
        '. "$UNSET_DIR/x.sh"\n' + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE
    ),
    "unread-source-before-lib-unresolved": Case(
        '. "$UNSET_DIR/x.sh"\n' + DOLLAR0 + SRC, 2, UNKNOWN_SOURCE, {"tools/lib.sh": CFG}
    ),
    "assigned-twice": Case(DOLLAR0 + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE),
    "append-assignment": Case(DOLLAR0 + "HERE+=/x\n" + SRC + RUN, 2, UNKNOWN_SOURCE),
    "lib-reassigns-name": Case(
        DOLLAR0 + SRC + 'bash "$HERE/inner.sh"\n', 2, AMBIGUOUS, {"tools/lib.sh": "HERE=/x\n"}
    ),
    "lib-reassigns-name-source": Case(
        DOLLAR0 + SRC + '. "$HERE/inner.sh"\n', 2, AMBIGUOUS, {"tools/lib.sh": "HERE=/x\n"}
    ),
    "sourced-not-executed": Case(DEFAULT + SRC + RUN, 2, UNKNOWN_SOURCE, command=". tools/run.sh"),
    "source-before-assignment": Case(SRC + DOLLAR0 + RUN, 2, UNKNOWN_SOURCE),
    "assignment-in-function": Case("f() {\n" + DOLLAR0 + "}\n" + SRC + RUN, 2, UNRESOLVED),
    "assignment-in-function-next-program": Case(
        "f() {\n" + DOLLAR0 + "}\nf\n" + '"$HERE/next.sh" check\n', 2, UNRESOLVED, BENIGN_NEXT
    ),
    "assignment-in-branch-next-program": Case(
        "if true; then\n" + DOLLAR0 + "fi\n" + '"$HERE/next.sh" check\n', 2, UNRESOLVED, BENIGN_NEXT
    ),
    "assignment-in-function-braced-default": Case(
        "f() {\n" + DOLLAR0 + "}\nf\n" + 'bash "${HERE:-.}/next.sh"\n', 2, UNRESOLVED, BENIGN_NEXT
    ),
    "assignment-in-function-longer-name": Case(
        "f() {\n" + DOLLAR0 + "}\nf\n" + 'bash "$HERE_X/next.sh"\n', 0, None, BENIGN_NEXT
    ),
    "local-in-function-reference": Case(
        "f() {\n  local " + DOLLAR0.lower() + '  bash "$here/next.sh"\n}\n',
        2,
        UNRESOLVED,
        BENIGN_NEXT,
    ),
    "local-in-function-unrelated-path": Case(
        "f() {\n  local " + DOLLAR0.lower() + '  echo "$here"\n}\n' + RUN, 0
    ),
    "override-assigned-next-program": Case(
        "LANE_TEST_DIR=/x\n" + DEFAULT + '"$HERE/next.sh" check\n',
        2,
        UNKNOWN_SOURCE,
        {"tools/next.sh": "echo benign\n"},
    ),
    "override-assigned-after": Case(DEFAULT + "LANE_TEST_DIR=/x\n" + SRC + RUN, 2, UNKNOWN_SOURCE),
    "unread-source-after-assignment-program": Case(
        DOLLAR0 + UNREAD + '"$HERE/next.sh" check\n',
        2,
        UNKNOWN_SOURCE,
        {"tools/next.sh": "echo benign\n"},
    ),
    "unread-source-after-assignment-lib": Case(DOLLAR0 + UNREAD + SRC, 2, UNKNOWN_SOURCE),
    "unread-source-keeps-configure-literal": Case(
        "CFG_PATH=<tree>/configure\n" + DOLLAR0 + UNREAD + '"$CFG_PATH" --prefix=/x\n',
        2,
        "tree/configure",
    ),
    "unread-source-keeps-plain-literal": Case(
        "NEXT=<case>/tools/next.sh\n" + DOLLAR0 + UNREAD + 'bash "$NEXT"\n',
        0,
        None,
        {"tools/next.sh": "echo benign\n"},
    ),
    "declare-lowercase": Case("declare -l " + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE),
    "export-assignment": Case("export " + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE),
    "local-in-function-keeps-top-level": Case(
        DEFAULT + "f() {\n  local " + DOLLAR0.lower() + "}\n" + SRC + RUN, 0
    ),
}


class TestRawConfigureScriptDir(RawConfigureGuardBase):
    """Resolve the own-directory idiom only where the guard knows the script directory."""

    def _case_dir(self, name: str, case: Case) -> Path:
        """Lay out benign root and tools scripts plus the case files under one directory."""
        base = self.root / name
        files = {
            "inner.sh": "echo benign\n",
            "sub/inner.sh": CFG,
            "tools/inner.sh": "echo benign\n",
            "tools/lib.sh": "X=1\n",
            "tools/run.sh": case.body,
            **(case.files or {}),
        }
        for rel, text in files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if text.startswith("-> "):
                path.symlink_to(text[3:])
                continue
            body = text.replace("<cfg>", f"{self.tree}/configure --prefix=/x")
            body = body.replace("<tree>", str(self.tree))
            script = "#!/usr/bin/env bash\n" + body.replace("<case>", str(base))
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        return base

    def _run(
        self, base: Path, command: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Classify a Claude command from the case directory with a minimal environment."""
        run_env = {"PATH": os.defpath, "LC_ALL": "C.UTF-8", **(env or {})}
        return self.run_guard("claude", self.payload("claude", command, base), env=run_env)

    def test_script_dir_shapes(self) -> None:
        """Replay all 39 own-directory shapes: resolved, marked uncertain or refused."""
        self.assertEqual(len(SCRIPT_DIR), 39)
        for name, case in SCRIPT_DIR.items():
            with self.subTest(shape=name):
                base = self._case_dir(name, case)
                result = self._run(base, case.command, case.env)
                self.assertEqual(result.returncode, case.code, result.stderr)
                if case.reason is not None:
                    self.assertIn(case.reason, result.stderr)
                self.assertFalse(self.marker.exists())

    def test_unreadable_script_dir_source_refuses(self) -> None:
        """Keep refusing a resolved source the guard cannot read or that is no regular file."""
        cases = [("not-regular", "(not a regular file)")]
        if os.geteuid() != 0:
            cases.append(("unreadable", "(unreadable script)"))
        for name, reason in cases:
            with self.subTest(shape=name):
                base = self._case_dir(name, Case(DEFAULT + SRC + RUN, 2))
                lib = base / "tools/lib.sh"
                if name == "unreadable":
                    lib.chmod(0)
                else:
                    lib.unlink()
                    lib.mkdir()
                result = self._run(base, "bash tools/run.sh")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertFalse(self.marker.exists())
