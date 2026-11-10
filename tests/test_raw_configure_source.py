# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Replay sourced shell files and parser setup failures without executing fixtures."""

from __future__ import annotations

import importlib
import json
import os
import subprocess  # nosec B404 - only Python imports and the fixed guard run.
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import raw_configure_body as body_module
from scripts.raw_configure_body import InspectionContext, bash_parser
from scripts.raw_configure_files import inspect_script_file
from scripts.raw_configure_inspect import classify_script_words
from tests.raw_configure_support import CONFIGS, GUARD, ROOT, RawConfigureGuardBase

BODY = {
    "dot-cd-then-bash": (". ./cdsub.sh\nbash inner.sh\n", "refuse"),
    "source-cd-then-bash": ("source ./cdsub.sh\nbash inner.sh\n", "refuse"),
    "dot-cd-then-exec": (". ./cdsub.sh\n./inner.sh\n", "refuse"),
    "dot-cd-in-func-then-bash": (". ./cdfunc.sh\ngo\nbash inner.sh\n", "refuse"),
    "control-dot-noop-then-bash": (". ./noop.sh\nbash inner.sh\n", "allow"),
    "control-direct-sub": ("bash sub/inner.sh\n", "refuse"),
    "control-cd-then-bash": ("cd sub\nbash inner.sh\n", "refuse"),
    "inline-func-cd-then-bash": ("go() { cd sub; }\ngo\nbash inner.sh\n", "refuse"),
    "dot-cd-then-abs-benign": (". ./cdsub.sh\nbash <root>/inner.sh\n", "allow"),
    "dot-pushd-then-bash": (". ./pushsub.sh\nbash inner.sh\n", "refuse"),
    "dot-var-path-cd-then-bash": ('F=./cdsub.sh\n. "$F"\nbash inner.sh\n', "refuse"),
    "dot-dashdash-cd-then-bash": (". -- ./cdsub.sh\nbash inner.sh\n", "refuse"),
    "dot-p-option": (". -p sub inner.sh\n", "refuse"),
    "dot-path-slashless": (". cfgpath.sh\n", "refuse"),
    "dot-slashless-cwd-noop": (". noop.sh\nbash inner.sh\n", "allow"),
    "dot-missing-then-bash": (". ./missing.sh\nbash inner.sh\n", "allow"),
    "dot-dynamic-then-bash": ('. "$UNSET_DIR/lib.sh"\nbash inner.sh\n', "refuse"),
    "dot-dynamic-then-abs": ('. "$UNSET_DIR/lib.sh"\nbash <root>/inner.sh\n', "allow"),
    "dot-self-loop": (". ./outer.sh\n", "refuse"),
    "dot-chain-cd-then-bash": (". ./chain.sh\nbash inner.sh\n", "refuse"),
    "dot-sourced-configure": (". ./sub/inner.sh\n", "refuse"),
    "bash-then-dot-cd": ("bash inner.sh\n. ./cdsub.sh\n", "allow"),
}
TOP = {
    "top-dot-cd-then-bash": (". ./cdsub.sh && bash inner.sh", "refuse"),
    "top-source-cd-then-bash": ("source ./cdsub.sh && bash inner.sh", "refuse"),
    "top-dot-noop-then-bash": (". ./noop.sh && bash inner.sh", "allow"),
    "top-dot-cd-then-abs-benign": (". ./cdsub.sh && bash <root>/inner.sh", "allow"),
    "top-dot-cd-subst": (". ./cdsub.sh; x=$(bash inner.sh)", "refuse"),
    "top-dot-p": (". -p sub inner.sh", "refuse"),
    "top-dot-path": (". cfgpath.sh", "refuse"),
    "top-activate-then-bash": (". ./activate && bash inner.sh", "allow"),
    "top-dot-chain-cd": (". ./chain.sh && bash inner.sh", "refuse"),
    "top-dot-pushd": (". ./pushsub.sh && bash inner.sh", "refuse"),
    "top-bash-then-dot-cd": ("bash inner.sh && . ./cdsub.sh", "allow"),
    "top-dot-sourced-configure": (". ./sub/inner.sh", "refuse"),
}
UNKNOWN_SOURCE = "relative script path after a source the guard cannot read; use an absolute path"
AMBIGUITY_BODY = {
    "f1a-src-then-caller-assign": ('. ./setA.sh\nA=inner.sh\nbash "$A"\n', "refuse"),
    "f1b-caller-assign-then-src": ('A=inner.sh\n. ./setA.sh\nbash "$A"\n', "refuse"),
    "f1c-src-benign-then-caller-cfg": ('. ./setB.sh\nA=sub/inner.sh\nbash "$A"\n', "refuse"),
    "f1f-caller-twice-then-src": ('A=x\nA=y\n. ./setB.sh\nbash "$A"\n', "refuse"),
    "f1g-same-value-both": ('A=inner.sh\n. ./setB.sh\nbash "$A"\n', "allow"),
    "f1h-src-only-cfg": ('. ./setA.sh\nbash "$A"\n', "refuse"),
    "f1i-two-src-assign": ('. ./setA.sh\n. ./setB.sh\nbash "$A"\n', "refuse"),
    "f1j-program-position": ('. ./setB.sh\nA=sub/inner.sh\n"$A"\n', "refuse"),
    "f1k-source-operand": ('. ./setA.sh\nA=inner.sh\n. "$A"\n', "refuse"),
    "f1l-ambiguous-not-a-path": ('. ./setA.sh\nA=inner.sh\necho "$A"\n', "allow"),
    "f1m-src-benign-only": ('. ./setB.sh\nbash "$A"\n', "allow"),
}
AMBIGUITY_LIMIT = {
    "lim-caller-twice": ('A=inner.sh\nA=sub/inner.sh\nbash "$A"\n', "allow"),
    "lim-unassigned": ('bash "$A"\n', "allow"),
}
SUBSTITUTION_TOP = {
    "f4-dot-arg-then-subst-benign": ("ls . ; x=$(bash inner.sh)", "allow"),
    "f4-dot-arg-then-subst-missing": ("ls . ; x=$(bash missing.sh)", "allow"),
    "f4-control-no-dot-missing": ("ls ; x=$(bash missing.sh)", "allow"),
    "f4-echo-dot-then-subst": ("echo . source ; x=$(bash inner.sh)", "allow"),
    "f4-dot-cd-then-subst": (". ./cdsub.sh ; x=$(bash inner.sh)", "refuse"),
    "f4-source-cd-then-subst": ("source ./cdsub.sh ; x=$(bash inner.sh)", "refuse"),
    "f4-command-dot-cd-subst": ("command . ./cdsub.sh ; x=$(bash inner.sh)", "refuse"),
    "f4-builtin-source-cd-subst": ("builtin source ./cdsub.sh ; x=$(bash inner.sh)", "refuse"),
    "f4-dot-redirect-cd-subst": (". ./cdsub.sh >/dev/null ; x=$(bash inner.sh)", "refuse"),
    "f4-path-dot-cd-subst": ("/bin/true ; . ./cdsub.sh ; x=$(bash inner.sh)", "refuse"),
}
EVAL_TOP = {
    "e1-eval-dot-cd-subst": ("eval . ./cdsub.sh ; x=$(bash inner.sh)", "refuse"),
    "e2-eval-dq-cd-subst": ('eval "cd sub" ; x=$(bash inner.sh)', "refuse"),
    "e3-eval-sq-cd-subst": ("eval 'cd sub' ; x=$(bash inner.sh)", "refuse"),
    "e4-eval-dq-dot-subst": ('eval "true; . ./cdsub.sh" ; x=$(bash inner.sh)', "refuse"),
    "e5-eval-cd-bare-subst": ("eval cd sub ; x=$(bash inner.sh)", "refuse"),
    "e6-eval-sq-cd-then-bash": ("eval 'cd sub' ; bash inner.sh", "refuse"),
    "e7-eval-dot-cd-then-bash": ("eval . ./cdsub.sh ; bash inner.sh", "refuse"),
    "e8-builtin-eval-dot-subst": ("builtin eval . ./cdsub.sh ; x=$(bash inner.sh)", "refuse"),
    "e9-command-eval-cd-subst": ("command eval 'cd sub' ; x=$(bash inner.sh)", "refuse"),
    "e10-nested-eval-cd": ("eval 'eval \"cd sub\"' ; bash inner.sh", "refuse"),
    "e11-eval-unknown-text": ("X='cd sub'; eval \"$X\" ; bash inner.sh", "refuse"),
    "e12-eval-backquote-text": ("eval `echo cd sub` ; bash inner.sh", "refuse"),
    "e13-eval-pushd": ("eval 'pushd sub' ; bash inner.sh", "refuse"),
    "e14-eval-cd-and-bash": ("eval 'cd sub' && bash inner.sh", "refuse"),
    "e15-eval-source-subst": ("true; eval 'source ./cdsub.sh'; x=$(bash inner.sh)", "refuse"),
    "e16-eval-body-cd-then-bash": ("eval 'cd sub; bash inner.sh'", "refuse"),
    "a1-eval-echo-subst": ("eval echo hi ; x=$(bash inner.sh)", "allow"),
    "a2-eval-sq-echo-bash": ("eval 'echo hi' ; bash inner.sh", "allow"),
    "a3-eval-body-bash-then-cd": ("eval 'bash inner.sh; cd sub'", "allow"),
    "a4-trap-dot-subst": ("trap '. ./cdsub.sh' EXIT ; x=$(bash inner.sh)", "allow"),
    "a5-eval-ls-dot-bash": ("eval 'ls .' ; bash inner.sh", "allow"),
    "a6-bash-then-eval-cd": ("bash inner.sh ; eval 'cd sub'", "allow"),
    "a7-echo-eval-word-subst": ("echo eval cd ; x=$(bash inner.sh)", "refuse"),
}


