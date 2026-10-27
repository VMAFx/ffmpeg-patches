# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/release_guard.py and for the release workflows that call it."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import release_guard  # noqa: E402

from tests.helpers import capture_main  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"


def guard(*argv: str) -> tuple[int, str, str]:
    """Run main() and return the exit status, stdout and stderr."""
    return capture_main(release_guard.main, *argv)


class TagTest(unittest.TestCase):
    def test_final_and_candidate_tags_are_classified(self) -> None:
        self.assertEqual(
            guard("tag", "tag", "v1.2.3", SHA), (0, "version=v1.2.3\nprerelease=false\n", "")
        )
        self.assertEqual(
            guard("tag", "tag", "v0.10.0-rc.12", SHA),
            (0, "version=v0.10.0-rc.12\nprerelease=true\n", ""),
        )

    def test_malformed_tags_are_refused(self) -> None:
        for tag in (
            "v1.2",
            "1.2.3",
            "v01.2.3",
            "v1.2.3-rc.0",
            "v1.2.3-rc1",
            "v1.2.3-beta.1",
            "v1.2.3 ",
            "v1.2.3-rc.1-rc.2",
            "v1.2.3\nprerelease=false",
        ):
            with self.subTest(tag=tag):
                status, out, err = guard("tag", "tag", tag, SHA)
                self.assertEqual((status, out), (1, ""))
                self.assertIn("neither vX.Y.Z nor vX.Y.Z-rc.N", err)

    def test_manual_run_has_a_run_version_and_no_prerelease(self) -> None:
        self.assertEqual(guard("tag", "branch", "main", SHA), (0, "version=run-0123456789ab\n", ""))


class PublishTest(unittest.TestCase):
    def test_matching_build_passes(self) -> None:
        self.assertEqual(guard("publish", "v1.2.3", "v1.2.3", "false")[0], 0)
        self.assertEqual(guard("publish", "v1.2.3-rc.1", "v1.2.3-rc.1", "true")[0], 0)

    def test_version_other_than_the_tag_is_refused(self) -> None:
        status, _, err = guard("publish", "v1.2.4", "v1.2.3", "false")
        self.assertEqual(status, 1)
        self.assertIn("release build named v1.2.4, the tag is v1.2.3", err)

    def test_missing_or_unknown_classification_is_refused(self) -> None:
        for value in ("", "True", "yes"):
            with self.subTest(value=value):
                status, _, err = guard("publish", "v1.2.3", "v1.2.3", value)
                self.assertEqual(status, 1)
                self.assertIn("did not classify", err)

    def test_wrong_prerelease_flag_is_refused(self) -> None:
        for tag, value in (("v1.2.3", "true"), ("v1.2.3-rc.1", "false")):
            with self.subTest(tag=tag):
                status, _, err = guard("publish", tag, tag, value)
                self.assertEqual(status, 1)
                self.assertIn(f"classified {tag} as prerelease={value}", err)

    def test_published_release_must_match(self) -> None:
        self.assertEqual(guard("published", "true", "true")[0], 0)
        for expected, actual in (("true", "false"), ("false", "true"), ("false", "")):
            with self.subTest(expected=expected, actual=actual):
                status, _, err = guard("published", expected, actual)
                self.assertEqual(status, 1)
                self.assertIn(f"isPrerelease={actual}, expected {expected}", err)


class ReleaseFiles:
    """A dist directory with two files and their SHA256SUMS."""

    def __init__(self, scratch: Path) -> None:
        self.dist = scratch / "dist"
        self.dist.mkdir()
        lines = []
        for name, data in (("a.tar.gz", b"tarball\n"), ("a.spdx.json", b"{}\n")):
            (self.dist / name).write_bytes(data)
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        self.sums = self.dist / "SHA256SUMS"
        self.sums.write_text("".join(lines), encoding="utf-8")


class ChecksumsTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.files = ReleaseFiles(Path(scratch.name))

    def test_unchanged_files_pass(self) -> None:
        self.assertEqual(
            guard("checksums", str(self.files.sums)), (0, "a.tar.gz: OK\na.spdx.json: OK\n", "")
        )

    def test_changed_file_is_refused(self) -> None:
        (self.files.dist / "a.spdx.json").write_bytes(b"{ }\n")
        status, _, err = guard("checksums", str(self.files.sums))
        self.assertEqual(status, 1)
        self.assertIn("a.spdx.json: checksum does not match SHA256SUMS", err)

    def test_missing_file_is_refused(self) -> None:
        (self.files.dist / "a.tar.gz").unlink()
        self.assertEqual(guard("checksums", str(self.files.sums))[0], 1)

    def test_malformed_or_empty_sums_are_refused(self) -> None:
        for text in ("", "not a checksum line\n", "abc  a.tar.gz\n"):
            with self.subTest(text=text):
                self.files.sums.write_text(text, encoding="utf-8")
                self.assertEqual(guard("checksums", str(self.files.sums))[0], 1)

    def test_missing_sums_is_a_tool_error(self) -> None:
        self.assertEqual(guard("checksums", str(self.files.dist / "NONE"))[0], 2)


class SignerTest(unittest.TestCase):
    def test_keyless_identity_is_release_build_at_the_ref(self) -> None:
        args = release_guard.parse_args(
            ["verify", "S", "S.json", "--workflow-of", "VMAFx/ffmpeg-patches", "refs/tags/v1.0.0"]
        )
        self.assertEqual(
            release_guard.cosign_arguments(args),
            [
                "cosign", "verify-blob", "--bundle", "S.json",
                "--certificate-identity",
                (
                    "https://github.com/VMAFx/ffmpeg-patches/.github/workflows/release-build.yml"
                    "@refs/tags/v1.0.0"
                ),
                "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
                "S",
            ],
        )  # fmt: skip

    def test_verify_needs_exactly_one_signer(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            for extra in ([], ["--test-key", "k.pub", "--workflow-of", "o/r", "refs/tags/v1"]):
                with self.subTest(extra=extra), self.assertRaises(SystemExit):
                    release_guard.parse_args(["verify", "S", "S.json", *extra])


COSIGN = shutil.which("cosign")


@unittest.skipIf(
    COSIGN is None and not os.environ.get("CI"),
    "cosign is not installed; CI installs it and runs these tests",
)
class CosignTest(unittest.TestCase):
    """Sign with a throwaway key and verify offline: no transparency log is contacted."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.files = ReleaseFiles(self.scratch)
        self.bundle = self.files.dist / "SHA256SUMS.sigstore.json"
        self.cosign("generate-key-pair", "--output-key-prefix", "signer")
        self.cosign("generate-key-pair", "--output-key-prefix", "other")
        self.cosign(
            "sign-blob", "--yes", "--key", "signer.key", "--bundle", str(self.bundle),
            "--use-signing-config=false", "--tlog-upload=false", str(self.files.sums),
        )  # fmt: skip

    def cosign(self, *args: str) -> None:
        subprocess.run(
            [COSIGN or "cosign", *args],
            cwd=self.scratch,
            env={**os.environ, "COSIGN_PASSWORD": ""},
            capture_output=True,
            check=True,
            timeout=120,
        )

    def verify(self, key: str) -> tuple[int, str]:
        argv = ["verify", str(self.files.sums), str(self.bundle)]
        status, out, err = guard(*argv, "--test-key", str(self.scratch / key))
        return status, out + err

    def test_signed_and_unchanged_files_pass(self) -> None:
        self.assertEqual(self.verify("signer.pub"), (0, "a.tar.gz: OK\na.spdx.json: OK\n"))

    def test_wrong_signer_is_refused(self) -> None:
        status, text = self.verify("other.pub")
        self.assertEqual(status, 1)
        self.assertIn("cosign verify-blob refused", text)

    def test_changed_sums_is_refused(self) -> None:
        with self.files.sums.open("a", encoding="utf-8") as sums:
            sums.write(f"{'0' * 64}  extra\n")
        self.assertEqual(self.verify("signer.pub")[0], 1)

    def test_changed_file_under_a_valid_signature_is_refused(self) -> None:
        (self.files.dist / "a.tar.gz").write_bytes(b"other tarball\n")
        status, text = self.verify("signer.pub")
        self.assertEqual(status, 1)
        self.assertIn("a.tar.gz: checksum does not match", text)


def step(workflow: str, name: str) -> str:
    """The step NAME in .github/workflows/WORKFLOW, shell line continuations joined."""
    lines = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"- name: {name}":
            indent = len(line) - len(line.lstrip())
            block = [line]
            for later in lines[index + 1 :]:
                if later.strip() and len(later) - len(later.lstrip()) <= indent:
                    break
                block.append(later)
            return re.sub(r"\\\n\s*", "", "\n".join(block))
    raise AssertionError(f"{workflow} has no step named {name!r}")


# A guard whose failure the step swallows reports green while the release is broken.
SWALLOWED = re.compile(r"continue-on-error|\|\|\s*(true|:|exit 0)\b|set \+e|; *true$|&& *true$")

GUARDS = {
    ("release-build.yml", "Release tag shape"): [
        r'python3 scripts/release_guard\.py tag "\$GITHUB_REF_TYPE" "\$GITHUB_REF_NAME"'
        r' "\$GITHUB_SHA" >> "\$GITHUB_OUTPUT"$',
    ],
    ("release-build.yml", "Verify the SHA256SUMS signature"): [
        (
            r"python3 \.\./scripts/release_guard\.py verify SHA256SUMS SHA256SUMS\.sigstore\.json"
            r' --workflow-of "\$GITHUB_REPOSITORY" "\$GITHUB_REF"$'
        ),
    ],
    ("release.yml", "Publish the GitHub release"): [
        r"python3 \.\./scripts/release_guard\.py checksums SHA256SUMS$",
        (
            r'python3 \.\./scripts/release_guard\.py publish "\$VERSION" "\$GITHUB_REF_NAME"'
            r' "\$PRERELEASE"$'
        ),
        r'python3 \.\./scripts/release_guard\.py published "\$PRERELEASE" "\$actual"$',
    ],
}


class WorkflowWiringTest(unittest.TestCase):
    """Each release guard step calls its script, and nothing ignores its exit status."""

    def test_every_guard_step_calls_its_script(self) -> None:
        for (workflow, name), calls in GUARDS.items():
            text = step(workflow, name)
            for call in calls:
                with self.subTest(workflow=workflow, call=call):
                    self.assertRegex(text, re.compile(r"^\s*" + call, re.MULTILINE))

    def test_no_guard_step_ignores_its_exit_status(self) -> None:
        for workflow, name in GUARDS:
            with self.subTest(workflow=workflow, step=name):
                self.assertIsNone(SWALLOWED.search(step(workflow, name)))

    def test_no_release_job_continues_on_error(self) -> None:
        for workflow in ("release.yml", "release-build.yml"):
            text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
            with self.subTest(workflow=workflow):
                self.assertNotIn("continue-on-error", text)

    def test_release_steps_run_bash_with_errexit(self) -> None:
        # GitHub runs `shell: bash` as bash -e -o pipefail, so a refusing
        # guard, also one whose output is redirected, fails the step.
        for workflow in ("release.yml", "release-build.yml"):
            text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
            with self.subTest(workflow=workflow):
                self.assertIn("defaults:\n  run:\n    shell: bash\n", text)
                self.assertNotRegex(text, r"\n\s+shell: (?!bash\n)")


if __name__ == "__main__":
    unittest.main()
