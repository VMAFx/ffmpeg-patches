# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise review-round program, reader and env header regressions for raw configure guard."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import patch

from scripts.raw_configure_body import BodyBudget, InspectionContext, _reader_operands, scan_body
from scripts.raw_configure_files import (
    MAX_SCRIPT_BYTES,
    MAX_SCRIPT_FILES,
    _env_interpreter,
    _path_program,
    _peek_program,
    _program_kind,
    _trusted_program,
    inspect_script_file,
)
from scripts.raw_configure_inspect import classify_script_words
from tests.raw_configure_support import RawConfigureGuardBase


class ReviewRoundFilesTest(RawConfigureGuardBase):
    """Exercise r9 program classification and nested path resolution regressions."""

    def test_program_kinds(self) -> None:
        """Classify shell text, binary headers and env interpreter options."""
        cases = (
            (b"echo clean", "shell"),
            (b"\x7fELF\x00", "skip"),
            (b"#!/bin/sh -e", "shell"),
            (b"#!/usr/bin/env -u X bash", "shell"),
            (b"#!/usr/bin/env -S bash -e", "shell"),
            (b"#!/usr/bin/env -C /x --unset X Y=z bash", "shell"),
            (b"#!/usr/bin/env python3", "skip"),
            (b"#!", "shell"),
        )
        for head, expected in cases:
            with self.subTest(head=head):
                self.assertEqual(_program_kind(head), expected)

    def test_non_shell_programs(self) -> None:
        """Allow Python and binary programs while refusing env shell programs."""
        program = self.root / "prog.py"
        program.write_text(
            "#!/usr/bin/env python3\nimport subprocess\n"
            'subprocess.run(["make"])\n# configure notes\n',
            encoding="utf-8",
        )
        program.chmod(0o755)
        self._assert_allowed((("./prog.py --x", self.root),))
        self._check_parser_body("./prog.py\n", refused=False)
        program.write_bytes(b"\x7fELF\x00")
        self._assert_allowed((("./prog.py --x", self.root),))
        self._check_parser_body("./prog.py\n", refused=False)
        program.write_text(
            f'#!/usr/bin/env -S bash -e\n"{self.tree}/configure"\n', encoding="utf-8"
        )
        self._assert_refused((("./prog.py", self.root),))

    def test_trusted_system_program(self) -> None:
        """Trust an unwritable root shell and reject a temporary executable as trusted."""
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("sh is unavailable")
        path = Path(shell)
        if os.geteuid() == 0 or path.stat().st_uid != 0:
            self.skipTest("trust requires a non-root user and a root-owned sh")
        self.assertTrue(_trusted_program(path))

    def test_temporary_program_is_untrusted(self) -> None:
        """Keep caller-writable programs outside the root-owned trust exemption."""
        program = self.root / "program"
        program.write_text("#!/bin/sh\necho clean\n", encoding="utf-8")
        program.chmod(0o755)
        self.assertFalse(_trusted_program(program))

    def test_body_piped_stdin(self) -> None:
        """Refuse unknown shell input and inspect literal echo and printf producers."""
        for body in (
            "cat inner.sh | bash\n",
            "curl -s https://example.invalid/x | sh\n",
            "bash <(cat inner.sh)\n",
            "printf '%s\\n' '<cfg>' | bash\n",
        ):
            with self.subTest(body=body):
                self._check_parser_body(body, refused=True)
        self._check_parser_body("printf '%s\\n' 'echo hi' | bash\n", refused=False)

    def test_body_literal_map(self) -> None:
        """Follow unique statement literals and leave reassigned operands unresolved."""
        self._check_parser_body('S=inner.sh\nbash "$S"\n', refused=True)
        self._check_parser_body("T=timeout\n$T 5 bash inner.sh\n", refused=True)
        self._check_parser_body('S=a.sh\nS=b.sh\nbash "$S"\n', refused=False)

    def test_body_base_search(self) -> None:
        """Resolve changed directories and globs with explicit missing-path refusals."""
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "inner.sh").write_text(f'"{self.tree}/configure"\n', encoding="utf-8")
        (sub / "clean.sh").write_text("echo clean\n", encoding="utf-8")
        self._check_parser_body("cd sub\nbash inner.sh\n", refused=True)
        self._check_parser_body("cd sub\nbash clean.sh\n", refused=False)
        self._check_parser_body(
            "cd sub\nbash nowhere.sh\n",
            refused=True,
            reason="not found under the session, cd target or script directories",
        )
        self._check_parser_body("bash inner*.sh\n", refused=True)
        self._check_parser_body("bash missing*.sh\n", refused=False)
        self._check_parser_body("bash inne?.sh\n", refused=True)

    def test_program_peek_budget(self) -> None:
        """Count skipped program peeks and refuse the first lookup beyond the bound."""
        missing = self.root / "missing-program"
        budget = BodyBudget()
        with patch("scripts.raw_configure_files.MAX_PROGRAM_PEEKS", 2):
            self.assertIs(_peek_program(missing, budget), False)
            self.assertIs(_peek_program(missing, budget), False)
            self.assertIn(
                "more program lookups than inspect limit", str(_peek_program(missing, budget))
            )
        self.assertEqual(budget.peeks, 3)

    def test_program_peeks_preserve_file_slots(self) -> None:
        """Skip binary headers before candidate slots and keep later shell inspection."""
        lines = []
        for i in range(20):
            program = self.root / f"binary_{i}"
            program.write_bytes(b"\x7fELF\x00")
            program.chmod(0o755)
            lines.append(f"./binary_{i}\n")
        self._check_parser_body("".join(lines), refused=False)
        self._check_parser_body("".join(lines) + "bash inner.sh\n", refused=True)

    def test_shell_operand_ignores_program_trust(self) -> None:
        """Inspect shell operands even when a program classifier would trust their path."""
        script = self.root / "operand.sh"
        script.write_text(f'"{self.tree}/configure"\n', encoding="utf-8")
        context = InspectionContext(self.root, False, [0], classify_script_words)
        with patch("scripts.raw_configure_files._trusted_program", return_value=True):
            self.assertIsNone(inspect_script_file(str(script), context, is_program=True))
            self.assertIsInstance(inspect_script_file(str(script), context, is_program=False), Path)

    def test_glob_zero_matches_and_overflow(self) -> None:
        """Allow absent glob matches and refuse expansion above the inspection cap."""
        outer = self.root / "glob.sh"
        outer.write_text("bash inner*.sh\n", encoding="utf-8")
        self._assert_allowed(((f"bash {outer}", self.root),))
        for i in range(MAX_SCRIPT_FILES + 2):
            (self.root / f"inner{i}.sh").write_text("echo clean\n", encoding="utf-8")
        self._assert_refused(((f"bash {outer}", self.root),))

    def test_body_searches_script_directory_ancestors(self) -> None:
        """Find changed relative operands under the script's parent directories."""
        directory = self.root / "scripts" / "nested"
        directory.mkdir(parents=True)
        (self.root / "scripts" / "clean.sh").write_text("echo clean\n", encoding="utf-8")
        outer = directory / "outer.sh"
        outer.write_text("cd missing\nbash clean.sh\n", encoding="utf-8")
        self._assert_allowed(((f"bash {outer}", self.other),))

    def test_path_lookup_excludes_relative_entries(self) -> None:
        """Keep relative PATH entries and shell builtins outside program lookup."""
        with (
            patch.dict(os.environ, {"PATH": "relative:."}),
            patch("scripts.raw_configure_files._PATH_PROGRAMS", {}),
            patch("scripts.raw_configure_files.shutil.which", return_value=None) as which,
        ):
            for name in (
                "lane-driver",
                "echo",
                "bash",
                "if",
                "[",
                "./driver",
                "$CMD",
                "`cmd`",
                "a b",
            ):
                self.assertIsNone(_path_program(name))
            which.assert_not_called()

    def test_payload_without_session_directory(self) -> None:
        """Refuse relative shell operands when a payload omits its session directory."""
        payload = self.payload("claude", "bash inner.sh", self.root)
        payload.pop("cwd")
        result = self.run_guard("claude", payload)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("missing session directory; use an absolute path", result.stderr)


