# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Bind Semgrep suppressions to explicit rule IDs, adjacent code and expiring reasons."""

from __future__ import annotations

import io
import re
import subprocess  # nosec B404 - fixed local Git listing, no shell, explicit timeout.
import tokenize
import unittest
from bisect import bisect_right
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tests.helpers import isolated_git_environment

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 14)
REGISTRY_ID = "python.lang.security.audit.example"
SUPPRESSION_TOKEN = re.compile(r"\bnosem(?:grep)?\b", re.IGNORECASE)
SUPPRESSION_FORM = re.compile(
    r"(?:#|//) nosemgrep: (?P<ids>[^\s,#]+(?:, [^\s,#]+)*)(?:  # noqa: E501)?"
)
REASON_TOKEN = re.compile(r"^(?:#|//)\s*semgrep-reason\b")
REASON_FORM = re.compile(
    r"(?P<marker>#|//) semgrep-reason \(expires (?P<expiry>[^)]*)\): (?P<text>.*)"
)
TEXT_TOKENS = re.compile(
    r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\''
    r'|"(?:\\[\s\S]|[^"\\\n])*"|\'(?:\\[\s\S]|[^\'\\\n])*\''
    r"|`(?:\\[\s\S]|[^`\\])*`|/\*[\s\S]*?\*/"
    r"|(?P<comment>\#[^\n\r]*|//[^\n\r]*)"
)


def local_rule_ids(source: str) -> set[str]:
    """Read repository rule IDs with safe YAML or the minimal dependency-free fallback."""

    if yaml is None:
        return set(re.findall(r"^\s*-\s*id:\s*(\S+)", source, re.MULTILINE))
    document = yaml.safe_load(source)
    return {rule["id"] for rule in document.get("rules", [])}


def tracked_texts(root: Path) -> list[tuple[str, str]]:
    """Read every tracked UTF-8 file without changing the caller's Git configuration."""

    tracked = subprocess.run(  # nosec B603 B607 - fixed local Git argv, no shell or network.
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=30,
        env=isolated_git_environment(),
    )
    sources = []
    for name in tracked.stdout.split(b"\0"):
        if not name:
            continue
        path = root / name.decode("utf-8", errors="surrogateescape")
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        sources.append((str(path.relative_to(root)), source))
    return sources


def comment_tokens(source: str, filename: str) -> dict[int, tuple[int, str]]:
    """Locate hash and slash comment tokens while excluding quoted string contents."""

    if filename.endswith((".py", ".pyi")) or re.match(r"^#![^\n]*\bpython", source):
        return {
            token.start[0]: (token.start[1], token.string)
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        }
    starts = [0, *(match.end() for match in re.finditer("\n", source))]
    comments = {}
    for token in TEXT_TOKENS.finditer(source):
        if token.group("comment") is not None:
            line = bisect_right(starts, token.start())
            comments[line] = (token.start() - starts[line - 1], token.group())
    return comments


def suppression_errors(comment: str, local_ids: set[str]) -> list[str]:
    """Enforce the canonical suppression form and resolve each listed rule ID."""

    form = SUPPRESSION_FORM.fullmatch(comment)
    if form is None:
        return ["R1 expected nosemgrep: <id>[, <id>...] with optional E501 noqa"]
    return [
        f"R2 unknown short rule ID {rule_id}"
        for rule_id in form["ids"].split(", ")
        if "." not in rule_id and rule_id not in local_ids
    ]


def expiry_errors(expiry: str, today: date) -> list[str]:
    """Reject invalid calendar dates and reasons whose expiry has already passed."""

    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry) is None:
            raise ValueError("expected YYYY-MM-DD")
        expires = date.fromisoformat(expiry)
    except ValueError:
        return [f"R5 invalid expiry date {expiry!r}"]
    return [f"R5 expired on {expiry}"] if expires < today else []


