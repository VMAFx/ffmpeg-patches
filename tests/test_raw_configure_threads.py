# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Replay review thread shapes without executing their shell commands."""

from __future__ import annotations

import os
from pathlib import Path

from tests.raw_configure_support import RawConfigureGuardBase

CASES = [
    (0, "relative-before-cd", "refuse", "body", "bash inner.sh\ncd /tmp\n"),
    (1, "cwd-missing-relative", "refuse", "cmd-badcwd", "bash inner.sh"),
    (2, "bare-configure-body", "refuse", "body", "configure --x\n"),
    (2, "bash-c-bare-configure", "refuse", "body", "bash -c 'configure --x'\n"),
    (2, "sh-c-dq-bare-configure", "refuse", "body", 'sh -c "configure --x"\n'),
    (2, "bash-ec-bare-configure", "refuse", "body", "bash -ec 'true; configure --x'\n"),
    (2, "cd-then-bash-c-bare", "refuse", "body", "cd tree\nbash -c 'configure'\n"),
    (2, "echo-bare-configure", "allow", "body", "echo configure --x\n"),
    (2, "top-bash-c-bare-configure", "refuse", "cmd", "bash -c 'configure --x'"),
    (2, "top-cd-bash-c-bare", "refuse", "cmd", "cd tree && bash -c 'configure'"),
    (2, "top-bash-c-echo-configure", "allow", "cmd", "bash -c 'echo configure'"),
    (3, "exec-no-shebang", "refuse", "body", "./noshebang\n"),
    (4, "path-resolved-driver", "refuse", "cmd-path", "driver"),
    (4, "timeout-bash-operand", "refuse", "body", "timeout 5 bash inner.sh\n"),
    (4, "env-bash-operand", "refuse", "body", "env bash inner.sh\n"),
    (5, "variable-nested-path", "refuse", "body", 'S=inner.sh\nbash "$S"\n'),
    (5, "relative-after-cd", "refuse", "body", "cd sub\nbash inner.sh\n"),
    (
        6,
        "many-inline-bodies",
        "refuse",
        "body",
        "".join(f"sh -c 'echo {i}'\n" for i in range(300)) + "sh -c '<cfg>'\n",
    ),
    (7, "shell-xc", "refuse", "body", "bash -xc '<cfg>'\n"),
    (7, "shell-o-pipefail-c", "refuse", "body", "bash -o pipefail -c '<cfg>'\n"),
    (8, "sourced-cd-then-relative", "refuse", "body", ". ./cdsub.sh\nbash inner.sh\n"),
    (11, "echo-configure-path", "allow", "body", "echo <tree>/configure\n"),
    (11, "printf-configure-path", "allow", "body", "printf '%s\\n' <tree>/configure\n"),
    (12, "cat-heredoc-configure", "allow", "body", "cat <<EOF\n<tree>/configure --x\nEOF\n"),
    (12, "bash-heredoc-configure", "refuse", "body", "bash <<EOF\n<cfg>\nEOF\n"),
    (13, "bash-stdin-redirect", "refuse", "body", "bash < inner.sh\n"),
    (13, "cat-pipe-bash", "refuse", "body", "cat inner.sh | bash\n"),
    (14, "quoted-exec-with-space", "refuse", "body", '"<root>/my driver.sh"\n'),
    (16, "glob-configure-command", "refuse", "body", "<tree>/configur?\n"),
    (
        17,
        "variable-built-command",
        "refuse",
        "body",
        'DIR=<tree>; BIN=configure; "$DIR/$BIN" --x\n',
    ),
    (18, "dollar-hash-then-configure", "refuse", "body", "n=$#; <cfg>\n"),
    (18, "apostrophe-comment", "refuse", "body", "sh <tree>/configure # don't edit\n"),
    (19, "shell-ec", "refuse", "body", "bash -ec '<cfg>'\n"),
    (19, "shell-lc", "refuse", "body", "bash -lc '<cfg>'\n"),
    (19, "shell-euo-pipefail-c", "refuse", "body", "bash -euo pipefail -c 'cd /tmp && <cfg>'\n"),
    (20, "inner-then-cd", "refuse", "body", "bash inner.sh\ncd /tmp\n"),
    (
        21,
        "twenty-abs-then-nested",
        "refuse",
        "body",
        "".join(f"/bin/true {i}\n" for i in range(20)) + "bash ./inner.sh\n",
    ),
    (22, "cd-then-c-relative", "refuse", "cmd", "cd build && bash -c 'bash driver.sh'"),
    (
        99,
        "agy-like-allow",
        "allow",
        "body",
        'set -euo pipefail\nW=$(dirname "$0")\nprintf "%s\\n" "$W"\n',
    ),
]


def setup(root: Path) -> dict[str, str]:
    """Write the fixture tree: configure, inner scripts, drivers."""
    tree = root / "tree"
    (tree / "libavcodec").mkdir(parents=True, exist_ok=True)
    (tree / "configure").write_text("#!/bin/sh\n", encoding="utf-8")
    cfg = f'"{tree}/configure" --x'
    (root / "inner.sh").write_text(cfg + "\n", encoding="utf-8")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "inner.sh").write_text(cfg + "\n", encoding="utf-8")
    (root / "cdsub.sh").write_text("cd sub\n", encoding="utf-8")
    nos = root / "noshebang"
    nos.write_text(cfg + "\n", encoding="utf-8")
    nos.chmod(0o755)
    spaced = root / "my driver.sh"
    spaced.write_text("#!/bin/sh\n" + cfg + "\n", encoding="utf-8")
    spaced.chmod(0o755)
    (root / "bin").mkdir(exist_ok=True)
    drv = root / "bin" / "driver"
    drv.write_text("#!/bin/sh\n" + cfg + "\n", encoding="utf-8")
    drv.chmod(0o755)
    (root / "build").mkdir(exist_ok=True)
    (root / "build" / "driver.sh").write_text(cfg + "\n", encoding="utf-8")
    return {"<cfg>": cfg, "<tree>": str(tree), "<root>": str(root)}


class TestReviewThreadShapes(RawConfigureGuardBase):
    """Exercise every review thread shape through the Claude guard payload."""

    def test_review_thread_shapes(self) -> None:
        """Replay review cases against a shared tree and assert their requested verdicts."""
        substitutions = setup(self.root)
        for thread, shape, want, mode, source in CASES:
            with self.subTest(thread=thread, shape=shape):
                text = source
                for key, value in substitutions.items():
                    text = text.replace(key, value)
                cwd = self.root
                env = None
                if mode == "body":
                    (self.root / "outer.sh").write_text(text, encoding="utf-8")
                    command = "bash outer.sh"
                else:
                    command = text
                if mode == "cmd-badcwd":
                    cwd = self.root / "missing-dir"
                if mode == "cmd-path":
                    env = {"PATH": f"{self.root}/bin:{os.defpath}", "LC_ALL": "C.UTF-8"}
                result = self.run_guard("claude", self.payload("claude", command, cwd), env=env)
                self.assertEqual(result.returncode, 2 if want == "refuse" else 0, result.stderr)
                self.assertFalse(self.marker.exists())