PROGRAM_HEADS = (
    (b"#!\n", "shell"),
    (b"#!   \n", "shell"),
    (b"echo clean\n\x00\x00\n", "shell"),
    (b"#!/usr/bin/env -Sbash\n", "shell"),
    (b"#!/usr/bin/env --split-string=bash\n", "shell"),
    (b"#!/bin/busybox sh\n", "shell"),
    (b"#!/bin/mksh\n", "shell"),
    (b"x\x00\n", "skip"),
    (b"\x7fELF\x02\x01\x01\x00\n", "skip"),
    (b"#!/usr/bin/env python3\n", "skip"),
)


class RoundTenFilesTest(RawConfigureGuardBase):
    """Cover executable headers, reader inputs and changed glob operands from r10."""

    def test_program_header_classification(self) -> None:
        """Classify first-line NUL markers and all requested shell interpreter heads."""
        for head, expected in PROGRAM_HEADS:
            with self.subTest(head=head):
                self.assertEqual(_program_kind(head), expected)

    def test_program_headers_top_level_and_body(self) -> None:
        """Refuse configure-bearing executable shell files directly and from a body."""
        program = self.root / "name"
        body = f'"{self.tree}/configure"\n'.encode()
        for head, kind in PROGRAM_HEADS:
            with self.subTest(head=head):
                program.write_bytes(head + body)
                program.chmod(0o755)
                assertion = self._assert_refused if kind == "shell" else self._assert_allowed
                assertion((("./name", self.root),))
                self._check_parser_body("./name\n", refused=kind == "shell")

    def test_env_split_words_and_bound(self) -> None:
        """Walk attached split words before later env parts within a 64-word cap;
        unknown options fail closed.
        """
        cases = (
            ([b"-S-u X bash", b"python3"], "bash"),
            ([b"--split-string=-C /x Y=z mksh", b"python3"], "mksh"),
            ([b"-u", b"-Sbash", b"python3"], "python3"),
            ([b"-i"] * 63 + [b"bash"], "bash"),
            ([b"-i"] * 64 + [b"bash"], ""),
            ([b"-x", b"bash"], ""),
            ([b"-S" + b"-x " * 64 + b"bash"], ""),
        )
        for parts, expected in cases:
            with self.subTest(parts=parts):
                self.assertEqual(_env_interpreter(parts), expected)

    def test_unresolved_line_filter_process_input(self) -> None:
        """Allow bounded sed line filters on unresolved launcher operands."""
        programs = (
            "/^LOCAL_DIR=/p;/^hidden_for()/,/^}/p",
            "/^LOCAL_DIR=/p;/^hidden_for()/,/^}/p;/^another()/,/^}/p",
        )
        for program in programs:
            with self.subTest(program=program):
                self._check_parser_body(
                    f"script=${{1:?launcher script}}\nsource <(sed -n '{program}' \"$script\")\n",
                    refused=False,
                )

    def test_clean_reader_shell_inputs(self) -> None:
        """Allow inspectable cat, head, tail and sed inputs containing clean shell text."""
        (self.root / "clean.sh").write_text("echo clean\n", encoding="utf-8")
        for body in (
            "source <(cat clean.sh)\n",
            "cat clean.sh | bash\n",
            "head -n 5 clean.sh | bash\n",
            "bash <(sed -n '1,3p' clean.sh)\n",
            "bash < <(tail --lines 5 clean.sh)\n",
            "cat clean.sh | sed -n p | bash\n",
            "cat -- clean.sh | bash\n",
            "bash <(sed -n -e p -e q -- clean.sh)\n",
        ):
            with self.subTest(body=body):
                self._check_parser_body(body, refused=False)

    def test_configure_reader_shell_inputs(self) -> None:
        """Refuse configure files supplied by supported reader pipelines and substitutions."""
        for body in (
            "cat inner.sh | bash\n",
            "bash <(cat inner.sh)\n",
            "source <(sed -n '/^x/p' inner.sh)\n",
            "bash < <(cat inner.sh)\n",
            "cat inner.sh | head -n 5 | bash\n",
            "tail -c11 inner.sh | bash\n",
        ):
            with self.subTest(body=body):
                self._check_parser_body(body, refused=True)

    def test_opaque_reader_shell_inputs(self) -> None:
        """Refuse substitutions and pipes outside the explicit reader grammar."""
        (self.root / "clean.sh").write_text("echo clean\n", encoding="utf-8")
        for body in (
            "sed 's/a/b/' clean.sh | bash\n",
            "source <(sed -n p clean.sh | grep x)\n",
            "source <(curl -s https://example.invalid/x)\n",
            "source <(cat clean.sh; cat clean.sh)\n",
            "source <(cat $(echo clean.sh))\n",
            "cat | bash\n",
            "head -n 5 - | bash\n",
            "source <(cat -)\n",
        ):
            with self.subTest(body=body):
                self._check_parser_body(body, refused=True)

    def test_filtered_substring_refusal(self) -> None:
        """Refuse raw configure substrings that filters can shorten into a command."""
        (self.root / "notes.sh").write_text("./configured\n", encoding="utf-8")
        self._check_parser_body(
            "head -c 11 notes.sh | bash\n",
            refused=True,
            reason="filtered text of this file feeds a shell and names configure",
        )
        self._check_parser_body("bash <(head -c11 notes.sh)\n", refused=True)
        self._check_parser_body("cat notes.sh | head -c11 | bash\n", refused=True)

    def test_reader_words_name_configure(self) -> None:
        """Check unquoted reader stage words for explicit configure references."""
        self._check_parser_body("source <(sed -n '/configure/p' missing.sh)\n", refused=True)
        self._check_parser_body("sed -n '/configure/p' missing.sh | bash\n", refused=True)

    def test_changed_unmatched_glob_refusal(self) -> None:
        """Refuse changed relative patterns without a matched or literal regular file."""
        (self.root / "sub").mkdir()
        outer = self.root / "outer.sh"
        outer.write_text("cd sub\nbash inne[r].sh\n", encoding="utf-8")
        result = self.run_guard("claude", self.payload("claude", "bash outer.sh", self.root))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(
            "inne[r].sh (relative script path after cd; "
            "not found under the session, cd target or script directories)",
            result.stderr,
        )
        self.assertFalse(self.marker.exists())

    def test_unchanged_unmatched_glob_allowed(self) -> None:
        """Allow an unmatched shell operand pattern before any directory change."""
        outer = self.root / "outer.sh"
        outer.write_text("bash inner*.sh\n", encoding="utf-8")
        self._assert_allowed((("bash outer.sh", self.root),))

    def test_changed_matching_glob_and_literal_fallback(self) -> None:
        """Inspect matched patterns and literal pattern filenames after a directory change."""
        sub = self.root / "sub"
        sub.mkdir()
        cfg = f'"{self.tree}/configure"\n'
        (sub / "inner.sh").write_text(cfg, encoding="utf-8")
        self._check_parser_body("cd sub\nbash inne?.sh\n", refused=True)
        (self.root / "inner.sh").unlink()
        (sub / "inner.sh").unlink()
        (sub / "inne[r].sh").write_text(cfg, encoding="utf-8")
        outer = self.root / "outer.sh"
        outer.write_text("cd sub\nbash inne[r].sh\n", encoding="utf-8")
        self._assert_refused((("bash outer.sh", self.root),))

    def test_additional_shell_operands(self) -> None:
        """Inspect all added shell families and busybox shell applets."""
        for family in ("ash", "mksh", "lksh", "posh", "yash", "hush", "busybox ash"):
            with self.subTest(family=family):
                self._check_parser_body(f"{family} inner.sh\n", refused=True)

    def test_reader_option_operands(self) -> None:
        """Extract reader files across option values, attached counts and double dashes."""
        cases = (
            (["cat", "-n", "--", "-file", "-"], ["-file", "-"]),
            (["head", "-n", "5", "-c11", "--bytes=4", "f"], ["f"]),
            (["tail", "--lines", "5", "--bytes", "11", "f"], ["f"]),
            (["head", "--", "-n", "f"], ["-n", "f"]),
            (["sed", "--quiet", "-E", "-e", "1p", "-e", "$q", "f"], ["f"]),
            (["sed", "--silent", "-r", "--regexp-extended", "p", "--", "-f"], ["-f"]),
            (["sed", "-n", r"\c^hidden()c,/^}/p", "f"], ["f"]),
            (["sed", "-n", r"/a\/b/p", "f"], ["f"]),
            (["sed", "p", "-e", "q", "f"], ["p", "f"]),
            (["cat"], []),
            (["head", "-n"], None),
            (["sed", "-e"], None),
            (["sed", "-n", "/unclosed", "f"], None),
            (["sed", "-n", "s/a/b/", "f"], None),
            (["grep", "x", "f"], None),
            ([], None),
        )
        for words, expected in cases:
            with self.subTest(words=words):
                self.assertEqual(_reader_operands(words, 0), expected)

    def test_sed_non_filter_options(self) -> None:
        """Reject every sed option outside the line-filter allow list."""
        for option in ("-f", "-i", "-s", "-z", "--expression=p", "--file=x", "-np", "-ep"):
            with self.subTest(option=option):
                self.assertIsNone(_reader_operands(["sed", option, "p", "clean.sh"], 0))

    def test_filtered_operand_limit(self) -> None:
        """Allow exactly the filtered operand cap and refuse its first excess entry."""
        context = InspectionContext(self.root, False, [0], classify_script_words)
        body = "head -n 5 a.sh b.sh | bash\n"
        with patch("scripts.raw_configure_body.MAX_SCRIPT_FILES", 2):
            hit, _, _, filtered = scan_body(body, context, BodyBudget())
            self.assertIsNone(hit)
            self.assertEqual(len(filtered), 2)
            hit, _, _, _ = scan_body(body.replace("b.sh", "b.sh c.sh"), context, BodyBudget())
            self.assertEqual(hit, "more nested scripts than inspect limit")

    def test_filtered_candidate_directory_state(self) -> None:
        """Preserve concrete filtered operands before and after directory changes."""
        context = InspectionContext(self.root, False, [0], classify_script_words)
        body = 'head a.sh | bash\ncd sub\nhead b.sh "$script" | bash\n'
        hit, _, _, filtered = scan_body(body, context, BodyBudget())
        self.assertIsNone(hit)
        self.assertEqual(filtered, [("a.sh", False, False), ("b.sh", False, True)])

    def test_filtered_file_byte_bound(self) -> None:
        """Read clean filtered files at the byte bound and refuse the first extra byte."""
        path = self.root / "notes.sh"
        raw = b"#" + b"a" * (MAX_SCRIPT_BYTES - 2) + b"\n"
        path.write_bytes(raw)
        self._check_parser_body("head notes.sh | bash\n", refused=False)
        path.write_bytes(raw + b"\n")
        self._check_parser_body(
            "head notes.sh | bash\n", refused=True, reason="script larger than inspect limit"
        )

    def test_shell_operand_with_reader_argument(self) -> None:
        """Keep the shell's actual script operand when another argument is a reader."""
        (self.root / "clean.sh").write_text("echo clean\n", encoding="utf-8")
        self._check_parser_body("bash inner.sh <(cat clean.sh)\n", refused=True)


