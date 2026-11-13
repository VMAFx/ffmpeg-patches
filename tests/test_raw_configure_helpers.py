# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise internal helpers of raw configure guard, inspection, and lexing."""

from __future__ import annotations

from scripts import raw_configure_inspect, raw_configure_unwrap, raw_configure_words
from tests.raw_configure_support import GUARD_MOD, RawConfigureGuardBase


class RawConfigureHelperTest(RawConfigureGuardBase):
    """Exercise internal helpers of raw configure guard, inspection, and lexing."""

    def test_tokens_helper(self) -> None:
        """Verify _tokens handles redirection merges, size caps, and syntax errors."""
        self.assertEqual(GUARD_MOD._tokens("echo 2>&1"), ["echo", "2>&1"])
        self.assertEqual(GUARD_MOD._tokens("cmd |& next"), ["cmd", "|", "next"])
        self.assertIsNone(GUARD_MOD._tokens("x" * (raw_configure_words.MAX_COMMAND + 1)))
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
            (["bash", "-s", "x"], (None, False)),
            (["bash", "-es", "--", "x"], (None, False)),
            (["bash", "-s", "-", "x"], (None, False)),
            (["sh", "-so", "pipefail", "x"], (None, False)),
            (["bash", "-", "script"], ("script", False)),
            (["bash", "-"], (None, False)),
            (["bash", "-", "-s"], ("-s", False)),
            (["bash", "-sc", "X", "x"], ("X", True)),
            (["bash", "-cs", "X"], ("X", True)),
            (["bash", "-s", "-c", "X"], ("X", True)),
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
                self.assertEqual(raw_configure_unwrap._command_start(words), expected)

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

    def test_tokenization_helper(self) -> None:
        """Verify _tokenize_command extracts heredocs, substitutions, and literal assignments."""
        cmd = "C=./configure; cat <<'EOF' > out.txt\ndata\nEOF\necho $(pwd) <(cat a.txt)"
        res = GUARD_MOD._tokenize_command(cmd)
        self.assertIsNotNone(res)
        pipelines, assignments, stdin_map, inners = res
        self.assertEqual(assignments.get("C"), "./configure")
        self.assertEqual(len(stdin_map), 1)
        self.assertIn("data\n", stdin_map.values())
        self.assertEqual(len(inners), 2)

    def test_command_classification_helper(self) -> None:
        """Verify _classify_command identifies inspected positions and distinguishes data."""
        blocked, _ = GUARD_MOD._classify_command(["./configure"], {})
        self.assertIsNotNone(blocked)
        blocked, _ = GUARD_MOD._classify_command(["grep", "-n", "pattern", "configure"], {})
        self.assertIsNone(blocked)
        blocked, _ = GUARD_MOD._classify_command(["$UNSET"], {})
        self.assertIsNotNone(blocked)
        blocked, _ = GUARD_MOD._classify_command(["$C"], {"C": "./configure"})
        self.assertIsNotNone(blocked)

    def test_nested_program_helper(self) -> None:
        """Verify _handle_nested_program queues inner programs and enforces recursion limits."""
        pending: list[tuple[str, int]] = []
        err = GUARD_MOD._handle_nested_program("echo 1", 1, pending)
        self.assertIsNone(err)
        self.assertEqual(pending, [("echo 1", 1)])
        err = GUARD_MOD._handle_nested_program("echo 1", GUARD_MOD.MAX_DEPTH + 1, pending)
        self.assertIsNotNone(err)

    def test_pipe_stdin_source_decision_helper(self) -> None:
        """Verify _decide_shell_stdin identifies stdin sources and enforces fail-closed rules."""
        blocked, queue = GUARD_MOD._decide_shell_stdin(None, "ls\n", {}, {})
        self.assertIsNone(blocked)
        self.assertEqual(queue, "ls\n")
        blocked, queue = GUARD_MOD._decide_shell_stdin(["echo", "ls"], None, {}, {})
        self.assertIsNone(blocked)
        self.assertEqual(queue, "ls")
        blocked, queue = GUARD_MOD._decide_shell_stdin(["printf", "ls\\n"], None, {}, {})
        self.assertIsNone(blocked)
        self.assertEqual(queue, "ls\n")
        blocked, queue = GUARD_MOD._decide_shell_stdin(["printf", "%s", "ls"], None, {}, {})
        self.assertIsNotNone(blocked)
        self.assertIsNone(queue)
        blocked, queue = GUARD_MOD._decide_shell_stdin(["printf", "ls\\x41"], None, {}, {})
        self.assertIsNotNone(blocked)
        self.assertIsNone(queue)
        hd_map = {"__RCG_HD_0__": "body\n"}
        blocked, queue = GUARD_MOD._decide_shell_stdin(["cat", "__RCG_HD_0__"], None, {}, hd_map)
        self.assertIsNone(blocked)
        self.assertEqual(queue, "body\n")
        blocked, queue = GUARD_MOD._decide_shell_stdin(["cat", "build.sh"], None, {}, {})
        self.assertIsNotNone(blocked)
        self.assertIsNone(queue)
        blocked, queue = GUARD_MOD._decide_shell_stdin(["curl", "https://x.invalid"], None, {}, {})
        self.assertIsNotNone(blocked)
        self.assertIsNone(queue)
        blocked, queue = GUARD_MOD._decide_shell_stdin(None, None, {}, {})
        self.assertIsNotNone(blocked)
        self.assertIsNone(queue)
        blocked, queue = GUARD_MOD._decide_shell_stdin(["echo", "$UNSET"], None, {}, {})
        self.assertIsNotNone(blocked)
        self.assertIsNone(queue)

    def test_configure_token_split_helper(self) -> None:
        """Verify _split_configure_tokens and _has_configure_token identify configure tokens."""
        tokens = raw_configure_words._split_configure_tokens("cd src && ./configure")
        self.assertEqual(tokens, ["cd", "src", "./configure"])
        tokens = raw_configure_words._split_configure_tokens("-c '!./configure'")
        self.assertEqual(tokens, ["-c", "./configure"])
        self.assertTrue(raw_configure_words._has_configure_token("./configure"))
        self.assertTrue(raw_configure_words._has_configure_token("!./configure"))
        self.assertTrue(raw_configure_words._has_configure_token("--to-command=./configure"))
        self.assertTrue(raw_configure_words._has_configure_token("$C", {"C": "./configure"}))
        self.assertFalse(raw_configure_words._has_configure_token("configure.orig"))
        self.assertFalse(raw_configure_words._has_configure_token("configure.bak"))
        self.assertFalse(raw_configure_words._has_configure_token("data.txt"))
        self.assertFalse(raw_configure_words._has_configure_token("--input"))

    def test_data_only_commands_membership_helper(self) -> None:
        """Verify _is_data_only_command resolves command membership by basename."""
        self.assertTrue(raw_configure_words._is_data_only_command("/usr/bin/cat"))
        self.assertTrue(raw_configure_words._is_data_only_command("cat"))
        self.assertTrue(raw_configure_words._is_data_only_command("/bin/grep"))
        self.assertTrue(raw_configure_words._is_data_only_command("grep"))
        self.assertFalse(raw_configure_words._is_data_only_command("make"))
        self.assertTrue(raw_configure_words._is_data_only_command("test"))
        self.assertTrue(raw_configure_words._is_data_only_command("["))
        self.assertTrue(raw_configure_words._is_data_only_command("/usr/bin/diff"))
        self.assertFalse(raw_configure_words._is_data_only_command("watch"))
        self.assertFalse(raw_configure_words._is_data_only_command("/usr/bin/watch"))
        self.assertFalse(raw_configure_words._is_data_only_command("vim"))
        self.assertFalse(raw_configure_words._is_data_only_command("less"))
        self.assertFalse(raw_configure_words._is_data_only_command("/usr/bin/tar"))
        self.assertFalse(raw_configure_words._is_data_only_command("ssh"))

    def test_long_option_matcher_helper(self) -> None:
        """Verify _long_option_values extracts values and respects prefix lengths."""
        self.assertEqual(
            raw_configure_words._long_option_values(
                ["sort", "--co=./configure"], 1, "--compress-program", 4
            ),
            ["./configure"],
        )
        self.assertEqual(
            raw_configure_words._long_option_values(
                ["sort", "--c=./configure"], 1, "--compress-program", 4
            ),
            [],
        )
        self.assertEqual(
            raw_configure_words._long_option_values(
                ["install", "--strip", "./configure"], 1, "--strip-program", 8
            ),
            [],
        )

    def test_interpreter_family_helper(self) -> None:
        """Verify _interpreter_family normalizes versions and maps aliases to family."""
        self.assertEqual(raw_configure_words._interpreter_family("python3.14"), "python")
        self.assertEqual(raw_configure_words._interpreter_family("mawk"), "awk")
        self.assertIsNone(raw_configure_words._interpreter_family("unknown"))

    def test_assignment_blocked_helper(self) -> None:
        """Verify _assignment_blocked identifies execution overrides and command positions."""
        self.assertTrue(raw_configure_inspect._assignment_blocked("GIT_PAGER=./configure", {}))
        self.assertFalse(raw_configure_inspect._assignment_blocked("CFLAGS=-O2", {}))
        self.assertFalse(raw_configure_inspect._assignment_blocked("not_an_assignment", {}))
