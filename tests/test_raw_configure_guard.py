# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise raw FFmpeg configure refusal and client hook wiring."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed guard and shell-hook commands, never configure.
import tempfile
from pathlib import Path

from tests.raw_configure_support import CLIENTS, CONFIGS, ROOT, RawConfigureGuardBase


class RawConfigureGuardTest(RawConfigureGuardBase):
    """Check command classification through each documented client payload."""

    def test_refuses_configure_in_non_tree_directory(self) -> None:
        """Refuse direct configure invocations in directories without libavcodec."""
        cases = (
            ("./configure --prefix=/x", self.other),
            ("sh configure", self.other),
        )
        self._assert_refused(cases)

    def test_allows_documented_ffmpeg_configure_pass(self) -> None:
        """Allow scripts/ffmpeg_configure.py from both tree and non-tree directories."""
        cases = (
            (
                f"python3 scripts/ffmpeg_configure.py {self.tree} {self.build} -- --prefix=/x",
                self.tree,
            ),
            (
                f"python3 scripts/ffmpeg_configure.py {self.tree} {self.build} -- --prefix=/x",
                self.other,
            ),
            (f"python3 {ROOT}/scripts/ffmpeg_configure.py --keep CPPFLAGS a b -- x", self.tree),
            (f"python3 {ROOT}/scripts/ffmpeg_configure.py --keep CPPFLAGS a b -- x", self.other),
        )
        for client in CLIENTS:
            for command, cwd in cases:
                with self.subTest(client=client, command=command, cwd=cwd):
                    result = self.run_guard(client, self.payload(client, command, cwd))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_allows_unrelated_commands(self) -> None:
        """Keep wrapper use, inspection, other trees, and ordinary builds available."""
        cases = (
            (f"python3 scripts/ffmpeg_configure.py {self.tree} {self.build}", self.root),
            (
                'python3 "$REPO/scripts/ffmpeg_configure.py" "$SRC" "$B" -- --enable-shared',
                self.root,
            ),
            ('python3 -c "print(1)"', self.tree),
            ("perl -e 'print 1'", self.tree),
            ("cat configure", self.tree),
            ("grep -n foo configure", self.tree),
            ("git diff -- configure", self.tree),
            ("make -j4", self.tree),
            ("head -50 configure", self.tree),
            ("git log -- configure", self.tree),
            (f"ls {self.tree}/configure", self.root),
            ('cd "$X" && make -j4', self.tree),
            (f"python3 -I scripts/ffmpeg_configure.py {self.tree} {self.build}", self.root),
            ("echo hello", self.tree),
            ("echo configure", self.tree),
            ("echo configure", self.other),
            (f"bash -c 'cd {self.tree} && make'", self.root),
            ("git diff configure", self.tree),
            ("git add configure", self.tree),
            ("git show HEAD:configure", self.tree),
            ("tail -f config.log", self.tree),
            ("grep -n foo configure | head -5", self.tree),
            ("git status", self.tree),
            ("env bash -c 'echo hi'", self.root),
            ("bash -o pipefail -c 'ls'", self.root),
            ("bash script.sh", self.other),
        )
        for client in CLIENTS:
            for command, cwd in cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_input_and_depth_boundaries(self) -> None:
        """Fail closed on malformed input and ambiguous bounds while allowing safe limits."""
        for client in CLIENTS:
            with self.subTest(client=client):
                self.assertEqual(self.run_guard(client, "{").returncode, 2)
                self.assertEqual(self.run_guard(client, "[]").returncode, 2)
                self.assertEqual(
                    self.run_guard(client, self.payload(client, None, self.tree)).returncode, 0
                )
                payload = self.payload(client, "./configure", self.tree)
                if client == "opencode":
                    payload["tool"] = "read"
                else:
                    payload["tool_name"] = "Read"
                self.assertEqual(self.run_guard(client, payload).returncode, 0)
                self.assertEqual(
                    self.run_guard(client, self.payload(client, "x" * 65536, self.tree)).returncode,
                    0,
                )
                self.assertEqual(
                    self.run_guard(
                        client, self.payload(client, "configure" + "x" * 65536, self.tree)
                    ).returncode,
                    2,
                )
                self.assertEqual(
                    self.run_guard(client, self.payload(client, "x " * 4000, self.tree)).returncode,
                    0,
                )
                self.assertEqual(
                    self.run_guard(
                        client, self.payload(client, "x " * 4000 + "configure", self.tree)
                    ).returncode,
                    2,
                )
                nested = "true"
                for _ in range(4):
                    nested = "bash -c " + json.dumps(nested)
                self.assertEqual(
                    self.run_guard(client, self.payload(client, nested, self.tree)).returncode, 0
                )
                nested = "bash -c " + json.dumps(nested)
                self.assertEqual(
                    self.run_guard(client, self.payload(client, nested, self.tree)).returncode, 2
                )
        self.assertFalse(self.marker.exists())

    def test_hook_wiring(self) -> None:
        """Keep the existing hook first and execute each new hook command."""
        for client, (path, event, tool, timeout) in CONFIGS.items():
            with self.subTest(client=client):
                groups = json.loads(path.read_text(encoding="utf-8"))["hooks"][event]
                group = next(item for item in groups if item["matcher"] == f"^{tool}$")
                hooks = group["hooks"]
                self.assertEqual(
                    hooks[0],
                    {
                        "type": "command",
                        "command": f"praetorctl hook {client} pre-tool",
                        "timeout": timeout,
                    },
                )
                self.assertGreaterEqual(len(hooks), 2)
                if client == "claude":
                    self.assertEqual(hooks[1].get("onFailure"), "block")
                self.assertIn("scripts/raw_configure_guard.py", hooks[1]["command"])
                self.assertIn(f"--client {client}", hooks[1]["command"])
                for command, expected in (
                    ("./configure", 2),
                    ("sudo ./configure", 2),
                    ("python3 scripts/ffmpeg_configure.py A B", 0),
                ):
                    result = subprocess.run(  # nosec B603 B607 - repo-owned hook command.
                        ["bash", "-c", hooks[1]["command"]],
                        input=json.dumps(self.payload(client, command, self.tree)),
                        text=True,
                        capture_output=True,
                        cwd=ROOT / "tests",
                        timeout=5,
                        check=False,
                        env={
                            **os.environ,
                            "CLAUDE_PROJECT_DIR": str(ROOT),
                            "GEMINI_PROJECT_DIR": str(ROOT),
                        },
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_missing_hook_script_blocks(self) -> None:
        """Refuse shell tool calls when a hook resolves to a missing guard script."""
        missing = self.root / "missing"
        (missing / "tests").mkdir(parents=True)
        subprocess.run(  # nosec B603 B607 - fixed git init in a temp test tree.
            ["git", "init", "-q", str(missing)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        for client in ("claude", "codex", "gemini"):
            with self.subTest(client=client):
                path = CONFIGS[client][0]
                hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
                event = CONFIGS[client][1]
                command = hooks[event][0]["hooks"][1]["command"]
                result = subprocess.run(  # nosec B603 B607 - repo-owned hook command.
                    ["bash", "-c", command],
                    input=json.dumps(self.payload(client, "./configure", self.tree)),
                    text=True,
                    capture_output=True,
                    cwd=missing / "tests",
                    timeout=5,
                    check=False,
                    env={
                        **os.environ,
                        "CLAUDE_PROJECT_DIR": str(missing),
                        "GEMINI_PROJECT_DIR": str(missing),
                    },
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(str(missing / "scripts/raw_configure_guard.py"), result.stderr)

    def test_opencode_plugin_wiring(self) -> None:
        """Verify the project plugin refuses blocks and has valid JavaScript syntax."""
        path = ROOT / ".opencode/plugins/raw-configure-guard.js"
        source = path.read_text(encoding="utf-8")
        self.assertIn('"tool.execute.before"', source)
        self.assertIn("--client", source)
        self.assertIn("opencode", source)
        self.assertIn("throw new Error", source)
        node = shutil.which("node")
        if node is None:
            self.skipTest("node unavailable; JavaScript syntax check skipped")
        result = subprocess.run(  # nosec B603 B607 - tracked plugin file.
            [node, "--check", str(path)], capture_output=True, text=True, timeout=5, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_start_failure_exits_two(self) -> None:
        """Fail closed with returncode 2 when python3 is missing or fails on startup."""
        git_bin = shutil.which("git")
        bash_bin = shutil.which("bash") or "/bin/bash"
        self.assertIsNotNone(git_bin)
        temp_missing = tempfile.TemporaryDirectory()
        self.addCleanup(temp_missing.cleanup)
        missing_bin_dir = Path(temp_missing.name)
        (missing_bin_dir / "git").symlink_to(git_bin)  # type: ignore[arg-type]

        temp_failing = tempfile.TemporaryDirectory()
        self.addCleanup(temp_failing.cleanup)
        failing_bin_dir = Path(temp_failing.name)
        (failing_bin_dir / "git").symlink_to(git_bin)  # type: ignore[arg-type]
        failing_py = failing_bin_dir / "python3"
        failing_py.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        failing_py.chmod(0o755)

        for client in ("claude", "codex", "gemini"):
            path, event, _, _ = CONFIGS[client]
            hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"][event][0]["hooks"]
            command = hooks[1]["command"]
            for label, bin_dir in (
                ("missing_python", missing_bin_dir),
                ("failing_python", failing_bin_dir),
            ):
                with self.subTest(client=client, setup=label):
                    env = {
                        **os.environ,
                        "PATH": str(bin_dir),
                        "CLAUDE_PROJECT_DIR": str(ROOT),
                        "GEMINI_PROJECT_DIR": str(ROOT),
                    }
                    result = subprocess.run(  # nosec B603 B607 - repo-owned hook command.
                        [bash_bin, "-c", command],
                        input=json.dumps(self.payload(client, "./configure", self.tree)),
                        text=True,
                        capture_output=True,
                        cwd=ROOT / "tests",
                        timeout=5,
                        check=False,
                        env=env,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)

    def test_unparsed_command_refusals(self) -> None:
        """Refuse unparsed configure invocations across shell commands."""
        pad = " # " + "x" * 65536
        big = ": " + "a" * 70000 + "; "
        cases = (
            ("./co{n,}figure" + pad, self.tree),
            ("./co`printf n`figure" + pad, self.tree),
            ("./co${x:-n}figure" + pad, self.tree),
            (f"cd {self.tree} && ./co{{n,}}figure" + pad, self.other),
            ("cd tree && ./co{n,}figure" + pad, self.root),
            (f"{self.tree}/co{{n,}}figure" + pad, self.other),
            (f"bash {self.tree}/co{{n,}}figure" + pad, self.other),
            ("./co{n,}figure # don't", self.tree),
            (f"cd {self.tree} && ./co{{n,}}figure # don't", self.other),
            (big + "sh co{n,}figure", self.tree),
            (big + "x=n; sh co${x}figure", self.tree),
            (big + "sh co`printf n`figure", self.tree),
            (big + "sh co$(printf n)figure", self.tree),
            (big + "sh co${x:-n}figure", self.tree),
        )
        self._assert_refused(cases, remedy="--body-file")

    def test_unparsed_command_refused_everywhere(self) -> None:
        """Refuse unparsed shell commands across directories and clients."""
        pad = " # " + "x" * 65536
        big = ": " + "a" * 70000 + "; "
        heredoc = "cat <<'EOF' > /dev/null\nfix(hooks): it's in scripts/x.py\nEOF"
        cases = (
            ("ls" + pad, self.other),
            ("ls # don't", self.other),
            (heredoc + pad, self.other),
            (big + "ls", self.other),
            ("ls # don't", self.tree),
            ("echo 'unbalanced", self.other),
            ("echo 'unbalanced", self.tree),
            (f"ls{pad}", self.root),
            ("x" * (65536 + 1), self.tree),
            ("x " * 4001, self.tree),
        )
        self._assert_refused(cases, remedy="--body-file")
