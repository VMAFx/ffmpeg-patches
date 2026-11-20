# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Keep the former GitHub organisation out of current owner positions."""

from __future__ import annotations

import re
import tempfile
import unittest
from itertools import chain
from pathlib import Path

from tests import helpers

ROOT = Path(__file__).resolve().parents[1]
OLD_OWNER = "VMAFx"
GENERATED = (
    "AGENTS.readonly.md",
    "CLAUDE.md",
    ".codex/rules.md",
    ".gemini/GEMINI.md",
    ".cursor/rules/hiss-invariants.mdc",
    ".github/copilot-instructions.md",
    ".windsurfrules",
)
RENAME_PHRASE = "moved from the VMAFx GitHub organisation"


def scan_owner_positions(root: Path) -> tuple[list[str], int]:
    """Return tracked old-owner file:line findings and the undecodable file count."""
    pattern = re.compile(
        rf"github\.com[/:]{OLD_OWNER}\b|ghcr\.io/{OLD_OWNER}\b|gh/{OLD_OWNER}\b|"
        rf"(?<![\w/.-]){OLD_OWNER}/[a-z0-9._-]+|owner: *{OLD_OWNER}\b|head={OLD_OWNER}:",
        re.IGNORECASE,
    )
    findings = {}
    skipped = 0
    for name in helpers.run_git(root, "ls-files", "-z").split("\0"):
        if not name:
            continue
        try:
            text = (root / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        findings[name] = [
            f"{name}:{number}: {line}"
            for number, line in enumerate(text.splitlines(), 1)
            if pattern.search(line)
            if name != "README.md" or RENAME_PHRASE not in line
        ]
    if not findings.get("AGENTS.md"):
        for name in GENERATED:
            findings.pop(name, None)
    return list(chain.from_iterable(findings.values())), skipped


class OrgOwnerTest(unittest.TestCase):
    """Exercise current owners, planted regressions and product-name boundaries."""

    def test_tracked_tree_has_no_old_owner(self) -> None:
        """Require the canonical harness and tracked tree to have no owner finding."""
        findings, _ = scan_owner_positions(ROOT)
        self.assertFalse(any(item.startswith("AGENTS.md:") for item in findings), findings)
        self.assertEqual(findings, [], "\n".join(findings))

    def test_planted_old_owners_are_reported(self) -> None:
        """Report exactly the planted GitHub URL and owner field with file and line."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            helpers.run_git(root, "init", "-q")
            url = f"https://github.com/{OLD_OWNER}/ffmpeg-patches"
            owner = f"owner: {OLD_OWNER}"
            (root / "url.txt").write_text(url + "\n", encoding="utf-8")
            (root / "owner.txt").write_text(owner + "\n", encoding="utf-8")
            helpers.run_git(root, "add", "url.txt", "owner.txt")
            self.assertEqual(
                scan_owner_positions(root),
                ([f"owner.txt:1: {owner}", f"url.txt:1: {url}"], 0),
            )

    def test_product_names_and_local_path_pass(self) -> None:
        """Preserve product identifiers, prose and a local checkout path."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            helpers.run_git(root, "init", "-q")
            (root / "products.txt").write_text(
                "VMAFX_REF\nvmafx_ref\nvmafx:FFMPEG_FIX_SERIES_TAG\n"
                "VMAFx and Pelorus\nvmafx consumer\n"
                f"/home/u/dev/{OLD_OWNER.lower()}/ffmpeg-patches\n",
                encoding="utf-8",
            )
            helpers.run_git(root, "add", "products.txt")
            self.assertEqual(scan_owner_positions(root), ([], 0))

    def test_mixed_case_owner_is_reported(self) -> None:
        """Match a mixed-case GitHub organisation exactly once."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            helpers.run_git(root, "init", "-q")
            mixed_owner = "vMaFx"
            line = f"github.com/{mixed_owner}/x"
            (root / "mixed.txt").write_text(line + "\n", encoding="utf-8")
            helpers.run_git(root, "add", "mixed.txt")
            self.assertEqual(scan_owner_positions(root), ([f"mixed.txt:1: {line}"], 0))

    def test_undecodable_files_are_counted(self) -> None:
        """Skip a non-UTF-8 tracked file and count it once."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            helpers.run_git(root, "init", "-q")
            (root / "binary.dat").write_bytes(bytes([255]))
            helpers.run_git(root, "add", "binary.dat")
            self.assertEqual(scan_owner_positions(root), ([], 1))

    def test_generated_copies_require_clean_canonical_harness(self) -> None:
        """Allow all seven generated copies only while AGENTS.md has no finding."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            helpers.run_git(root, "init", "-q")
            line = f"{OLD_OWNER}/ffmpeg-patches"
            for name in GENERATED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(line + "\n", encoding="utf-8")
            agents = root / "AGENTS.md"
            agents.write_text("vidiko/ffmpeg-patches\n", encoding="utf-8")
            helpers.run_git(root, "add", ".")
            self.assertEqual(scan_owner_positions(root), ([], 0))
            agents.write_text(line + "\n", encoding="utf-8")
            self.assertEqual(
                scan_owner_positions(root),
                ([f"{name}:1: {line}" for name in sorted((*GENERATED, "AGENTS.md"))], 0),
            )

    def test_rename_exception_is_one_readme_line(self) -> None:
        """Exempt the rename line only in README and retain other owner findings."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            helpers.run_git(root, "init", "-q")
            line = f"https://github.com/{OLD_OWNER}/ffmpeg-patches"
            note = f"The repository {RENAME_PHRASE} to vidiko; {line} redirects."
            (root / "README.md").write_text(note + "\n" + line + "\n", encoding="utf-8")
            (root / "other.md").write_text(note + "\n", encoding="utf-8")
            helpers.run_git(root, "add", ".")
            self.assertEqual(
                scan_owner_positions(root),
                ([f"README.md:2: {line}", f"other.md:1: {note}"], 0),
            )
