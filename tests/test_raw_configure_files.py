# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise script file inspection and path resolution for raw configure guard."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed git ls-files command for corpus discovery.
import sys
from pathlib import Path

from scripts.raw_configure_files import MAX_SCRIPT_BYTES, MAX_SCRIPT_FILES
from tests.helpers import isolated_git_environment
from tests.raw_configure_support import CLIENTS, ROOT, RawConfigureGuardBase


class RawConfigureFilesTest(RawConfigureGuardBase):
    """Exercise script file operand and program path inspection in the guard."""

    def _assert_allowed(
        self, cases: tuple[tuple[str, Path], ...], env: dict[str, str] | None = None
    ) -> None:
        """Verify the guard allows safe commands across all supported clients."""
        for client in CLIENTS:
            for command, cwd in cases:
                with self.subTest(client=client, command=command):
                    result = self.run_guard(client, self.payload(client, command, cwd), env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(self.marker.exists())

    def _make_configure_driver(self) -> Path:
        """Create an absolute driver script invoking configure in a subshell."""
        driver = self.root / "driver.sh"
        driver.write_text(
            f"#!/bin/sh\n"
            f'BUILD="{self.build}"\n'
            f'SRC="{self.tree}"\n'
            '( cd "$BUILD" && "$SRC/configure" --toolchain=clang-asan --enable-gpl )\n',
            encoding="utf-8",
        )
        driver.chmod(0o755)
        return driver

    def test_refuses_bash_driver_script(self) -> None:
        """Refuse execution of a script file containing a raw configure subshell."""
        driver = self._make_configure_driver()
        self._assert_refused(((f"bash {driver}", self.root),))

    def test_refuses_sh_verbose_driver_script(self) -> None:
        """Refuse execution of a script file passed to sh with options."""
        driver = self._make_configure_driver()
        self._assert_refused(((f"sh -x {driver}", self.root),))

    def test_refuses_direct_executable_script(self) -> None:
        """Refuse direct execution of an executable script invoking configure."""
        driver = self._make_configure_driver()
        self._assert_refused(((str(driver), self.root),))

    def test_refuses_source_script(self) -> None:
        """Refuse execution of a script file via the source builtin."""
        driver = self._make_configure_driver()
        self._assert_refused(((f"source {driver}", self.root),))

    def test_refuses_dot_script(self) -> None:
        """Refuse execution of a script file via the dot builtin."""
        driver = self._make_configure_driver()
        self._assert_refused(((f". {driver}", self.root),))

    def test_refuses_praetorctl_wrapped_script(self) -> None:
        """Refuse execution of a script file wrapped by praetorctl admit."""
        driver = self._make_configure_driver()
        cmd = f"praetorctl admit --label t -- bash {driver} > /dev/null 2>&1"
        self._assert_refused(((cmd, self.root),))

    def test_refuses_env_wrapped_script(self) -> None:
        """Refuse execution of a script file wrapped by env with variables."""
        driver = self._make_configure_driver()
        self._assert_refused(((f"env FOO=1 bash {driver}", self.root),))

    def test_refuses_nested_script(self) -> None:
        """Refuse execution of an outer script that runs an inner configure script."""
        inner = self.root / "inner.sh"
        inner.write_text(
            f'( cd "{self.build}" && "{self.tree}/configure" --enable-gpl )\n',
            encoding="utf-8",
        )
        outer = self.root / "outer.sh"
        outer.write_text(f"bash {inner}\n", encoding="utf-8")
        self._assert_refused(((f"bash {outer}", self.root),))

    def test_refuses_relative_script_with_cwd(self) -> None:
        """Refuse execution of a relative script resolved against payload cwd."""
        driver_dir = self.root / "subdir"
        driver_dir.mkdir()
        driver = driver_dir / "driver.sh"
        driver.write_text(
            f'( cd "{self.build}" && "{self.tree}/configure" --enable-gpl )\n',
            encoding="utf-8",
        )
        self._assert_refused((("bash driver.sh", driver_dir),))

    def test_refuses_relative_script_after_cd(self) -> None:
        """Refuse execution of a relative script after directory change in program."""
        self._assert_refused(((f"cd {self.build} && bash driver.sh", self.root),))

    def test_refuses_fifo_script_operand(self) -> None:
        """Refuse execution when script operand refers to a FIFO special file."""
        fifo_path = self.root / "fifo_script"
        os.mkfifo(fifo_path)
        self._assert_refused(((f"bash {fifo_path}", self.root),))

    def test_refuses_script_larger_than_byte_limit(self) -> None:
        """Refuse execution of a script file exceeding the size inspection limit."""
        large_file = self.root / "large.sh"
        large_file.write_bytes(b"# " + b"a" * (MAX_SCRIPT_BYTES - 1) + b"\n# extra\n")
        self._assert_refused(((f"bash {large_file}", self.root),))

    def test_refuses_script_with_nul_byte(self) -> None:
        """Refuse execution of a script file containing a binary NUL byte."""
        nul_file = self.root / "nul.sh"
        nul_file.write_bytes(b"echo hello\x00world\n")
        self._assert_refused(((f"bash {nul_file}", self.root),))

    def test_refuses_chained_scripts_exceeding_file_limit(self) -> None:
        """Refuse execution when chained script invocations exceed the file inspection limit."""
        script_paths: list[Path] = [
            self.root / f"chain_{idx}.sh" for idx in range(MAX_SCRIPT_FILES + 1)
        ]
        for idx in range(MAX_SCRIPT_FILES):
            script_paths[idx].write_text(f"bash {script_paths[idx + 1]}\n", encoding="utf-8")
        script_paths[MAX_SCRIPT_FILES].write_text("echo done\n", encoding="utf-8")
        self._assert_refused(((f"bash {script_paths[0]}", self.root),))

    def test_refuses_script_with_inline_configure_in_dash_c(self) -> None:
        """Refuse execution of a script running configure through an inline bash command."""
        script = self.root / "dash_c.sh"
        script.write_text("bash -c '\"$SRC/configure\" --x'\n", encoding="utf-8")
        self._assert_refused(((f"bash {script}", self.root),))

    def test_refuses_script_with_spliced_quotes_configure(self) -> None:
        """Refuse execution of a script invoking configure with spliced quoted segments."""
        script = self.root / "spliced.sh"
        script.write_text("\"${SRC}\"/conf''igure --enable-gpl\n", encoding="utf-8")
        self._assert_refused(((f"bash {script}", self.root),))

    def test_refuses_script_with_unbalanced_quote_and_configure(self) -> None:
        """Refuse execution of an unparsable script line containing configure via regex fallback."""
        script = self.root / "unbalanced.sh"
        script.write_text('echo "unbalanced ./configure\n', encoding="utf-8")
        self._assert_refused(((f"bash {script}", self.root),))

    def test_allows_clean_script(self) -> None:
        """Allow execution of a script file running benign build commands."""
        clean = self.root / "clean.sh"
        clean.write_text("make -j2\necho done\n", encoding="utf-8")
        self._assert_allowed(((f"bash {clean}", self.root),))

    def test_allows_script_with_undecidable_words(self) -> None:
        """Allow execution of a script file containing undecidable dynamic command words."""
        script = self.root / "undecidable.sh"
        script.write_text('bash "$RUNNER" "$@"\nexec "$TOOL" --flag\n', encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_echoing_configure_phrase(self) -> None:
        """Allow execution of a script file printing configure in a string argument."""
        script = self.root / "echo_conf.sh"
        script.write_text('echo "configure and build"\n', encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_with_configure_in_case_pattern(self) -> None:
        """Allow execution of a script file matching configure in a case pattern."""
        script = self.root / "case_conf.sh"
        script.write_text('case "$1" in configure) echo x ;; esac\n', encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_with_apostrophe_in_heredoc(self) -> None:
        """Allow execution of a script file containing an apostrophe inside a heredoc."""
        script = self.root / "heredoc.sh"
        script.write_text("cat <<EOF\nit's fine\nEOF\n", encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_with_shebang_and_set_flags(self) -> None:
        """Allow execution of a script file starting with shebang and shell options."""
        script = self.root / "shebang.sh"
        script.write_text("#!/bin/sh\nset -eu\n", encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_large_body_of_plain_commands(self) -> None:
        """Allow execution of a script file containing a forty kilobyte body of plain commands."""
        script = self.root / "large_plain.sh"
        lines = [
            "echo hello\n",
            "sed -e 's/a/b/' /dev/null\n",
            "grep -v pattern /dev/null\n",
            "printf '%s\\n' text\n",
        ] * 400
        script.write_text("".join(lines), encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_calling_ffmpeg_configure_script(self) -> None:
        """Allow execution of a script file invoking the approved configure helper."""
        script = self.root / "ffmpeg_conf.sh"
        script.write_text(
            'python3 scripts/ffmpeg_configure.py "$SRC" "$BUILD" -- --enable-gpl\n',
            encoding="utf-8",
        )
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_ffmpeg_configure_wrapper_in_script(self) -> None:
        """Allow execution of a script file using the approved configure wrapper."""
        run_ok = self.root / "run_ok.sh"
        wrapper = ROOT / "scripts" / "ffmpeg_configure.py"
        cmd = f"python3 {wrapper} {self.tree} {self.build} -- --enable-gpl\n"
        run_ok.write_text(cmd, encoding="utf-8")
        self._assert_allowed(((f"bash {run_ok}", self.root),))

    def test_allows_all_tracked_repository_shell_scripts(self) -> None:
        """Verify every tracked repository shell script is allowed across all clients."""
        proc = subprocess.run(  # nosec B603 B607 - fixed git ls-files argv in the repository checkout.
            ["git", "ls-files", "*.sh"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=isolated_git_environment(),
        )
        files = [ROOT / line.strip() for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(files, "git ls-files found no tracked shell scripts")
        cases = tuple((f"bash {p}", ROOT) for p in files)
        self._assert_allowed(cases)

    def test_allows_missing_script(self) -> None:
        """Allow missing script operands to let the executing shell handle errors."""
        missing = self.root / "missing.sh"
        self._assert_allowed(((f"bash {missing}", self.root),))

    def test_allows_binary_program_path(self) -> None:
        """Allow binary executable program paths without shebang script headers."""
        self._assert_allowed(((f"{sys.executable} -c pass", self.root),))

    def test_allows_script_at_exact_byte_limit(self) -> None:
        """Allow execution of a benign script of exactly MAX_SCRIPT_BYTES bytes."""
        exact = self.root / "exact.sh"
        exact.write_bytes(b"# " + b"b" * (MAX_SCRIPT_BYTES - 3) + b"\n")
        self._assert_allowed(((f"bash {exact}", self.root),))

    def test_allows_absolute_script_after_cd(self) -> None:
        """Allow execution of an absolute benign script after directory change."""
        clean = self.root / "clean.sh"
        clean.write_text("echo clean\n", encoding="utf-8")
        self._assert_allowed(((f"cd {self.build} && bash {clean}", self.root),))

    def test_allows_script_with_array_assignment_of_directories(self) -> None:
        """Allow execution of a script assigning directory paths in an array."""
        d1 = self.root / "d1"
        d1.mkdir()
        d2 = self.root / "d2"
        d2.mkdir()
        script = self.root / "arr.sh"
        script.write_text(f"ROOTS=({d1} {d2})\n", encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_with_python_heredoc_referencing_directory(self) -> None:
        """Allow execution of a script containing Python heredoc with a directory path."""
        d1 = self.root / "d1"
        d1.mkdir()
        script = self.root / "python_heredoc.sh"
        script.write_text(
            f"python3 - <<'EOF'\nimport os\nprint(os.path.realpath(\"{d1}\"))\nEOF\n",
            encoding="utf-8",
        )
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_with_continuation_list_of_directories(self) -> None:
        """Allow execution of a script iterating over continued lines of directories."""
        d1 = self.root / "d1"
        d1.mkdir()
        d2 = self.root / "d2"
        d2.mkdir()
        script = self.root / "continuation.sh"
        script.write_text(
            f'for d in \\\n  "{d1}" \\\n  "{d2}"; do echo "$d"; done\n',
            encoding="utf-8",
        )
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_with_nested_fifo_candidate(self) -> None:
        """Allow execution of a script referencing a FIFO special file in a subshell."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo is not available on this platform")
        fifo = self.root / "fifo"
        try:
            os.mkfifo(fifo)
        except OSError as exc:
            self.skipTest(f"os.mkfifo failed: {exc}")
        script = self.root / "subshell_fifo.sh"
        script.write_text(f"( {fifo} )\n", encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_allows_script_with_nested_nonexistent_candidate(self) -> None:
        """Allow execution of a script referencing a nested path that does not exist."""
        missing = self.root / "nonexistent.sh"
        script = self.root / "nested_missing.sh"
        script.write_text(f"bash {missing}\n", encoding="utf-8")
        self._assert_allowed(((f"bash {script}", self.root),))

    def test_refuses_directory_script_operand(self) -> None:
        """Refuse execution when the hook script operand refers to a directory."""
        d1 = self.root / "d1"
        d1.mkdir()
        for client in CLIENTS:
            with self.subTest(client=client):
                result = self.run_guard(client, self.payload(client, f"bash {d1}", self.root))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("(not a regular file)", result.stderr)
        self._assert_refused(((f"bash {d1}", self.root),))

    def test_refuses_nested_script_larger_than_byte_limit(self) -> None:
        """Refuse execution when a nested script exceeds the size inspection limit."""
        big_sh = self.root / "big.sh"
        big_sh.write_bytes(b"# " + b"c" * (MAX_SCRIPT_BYTES - 1) + b"\n# extra\n")
        outer = self.root / "outer_big.sh"
        outer.write_text(f"bash {big_sh}\n", encoding="utf-8")
        for client in CLIENTS:
            with self.subTest(client=client):
                result = self.run_guard(client, self.payload(client, f"bash {outer}", self.root))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("(script larger than inspect limit)", result.stderr)
        self._assert_refused(((f"bash {outer}", self.root),))

    def test_refuses_nested_configure_after_dropped_directory(self) -> None:
        """Refuse execution when nested script follows a dropped directory candidate."""
        d1 = self.root / "d1"
        d1.mkdir()
        inner = self.root / "inner.sh"
        inner.write_text(
            f'( cd "{self.build}" && "{self.tree}/configure" --x )\n',
            encoding="utf-8",
        )
        outer = self.root / "outer_dir_then_inner.sh"
        outer.write_text(f"{d1}\nbash {inner}\n", encoding="utf-8")
        self._assert_refused(((f"bash {outer}", self.root),))