class RoundElevenFilesTest(RawConfigureGuardBase):
    """Cover kernel shebang arguments and bounded GNU env option emulation."""

    def test_judge_header_shapes(self) -> None:
        """Classify judge-measured shell headers and allowed non-shell interpreter names."""
        cases = (
            (b"#!/usr/bin/env -S 'ba''sh'\n", "shell"),
            (b'#!/usr/bin/env -S b"as"h\n', "shell"),
            (b"#!/bin/busybox ash\n", "shell"),
            (b"#!/bin/busybox --x sh\n", "shell"),
            (b"#!/usr/bin/env  bash\n", "shell"),
            (b"#!/usr/bin/env\tbash\n", "shell"),
            (b"#!/bin/bash\r\n", "shell"),
            (b"#!/usr/bin/env -S\tbash\n", "shell"),
            (b"#!/usr/bin/env --sp=bash\n", "shell"),
            (b"#!/usr/bin/env --s bash\n", "shell"),
            (b"#!/usr/bin/env -S -C . bash\n", "shell"),
            (b"#!/usr/bin/env -S --env0-from=/dev/null bash\n", "shell"),
            (b"#!/usr/bin/env python3\n", "skip"),
            (b"#!/usr/bin/perl -w\n", "skip"),
            (b"#!/usr/bin/env node\n", "skip"),
            (b"#!/usr/bin/python3.12\n", "skip"),
            (b"#!/usr/bin/env -S uv run --script\n", "skip"),
        )
        for head, expected in cases:
            with self.subTest(head=head):
                self.assertEqual(_program_kind(head), expected)

    def test_env_header_classification(self) -> None:
        """Replay every r11 judge header and the requested fail-closed variants."""
        cases = (
            (b"#!/usr/bin/env -vSbash\n", "shell"),
            (b"#!/usr/bin/env -iSbash\n", "shell"),
            (b"#!/usr/bin/env -S -a x bash\n", "shell"),
            (b"#!/usr/bin/env -S --argv0=x bash\n", "shell"),
            (b"#!/usr/bin/env -S -u X bash\n", "shell"),
            (b"#!/usr/bin/env -S -- bash\n", "shell"),
            (b"#!/usr/bin/env -S A=1 bash\n", "shell"),
            (b"#!/usr/bin/env --split=bash\n", "shell"),
            (b"#!/usr/bin/env -S --split=bash\n", "shell"),
            (b"#!/usr/bin/env -S nice bash\n", "shell"),
            (b"#!/usr/bin/env -S env bash\n", "shell"),
            (b'#!/usr/bin/env -S "bash"\n', "shell"),
            (b"#!/usr/bin/env -Sbash #c\n", "shell"),
            (b"#!/usr/bin/env -S - bash\n", "shell"),
            (b"#!/usr/bin/env -S --ignore-signal=INT bash\n", "shell"),
            (b"#!/usr/bin/env -S --chdir=. bash\n", "shell"),
            (b"#!/opt/x/bin/mysh\n", "shell"),
            (b"#!/usr/bin/env python3\n", "skip"),
            (b"#!/usr/bin/env -S python3 -u\n", "skip"),
            (b"#!/usr/bin/env -S -i python3\n", "skip"),
            (b"#!/usr/bin/python3\n", "skip"),
            (b"\x7fELF\x02\x01\x01\x00\n", "skip"),
            (b"#!/usr/bin/env -i bash\n", "shell"),
            (b"#!/usr/bin/env -S ba\\sh\n", "shell"),
            (b"#!/usr/bin/env -uSbash\n", "shell"),
            (b"#!/usr/bin/env --s=bash\n", "shell"),
            (b"#!/usr/bin/env --un=X\n", "shell"),
            (b"#!/usr/bin/env -S nice python3\n", "shell"),
            (b"#!/usr/bin/perl5.40 -w\n", "skip"),
            (b"#!/bin/busybox awk -f\n", "skip"),
            (b"#!/bin/busybox env sh\n", "shell"),
            (b"#!/usr/bin/env -S " + b"A=1 " * 70 + b"python3\n", "shell"),
        )
        for head, expected in cases:
            with self.subTest(head=head):
                self.assertEqual(_program_kind(head), expected)

    def test_kernel_header_arguments(self) -> None:
        """Preserve a single kernel argument and split only spaces and tabs."""
        cases = (
            (b"#! \t/usr/bin/env\t-S -i python3 \t\n./configure", "skip"),
            (b"#!/usr/bin/env python3 -u\n", "shell"),
            (b"#!/usr/bin/env python3\r\n", "shell"),
            (b"#!/usr/bin/python3\v-x\n", "shell"),
            (b"#!/usr/bin/env\n", "shell"),
            (b"#!\t \n", "shell"),
            (b"#!/bin/busybox -x busybox awk\n", "shell"),
            (b"#!/bin/busybox -x unknown\n", "shell"),
        )
        for head, expected in cases:
            with self.subTest(head=head):
                self.assertEqual(_program_kind(head), expected)

    def test_non_shell_allow_list(self) -> None:
        """Allow every named non-shell family and reject names outside its pattern."""
        programs = (
            "python",
            "python2",
            "python3",
            "pypy",
            "pypy3",
            "perl",
            "ruby",
            "node",
            "nodejs",
            "deno",
            "bun",
            "lua",
            "luajit",
            "php",
            "tclsh",
            "wish",
            "awk",
            "gawk",
            "mawk",
            "nawk",
            "Rscript",
            "julia",
            "uv",
            "make",
            "gmake",
            "python3.12",
            "pypy3.11",
            "perl5.40",
            "ruby3.4",
            "lua5.4",
            "php8.4",
            "tclsh9.0",
            "node22.1",
        )
        for program in programs:
            with self.subTest(program=program):
                self.assertEqual(_program_kind(f"#!/usr/bin/{program}\n".encode()), "skip")
        for program in ("mysh", "nice", "makepkg", "pythonx", "python3x", "python.3"):
            with self.subTest(program=program):
                self.assertEqual(_program_kind(f"#!/usr/bin/{program}\n".encode()), "shell")

    def test_env_option_values(self) -> None:
        """Consume required values, preserve optional operands and reject invalid options."""
        options = (
            b"-a x",
            b"-ax",
            b"-C .",
            b"-C.",
            b"-u X",
            b"-uX",
            b"-iv",
            b"--argv0 x",
            b"--argv0=x",
            b"--env0-from x",
            b"--env0-from=x",
            b"--unset X",
            b"--unset=X",
            b"--chdir .",
            b"--chdir=.",
            b"--ignore-environment",
            b"--null",
            b"--list-signal-handling",
            b"--debug",
            b"--help",
            b"--version",
            b"--block-signal",
            b"--block-signal=INT",
            b"--default-signal",
            b"--default-signal=INT",
            b"--ignore-signal",
            b"--ignore-signal=INT",
            b"--split-string=python3",
            b"--split=python3",
            b"--s=python3",
            b"--argv=x",
            b"--un=X",
            b"--",
            b"-",
            b"A=1 -uX",
        )
        for option in options:
            with self.subTest(option=option):
                head = b"#!/usr/bin/env -S " + option + b" python3\n"
                expected = "shell" if option == b"A=1 -uX" else "skip"
                self.assertEqual(_program_kind(head), expected)
        for option in (b"--", b"--unknown", b"--d", b"--ignore", b"--debug=x", b"-x", b"-i "):
            with self.subTest(invalid=option):
                self.assertEqual(_program_kind(b"#!/usr/bin/env " + option + b"\n"), "shell")

    def test_env_split_quoting(self) -> None:
        """Remove literal quotes while failing closed on escapes and expansion."""
        cases = (
            (b"'python3'", "skip"),
            (b'"python3"', "skip"),
            (b"py'thon'3", "skip"),
            (b"python3 # comment", "skip"),
            (b"'python3 -u'", "shell"),
            (b"'python3", "shell"),
            (b'"python3', "shell"),
            (b"python3\\", "shell"),
            (b"python3 $X", "shell"),
            (b"'python3$X'", "shell"),
            (b"'' python3", "shell"),
            (b"# python3", "shell"),
            (b"'python3'#comment", "shell"),
            (b"\tpython3\t-u", "skip"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_program_kind(b"#!/usr/bin/env -S " + value + b"\n"), expected)

    def test_env_parser_bounds(self) -> None:
        """Allow the last bounded word and split insertion while rejecting either excess."""
        cases = (
            ([b"A=1"] * 63 + [b"python3"], "python3"),
            ([b"A=1"] * 64 + [b"python3"], ""),
            ([b"-u", b"X"] * 31 + [b"-i", b"python3"], "python3"),
            ([b"-u", b"X"] * 32 + [b"python3"], ""),
            ([b"-S"] * 8 + [b"python3"], "python3"),
            ([b"-S"] * 9 + [b"python3"], ""),
            ([b"-S", b"A=1 " * 70 + b"python3"], ""),
        )
        for parts, expected in cases:
            with self.subTest(parts=parts):
                self.assertEqual(_env_interpreter(parts), expected)

    def test_env_headers_top_level_and_body(self) -> None:
        """Inspect shell and unknown headers directly and through a nested body."""
        cases = (
            (b"#!/usr/bin/env -vSbash\n", True),
            (b"#!/usr/bin/env -S -a x bash\n", True),
            (b"#!/usr/bin/env -S nice bash\n", True),
            (b"#!/opt/x/bin/mysh\n", True),
            (b"#!/usr/bin/env -S -i python3\n", False),
            (b"#!/usr/bin/env -S 'ba''sh'\n", True),
            (b"#!/usr/bin/env\tbash\n", True),
            (b"#!/bin/bash\r\n", True),
            (b"#!/usr/bin/env -S uv run --script\n", False),
        )
        program = self.root / "name"
        for head, refused in cases:
            with self.subTest(head=head):
                program.write_bytes(head + f'"{self.tree}/configure"\n'.encode())
                program.chmod(0o755)
                assertion = self._assert_refused if refused else self._assert_allowed
                assertion((("./name", self.root),))
                self._check_parser_body("./name\n", refused=refused)
