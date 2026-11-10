# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise script file inspection and path resolution for raw configure guard."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - fixed git ls-files command for corpus discovery.
import sys
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
from tests.helpers import isolated_git_environment
from tests.raw_configure_support import CLIENTS, ROOT, RawConfigureGuardBase


def _shape_records() -> list[tuple[str, str, str]]:
    """Read allow and refuse script bodies with their unique fixture identifiers."""
    path = ROOT / "tests/fixtures/raw_configure_body/shapes.txt"
    records: list[tuple[str, str, str]] = []
    kind = ident = ""
    body: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("==="):
            header = line.split()
            if len(header) != 3 or header[0] != "===" or header[1] not in ("allow", "refuse"):
                raise ValueError(f"Invalid shape header: {line!r}")
            if kind:
                records.append((kind, ident, "".join(body)))
            _, kind, ident = header
            body = []
        elif kind:
            body.append(line)
        elif not line.startswith("#"):
            raise ValueError(f"Shape body before first header: {line!r}")
    if kind:
        records.append((kind, ident, "".join(body)))
    return records


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

    def test_script_body_shapes(self) -> None:
        """Replay every allow and refuse script shape across all clients."""
        records = _shape_records()
        self.assertEqual(len(records), 38)
        for kind, ident, body in records:
            with self.subTest(shape=ident):
                self._check_parser_body(body, refused=kind == "refuse")
        self.assertEqual(len({ident for _, ident, _ in records}), len(records))

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
        if not hasattr(os, "mkfifo"):
            self.skipTest("os.mkfifo is not available on this platform")
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

    def _check_parser_body(self, body: str, *, refused: bool, reason: str | None = None) -> None:
        """Inspect a fixture body across clients without executing the script."""
        cfg = f'"{self.tree}/configure" --x'
        body = body.replace("<cfg>", cfg).replace("<tree>", str(self.tree))
        body = body.replace("<build>", str(self.build))
        script = self.root / "parser_body.sh"
        script.write_text(body, encoding="utf-8")
        (self.root / "inner.sh").write_text(cfg + "\n", encoding="utf-8")
        assertion = self._assert_refused if refused else self._assert_allowed
        assertion(((f"bash {script}", self.root),))
        if reason is not None:
            for client in CLIENTS:
                result = self.run_guard(client, self.payload(client, f"bash {script}", self.root))
                self.assertIn(reason, result.stderr)

    def test_allows_parser_case_terminators(self) -> None:
        """Allows the case terminators script shape."""
        self._check_parser_body(
            'case "$u" in\n  http*) echo web ;;\n  */*) echo path ;;\n  *) echo other ;;\nesac\n',
            refused=False,
        )

    def test_allows_parser_named_fd(self) -> None:
        """Allows the named fd script shape."""
        self._check_parser_body("exec {fd}>/dev/null\nexec {fd}>&-\n", refused=False)

    def test_allows_parser_named_fd_function(self) -> None:
        """Allows the named fd function script shape."""
        self._check_parser_body(
            'f() {\n  if ! exec {fd}<>"$lock_file" 2>/dev/null; then\n    return 0\n  fi\n}\n',
            refused=False,
        )

    def test_allows_parser_fd_subshell(self) -> None:
        """Allows the fd subshell script shape."""
        self._check_parser_body('(\n  exec 9<>"$lock_file"\n  flock -n 9\n)\n', refused=False)

    def test_allows_parser_parameter_message(self) -> None:
        """Allows the parameter message script shape."""
        self._check_parser_body('launcher="${1:?launcher script}"\n', refused=False)

    def test_allows_parser_continuations(self) -> None:
        """Allows the continuations script shape."""
        self._check_parser_body('printf \'%s\\n\' \\\n  "a b" \\\n  "c d"\n', refused=False)

    def test_allows_parser_multiline_array(self) -> None:
        """Allows the multiline array script shape."""
        self._check_parser_body('items=(\n  "one two"\n  three\n)\n', refused=False)

    def test_allows_parser_arithmetic(self) -> None:
        """Allows the arithmetic script shape."""
        self._check_parser_body('n=$(( $# + 1 ))\necho "$((n * 2))"\n', refused=False)

    def test_allows_parser_xargs_placeholder(self) -> None:
        """Allows the xargs placeholder script shape."""
        self._check_parser_body(
            "printf '%s\\n' a b | xargs -I{} stat -c %s {}\nexec \"$@\"\n", refused=False
        )

    def test_allows_parser_sed_program(self) -> None:
        """Allows the sed program script shape."""
        self._check_parser_body("sed -n '/^x/{p;q;}' \"$f\"\n", refused=False)

    def test_allows_parser_run_helper(self) -> None:
        """Allows the run helper script shape."""
        self._check_parser_body("run configure only 0 configure\n", refused=False)

    def test_allows_parser_error_parameter_parentheses(self) -> None:
        """Allows the error parameter parentheses script shape."""
        self._check_parser_body(
            'run_refuse 2 "${hp_out:-refusing: reader failed (rc $hp_rc)}" err\n', refused=False
        )

    def test_allows_parser_error_parameter_missing(self) -> None:
        """Allows the error parameter missing script shape."""
        self._check_parser_body(
            'check "every script${missing:+ (missing: $missing)}" 0\n', refused=False
        )

    def test_allows_parser_error_parameter_semicolon(self) -> None:
        """Allows the error parameter semicolon script shape."""
        self._check_parser_body('reason="${reason:+$reason; }CANCELLED: $c"\n', refused=False)

    def test_allows_parser_error_case_home(self) -> None:
        """Allows the error case home script shape."""
        self._check_parser_body(
            'for hi in "${hide[@]}"; do case "$hp:$hi" in --tmpfs:"$HOME"/*) '
            '[ -e "$hi" ] || echo gone ;; esac; done\n',
            refused=False,
        )

    def test_allows_parser_error_case_quotes(self) -> None:
        """Allows the error case quotes script shape."""
        self._check_parser_body(
            'case "$line" in *"model=a"*"b=c"*) echo ok ;; *) echo no ;; esac\n', refused=False
        )

    def test_allows_parser_error_case_escape(self) -> None:
        """Allows the error case escape script shape."""
        self._check_parser_body(
            'case "${T}" in\n  [Ff][Ii][Xx]:* | [Ff][Ii][Xx]\\(*)\n'
            '    r="fix: prefix"\n    ;;\nesac\n',
            refused=False,
        )

    def test_allows_parser_background_python_heredoc(self) -> None:
        """Allows the background python heredoc script shape."""
        self._check_parser_body(
            "python3 - \"$tmp/a\" <<'PY' &\nimport sys\nprint(sys.argv)\nPY\nwait\n", refused=False
        )

    def test_allows_parser_configure_data(self) -> None:
        """Allows the configure data script shape."""
        self._check_parser_body(
            "echo ./configure\nprintf '%s\\n' ./configure\nls -l ./configure\n"
            "grep -n x ./configure\n[[ -x ./configure ]]\n[ -x ./configure ]\n",
            refused=False,
        )

    def test_allows_parser_cat_heredoc(self) -> None:
        """Allows the cat heredoc script shape."""
        self._check_parser_body("cat <<EOF\n./configure --x\nEOF\n", refused=False)

    def test_allows_parser_quoted_cat_heredoc(self) -> None:
        """Allows the quoted cat heredoc script shape."""
        self._check_parser_body("cat <<'EOF'\n$(<cfg>)\nEOF\n", refused=False)

    def test_allows_parser_configure_comment(self) -> None:
        """Allows the configure comment script shape."""
        self._check_parser_body("echo x # <cfg>\n", refused=False)

    def test_refuses_parser_hash_parameter(self) -> None:
        """Refuses the hash parameter script shape."""
        self._check_parser_body("n=$#; <cfg>\n", refused=True)

    def test_refuses_parser_embedded_hash(self) -> None:
        """Refuses the embedded hash script shape."""
        self._check_parser_body("echo a#b; <cfg>\n", refused=True)

    def test_refuses_parser_shell_comment(self) -> None:
        """Refuses the shell comment script shape."""
        self._check_parser_body("sh configure # don't edit\n", refused=True)

    def test_refuses_parser_shell_ec(self) -> None:
        """Refuses the shell ec script shape."""
        self._check_parser_body("bash -ec '<cfg>'\n", refused=True)

    def test_refuses_parser_shell_lc(self) -> None:
        """Refuses the shell lc script shape."""
        self._check_parser_body("bash -lc '<cfg>'\n", refused=True)

    def test_refuses_parser_shell_o_c(self) -> None:
        """Refuses the shell o c script shape."""
        self._check_parser_body("bash -o pipefail -c '<cfg>'\n", refused=True)

    def test_refuses_parser_shell_euo_c(self) -> None:
        """Refuses the shell euo c script shape."""
        self._check_parser_body("bash -euo pipefail -c '<cfg>'\n", refused=True)

    def test_refuses_parser_bare_configure(self) -> None:
        """Refuses the bare configure script shape."""
        self._check_parser_body("configure --x\n", refused=True)

    def test_refuses_parser_glob_configure(self) -> None:
        """Refuses the glob configure script shape."""
        self._check_parser_body("./configur? --x\n", refused=True)

    def test_refuses_parser_bash_heredoc(self) -> None:
        """Refuses the bash heredoc script shape."""
        self._check_parser_body("bash <<EOF\n<cfg>\nEOF\n", refused=True)

    def test_refuses_parser_piped_heredoc(self) -> None:
        """Refuses the piped heredoc script shape."""
        self._check_parser_body("cat <<EOF | sh\n<cfg>\nEOF\n", refused=True)

    def test_refuses_parser_python_heredoc(self) -> None:
        """Refuses the python heredoc script shape."""
        self._check_parser_body(
            "python3 <<'EOF'\nimport os; os.system(\"<tree>/configure\")\nEOF\n", refused=True
        )

    def test_refuses_parser_cat_substitution(self) -> None:
        """Refuses the cat substitution script shape."""
        self._check_parser_body("cat <<EOF\n$(<cfg>)\nEOF\n", refused=True)

    def test_refuses_parser_shell_herestring(self) -> None:
        """Refuses the shell herestring script shape."""
        self._check_parser_body("sh <<< '<cfg>'\n", refused=True)

    def test_refuses_parser_echo_substitution(self) -> None:
        """Refuses the echo substitution script shape."""
        self._check_parser_body('echo "$(<cfg>)"\n', refused=True)

    def test_refuses_parser_assignment_substitution(self) -> None:
        """Refuses the assignment substitution script shape."""
        self._check_parser_body("x=$(<cfg>)\n", refused=True)

    def test_refuses_parser_loop_substitution(self) -> None:
        """Refuses the loop substitution script shape."""
        self._check_parser_body("for i in $(<cfg>); do :; done\n", refused=True)

    def test_refuses_parser_process_substitution(self) -> None:
        """Refuses the process substitution script shape."""
        self._check_parser_body("while read -r l; do :; done < <(<cfg>)\n", refused=True)

    def test_refuses_parser_backticks(self) -> None:
        """Refuses the backticks script shape."""
        self._check_parser_body("echo `<cfg>`\n", refused=True)

    def test_refuses_parser_subshell_directory(self) -> None:
        """Refuses the subshell directory script shape."""
        self._check_parser_body('( cd "<build>" && true )\n<cfg>\n', refused=True)

    def test_refuses_parser_function_body(self) -> None:
        """Refuses the function body script shape."""
        self._check_parser_body("f() { <cfg>; }\n", refused=True)

    def test_refuses_parser_case_body(self) -> None:
        """Refuses the case body script shape."""
        self._check_parser_body("case x in a) <cfg> ;; esac\n", refused=True)

    def test_refuses_parser_if_body(self) -> None:
        """Refuses the if body script shape."""
        self._check_parser_body("if true; then <cfg>; fi\n", refused=True)

    def test_refuses_parser_negated(self) -> None:
        """Refuses the negated script shape."""
        self._check_parser_body("! <cfg>\n", refused=True)

    def test_refuses_parser_background(self) -> None:
        """Refuses the background script shape."""
        self._check_parser_body("<cfg> &\n", refused=True)

    def test_refuses_parser_time(self) -> None:
        """Refuses the time script shape."""
        self._check_parser_body("time <cfg>\n", refused=True)

    def test_refuses_parser_coproc(self) -> None:
        """Refuses the coproc script shape."""
        self._check_parser_body("coproc <cfg>\n", refused=True)

    def test_refuses_parser_dynamic_wrapper(self) -> None:
        """Refuses the dynamic wrapper script shape."""
        self._check_parser_body("$SUDO <cfg>\n", refused=True)

    def test_refuses_parser_array_configure(self) -> None:
        """Refuses the array configure script shape."""
        self._check_parser_body("cmd=(<tree>/configure --x)\n", refused=True)

    def test_refuses_parser_case_data_next_line(self) -> None:
        """Refuses the case data next line script shape."""
        self._check_parser_body("echo case x in a) ;; esac\n<cfg>\n", refused=True)

    def test_refuses_parser_echo_pipeline(self) -> None:
        """Refuses the echo pipeline script shape."""
        self._check_parser_body("echo '<cfg>' | sh\n", refused=True)

    def test_refuses_parser_shell_process_argument(self) -> None:
        """Refuses the shell process argument script shape."""
        self._check_parser_body("bash <(echo '<cfg>')\n", refused=True)

    def test_refuses_parser_inline_comment(self) -> None:
        """Refuses the inline comment script shape."""
        self._check_parser_body("bash -c '\n# note\n<cfg>\n'\n", refused=True)

    def test_refuses_parser_error_configure(self) -> None:
        """Refuses the error configure script shape."""
        self._check_parser_body('echo "${x:-(}" ; <cfg> "${y:-(}"\n', refused=True)

    def test_refuses_parser_error_shell(self) -> None:
        """Refuses the error shell script shape."""
        self._check_parser_body("case x in [a]\\(*) bash inner.sh ;; esac\n", refused=True)

    def test_refuses_parser_candidate_before_cd(self) -> None:
        """Refuses the candidate before cd script shape."""
        self._check_parser_body("bash inner.sh\ncd /tmp\n", refused=True)

    def test_refuses_parser_inline_limit(self) -> None:
        """Refuses the inline limit script shape."""
        self._check_parser_body(
            "eval :\n" * 300, refused=True, reason="more than 256 inline shell bodies"
        )

    def test_refuses_parser_inline_limit_hidden_configure(self) -> None:
        """Refuses the inline limit hidden configure script shape."""
        self._check_parser_body("eval :\n" * 300 + "eval '<cfg>'\n", refused=True)

    def test_refuses_missing_body_parser(self) -> None:
        """Refuse a readable clean script when the parser dependency is unavailable."""

        script = self.root / "missing_parser.sh"
        script.write_text("echo clean\n", encoding="utf-8")
        context = InspectionContext(self.root, False, [0], classify_script_words)
        reason = "shell script parser unavailable: install --require-hashes -r requirements-dev.txt"
        with patch("scripts.raw_configure_body.bash_parser", return_value=reason):
            result = inspect_script_file(str(script), context, is_program=False)
        self.assertIsInstance(result, Path)
        self.assertIn("requirements-dev.txt", str(result))

    def test_refuses_body_node_limit(self) -> None:
        """Refuse clean syntax exceeding the shared syntax node inspection limit."""

        script = self.root / "many_nodes.sh"
        script.write_text("echo clean\n" * 200, encoding="utf-8")
        context = InspectionContext(self.root, False, [0], classify_script_words)
        with patch("scripts.raw_configure_body.MAX_BODY_NODES", 64):
            result = inspect_script_file(str(script), context, is_program=False)
        self.assertIsInstance(result, Path)
        self.assertIn("shell syntax tree larger than inspect limit", str(result))

    def test_refuses_nested_candidate_overflow(self) -> None:
        """Refuse more distinct nested operands than the per-file inspection bound."""
        body = "".join(f"bash missing_{i}.sh\n" for i in range(MAX_SCRIPT_FILES + 2))
        self._check_parser_body(body, refused=True, reason="more nested scripts than inspect limit")

    def test_refuses_unfinished_nested_worklist(self) -> None:
        """Refuse an unfinished worklist even when all inspected operands are missing."""
        body = "".join(f"bash missing_{i}.sh\n" for i in range(MAX_SCRIPT_FILES + 1))
        self._check_parser_body(body, refused=True, reason="more nested scripts than inspect limit")

    def test_allows_duplicate_nested_candidates(self) -> None:
        """Deduplicate repeated missing file operands before applying the file bound."""
        self._check_parser_body("bash missing.sh\n" * 300, refused=False)

    def test_allows_nested_worklist_boundary(self) -> None:
        """Allow the largest missing-candidate worklist that can be fully inspected."""
        body = "".join(f"bash missing_{i}.sh\n" for i in range(MAX_SCRIPT_FILES))
        self._check_parser_body(body, refused=False)

    def test_refuses_nodes_shared_across_files(self) -> None:
        """Apply one syntax node budget across an outer script and its nested file."""
        script = self.root / "outer_nodes.sh"
        script.write_text("echo clean\n" * 8 + "bash inner.sh\n", encoding="utf-8")
        (self.root / "inner.sh").write_text("echo clean\n" * 8, encoding="utf-8")
        context = InspectionContext(self.root, False, [0], classify_script_words)
        with patch("scripts.raw_configure_body.MAX_BODY_NODES", 64):
            result = inspect_script_file(str(script), context, is_program=False)
        self.assertIsInstance(result, Path)
        self.assertIn("shell syntax tree larger than inspect limit", str(result))

    def test_refuses_inline_bodies_shared_across_files(self) -> None:
        """Apply one inline body budget across distinct nested script files."""
        script = self.root / "outer_inline.sh"
        script.write_text("bash first.sh\nbash second.sh\n", encoding="utf-8")
        names = ("first.sh", "second.sh")
        for i in range(len(names)):
            (self.root / names[i]).write_text("eval :\n" * 150, encoding="utf-8")
        self._assert_refused(((f"bash {script}", self.root),))

    def test_refuses_source_process_argument(self) -> None:
        """Refuse configure-bearing text supplied to source through process substitution."""
        self._check_parser_body("source <(echo '<cfg>')\n", refused=True)

    def test_refuses_shell_process_input(self) -> None:
        """Refuse configure-bearing text supplied through a shell input redirection."""
        self._check_parser_body("bash < <(echo '<cfg>')\n", refused=True)

    def test_refuses_shell_input_file(self) -> None:
        """Inspect a shell script selected through a plain input file redirection."""
        self._check_parser_body("bash < inner.sh\n", refused=True)

    def test_refuses_brace_configure_command(self) -> None:
        """Refuse a command whose brace expansion can name configure."""
        self._check_parser_body("./co{n,}figure --x\n", refused=True)

    def test_refuses_source_option_shaped_file(self) -> None:
        """Treat source operands as filenames even when they look like shell options."""
        (self.root / "-c").write_text(f'"{self.tree}/configure" --x\n', encoding="utf-8")
        self._check_parser_body("source -c\n", refused=True)


class ReviewRoundFilesTest(RawConfigureGuardBase):
    """Exercise r9 program classification and nested path resolution regressions."""

    _assert_allowed = RawConfigureFilesTest._assert_allowed
    _check_parser_body = RawConfigureFilesTest._check_parser_body

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

    _assert_allowed = RawConfigureFilesTest._assert_allowed
    _check_parser_body = RawConfigureFilesTest._check_parser_body

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
        for shell in ("ash", "mksh", "lksh", "posh", "yash", "hush", "busybox ash"):
            with self.subTest(shell=shell):
                self._check_parser_body(f"{shell} inner.sh\n", refused=True)

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

    _assert_allowed = RawConfigureFilesTest._assert_allowed
    _check_parser_body = RawConfigureFilesTest._check_parser_body

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
