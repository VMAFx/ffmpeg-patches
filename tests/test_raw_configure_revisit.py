# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Exercise script candidate deduplication and self-invoking recursion guards."""

from __future__ import annotations

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
