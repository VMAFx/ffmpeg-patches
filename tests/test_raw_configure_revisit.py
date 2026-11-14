# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise script candidate deduplication and self-invoking recursion guards."""

from __future__ import annotations

from scripts.raw_configure_body import InspectionContext
from scripts.raw_configure_files import MAX_SCRIPT_FILES, inspect_sourced_file
from scripts.raw_configure_inspect import classify_script_words
from tests.raw_configure_support import RawConfigureGuardBase


class RawConfigureRevisitTest(RawConfigureGuardBase):
    """Exercise script revisit deduplication and raw configure detection."""

    def test_allows_script_that_runs_itself(self) -> None:
        """Allow a script file that recursively invokes itself directly."""
        self_sh = self.root / "self.sh"
        self_sh.write_text(f"bash {self_sh}\necho done\n", encoding="utf-8")
        self._assert_allowed(((f"bash {self_sh}", self.root),))

    def test_allows_script_that_runs_itself_through_its_directory(self) -> None:
        """Allow a script that invokes itself through its resolved directory path."""
        self_dir = self.root / "self_dir.sh"
        self_dir.write_text(
            'HERE=$(dirname "$(readlink -f "$0")")\nbash "$HERE/self_dir.sh"\necho done\n',
            encoding="utf-8",
        )
        self._assert_allowed(((f"bash {self_dir}", self.root),))

    def test_allows_two_scripts_that_run_each_other(self) -> None:
        """Allow two script files that mutually invoke each other."""
        a_sh = self.root / "a.sh"
        b_sh = self.root / "b.sh"
        a_sh.write_text(f"bash {b_sh}\n", encoding="utf-8")
        b_sh.write_text(f"bash {a_sh}\n", encoding="utf-8")
        self._assert_allowed(((f"bash {a_sh}", self.root),))

    def test_refuses_script_that_runs_itself_and_configure(self) -> None:
        """Refuse a self-invoking script that directly invokes raw configure."""
        self_cfg = self.root / "self_cfg.sh"
        self_cfg.write_text(
            f'bash {self_cfg}\n"{self.tree}/configure" --enable-gpl\n',
            encoding="utf-8",
        )
        self._assert_refused(((f"bash {self_cfg}", self.root),))

    def test_refuses_program_reread_as_shell_script(self) -> None:
        """Refuse execution when a non-shell program is re-read as a shell script."""
        tool = self.root / "tool"
        tool.write_text(
            f'#!/usr/bin/perl\n"{self.tree}/configure" --enable-gpl\n',
            encoding="utf-8",
        )
        tool.chmod(0o755)
        driver = self.root / "driver.sh"
        driver.write_text(f"{tool}\nbash {tool}\n", encoding="utf-8")
        self._assert_refused(((f"bash {driver}", self.root),))

    def test_refuses_real_script_after_missing_alias(self) -> None:
        """Refuse a real configure script following an unresolved missing-parent alias."""
        payload = self.root / "payload.sh"
        payload.write_text(f'"{self.tree}/configure" --enable-gpl\n', encoding="utf-8")
        driver = self.root / "driver.sh"
        driver.write_text(
            f"bash {self.root}/missing/../payload.sh\nbash {payload}\n",
            encoding="utf-8",
        )
        self._assert_refused(((f"bash {driver}", self.root),))

    def test_reads_duplicate_sourced_operands_once(self) -> None:
        """Read a helper script once when a sourced file names it under duplicate spellings."""
        helper = self.root / "helper.sh"
        helper.write_text("true\n", encoding="utf-8")
        sourced = self.root / "sourced.sh"
        sourced.write_text(
            f"bash {helper}\nbash {self.root}/./helper.sh\nbash {self.build}/../helper.sh\n",
            encoding="utf-8",
        )
        self._assert_allowed(((f". {sourced}", self.root),))
        files_read = [0]
        refusal, _ = inspect_sourced_file(
            str(sourced),
            InspectionContext(self.root, False, files_read, classify_script_words),
        )
        self.assertIsNone(refusal)
        self.assertEqual(files_read, [2])

    def test_inspects_script_again_after_directory_change(self) -> None:
        """Re-inspect a script after cd when its relative operand changes verdict."""
        leaf = self.build / "leaf.sh"
        leaf.write_text(f'"{self.tree}/configure" --enable-gpl\n', encoding="utf-8")
        helper = self.root / "helper.sh"
        helper.write_text("bash leaf.sh\n", encoding="utf-8")
        driver = self.root / "driver.sh"
        driver.write_text(
            f'bash {helper}\ncd "{self.build}"\nbash ../helper.sh\n',
            encoding="utf-8",
        )
        self._assert_refused(((f"bash {driver}", self.root),))

    def test_refuses_more_distinct_scripts_than_file_limit(self) -> None:
        """Allow MAX_SCRIPT_FILES - 1 distinct helpers and refuse MAX_SCRIPT_FILES."""
        helpers = [self.root / f"helper_{idx}.sh" for idx in range(MAX_SCRIPT_FILES)]
        for helper in helpers:
            helper.write_text("true\n", encoding="utf-8")
        for count, refused in ((MAX_SCRIPT_FILES - 1, False), (MAX_SCRIPT_FILES, True)):
            with self.subTest(count=count):
                driver = self.root / f"driver_{count}.sh"
                body = "".join(f"bash {helper}\n" for helper in helpers[:count])
                driver.write_text(body, encoding="utf-8")
                if refused:
                    result = self.run_guard(
                        "claude", self.payload("claude", f"bash {driver}", self.root)
                    )
                    self.assertIn("configure (script file limit)", result.stderr)
                    self._assert_refused(((f"bash {driver}", self.root),))
                else:
                    self._assert_allowed(((f"bash {driver}", self.root),))