def binding_errors(
    lines: list[str], comments: dict[int, tuple[int, str]], line: int, today: date
) -> list[str]:
    """Require a standalone suppression between its reviewed reason and immediate code."""

    column, comment = comments[line]
    following = lines[line] if line < len(lines) else ""
    next_column, _ = comments.get(line + 1, (len(following), ""))
    errors = []
    if (
        lines[line - 1][:column].strip()
        or not following.strip()
        or not following[:next_column].strip()
        or following.lstrip().startswith("/*")
    ):
        errors.append("R3 misplaced suppression: standalone line immediately before code required")
    previous_column, previous = comments.get(line - 1, (0, ""))
    reason = REASON_FORM.fullmatch(previous)
    if (
        reason is None
        or not reason["text"].strip()
        or lines[line - 2][:previous_column].strip()
        or not comment.startswith(reason["marker"] + " ")
    ):
        return [*errors, "R4 missing or blank adjacent semgrep-reason"]
    return [*errors, *expiry_errors(reason["expiry"], today)]


def source_rows(source: str) -> list[str]:
    """Split only at token-counted newlines, removing the CR in CRLF endings."""

    return [row.removesuffix("\r") for row in source.split("\n")]


def find_nosemgrep_offenders(
    source: str,
    local_ids: set[str],
    *,
    filename: str = "planted.py",
    today: date | None = None,
) -> list[str]:
    """Return path:line:rule diagnostics for malformed or unbound suppressions."""

    current_date = date.today() if today is None else today
    lines = source_rows(source)
    comments = comment_tokens(source, filename)
    suppressions = {
        line: comment
        for line, (_, comment) in comments.items()
        if SUPPRESSION_TOKEN.search(comment) and not REASON_TOKEN.match(comment)
    }
    offenders = [
        f"{filename}:{line}: R6 orphan semgrep-reason"
        for line, (_, comment) in comments.items()
        if REASON_TOKEN.match(comment) and line + 1 not in suppressions
    ]
    for line, comment in suppressions.items():
        offenders.extend(
            f"{filename}:{line}: {error}"
            for error in (
                *suppression_errors(comment, local_ids),
                *binding_errors(lines, comments, line, current_date),
            )
        )
    return offenders


def bound_source(
    *, marker: str = "#", rule_id: str = REGISTRY_ID, expires: str = "2026-11-14"
) -> str:
    """Build a reviewed suppression followed immediately by a code statement."""

    return (
        f"{marker} semgrep-reason (expires {expires}): fixed test input\n"
        f"{marker} nosemgrep: {rule_id}\n"
        "run()\n"
    )


