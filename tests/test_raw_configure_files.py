# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise script file inspection and path resolution for raw configure guard."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed git ls-files command for corpus discovery.
import sys
from pathlib import Path
from unittest.mock import patch

from scripts.raw_configure_body import InspectionContext
from scripts.raw_configure_files import (
    MAX_SCRIPT_BYTES,
    MAX_SCRIPT_FILES,
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
