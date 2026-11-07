# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise raw FFmpeg configure refusal and client hook wiring."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed guard and shell-hook commands, never configure.
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "raw_configure_guard.py"
CLIENTS = ("claude", "codex", "gemini", "opencode")
CONFIGS = {
    "claude": (ROOT / ".claude/settings.json", "PreToolUse", "Bash", 15),
    "codex": (ROOT / ".codex/hooks.json", "PreToolUse", "Bash", 15),
    "gemini": (ROOT / ".gemini/settings.json", "BeforeTool", "run_shell_command", 15000),
}


class RawConfigureGuardTest(unittest.TestCase):
    """Check command classification through each documented client payload."""

    def setUp(self) -> None:
        """Create FFmpeg-shaped and unrelated trees without running configure."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree = self.root / "tree"
        self.tree.mkdir()
        (self.tree / "libavcodec").mkdir()
        self.build = self.root / "build"
        self.build.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        self.marker = self.root / "ran"
        for directory in (self.tree, self.other):
            script = directory / "configure"
            script.write_text(f"#!/bin/sh\ntouch '{self.marker}'\n", encoding="utf-8")
            script.chmod(0o755)

    def payload(self, client: str, command: str | None, cwd: Path) -> dict[str, object]:
        """Build the documented pre-tool shell input for one client."""
        if client == "opencode":
            return {"tool": "bash", "args": {"command": command}, "cwd": str(cwd)}
        tool = "run_shell_command" if client == "gemini" else "Bash"
        arguments = {} if command is None else {"command": command}
        return {"tool_name": tool, "tool_input": arguments, "cwd": str(cwd)}

    def run_guard(self, client: str, payload: object) -> subprocess.CompletedProcess[str]:
        """Run only the guard with JSON on stdin and a short deadline."""
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, "-I", str(GUARD), "--client", client],
            input=data,
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=5,
            check=False,
        )

    def test_refuses_raw_configure_forms(self) -> None:
        """Block direct, wrapped, nested, and relocated FFmpeg configure calls."""
        cases = (
            ("./configure --prefix=/x", self.tree),
            ("bash configure", self.tree),
            ("sh ./configure", self.tree),
            (f"cd {self.tree} && ./configure", self.root),
            ("env FOO=1 nice -n 10 ./configure", self.tree),
            (str(self.tree / "configure"), self.root),
            ("cd BUILD && ../tree/configure", self.root),
            ("bash -c './configure'", self.tree),
            ("systemd-run --user --scope -p MemoryHigh=8G -- ./configure", self.tree),
            ("praetorctl admit -label x -- ./configure", self.tree),
            ('"$SRC"/configure', self.tree),
            ("./configure`unknown`", self.tree),
            ("true\n./configure", self.tree),
            ("env -u FOO ./configure", self.tree),
            ("timeout 5 ./configure", self.tree),
            ("taskset 0x1 ./configure", self.tree),
            ("chrt -f 1 ./configure", self.tree),
            ("ionice -c 2 -n 0 ./configure", self.tree),
            ("stdbuf -oL ./configure", self.tree),
            ("setsid ./configure", self.tree),
            ("nohup ./configure", self.tree),
            ("exec ./configure", self.tree),
            ("command ./configure", self.tree),
            ("builtin ./configure", self.tree),
            ("time ./configure", self.tree),
            ("systemd-run --user -p MemoryHigh=8G ./configure", self.tree),
            ('cd "$X" && ./configure', self.tree),
            ("cd - && ./configure", self.tree),
            ("popd; ./configure", self.tree),
            ("sudo ./configure", self.tree),
            ("flock /tmp/l ./configure", self.tree),
            ("xargs ./configure", self.tree),
            ("echo ./configure | bash", self.tree),
            ("f=./configure; $f", self.tree),
            ("source ./configure", self.tree),
            (". ./configure", self.tree),
            ("find . -name configure -exec sh {} \\;", self.tree),
            ("echo $(./configure)", self.tree),
            ("echo `./configure`", self.tree),
            ("bash -c 'sudo ./configure'", self.tree),
        )
        (self.root / "BUILD").mkdir()
        for client in CLIENTS:
            for command, cwd in cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("configure", result.stderr)
                    self.assertIn("python3 scripts/ffmpeg_configure.py", result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_allows_unrelated_commands(self) -> None:
        """Keep wrapper use, inspection, other trees, and ordinary builds available."""
        cases = (
            (f"python3 scripts/ffmpeg_configure.py {self.tree} {self.build}", self.root),
            ("cat configure", self.tree),
            ("grep -n foo configure", self.tree),
            ("git diff -- configure", self.tree),
            ("./configure", self.other),
            ("make -j4", self.tree),
            ("sed -n 1,20p configure", self.tree),
            ("head -50 configure", self.tree),
            ("git log -- configure", self.tree),
            (f"ls {self.tree}/configure", self.root),
            ("rg -n foo configure", self.tree),
            ('cd "$X" && make -j4', self.tree),
            (f"python3 -I scripts/ffmpeg_configure.py {self.tree} {self.build}", self.root),
            ("sudo ./configure", self.other),
            ("echo hello", self.tree),
            (f"bash -c 'cd {self.tree} && make'", self.root),
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
                    self.run_guard(
                        client, self.payload(client, "x" * (65536 + 1), self.tree)
                    ).returncode,
                    0,
                )
                self.assertEqual(
                    self.run_guard(
                        client, self.payload(client, "configure" + "x" * 65536, self.tree)
                    ).returncode,
                    2,
                )
                self.assertEqual(
                    self.run_guard(client, self.payload(client, "x " * 4001, self.tree)).returncode,
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
                self.assertIn("scripts/raw_configure_guard.py", hooks[1]["command"])
                self.assertIn(f"--client {client}", hooks[1]["command"])
                for command, expected in (
                    ("./configure", 2),
                    ("sudo ./configure", 2),
                    ("python3 scripts/ffmpeg_configure.py A B", 0),
                ):
                    result = subprocess.run(
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
        subprocess.run(
            ["git", "init", "-q", str(missing)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        for client in ("codex", "gemini"):
            with self.subTest(client=client):
                path = CONFIGS[client][0]
                hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
                event = CONFIGS[client][1]
                command = hooks[event][0]["hooks"][1]["command"]
                result = subprocess.run(
                    ["bash", "-c", command],
                    input=json.dumps(self.payload(client, "./configure", self.tree)),
                    text=True,
                    capture_output=True,
                    cwd=missing / "tests",
                    timeout=5,
                    check=False,
                    env={**os.environ, "GEMINI_PROJECT_DIR": str(missing)},
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
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, timeout=5, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