class TestRawConfigureSource(RawConfigureGuardBase):
    """Distinguish caller directory state, source options and unavailable parsers."""

    def _write(self, name: str, text: str) -> None:
        """Write an executable fixture that only the guard will read."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def _setup_sources(self) -> None:
        """Keep root inner benign and put configure execution only under sub and bin."""
        configure = f"{self.tree}/configure --prefix=/x\n"
        self._write("inner.sh", "#!/bin/sh\necho benign\n")
        self._write("sub/inner.sh", "#!/bin/sh\n" + configure)
        self._write("bin/cfgpath.sh", configure)
        self._write("cdsub.sh", "cd sub\n")
        self._write("cdfunc.sh", "go() { cd sub; }\n")
        self._write("noop.sh", "X=1\n")
        self._write("pushsub.sh", "pushd sub >/dev/null\n")
        self._write("chain.sh", ". ./cdsub.sh\n")
        self._write(
            "activate",
            "deactivate () { unset VIRTUAL_ENV; hash -r 2>/dev/null; }\n"
            f'VIRTUAL_ENV="{self.root}/environment"\nexport VIRTUAL_ENV\n'
            'PATH="$VIRTUAL_ENV/bin:$PATH"\nexport PATH\n'
            'if [ -n "${PS1:-}" ]; then PS1="(fixture) $PS1"; export PS1; fi\n'
            "hash -r 2>/dev/null\n",
        )

    def _run(self, command: str) -> subprocess.CompletedProcess[str]:
        """Classify a Claude command with the fixture bin first in PATH."""
        return self.run_guard(
            "claude",
            self.payload("claude", command, self.root),
            env={"PATH": f"{self.root}/bin:{os.defpath}", "LC_ALL": "C.UTF-8"},
        )

    def test_source_shapes(self) -> None:
        """Replay all 34 judge shapes with distinct scripts in the two directories."""
        self._setup_sources()
        cases = [(name, text, want, True) for name, (text, want) in BODY.items()]
        cases.extend((name, text, want, False) for name, (text, want) in TOP.items())
        self.assertEqual(len(cases), 34)
        for name, text, want, is_body in cases:
            with self.subTest(shape=name):
                command = text.replace("<root>", str(self.root))
                if is_body:
                    self._write("outer.sh", command)
                    command = "bash outer.sh"
                result = self._run(command)
                self.assertEqual(result.returncode, 2 if want == "refuse" else 0, result.stderr)
                self.assertFalse(self.marker.exists())

    def _setup_ambiguity_sources(self) -> None:
        """Add the two literal assignment sources to the directory-change fixtures."""
        self._setup_sources()
        self._write("setA.sh", "A=sub/inner.sh\n")
        self._write("setB.sh", "A=inner.sh\n")

    def test_source_assignment_and_substitution_shapes(self) -> None:
        """Replay every r14 body, top-level and documented-limit verdict without execution."""
        self._setup_ambiguity_sources()
        cases = [(name, text, want, True) for name, (text, want) in AMBIGUITY_BODY.items()]
        cases.extend((name, text, want, False) for name, (text, want) in SUBSTITUTION_TOP.items())
        cases.extend((name, text, want, True) for name, (text, want) in AMBIGUITY_LIMIT.items())
        self.assertEqual(len(cases), 23)
        for name, text, want, is_body in cases:
            with self.subTest(shape=name):
                command = text
                if is_body:
                    self._write("outer.sh", text)
                    command = "bash outer.sh"
                result = self._run(command)
                self.assertEqual(result.returncode, 2 if want == "refuse" else 0, result.stderr)
                self.assertFalse(self.marker.exists())

    def test_ambiguous_script_variable_names_reason(self) -> None:
        """Name conflicting source assignments while allowing equal values and longer names."""
        self._setup_ambiguity_sources()
        cases = (
            ('. ./setB.sh\nA=sub/inner.sh\nbash "$A"\n', 2),
            ('A=inner.sh\n. ./setB.sh\nbash "$A"\n', 0),
            ('. ./setB.sh\nA=sub/inner.sh\nbash "${A}"\n', 2),
            ('. ./setB.sh\nA=sub/inner.sh\nbash "$AB"\n', 0),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self._write("outer.sh", body)
                result = self._run("bash outer.sh")
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 2:
                    self.assertIn("assigned in a sourced file and elsewhere", result.stderr)
                self.assertFalse(self.marker.exists())

    def test_dot_argument_keeps_substitution_directory(self) -> None:
        """Allow dot arguments before substitutions and refuse paths after source commands."""
        self._setup_sources()
        cases = (
            ("ls . ; x=$(bash inner.sh)", 0),
            (". ./cdsub.sh ; x=$(bash inner.sh)", 2),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                result = self._run(command)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 2:
                    self.assertIn("relative script path after cd", result.stderr)
                self.assertFalse(self.marker.exists())

    def test_source_option_refusal_names_word(self) -> None:
        """Refuse unsupported source options in bodies and top-level commands by name."""
        self._setup_sources()
        self._write("outer.sh", ". -p sub inner.sh\n")
        for command in ("bash outer.sh", ". -p sub inner.sh"):
            with self.subTest(command=command):
                result = self._run(command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("source option the guard does not follow: -p", result.stderr)
                self.assertFalse(self.marker.exists())

    def test_eval_directory_change_reaches_later_stages(self) -> None:
        """Replay every r15 top-level eval verdict without executing fixture scripts."""
        self._setup_sources()
        self.assertEqual(len(EVAL_TOP), 23)
        for name, (command, want) in EVAL_TOP.items():
            with self.subTest(shape=name):
                result = self._run(command)
                self.assertEqual(result.returncode, 2 if want == "refuse" else 0, result.stderr)
                if want == "refuse":
                    self.assertIn("relative script path after cd", result.stderr)
                self.assertFalse(self.marker.exists())

    def test_eval_body_keeps_state_before_eval(self) -> None:
        """Keep eval entry state while tracking directory changes inside its queued body."""
        self._setup_sources()
        for name in ("a3-eval-body-bash-then-cd", "e16-eval-body-cd-then-bash"):
            command, want = EVAL_TOP[name]
            with self.subTest(shape=name):
                result = self._run(command)
                self.assertEqual(result.returncode, 2 if want == "refuse" else 0, result.stderr)
                if want == "refuse":
                    self.assertIn("relative script path after cd", result.stderr)
                self.assertFalse(self.marker.exists())

    def test_source_literal_script_operand(self) -> None:
        """Preserve literal substitution when sourced text names another script file."""
        self._setup_sources()
        self._write("literal.sh", 'NEXT=sub/inner.sh\nbash "$NEXT"\n')
        self._write("bin/alias.sh", 'NEXT=inner.sh\nbash "$NEXT"\n')
        self._write("alias.sh", 'NEXT=sub/inner.sh\nbash "$NEXT"\n')
        self._write("outer.sh", ". ./literal.sh\n")
        for command in ("bash outer.sh", ". ./literal.sh", ". alias.sh"):
            with self.subTest(command=command):
                result = self._run(command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.marker.exists())

    def test_unknown_source_then_relative_names_reason(self) -> None:
        """Refuse relative paths after unreadable sources while allowing absolute scripts."""
        self._setup_sources()
        for operand, expected in (("inner.sh", 2), (str(self.root / "inner.sh"), 0)):
            with self.subTest(operand=operand):
                self._write("outer.sh", f'. "$UNSET_DIR/lib.sh"\nbash {operand}\n')
                result = self._run("bash outer.sh")
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 2:
                    self.assertIn(UNKNOWN_SOURCE, result.stderr)
                self.assertFalse(self.marker.exists())

    def _assert_parser_failure(self, failure: str) -> None:
        """Replay an import or native Language failure without caching a broken parser."""
        original_import = importlib.import_module

        def importing(name: str) -> object:
            """Break only the requested parser component and import other modules normally."""
            if failure == "import" and name == "tree_sitter_bash":
                raise ImportError("planted missing bash grammar")
            if failure == "language" and name == "tree_sitter":
                return SimpleNamespace(
                    Parser=original_import(name).Parser,
                    Language=Mock(side_effect=OSError("planted native error")),
                )
            return original_import(name)

        with patch.object(body_module.importlib, "import_module", side_effect=importing):
            result = bash_parser()
            self.assertIsInstance(result, str)
            for text in (
                "python3 -m venv .venv",
                "--require-hashes -r requirements-dev.txt",
                sys.executable,
            ):
                self.assertIn(text, result)
            self.assertEqual(body_module._PARSERS, [])
            context = InspectionContext(self.root, False, [0], classify_script_words)
            refusal = inspect_script_file("clean.sh", context, is_program=False)
            self.assertIsInstance(refusal, Path)
            self.assertIn("shell script parser unavailable", str(refusal))
            self.assertEqual(body_module._PARSERS, [])

    def test_bash_parser_import_failure_names_setup(self) -> None:
        """Name pinned setup steps for missing grammar imports and native loader errors."""
        saved = body_module._PARSERS[:]
        self.addCleanup(body_module._PARSERS.extend, saved)
        self.addCleanup(body_module._PARSERS.clear)
        body_module._PARSERS.clear()
        self._write("clean.sh", "echo benign\n")
        for failure in ("import", "language"):
            with self.subTest(failure=failure):
                self._assert_parser_failure(failure)

    def test_guard_without_tree_sitter_refuses_script_files(self) -> None:
        """Fail closed on script files with site imports disabled and still allow ls."""
        env = {"PATH": f"{self.root}/bin:{os.defpath}", "LC_ALL": "C.UTF-8"}
        available = subprocess.run(  # nosec B603 - fixed Python import, never a fixture.
            [sys.executable, "-I", "-S", "-c", "import tree_sitter_bash"],
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=5,
            check=False,
            env=env,
        )
        if available.returncode == 0:
            self.skipTest("tree_sitter_bash is importable without site initialization")
        self._write("outer.sh", "echo benign\n")
        for command, expected in (("bash outer.sh", 2), ("ls", 0)):
            with self.subTest(command=command):
                result = subprocess.run(  # nosec B603 - only the fixed Python guard runs.
                    [sys.executable, "-I", "-S", str(GUARD), "--client", "claude"],
                    input=json.dumps(self.payload("claude", command, self.root)),
                    text=True,
                    capture_output=True,
                    cwd=ROOT,
                    timeout=5,
                    check=False,
                    env=env,
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 2:
                    self.assertIn("python3 -m venv .venv", result.stderr)
                self.assertFalse(self.marker.exists())

    def test_hooks_start_guard_through_python_sh(self) -> None:
        """Keep every client hook on the isolated checkout Python launcher."""
        for client, (path, event, _, _) in CONFIGS.items():
            with self.subTest(client=client):
                config = json.loads(path.read_text(encoding="utf-8"))
                commands = [
                    hook["command"]
                    for group in config["hooks"][event]
                    for hook in group["hooks"]
                    if "scripts/raw_configure_guard.py" in hook.get("command", "")
                ]
                self.assertTrue(commands)
                for command in commands:
                    self.assertIn(".config/lefthook/python.sh", command)
                    self.assertIn(" -I ", command)
                    self.assertLess(
                        command.index(".config/lefthook/python.sh"),
                        command.index("scripts/raw_configure_guard.py"),
                    )
        plugin = (ROOT / ".opencode/plugins/raw-configure-guard.js").read_text(encoding="utf-8")
        self.assertIn(".config/lefthook/python.sh", plugin)
        self.assertIn('"-I"', plugin)