class NosemgrepBindingTest(unittest.TestCase):
    """Exercise every refusal rule with planted text and the tracked repository."""

    def assert_rule(self, source: str, rule: str, *, filename: str = "planted.py") -> None:
        """Require a planted defect to produce its rule and a located diagnostic."""

        offenders = find_nosemgrep_offenders(source, set(), filename=filename, today=TODAY)
        self.assertTrue(any(f": {rule} " in item for item in offenders), offenders)
        for offender in offenders:
            self.assertRegex(offender, rf"^{re.escape(filename)}:\d+: R[1-6] ")

    def test_bare_and_noncanonical_suppressions(self) -> None:
        """Bare aliases, uppercase tokens and noncanonical forms cannot hide rules."""

        for comment in (
            "# nosemgrep",
            "# nosem",
            "# NOSEMGREP",
            "# NoSeM",
            f"# nosem: {REGISTRY_ID}",
            f"# nosemgrep: {REGISTRY_ID},",
            f"# nosemgrep: {REGISTRY_ID} trailing explanation",
        ):
            with self.subTest(comment=comment):
                self.assert_rule(f"{comment}\nrun()\n", "R1")

    def test_missing_and_blank_reasons(self) -> None:
        """A named rule still needs a nonblank reason directly above its suppression."""

        for prefix in ("", "# semgrep-reason (expires 2026-11-14):   \n"):
            with self.subTest(prefix=prefix):
                self.assert_rule(prefix + f"# nosemgrep: {REGISTRY_ID}\nrun()\n", "R4")

    def test_unknown_short_id(self) -> None:
        """An unregistered short ID cannot stand in for a registry rule ID."""

        self.assert_rule(bound_source(rule_id="foo-bar"), "R2")

    def test_defined_short_id(self) -> None:
        """A short ID defined in the actual repository configuration remains valid."""

        ids = local_rule_ids((ROOT / ".semgrep.yml").read_text(encoding="utf-8"))
        self.assertIn("subprocess-shell-true", ids)
        self.assertEqual(
            find_nosemgrep_offenders(
                bound_source(rule_id="subprocess-shell-true"), ids, today=TODAY
            ),
            [],
        )

    def test_rule_id_fallback(self) -> None:
        """The dependency-free reader finds only rule ID declarations."""

        with patch(f"{__name__}.yaml", None):
            self.assertEqual(
                local_rule_ids("rules:\n  - id: local-one\n    message: id: fake\n"),
                {"local-one"},
            )

    def test_misplaced_suppressions(self) -> None:
        """Trailing suppressions and gaps, comments or EOF before code are refused."""

        source = bound_source()
        variants = (
            source.replace("# nosemgrep:", "run()  # nosemgrep:"),
            source.replace("\nrun()", "\n\nrun()"),
            source.replace("\nrun()", "\n# unrelated comment\nrun()"),
            source.replace("run()\n", ""),
        )
        for planted in variants:
            with self.subTest(source=planted):
                self.assert_rule(planted, "R3")

    def test_form_feeds_preserve_valid_binding(self) -> None:
        """Form feeds before a bound block cannot create misplaced suppressions."""

        for filename, marker in (("planted.py", "#"), ("planted.txt", "//")):
            for newline in ("\n", "\r\n"):
                with self.subTest(filename=filename, newline=newline):
                    source = "x = 1\f\f\f\n" + bound_source(marker=marker)
                    self.assertEqual(
                        find_nosemgrep_offenders(
                            source.replace("\n", newline), set(), filename=filename, today=TODAY
                        ),
                        [],
                    )

    def test_form_feed_preserves_misplaced_suppression(self) -> None:
        """A form feed cannot hide a blank line between suppression and code."""

        for filename, marker in (("planted.py", "#"), ("planted.txt", "//")):
            for newline in ("\n", "\r\n"):
                with self.subTest(filename=filename, newline=newline):
                    source = "x = 1\f\n" + bound_source(marker=marker)
                    source = source.replace("\nrun()", "\n\nrun()")
                    self.assertEqual(
                        find_nosemgrep_offenders(
                            source.replace("\n", newline), set(), filename=filename, today=TODAY
                        ),
                        [
                            f"{filename}:3: R3 misplaced suppression: "
                            "standalone line immediately before code required"
                        ],
                    )

    def test_unicode_separator_preserves_valid_binding(self) -> None:
        """Unicode line separators before a bound block cannot shift comment rows."""

        for filename, marker in (("planted.py", "#"), ("planted.txt", "//")):
            for newline in ("\n", "\r\n"):
                with self.subTest(filename=filename, newline=newline):
                    source = "x = 1\u2028\u2028\u2028\n" + bound_source(marker=marker)
                    self.assertEqual(
                        find_nosemgrep_offenders(
                            source.replace("\n", newline), set(), filename=filename, today=TODAY
                        ),
                        [],
                    )

    def test_expired_reason(self) -> None:
        """Yesterday's reason is expired against the caller's fixed date."""

        source = bound_source(expires="2026-11-13")
        self.assert_rule(source, "R5")
        self.assertIn("expired", "\n".join(find_nosemgrep_offenders(source, set(), today=TODAY)))

    def test_same_day_expiry(self) -> None:
        """A reason remains valid through its stated expiry date."""

        self.assertEqual(find_nosemgrep_offenders(bound_source(), set(), today=TODAY), [])

    def test_invalid_expiry(self) -> None:
        """An impossible month is rejected as an invalid date."""

        self.assert_rule(bound_source(expires="2026-13-01"), "R5")

    def test_orphan_reason(self) -> None:
        """A reason must directly precede a suppression instead of plain code or a gap."""

        for following in ("run()\n", "\n# nosemgrep: rule.id\nrun()\n", ""):
            with self.subTest(following=following):
                self.assert_rule(
                    "# semgrep-reason (expires 2026-11-14): reviewed\n" + following, "R6"
                )

    def test_reason_label_is_not_suppression(self) -> None:
        """The reason label alone is an orphan reason, never a suppression token."""

        offenders = find_nosemgrep_offenders("# semgrep-reason\n", set(), today=TODAY)
        self.assertEqual(offenders, ["planted.py:1: R6 orphan semgrep-reason"])

    def test_string_literals_are_not_suppressions(self) -> None:
        """Python strings, including multiline strings, cannot masquerade as comments."""

        source = 'value = "# nosemgrep"\nother = "// NOSEM"\ntext = """\n# nosem\n"""\n'
        self.assertEqual(find_nosemgrep_offenders(source, set(), today=TODAY), [])

    def test_comment_forms_across_languages(self) -> None:
        """Hash and slash comments are checked in all tracked text language families."""

        for filename, marker in (
            ("test.sh", "#"),
            ("test.yaml", "#"),
            ("test.c", "//"),
            ("test.txt", "//"),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(
                    find_nosemgrep_offenders(
                        bound_source(marker=marker), set(), filename=filename, today=TODAY
                    ),
                    [],
                )
                self.assert_rule(f"{marker} NOSEM\nrun()\n", "R1", filename=filename)

    def test_multiple_ids_and_noqa(self) -> None:
        """Comma-separated IDs and the sole allowed noqa suffix preserve the binding."""

        source = bound_source(rule_id=f"{REGISTRY_ID}, local-one")
        source = source.replace("local-one\n", "local-one  # noqa: E501\n")
        self.assertEqual(find_nosemgrep_offenders(source, {"local-one"}, today=TODAY), [])
        self.assert_rule(source.replace("local-one", "unknown"), "R2")
        self.assert_rule(source.replace("E501", "E999"), "R1")

    def test_real_upstream_lint(self) -> None:
        """The existing reviewed suppression is valid on the task's reference date."""

        source = (ROOT / "scripts/upstream_lint.py").read_text(encoding="utf-8")
        self.assertEqual(
            find_nosemgrep_offenders(
                source, set(), filename="scripts/upstream_lint.py", today=date(2026, 11, 4)
            ),
            [],
        )

    def test_real_file_without_reason(self) -> None:
        """Removing only the real reason yields exactly one missing-reason offender."""

        source = (ROOT / "scripts/upstream_lint.py").read_text(encoding="utf-8")
        planted = re.sub(r"^.*# semgrep-reason[^\n]*\n", "", source, flags=re.MULTILINE)
        offenders = find_nosemgrep_offenders(
            planted, set(), filename="scripts/upstream_lint.py", today=date(2026, 11, 4)
        )
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn(": R4 ", offenders[0])

    def test_real_file_without_ids(self) -> None:
        """Removing the real IDs leaves a detectable bare suppression."""

        source = (ROOT / "scripts/upstream_lint.py").read_text(encoding="utf-8")
        planted = re.sub(r"(# nosemgrep):[^\n]*", r"\1", source)
        self.assert_rule(planted, "R1")

    def test_repository_suppressions_have_bindings(self) -> None:
        """Every tracked UTF-8 file must have fully bound, unexpired suppressions."""

        ids = local_rule_ids((ROOT / ".semgrep.yml").read_text(encoding="utf-8"))
        offenders = [
            offender
            for filename, source in tracked_texts(ROOT)
            for offender in find_nosemgrep_offenders(
                source, ids, filename=filename, today=date.today()
            )
        ]
        self.assertEqual(offenders, [], "invalid Semgrep suppression:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
