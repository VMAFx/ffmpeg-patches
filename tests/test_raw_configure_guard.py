# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise raw FFmpeg configure refusal and client hook wiring."""

from __future__ import annotations

import importlib.util
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
PROBE_GAP_SHAPES = (
    "command -p cd $R/tree; ./configure",
    "command -- cd $R/tree; ./configure",
    "builtin -- cd $R/tree; ./configure",
    "function f { cd $R/tree; }; f; ./configure",
    "time -p -- cd $R/tree; ./configure",
    "CDPATH=$R cd tree; ./configure",
    "shopt -s expand_aliases; alias c=cd\nc $R/tree; ./configure",
    "export CDPATH=$R; cd tree; ./configure",
    "CDPATH=$R; cd tree; ./configure",
    "pushd $R/tree; pushd /tmp; pushd +1; ./configure",
    "source $R/s.sh; ./configure",
    ". $R/s.sh; ./configure",
    "command -p . $R/s.sh; ./configure",
    "BASH_ENV=$R/s.sh bash -c ./configure",
    "env -C $R/tree ./configure",
    "env --chdir=$R/tree ./configure",
    "cd $R/tree; ~+/configure",
    "false && cd /tmp; ./configure",
    "if false; then cd /tmp; fi; ./configure",
    "true || cd /tmp; ./configure",
    "while false; do cd /tmp; done; ./configure",
    "pushd /tmp; pushd +1; ./configure",
    "[ -d /nope ] && cd /tmp; ./configure",
    "f() { cd /tmp; }; ./configure",
    "cd() { :; }; cd /tmp; ./configure",
    "enable -n cd; cd /tmp; ./configure",
    "shopt -s expand_aliases\nalias cd=:\ncd /tmp; ./configure",
    "for i in; do cd /tmp; done; ./configure",
)
SPEC = importlib.util.spec_from_file_location("raw_configure_guard", GUARD)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load module from {GUARD}")
GUARD_MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD_MOD)


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
        (self.root / "BUILD").mkdir()
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

    def run_guard(
        self, client: str, payload: object, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run only the guard with JSON on stdin and a short deadline."""
        data = payload if isinstance(payload, str) else json.dumps(payload)
        run_env = {"PATH": os.defpath, "LC_ALL": "C.UTF-8"} if env is None else env
        return subprocess.run(  # nosec B603 - fixed guard argv with explicit interpreter.
            [sys.executable, "-I", str(GUARD), "--client", client],
            input=data,
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=5,
            check=False,
            env=run_env,
        )

    def _assert_refused(
        self,
        cases: tuple[tuple[str, Path], ...],
        env: dict[str, str] | None = None,
        remedy: str = "python3 scripts/ffmpeg_configure.py",
    ) -> None:
        """Verify the guard refuses raw configure invocations across clients."""
        for client in CLIENTS:
            for command, cwd in cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd), env=env)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("configure", result.stderr)
                    self.assertIn(remedy, result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_refuses_all_probe_gap_shapes(self) -> None:
        """Refuse all 28 probe gap shapes from non-tree directories."""
        (self.root / "s.sh").write_text(f"cd {self.tree}\n", encoding="utf-8")
        cases = tuple((cmd.replace("$R", str(self.root)), self.other) for cmd in PROBE_GAP_SHAPES)
        self._assert_refused(cases)

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

    def test_refuses_wrapper_and_relocation_forms(self) -> None:
        """Block direct, wrapped, and relocated FFmpeg configure calls."""
        cases = (
            ("./configure --prefix=/x", self.tree),
            ("bash configure", self.tree),
            ("sh ./configure", self.tree),
            (f"cd {self.tree} && ./configure", self.root),
            ("env FOO=1 nice -n 10 ./configure", self.tree),
            (str(self.tree / "configure"), self.root),
            ("cd BUILD && ../tree/configure", self.root),
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
            ("f=./configure; $f", self.tree),
            ("source ./configure", self.tree),
            (". ./configure", self.tree),
            ("./conf*", self.tree),
        )
        self._assert_refused(cases)

    def test_refuses_interpreter_and_reader_abuse_forms(self) -> None:
        """Block interpreter one-liners and reader-abuse configure invocations."""
        cases = (
            ("bash -c './configure'", self.tree),
            ("bash -c 'sudo ./configure'", self.tree),
            ("xargs ./configure", self.tree),
            ("echo ./configure | bash", self.tree),
            ("find . -name configure -exec sh {} \\;", self.tree),
            ("echo $(./configure)", self.tree),
            ("echo `./configure`", self.tree),
            ("python3 -c \"import os; os.system('./configure')\"", self.tree),
            ("perl -e 'system q(./configure)'", self.tree),
            ('awk "BEGIN{system(\\"./configure\\")}"', self.tree),
            ("sed -n '1e ./configure' README", self.tree),
            ("less +'!./configure' x", self.tree),
            ("rg --pre ./configure x .", self.tree),
            ("git -c alias.x='!./configure' x", self.tree),
            ("git -c core.pager=./configure log", self.tree),
            ("git rebase -x ./configure HEAD~1", self.tree),
            ("git bisect run ./configure", self.tree),
            ("git submodule foreach ./configure", self.tree),
            ("LESSOPEN='|./configure %s' less README", self.tree),
            ("GIT_PAGER=./configure git log", self.tree),
            ("GIT_EXTERNAL_DIFF=./configure git diff", self.tree),
            ("cat configure | sh", self.tree),
            ("sed -n 1p configure", self.tree),
            ("less configure", self.tree),
        )
        self._assert_refused(cases)

    def test_refuses_subshell_and_pipeline_directory_leaks(self) -> None:
        """Refuse subshell and pipeline forms leaking directory state to configure."""
        cases = (
            ("(cd /tmp); ./configure", self.tree),
            ("(cd /tmp) && ./configure", self.tree),
            ("cd /tmp | true; ./configure", self.tree),
            ("(cd /tmp; (cd /var)); ./configure", self.tree),
        )
        self._assert_refused(cases)

    def test_refuses_subshell_and_cd_resolution_fail_opens(self) -> None:
        """Refuse background cd, pipe-and, failed cd, lexical dots, and CDPATH lookups."""
        plain_file = self.root / "plain_file"
        plain_file.write_text("x", encoding="utf-8")
        symlink = self.tree / "l"
        symlink.symlink_to(self.other)
        cases = (
            ("cd /tmp & ./configure", self.tree),
            ("cd /tmp &\n./configure", self.tree),
            ("cd /tmp |& true; ./configure", self.tree),
            ("cd /tmp && true & ./configure", self.tree),
            ("true && cd /tmp & ./configure", self.tree),
            ("cd /nonexistent-rcg9; ./configure", self.tree),
            ("cd /nonexistent-rcg9 || true; ./configure", self.tree),
            (f"cd {plain_file}; ./configure", self.tree),
            ("cd l/..; ./configure", self.tree),
        )
        self._assert_refused(cases)
        other_tree = self.other / "tree"
        other_tree.mkdir()
        cdpath_cases = (("cd tree; ./configure", self.other),)
        self._assert_refused(cdpath_cases, env={**os.environ, "CDPATH": str(self.root)})

    def test_refuses_redirect_ampersand_forms(self) -> None:
        """Refuse configure runs where cd or configure contains redirect ampersands."""
        cases = (
            (f"cd {self.tree} 2>&1; ./configure", self.other),
            (f"cd {self.tree} &>/dev/null; ./configure", self.other),
            (f"cd {self.tree} >&2; ./configure", self.other),
            (f"cd {self.tree} &>>log; ./configure", self.other),
            (f"cd {self.tree} 2>&-; ./configure", self.other),
            ("./configure 2>&1", self.tree),
            ("./configure >log 2>&1 &", self.tree),
        )
        self._assert_refused(cases)

    def test_refuses_unsearchable_directory_cd(self) -> None:
        """Refuse raw configure after cd into a directory without search permission."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory search permissions")
        noexec = self.root / "noexec"
        noexec.mkdir(mode=0o700, exist_ok=True)
        noexec.chmod(0o600)
        self.addCleanup(lambda: noexec.chmod(0o700) if noexec.exists() else None)
        self._assert_refused(((f"cd {noexec}; ./configure", self.tree),))

    def test_refuses_digit_directory_redirect_ambiguities(self) -> None:
        """Refuse digit directory cd redirect ambiguities while keeping valid targets allowed."""
        digit_tree = self.root / "2"
        digit_tree.mkdir()
        (digit_tree / "libavcodec").mkdir()
        script = digit_tree / "configure"
        script.write_text(f"#!/bin/sh\ntouch '{self.marker}'\n", encoding="utf-8")
        script.chmod(0o755)
        refuse_cases = (
            ("cd 2 >&1; ./configure", self.root),
            ("cd 2 2>&1; ./configure", self.root),
            ("cd 2>&1 2; ./configure", self.root),
            ("cd 2>&1; ./configure", self.root),
            ("pushd 2 >&1; ./configure", self.root),
        )
        self._assert_refused(refuse_cases)

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

    def test_tokens_helper(self) -> None:
        """Verify _tokens handles redirection merges, size caps, and syntax errors."""
        self.assertEqual(GUARD_MOD._tokens("echo 2>&1"), ["echo", "2>&1"])
        self.assertEqual(GUARD_MOD._tokens("cmd |& next"), ["cmd", "|", "next"])
        self.assertIsNone(GUARD_MOD._tokens("x" * (GUARD_MOD.MAX_COMMAND + 1)))
        self.assertIsNone(GUARD_MOD._tokens('echo "unbalanced'))

    def test_shell_argument_helper(self) -> None:
        """Verify _shell_argument parses bash options and identifies inline commands."""
        cases = (
            (["bash", "-o", "pipefail", "-c", "X"], ("X", True)),
            (["bash", "--norc", "-c", "X"], ("X", True)),
            (["bash", "-ec", "X"], ("X", True)),
            (["bash", "-eo", "pipefail", "-c", "X"], ("X", True)),
            (["bash", "+o", "posix", "-c", "X"], ("X", True)),
            (["bash", "--", "script"], ("script", False)),
            (["bash", "script.sh"], ("script.sh", False)),
            (["bash"], (None, False)),
        )
        for words, expected in cases:
            with self.subTest(words=words):
                self.assertEqual(GUARD_MOD._shell_argument(words), expected)

    def test_command_start_helper(self) -> None:
        """Verify _command_start skips prefix reserved words, timers, and assignments."""
        cases = (
            (["cd", "x"], 0),
            (["if", "cd", "x"], 1),
            (["time", "-p", "cd", "x"], 2),
            (["time", "cd", "x"], 1),
            (["builtin", "command", "while", "do", "cd", "x"], 4),
            (["X=1", "Y=2", "cd", "x"], 2),
            (
                [
                    "if",
                    "then",
                    "elif",
                    "else",
                    "do",
                    "while",
                    "until",
                    "!",
                    "{",
                    "}",
                    "command",
                    "builtin",
                ],
                12,
            ),
            ([], 0),
            (["unknown", "cd", "x"], 0),
            (["-p", "cd", "x"], 0),
            (["time", "-p"], 2),
            (["time"], 1),
        )
        for words, expected in cases:
            with self.subTest(words=words):
                self.assertEqual(GUARD_MOD._command_start(words), expected)

    def test_is_configure_word_helper(self) -> None:
        """Verify _is_configure_word distinguishes configure and benign names."""
        self.assertTrue(GUARD_MOD._is_configure_word("./configure"))
        self.assertTrue(GUARD_MOD._is_configure_word("configure"))
        self.assertTrue(GUARD_MOD._is_configure_word("~+/configure"))
        self.assertTrue(GUARD_MOD._is_configure_word("$PWD/configure"))
        self.assertTrue(GUARD_MOD._is_configure_word("./conf*"))
        self.assertTrue(GUARD_MOD._is_configure_word("./co{n,}figure"))
        self.assertTrue(GUARD_MOD._is_configure_word("$f"))
        self.assertFalse(GUARD_MOD._is_configure_word("make"))
        self.assertFalse(GUARD_MOD._is_configure_word("ls"))
        self.assertFalse(GUARD_MOD._is_configure_word("$HOME/bin/tool"))
        self.assertFalse(GUARD_MOD._is_configure_word("scripts/ffmpeg_configure.py"))

    def test_refuses_wrapped_shell_and_option_forms(self) -> None:
        """Refuse shell invocations behind wrappers and with complex options."""
        cases = (
            (f"env bash -c 'cd {self.tree} && ./configure'", self.root),
            (f"timeout 5 sh -c 'cd {self.tree} && ./configure'", self.root),
            (f"nohup bash -c 'cd {self.tree} && ./configure'", self.root),
            (f"bash -o pipefail -c 'cd {self.tree} && ./configure'", self.root),
            (f"bash --norc -c 'cd {self.tree} && ./configure'", self.root),
            (f"bash -eo pipefail -c 'cd {self.tree} && ./configure'", self.root),
        )
        self._assert_refused(cases)

    def test_oversized_glob_refusal_and_benign_allow(self) -> None:
        """Refuse oversized glob configure invocations while allowing benign commands."""
        padding = " # " + "x" * 65536
        oversized_refusal = f"cd {self.tree} && ./c?nfigure{padding}"
        self._assert_refused(((oversized_refusal, self.root),), remedy="--body-file")
        oversized_allow = "ls"
        for client in CLIENTS:
            with self.subTest(client=client):
                result = self.run_guard(client, self.payload(client, oversized_allow, self.root))
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_oversized_quote_and_backslash_configure_refusal(self) -> None:
        """Refuse oversized configure invocations split by quotes and backslashes."""
        padding = " # " + "x" * 65536
        cases = (
            (f"cd {self.tree} && ./con''figure{padding}", self.root),
            (f'cd {self.tree} && ./con""figure{padding}', self.root),
            (f"cd {self.tree} && ./conf\\igure{padding}", self.root),
            (f"./con''figure{padding}", self.tree),
        )
        self._assert_refused(cases, remedy="--body-file")

    def test_midword_expansion_configure_refusal_and_benign_allow(self) -> None:
        """Refuse mid-word configure expansions while allowing benign variants."""
        refuse_cases = (
            (f"cd {self.tree} && ./conf${{e}}igure", self.root),
            (f"cd {self.tree} && ./${{e}}con${{e}}figure", self.root),
            ("./co$'n'figure", self.tree),
            ("./conf$eigure", self.tree),
        )
        self._assert_refused(refuse_cases)
        allow_cases = (("echo conf${e}igure", self.tree),)
        for client in CLIENTS:
            for command, cwd in allow_cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_undecidable_configure_refusal_and_benign_allow(self) -> None:
        """Refuse undecidable dynamic commands in trees while allowing benign commands."""
        (self.tree / "tools").mkdir(exist_ok=True)
        refuse_cases = (
            ("./co$'n'figure", self.tree),
            (r"./co$'\x6e'figure", self.tree),
            ("./$X", self.tree),
            (f"cd {self.tree} && ./co$'n'figure", self.root),
        )
        self._assert_refused(refuse_cases)
        allow_cases = (
            ("echo co$'n'figure", self.tree),
            ("echo $PATH", self.tree),
            ("$HOME/bin/tool", self.tree),
            ("ls $PWD", self.tree),
        )
        for client in CLIENTS:
            for command, cwd in allow_cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_decidable_allowlist_configure_refusal_and_benign_allow(self) -> None:
        """Refuse undecidable names and directory expansions while allowing safe patterns."""
        (self.tree / "tools").mkdir(exist_ok=True)
        refuse_cases = (
            ("./co`printf n`figure", self.tree),
            ("./co{n,}figure", self.tree),
            ("{.,x}/configure", self.tree),
            ("./co{n..n}figure", self.tree),
            ("./co[n]figure", self.tree),
        )
        self._assert_refused(refuse_cases)
        allow_cases = (
            ("ls ./*.c", self.tree),
            ("cp ./a.txt ./b.txt", self.tree),
            ("echo {a,b}", self.tree),
            ("ls libavcodec/{a,b}.c", self.tree),
            ("$HOME/bin/tool", self.tree),
            ("ls $PWD", self.tree),
        )
        for client in CLIENTS:
            for command, cwd in allow_cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.marker.exists())

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

    def test_command_substitution_path_refusals(self) -> None:
        """Refuse command substitutions glued to configure paths targeting a tree."""
        cases = (
            ("$(pwd)/configure", self.tree),
            ("$(git rev-parse --show-toplevel 2>/dev/null || pwd)/configure", self.tree),
            ('"$(pwd)"/configure', self.tree),
            ("$(pwd)/co{n,}figure", self.tree),
        )
        self._assert_refused(cases)

    def test_shell_script_operand_without_slash_refusals(self) -> None:
        """Refuse shell script arguments without a slash that target configure."""
        cases = (
            ("sh co{n,}figure", self.tree),
            ("bash co`echo n`figure", self.tree),
            ("sh co$'n'figure", self.tree),
        )
        self._assert_refused(cases)

    def test_unparsed_command_refused_everywhere(self) -> None:
        """Refuse unparsed shell commands across directories and clients."""
        pad = " # " + "x" * 65536
        big = ": " + "a" * 70000 + "; "
        heredoc = "cat <<'EOF' > /dev/null\nfix(hooks): it's in scripts/x.py\nEOF"
        cases = (
            ("ls" + pad, self.other),
            ("ls # don't", self.other),
            (heredoc, self.other),
            (heredoc + pad, self.other),
            ("git commit -F - <<'EOF'\nfix: don't refuse scripts/a.py\nEOF", self.other),
            (big + "ls", self.other),
            ("ls # don't", self.tree),
            ("echo 'unbalanced", self.other),
            ("echo 'unbalanced", self.tree),
            (f"ls{pad}", self.root),
            ("x" * (65536 + 1), self.tree),
            ("x " * 4001, self.tree),
        )
        self._assert_refused(cases, remedy="--body-file")

    def test_parsed_dynamic_path_allows(self) -> None:
        """Allow parsed commands with substitutions and dynamic paths outside trees."""
        cases = (
            ("echo $(pwd)/x", self.tree),
            ("sh ./tools/x.sh", self.tree),
            ("cat $(pwd)/README", self.tree),
            ("git commit -F - <<'EOF'\nfix: refuse scripts/a.py\nEOF", self.other),
            ("git commit -F /tmp/msg.txt", self.other),
            ("gh pr create --body-file /tmp/body.md --title x", self.other),
            ("ls", self.other),
        )
        for client in CLIENTS:
            for command, cwd in cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.marker.exists())

    def test_eval_and_prefix_command_refusals(self) -> None:
        """Refuse configure execution reached through eval or prefix-wrapped directory changes."""
        cases = (
            (f"eval 'cd {self.tree}' && ./configure", self.root),
            (f"eval cd {self.tree}; ./configure", self.root),
            (f'eval "cd {self.tree}"; sh configure', self.root),
            (f"if cd {self.tree}; then ./configure; fi", self.root),
            (f"{{ cd {self.tree}; ./configure; }}", self.root),
            (f"while cd {self.tree}; do ./configure; break; done", self.root),
            (f"! cd {self.tree} || ./configure", self.root),
            (f"time cd {self.tree}; ./configure", self.root),
            (f"time -p cd {self.tree}; ./configure", self.root),
            (f"command cd {self.tree}; ./configure", self.root),
            (f"builtin cd {self.tree}; ./configure", self.root),
            (f"X=1 cd {self.tree}; ./configure", self.root),
        )
        self._assert_refused(cases)

    def test_eval_and_prefix_command_controls_and_boundaries(self) -> None:
        """Allow benign eval and prefix commands and enforce boundary limits."""
        allow_cases = (
            (f"if cd {self.tree}; then ls; fi", self.root),
            ("eval 'echo hi'; ls", self.root),
            ("command -v ls", self.root),
            ("time ls", self.root),
            ("ls", self.tree),
            ("{ ls; }", self.tree),
            ("if then elif else do while until ! { time command builtin", self.root),
        )
        for client in CLIENTS:
            for command, cwd in allow_cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.marker.exists())
            long_if = "if " * 4000 + f"cd {self.tree}; ./configure"
            with self.subTest(client=client, boundary="long_if"):
                result = self.run_guard(client, self.payload(client, long_if, self.root))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.marker.exists())
