#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Check an upstream-bound FFmpeg branch before posting it.

Use the pre-commit configuration, commit-message checker and patcheck from
--base, never a branch's replacement. Every step reports its own return code;
patcheck's added-line hints fail only for the documented category allow-list,
even when its process exits zero. Other added-line hints, including unknown
categories, are reported for review. Hook edits fail inside a disposable worktree; dirty trees
are refused. --head must be checked out and --base must be its ancestor.

The added C/header line rules implement developer.texi, "Code formatting
conventions": no TAB characters, no trailing whitespace, and K&R braces.
Brace detection deliberately covers only single-line function declarations and
single-line control headers followed by a standalone brace. Comments, literals,
preprocessor directives, multiline signatures and ambiguous constructs are not
interpreted as declarations. Untouched whitespace violations in changed C/header
files are listed as pre-existing, without failing. This is a mechanical subset,
not a replacement for upstream review or the remaining developer.texi rules.

The fate-deps step checks lavfi video FATE registrations for LAVFI_INDEV and
WRAPPED_AVFRAME_DECODER: lavfi emits wrapped_avframe video packets, so an encoder
alone cannot satisfy decoding. Templates are reported without expansion,
macro argument transforms such as `$(2:%=%_DECODER)` are not expanded,
multi-file variables are reported unresolved, and audio PCM decoders are not checked.

The fate-shape step reads added logical lines in tests/fate/*.mak and
tests/fate-run.sh. It reads the ffmpeg-dependent target variables from
tests/Makefile and follows plain variable aggregation through tests/Makefile
and every tests/fate/*.mak file at the checked revision. A registration that
reaches those targets and runs lib*/tests/ needs the matching FATE_LIB* group.
It checks CMD values for unquoted pipelines and whole-word grep assertions,
and helper pipelines starting with test commands or command variables. In CMD
filter graphs after -lavfi, -vf, -filter_complex or -filter:v, an endian-specific
format= is checked against its next consumer in the same chain: scale and
format convert it, other consumers fail, and chain ends pass. Not checked:
forced formats ending labelled chains feeding native-endian filters in another
chain, aggregation built with make functions other than plain $(VAR) references
(for example $(foreach) or $(call)), reference-file and C-test endian pairing,
missing CONFIG gates, phony alias targets and pipes inside quoted strings.
--fate-shape-reason TEXT waives findings, never Git or read errors.

The regression-test step fails when a commit whose subject or body, outside its
Signed-off-by lines, says fix, fixes or fixed or names a defect (bug,
regression, misalignment, crash, overflow, overread, leak, wrong, incorrect,
broken, truncation, past the end, out of bounds, uninitialised, uninitialized,
use after free, double free, null dereference, null pointer dereference,
division by zero, divide by zero, undefined behaviour, undefined behavior,
deadlock, segfault, segmentation fault, memory corruption, heap corruption,
stack corruption, OOB, also when a line break splits the phrase) changes C,
header or assembly sources under lib*/ or fftools/ and no path
under a tests directory changes anywhere in base..head. --no-test-reason TEXT
waives it and prints the reason; when test paths do change, each fix commit must
be reached by name: a changed test path or one of its added lines must contain,
as a whole word, the stem of a source file the commit changes (a vf_, af_,
vsrc_, asrc_, avf_, avsrc_, vsink_, asink_ or f_ prefix also stripped) or a
function name Git prints in one of its source hunk headers. Quoted,
tab-terminated and binary test paths count, and the range diff forces the a/
and b/ prefixes whatever diff.noprefix says. The same waiver covers a fix the
changed tests do not name. A test that names the code without running the fixed
path is not detected.

The diagnostic-citation step fails a commit whose message names a compiler
diagnostic flag (-W...) together with workaround wording (workaround, false
positive, silence, suppress, spurious) unless the message carries a GCC
Bugzilla, LLVM bug, llvm-project issue or Compiler Explorer URL, or a line
starting `Reproducer:`; diagnostics named without a flag and workarounds
described in other words are not detected. The two-word phrases match across a
line break, and a flag suffix may contain `#`, uppercase letters, digits, `+`,
`_`, `=` and `-`.

The claim-scope step fails a commit whose message makes a blanket verification
claim (passes all tests, all or every test passes, fully or thoroughly tested
or verified, no regressions or no performance regressions, completely or 100%
safe, verified or tested), never waivable; a claim directly after not, never,
cannot, without or an n't contraction, and a safe claim followed by because or
since, are not claims. It also fails, never waivable, a commit whose message
claims faster encoding, decoding or transcoding (encoding is faster, faster
decoding, an encoder speedup, speeds up the encoder, an end-to-end speedup,
performance or throughput) without an end-to-end figure anywhere in the
message: an fps or frames/s number, ffmpeg -benchmark, a bench: utime= line,
speed= followed by a number, or hyperfine. A benchmark of one helper does not
show that; a claim after not, never, cannot, without, no or an n't contraction
earlier in its sentence (a decimal point inside a number is not a sentence end)
is no claim, and speed words other than these, such as time or a bare encoding
speed, are not detected. It also fails a commit whose message claims several
sites (both or all followed within two words, line breaks included, by loops,
callers, call sites, branches, paths, cases, functions, uses, instances,
places, sites or occurrences; every or each followed the same way by the
singular or plural of those nouns) while its diff changes exactly one hunk in
library or fftools sources; renamed files are diffed as renames, a deleted
source file counts its hunks, and one contiguous hunk counts as one site even
when it edits two adjacent branches or states an invariant of all callers;
--claim-reason TEXT waives only that several-sites rule. Signed-off-by lines in
any letter case are ignored, and a range of more than MAX_CLAIM_COMMITS commits
fails with rc 2; claims worded otherwise, and claims that a multi-hunk diff
still does not support, are not detected.

The doc-terms step fails when base..head adds lines both to library or fftools
sources and to Texinfo files under doc/, and an added source line shows a
behaviour whose added description leaves one of its cases out: a test for
non-RGB input (a comparison against a COLOR_RGB constant, or a negated
AV_PIX_FMT_FLAG_RGB flag test, in either operand order) while the added
docs say YUV but never non-RGB, gray or grey, or a colour range comparison
while the added docs mention range
or rangein but never unspecified, unknown or untagged. The added source lines
are read from the head file with comments, string literals and preprocessor
lines blanked, so a comment that names the behaviour does not count, and
neither does a macro that tests the flag. --doc-reason TEXT waives it and
prints the reason. Only these two pairs are checked; any other gap between
what the code does and what the docs say is not detected.

The doc-wrap step fails an added line of filled prose in a changed doc/*.texi
file that is shorter than half of both neighbouring prose lines, each at least
WRAP_MIN_NEIGHBOUR characters, does not end with a sentence mark or @*, and
leaves room on it for the first word of the next line (a leading inline
command counts up to its closing brace). Prose is a non-blank line outside
every block except the filled ones in TEXI_FILLED that starts with text or with
a TEXI_INLINE command and its brace; block names come from the file's own @end
lines read outside raw blocks, so lines in an unlisted block are never checked.
The raw blocks in TEXI_RAW (@verbatim, @ignore, @macro, @rmacro, @linemacro)
always open, and inside one only a nested opener that texi2any counts there and
an @end naming the innermost raw block change the open blocks, so an @end line
shown inside a raw block closes no outer block. --doc-reason TEXT waives it and
prints the reason.

The test-program step fails when base..head adds a C file directly under a
lib*/tests/ directory and a TESTPROGS assignment (TESTPROGS =, := or +=, or
TESTPROGS-$(CONFIG_X) +=) in that library's Makefile at head lists the file's
stem as a word, which registers a new TESTPROGS program. Continuation
lines are joined and make comments dropped first, so an OBJS entry or a
comment that names the stem does not count. Upstream reviewers ask for the
standard FATE structure instead: an input sample run through ffmpeg or
ffprobe and compared against a tests/ref reference. --test-program-reason TEXT
waives it and prints the reason. A program registered in another file or
through another variable, and a FATE test whose input another custom tool
builds, are not detected.

The c-rules step runs scripts/semgrep_scan.sh with .semgrep-ffmpeg.yml from
this repository over added, modified or renamed library and fftools C and
header sources in base..head, using the FFmpeg checkout at head as the working
directory, and skips the run when none changed. Results are read through an
allow-list: exit status 0 or 1, no semgrep_scan: warning line, and a JSON
output object whose paths.scanned list contains every requested path, whose
results name requested paths with non-empty rule ids, and whose errors list
holds only PartialParsing errors. Matches whose line range
meets a file's rename-aware added lines or deletion positions fail with rc 1,
untouched matches and PartialParsing errors are reported without failing,
and any other status, warning, error type or JSON failure gives rc 2.
--c-rule-reason TEXT waives rc 1 findings and prints the reason, never rc 2.

The docstrings step checks diff-scoped doc-comment coverage across base..head
in --tree by running the pinned Praetor engine via .config/lefthook/engine.sh
docstrings check from this repository's root directory, where engine.sh reads
the workflow pin. Results pass through an allow-list: only exit 0 or 1 whose
first output line has the exact `Docstring coverage: ...% (... covered,
counted: ..., covered: ..., threshold: ...%)` form counts as a measured pass
(rc 0) or finding (rc 1); any other exit status or output, including a timeout
or an engine.sh refusal, fails with rc 2. --docstring-reason TEXT waives a
finding and prints the reason, and never waives an rc 2 result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess  # nosec B404 - explicit argv, process-group cleanup, bounded deadlines.
import sys
import tempfile
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Ensure repository root is on sys.path for test/tool imports
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.process_group import kill_process_group  # noqa: E402

PRE_COMMIT = "pre-commit"
ALLOWED_EXECUTABLES = frozenset({"git", "pre-commit", "bash"})
# Match tests.helpers' repository isolation without importing test dependencies.
GIT_ENVIRONMENT_NAMES = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)
GIT_CONFIG_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
DIFF_COMMON = (
    "diff",
    "--inter-hunk-context=0",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
C_RULES_DIFF = (
    "diff",
    "--inter-hunk-context=0",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--find-renames",
    "--diff-filter=AMR",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
MAX_PATHS = 2000
MAX_CLAIM_COMMITS = 1000
RUN_TIMEOUT = 1800
C_RULES_TIMEOUT = 300
DOCSTRINGS_TIMEOUT = 300
CLEANUP_TIMEOUT = 30
REAP_TIMEOUT = 10
SEMGREP_SCAN = REPO_ROOT / "scripts" / "semgrep_scan.sh"
SEMGREP_FFMPEG_RULES = REPO_ROOT / ".semgrep-ffmpeg.yml"
_DEADLINE: ContextVar[float | None] = ContextVar("deadline", default=None)
CONFIG = ".forgejo/pre-commit/config.yaml"
ENGINE_SCRIPT = ".config/lefthook/engine.sh"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
HUNK_LENGTHS = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
GIT_QUOTED_ESCAPE = re.compile(r'\\([0-3][0-7]{2}|[abtnvfr"\\])')
GIT_ESCAPE_BYTES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
REFERENCE = re.compile(r"^[^:\n]+?[:-]\d+[:-](.*)$")
FUNCTION = re.compile(r"^(?:[A-Za-z_]\w*[ *]+)+[A-Za-z_]\w*\([^();{}]*\)\s*\{")
CONTROL = re.compile(r"(?:}\s*)?(?:(?:else\s+)?(?:if|for|while|switch)\s*\(.*\)|else|do)")
C_NONCODE = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    r"|^[ \t]*\#[^\n]*(?:\n(?<=\\\n)[^\n]*)*",
    re.DOTALL | re.MULTILINE,
)
FIX_MESSAGE = re.compile(
    r"\b(?:fix(?:es|ed)?|bugs?|regressions?|misalign(?:s|ed|ment)?|crash(?:es|ed)?"
    r"|overflow(?:s|ed)?|overreads?|leak(?:s|ed)?|wrong|incorrect(?:ly)?|broken"
    r"|truncat(?:e|es|ed|ion)|past\s+the\s+end|out(?:\s+|\s*-\s*)of(?:\s+|\s*-\s*)bounds"
    r"|(?<!maybe-)(?<!sometimes-)uninitiali[sz]ed|use(?:\s+|\s*-\s*)after(?:\s+|\s*-\s*)free"
    r"|double(?:\s+|\s*-\s*)free|null\s+dereferences?|null\s+pointer\s+dereferences?"
    r"|(?:division|divide)\s+by\s+zero|undefined\s+behaviou?r|deadlock(?:s|ed)?"
    r"|segfault(?:s|ed)?|segmentation\s+faults?|(?:memory|heap|stack)\s+corruption|oob)\b",
    re.IGNORECASE,
)
LAVFI_PREFIX = re.compile(r"^(?:vf|af|vsrc|asrc|avf|avsrc|vsink|asink|f)_")
FUNCTION_NAME = re.compile(r"([A-Za-z_]\w*)\s*\(")
WORD = re.compile(r"[A-Za-z0-9_]+")
SOURCE_SUFFIXES = (".c", ".h", ".asm", ".S")
DIAGNOSTIC_FLAG = re.compile(r"(?<![\w-])-W[A-Za-z0-9#][A-Za-z0-9#+=_-]*")
WORKAROUND_WORDING = re.compile(
    r"\b(work(s|ed)?(?:\s+|-)?around|false(?:\s+|-)positives?"
    r"|silence[sd]?|suppress(es|ed)?|spurious)\b",
    re.IGNORECASE,
)
DIAGNOSTIC_CITATION = re.compile(
    r"https?://(gcc\.gnu\.org/bugzilla/|bugs\.llvm\.org/"
    r"|github\.com/llvm/llvm-project/issues/|godbolt\.org/)\S+"
    r"|^Reproducer:[ \t]*\S",
    re.IGNORECASE | re.MULTILINE,
)
VERIFICATION_CLAIM = re.compile(
    r"\b(pass(?:es|ed)?\s+all\s+(?:the\s+)?(?:fate\s+|unit\s+)?tests"
    r"|(?:all|every)\s+(?:the\s+)?(?:fate\s+|unit\s+)?tests?\s+pass(?:es|ed)?"
    r"|(?:fully|thoroughly)\s+(?:tested|verified)"
    r"|no\s+(?:performance\s+)?regressions?(?!\s+test(?:s|ing|ed)?\b)"
    r"|(?:completely|100%)\s+(?:safe|verified|tested))\b",
    re.IGNORECASE,
)
NEGATED_CLAIM = re.compile(
    r"(?:\bnot|\bnever|\bcannot|n't|\bwithout)\s+(?:been\s+|be\s+)?$", re.IGNORECASE
)
SAFETY_REASON = re.compile(r"\s+(?:because|since)\b", re.IGNORECASE)
SCOPE_CLAIM = re.compile(
    r"\b(?:(?:both|all)\s+(?:\w+(?:\s+|-)){0,2}?"
    r"(?:loops?|callers?|call(?:\s+|-)sites?|branches|paths|cases"
    r"|functions|uses|instances|places|sites|occurrences)"
    r"|(?:every|each)\s+(?:\w+(?:\s+|-)){0,2}?"
    r"(?:branch(?:es)?|(?:loop|caller|call(?:\s+|-)site|path|case"
    r"|function|use|instance|place|site|occurrence)s?))\b",
    re.IGNORECASE,
)
CODING = r"(?:encod|decod|transcod)\w*"
SPEED_CLAIM = re.compile(
    rf"\b(?:{CODING}\s+(?:is\s+|runs\s+|gets\s+|becomes\s+)?"
    r"(?:\d+(?:\.\d+)?\s*(?:x|%|times)\s+)?(?:faster|quicker)"
    rf"|(?:faster|quicker)\s+{CODING}"
    rf"|{CODING}\s+speed-?ups?"
    rf"|speeds?\s+up\s+(?:the\s+)?{CODING}"
    rf"|end-to-end\s+(?:{CODING}\s+)?(?:speed(?:-?ups?)?|performance|throughput))\b",
    re.IGNORECASE,
)
SPEED_NEGATION = re.compile(
    r"(?:\bnot|\bnever|\bcannot|\bwithout|\bno|n't)\b(?:[^.;!?]|(?<=\d)\.(?=\d))*$",
    re.IGNORECASE,
)
END_TO_END_MEASURE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:fps|frames/s)\b|(?<![\w-])-benchmark\b|\bbench:\s*utime="
    r"|\bspeed=\s*\d|\bhyperfine\b",
    re.IGNORECASE,
)
# Exact upstream category headers allowed to block added lines. Keep this an
# allow-list: heuristic and newly introduced categories require human review.
# Whitespace and K&R formatting are enforced separately by developer.texi rules.
PATCHECK_BLOCKING_CATEGORIES = frozenset(
    {
        "double ;",
        "common typos",
        "useless casts",
        "useless #if",
        "useless if()",
        "useless & [0]",
        "empty comment",
        "inconsistent doxygen syntax",
        "Misformatted doxygen comment",
        "duplicate word",
        "can be simplified to ++",
        "can be simplified to --",
        "Please use av_malloc",
        "Please use av_assert0, av_assert1 or av_assert2",
        "missing const for pix_fmts array",
        "missing const for sample_fmts array",
        "missing const for supported_framerates array",
        "missing const for channel_layouts array",
        "missing NULL_IF_CONFIG_SMAL",
    }
)


@dataclass(frozen=True, slots=True)
class RunResult:
    """One bounded command's arguments, exit status and captured output."""

    args: tuple[str, ...]
    returncode: int
    stdout: str


@dataclass(frozen=True)
class Waivers:
    """Reasons that waive single upstream checks; a blank reason waives nothing."""

    no_test: str = ""
    claim: str = ""
    doc: str = ""
    test_program: str = ""
    c_rule: str = ""
    fate_shape: str = ""
    docstring: str = ""


@dataclass
class Step:
    """One independently collected check, with an effective exit status."""

    name: str
    rc: int
    output: str


def _validate_executable(argv: list[str]) -> None:
    if not argv or Path(argv[0]).name not in ALLOWED_EXECUTABLES:
        raise ValueError(f"executable refused: {argv[0] if argv else '<empty>'!r}")


def _remaining(timeout: float) -> float:
    deadline = _DEADLINE.get()
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("overall deadline exhausted")
    return min(timeout, remaining)


def _reap_timeout(
    process: subprocess.Popen[bytes], argv: list[str], partial_output: bytes | None = None
) -> RunResult:
    """Kill a timed-out process group and bound the remaining pipe drain."""
    kill_process_group(process)
    try:
        output, _ = process.communicate(timeout=_remaining(REAP_TIMEOUT))
    except subprocess.TimeoutExpired as error:
        output = error.output if error.output is not None else partial_output
        text = (output or b"").decode("utf-8", errors="replace")
        return RunResult(
            tuple(argv), 124, text + "\ncommand timed out (descendant kept the pipe open)\n"
        )
    text = output.decode("utf-8", errors="replace")
    if process.returncode == -signal.SIGKILL:
        return RunResult(tuple(argv), 124, text + "\ncommand timed out\n")
    return RunResult(tuple(argv), process.returncode, text)


def _run(argv: list[str], cwd: Path, data: str | None = None, timeout: float = 60) -> RunResult:
    """Run an allowed tool with isolated Git state and bounded communication."""
    try:
        _validate_executable(argv)
    except ValueError as error:
        return RunResult(tuple(argv), 2, str(error))
    if _DEADLINE.get() is not None:
        timeout = min(timeout, _remaining(timeout + REAP_TIMEOUT) - REAP_TIMEOUT)
        if timeout <= 0:
            raise RuntimeError("overall deadline exhausted (reap budget reserved)")
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in GIT_ENVIRONMENT_NAMES and not name.startswith(GIT_CONFIG_PREFIXES)
    }
    environment["PRE_COMMIT_NO_CONCURRENCY"] = "1"
    with subprocess.Popen(  # nosec B603 - _validate_executable enforces the tool allow-list.
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        shell=False,
    ) as process:
        try:
            output, _ = process.communicate(
                None if data is None else data.encode("utf-8"), timeout=timeout
            )
            return RunResult(
                tuple(argv), process.returncode, output.decode("utf-8", errors="replace")
            )
        except subprocess.TimeoutExpired as error:
            return _reap_timeout(process, argv, error.output)


def _git(tree: Path, *args: str, allow_no_match: bool = False) -> str:
    """Return Git output, optionally accepting grep's explicit no-match result."""
    result: RunResult = _run(["git", *args], tree)
    no_match = allow_no_match and args[:1] == ("grep",) and result.returncode == 1
    if result.returncode and not no_match:
        raise RuntimeError(f"git {shlex.join(args)}: rc={result.returncode}\n{result.stdout}")
    return result.stdout


def _status(tree: Path) -> str:
    return _git(tree, "status", "--porcelain=v1", "-z", "--untracked-files=all")


def _prepare(tree: Path, base: str, head: str) -> tuple[str, str]:
    root = Path(_git(tree, "rev-parse", "--show-toplevel").strip()).resolve()
    if root != tree:
        raise RuntimeError("--tree must name the worktree root")
    if _status(tree):
        raise RuntimeError("dirty tree refused (tracked, staged or untracked files); no stash used")
    refs = [
        _git(tree, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()
        for ref in (base, head, "HEAD")
    ]
    if refs[1] != refs[2]:
        raise RuntimeError("--head must be the checked-out HEAD; checkout that revision first")
    _git(tree, "merge-base", "--is-ancestor", refs[0], refs[1])
    _changed_paths(tree, refs[0], refs[1])
    return refs[0], refs[1]


def _base_file(tree: Path, base: str, name: str, scratch: Path) -> Path:
    target = scratch / Path(name).name
    target.write_text(_git(tree, "show", f"{base}:{name}"), encoding="utf-8")
    target.chmod(0o700)
    return target


def _pre_commit() -> str:
    """Find pre-commit on PATH once per step."""
    executable = shutil.which(PRE_COMMIT)
    if not executable:
        raise RuntimeError("pre-commit not found: install pre-commit on PATH")
    return str(Path(executable).absolute())


def _precommit(tree: Path, base: str, head: str, scratch: Path) -> Step:
    """Run the resolved pre-commit in a disposable worktree and discard edits."""
    executable = _pre_commit()
    config = _base_file(tree, base, CONFIG, scratch)
    argv = [executable, "run", "-c", str(config), "--from-ref", base, "--to-ref", head]
    worktree = scratch / "hook-tree"
    deadline = _DEADLINE.get()
    # Reserve part of the overall budget for removing the disposable worktree.
    token = _DEADLINE.set(None if deadline is None else deadline - CLEANUP_TIMEOUT)
    try:
        _git(tree, "worktree", "add", "--detach", str(worktree), head)
        result = _run(argv, worktree, timeout=600)
        changed = bool(_status(worktree))
    finally:
        _DEADLINE.reset(token)
        try:
            if worktree.exists():
                _git(tree, "worktree", "remove", "--force", str(worktree))
        finally:
            _git(tree, "worktree", "prune")
    output = f"$ {shlex.join(argv)}\ncommand rc: {result.returncode}\n{result.stdout}"
    if changed:
        output += "\nfixer modified files: failure; disposable worktree discarded\n"
    return Step("pre-commit", result.returncode or int(changed), output)


def _commit_messages(tree: Path, base: str, head: str, scratch: Path) -> Step:
    tool = _base_file(tree, base, "tools/check_commit_msg.sh", scratch)
    argv = ["bash", str(tool), f"{base}..{head}"]
    result = _run(argv, tree)
    return Step("commit-msg", result.returncode, f"$ {shlex.join(argv)}\n{result.stdout}")


def _patcheck_hints(output: str) -> tuple[int, str]:
    """Block allow-listed added-line categories; retain other hints for review."""
    findings, reports, category = 0, [], "unknown"
    for line in ANSI.sub("", output).splitlines():
        match = REFERENCE.match(line)
        source = match[1] if match and not line.startswith("+") else line
        added = source.startswith("+") and not source.startswith("+++ ")
        if added:
            blocking = category in PATCHECK_BLOCKING_CATEGORIES
            findings += int(blocking)
            label = "finding" if blocking else "hint (review)"
            reports.append(f"{label}: {category}: {line}")
        else:
            if not match and line.strip() and not line.startswith(("+", "-", " ", "\t")):
                category = line.strip()
            reports.append(f"informational: {line}")
    return int(findings > 0), "\n".join(reports)


def _patcheck(tree: Path, base: str, head: str, scratch: Path) -> Step:
    tool = _base_file(tree, base, "tools/patcheck", scratch)
    patch = _git(tree, "format-patch", "--stdout", f"{base}..{head}")
    result = _run(["bash", str(tool)], scratch, patch)
    rc, output = _patcheck_hints(result.stdout)
    return Step("patcheck", result.returncode or rc, f"command rc: {result.returncode}\n{output}")


def _added_lines(patch: str) -> set[int]:
    """Collect added physical line numbers from zero-context diff hunks."""
    added = set()
    for line in patch.splitlines():
        match = HUNK.match(line)
        if match:
            start, count = int(match[1]), int(match[2] or "1")
            added.update(range(start, start + count))
    return added


def _deleted_lines(patch: str) -> set[int]:
    """Map deletion-only hunks to their preceding new-file line, or 1 at top of file."""
    deleted = set()
    for line in patch.splitlines():
        match = HUNK.match(line)
        if match and match[2] == "0":
            deleted.add(max(1, int(match[1])))
    return deleted


def _code_lines(source: str) -> list[str]:
    return C_NONCODE.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), source).split("\n")


def _line_rules(raw: str, code: str, previous: str) -> list[str]:
    rules = []
    if "\t" in raw:
        rules.append("TAB character")
    if raw.rstrip(" \t\r\v\f") != raw:
        rules.append("trailing whitespace")
    if FUNCTION.match(code):
        rules.append("K&R function opening brace must be on its own line")
    if code.strip() == "{" and CONTROL.fullmatch(previous):
        rules.append("K&R control opening brace must be on the header line")
    return rules


def _file_rules(path: str, source: str, added: set[int]) -> tuple[int, list[str]]:
    lines, code = source.split("\n"), _code_lines(source)
    findings, reports, previous = 0, [], ""
    for number, (raw, clean) in enumerate(zip(lines, code, strict=True), 1):
        rules = _line_rules(raw, clean, previous)
        for rule in rules:
            if number in added:
                findings += 1
                reports.append(f"finding: {path}:{number}: {rule}")
            elif rule in ("TAB character", "trailing whitespace"):
                reports.append(f"pre-existing (untouched): {path}:{number}: {rule}")
        if clean.strip():
            previous = clean.strip()
    return findings, reports


def _changed_paths(
    tree: Path, base: str, head: str, *patterns: str, added_or_modified: bool = False
) -> list[str]:
    names = _git(
        tree,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        *(["--diff-filter=AM"] if added_or_modified else []),
        "--name-only",
        "-z",
        base,
        head,
        "--",
        *patterns,
    ).split("\0")
    paths = list(filter(None, names))
    if len(paths) > MAX_PATHS:
        raise RuntimeError(f"changed paths exceed MAX_PATHS={MAX_PATHS}: {len(paths)}")
    return paths


def _mechanical(tree: Path, base: str, head: str) -> Step:
    """Check added C and header lines against mechanical formatting rules."""
    names = _changed_paths(tree, base, head, "*.c", "*.h", added_or_modified=True)
    findings, reports = 0, []
    for name in names:
        patch = _git(tree, *DIFF_COMMON, "-U0", base, head, "--", name)
        source = _git(tree, "show", f"{head}:{name}")
        count, lines = _file_rules(name, source, _added_lines(patch))
        findings += count
        reports.extend(lines)
    reports.append(f"{findings} added-line finding(s); developer.texi: Code formatting conventions")
    return Step("developer.texi", int(findings > 0), "\n".join(reports))


FATE_PATTERNS = (
    ("variable", re.compile(r"^([A-Z][A-Z0-9_]*)\s*(\+=|:=|=)\s*(.*)$")),
    ("registration", re.compile(r"^([A-Z][A-Z0-9_]*)-(\$\(.+?|yes)\s*\+=\s*(.*)$")),
    ("command", re.compile(r"^(.+?)\s*:\s*CMD\s*:?=\s*(.*)$")),
    ("open", re.compile(r"^(?:ifeq|ifneq|ifdef|ifndef)\b.*$")),
    ("else", re.compile(r"^else\b.*$")),
    ("close", re.compile(r"^endif\s*(?:#.*)?$")),
    ("define", re.compile(r"^define\s+\S+.*$")),
    ("endef", re.compile(r"^endef\s*(?:#.*)?$")),
)
FATE_VARIABLE = re.compile(r"\$\(([A-Z][A-Z0-9_]*)\)")
FATE_TOKEN_BUDGET = 4096
FATE_CHAR_BUDGET = 65536
LAVFI_FORMAT = re.compile(r"(?:^|\s)-f\s+lavfi(?=\s|$)")
LAVFI_OPTION = re.compile(r"^-[\w:]+$")
LAVFI_NAME = re.compile(r"""^["']?([a-z0-9_]+)(?=[=,;\s"']|$)""")
FATE_REQUIRED = ("LAVFI_INDEV", "WRAPPED_AVFRAME_DECODER")


@dataclass(frozen=True)
class FateLine:
    """A logical make line and its inclusive physical source range."""

    text: str
    first: int
    last: int


@dataclass(frozen=True)
class FateToken:
    """One target token with the physical lines that contributed it."""

    text: str
    lines: frozenset[int]


@dataclass(frozen=True)
class FateEntry:
    """One command or registration with target provenance and dependency tokens."""

    line: FateLine
    target: FateToken
    deps: frozenset[str] = frozenset()
    command: str = ""
    provenance: frozenset[int] = frozenset()
    classified: bool = True


@dataclass
class FateFile:
    """File-local variable state, registrations, commands and unexpanded templates."""

    variables: dict[str, list[FateToken]] = field(default_factory=dict)
    registrations: dict[str, list[FateEntry]] = field(default_factory=dict)
    commands: dict[str, list[FateEntry]] = field(default_factory=dict)
    templates: list[FateLine] = field(default_factory=list)
    simple: set[str] = field(default_factory=set)
    overflows: set[int] = field(default_factory=set)
    macros: frozenset[str] = frozenset()
    needs_macros: bool = False


@dataclass(frozen=True)
class FateContext:
    """Inject base source, deletion positions and macros into the pure file checker."""

    base: str | None = None
    deleted: frozenset[int] = frozenset()
    macros: frozenset[str] = frozenset()


@dataclass
class FateReport:
    """Accumulate independent file diagnostics and lavfi classification counts."""

    checked: int = 0
    audio: int = 0
    unclassified: int = 0
    findings: int = 0
    unresolved: int = 0
    lines: list[str] = field(default_factory=list)


@dataclass
class FateCondition:
    """One conditional branch with explicit polarity and source provenance."""

    deps: frozenset[str]
    positive: bool | None
    lines: frozenset[int]
    active: bool = True


def _fate_uncomment(text: str) -> str:
    """Strip only make comments whose hash is preceded by an even backslash run."""
    slashes = 0
    for index, char in enumerate(text):
        if char == "#" and slashes % 2 == 0:
            return text[:index]
        slashes = slashes + 1 if char == "\\" else 0
    return text


def _fate_shell_uncomment(text: str) -> str:
    """Strip word-start shell comments outside quotes and escaped characters."""
    for match in FATE_SHELL_TOKEN.finditer(text):
        if match[0].startswith(("'", '"', "\\")):
            continue
        for comment in re.finditer("#", match[0]):
            index = match.start() + comment.start()
            if index == 0 or text[index - 1].isspace():
                return text[:index]
    return text


def _fate_lines(source: str, *, shell_grammar: bool = False) -> list[FateLine]:
    """Join continuations with syntax-specific comments and physical endpoints."""
    uncomment = _fate_shell_uncomment if shell_grammar else _fate_uncomment
    lines, parts, first = [], [], 1
    for number, text in enumerate(source.splitlines(), 1):
        if not parts:
            first = number
        continued = text.endswith("\\")
        parts.append(text[:-1] if continued else text)
        if not continued:
            lines.append(FateLine(uncomment(" ".join(parts)).strip(), first, number))
            parts = []
    if parts:
        lines.append(FateLine(uncomment(" ".join(parts)).strip(), first, number))
    return lines


def _fate_kind(line: FateLine) -> tuple[str, tuple[str, ...]]:
    """Classify a logical line using only the supported positive patterns."""
    for kind, pattern in FATE_PATTERNS:
        match = pattern.fullmatch(line.text)
        if match:
            return kind, match.groups()
    return "", ()


def _fate_characters(tokens: list[FateToken]) -> int:
    """Count token characters including the spaces needed to join them."""
    return sum(len(token.text) for token in tokens) + max(0, len(tokens) - 1)


def _fate_replace(
    token: FateToken, variables: dict[str, list[FateToken]], remaining: int, characters: int
) -> tuple[str, frozenset[int], int] | None:
    """Substitute embedded references only after checking token and character budgets."""
    parts, lines, cursor, cost, size = [], token.lines, 0, 1, 0
    for match in FATE_VARIABLE.finditer(token.text):
        replacements = variables.get(match[1])
        if replacements is None:
            continue
        cost += max(1, len(replacements)) - 1
        size += match.start() - cursor + _fate_characters(replacements)
        if cost > remaining or size > characters:
            return None
        parts.extend((token.text[cursor : match.start()], " ".join(t.text for t in replacements)))
        lines |= frozenset().union(*(t.lines for t in replacements))
        cursor = match.end()
    if cost > remaining or size + len(token.text) - cursor > characters:
        return None
    parts.append(token.text[cursor:])
    return "".join(parts), lines, cost


def _fate_expand_token(
    token: FateToken, variables: dict[str, list[FateToken]], remaining: int, characters: int
) -> tuple[list[FateToken], int] | None:
    """Expand one token's whole or embedded references within both remaining budgets."""
    match = FATE_VARIABLE.fullmatch(token.text)
    replacements = variables.get(match[1]) if match else None
    if replacements is not None:
        if len(replacements) > remaining or _fate_characters(replacements) > characters:
            return None
        return [FateToken(t.text, t.lines | token.lines) for t in replacements], len(replacements)
    result = _fate_replace(token, variables, remaining, characters)
    if result is None:
        return None
    text, lines, cost = result
    expanded: list[FateToken] = []
    for item in re.finditer(r"\S+", text):
        if len(expanded) == remaining:
            return None
        expanded.append(FateToken(item[0], lines))
    return expanded, cost


def _fate_expand_pass(
    tokens: list[FateToken], variables: dict[str, list[FateToken]]
) -> list[FateToken] | None:
    """Bound each expansion pass by tokens and characters while retaining provenance."""
    expanded: list[FateToken] = []
    spent = characters = 0
    for token in tokens:
        result = _fate_expand_token(
            token, variables, FATE_TOKEN_BUDGET - spent, FATE_CHAR_BUDGET - characters
        )
        if result is None:
            return None
        replacements, cost = result
        spent += cost
        characters += _fate_characters(replacements) + bool(expanded and replacements)
        if characters > FATE_CHAR_BUDGET:
            return None
        expanded.extend(replacements)
    return expanded


def _fate_expand(
    tokens: list[FateToken],
    variables: dict[str, list[FateToken]],
    overflows: set[int] | None = None,
) -> list[FateToken]:
    """Expand whole and embedded variables within eight passes and both size budgets."""
    for _ in range(8):
        expanded = _fate_expand_pass(tokens, variables)
        if expanded is None or any(t.text == "$(EXPANSION_OVER_BUDGET)" for t in tokens):
            if overflows is not None:
                overflows.update(number for token in tokens for number in token.lines)
            break
        if expanded == tokens:
            break
        tokens = expanded
    return tokens


def _fate_tokens(text: str, line: FateLine) -> list[FateToken]:
    """Attach physical provenance to make tokens within the expansion budget."""
    tokens: list[FateToken] = []
    lines = frozenset(range(line.first, line.last + 1))
    for match in re.finditer(r"\S+", text):
        if len(tokens) == FATE_TOKEN_BUDGET:
            return [FateToken("$(EXPANSION_OVER_BUDGET)", lines)]
        tokens.append(FateToken(match[0], lines))
    return tokens


def _fate_dependencies(text: str) -> frozenset[str]:
    """Extract only literal uppercase dependency words outside make expressions."""
    plain, depth = [], 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        plain.append(char if depth == 0 and char not in "$()" else " ")
    return frozenset(
        word.removeprefix("CONFIG_")
        for word in "".join(plain).replace(",", " ").split()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", word)
    )


def _fate_arguments(text: str) -> list[str]:
    """Split make arguments on top-level commas with one bounded character walk."""
    parts, start, depth = [], 0, 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        if depth < 0:
            return []
    return [*parts, text[start:].strip()] if depth == 0 else []


def _fate_guard(text: str, macros: frozenset[str]) -> tuple[frozenset[str], bool]:
    """Grant only positive ALLYES wrappers, ignoring conditions in supported if forms."""
    for _ in range(8):
        if text == "yes":
            return frozenset(), True
        if not (text.startswith("$(") and text.endswith(")")):
            break
        args = _fate_arguments(text[2:-1])
        if not args:
            break
        call = re.fullmatch(r"call\s+([A-Z][A-Z0-9_]*)", args[0])
        if call and call[1] in {"ALLYES", *macros}:
            words = " ".join(args[1:])
            return _fate_dependencies(words), True
        if args[0].startswith("if ") and len(args) == 3 and args[1] == "no":
            text = args[2]
        elif args[0].startswith("if ") and len(args) == 2:
            text = args[1]
        else:
            break
    return frozenset(), False


def _fate_condition(line: FateLine) -> FateCondition:
    """Recognize only the six allow-listed make condition polarities."""
    positive: bool | None = None
    variable = ""
    direct = re.fullmatch(r"(ifdef|ifndef)\s+([A-Z][A-Z0-9_]*)", line.text)
    compare = re.fullmatch(
        r"(ifeq|ifneq)\s*\(\s*\$\(([A-Z][A-Z0-9_]*)\)\s*,\s*(yes)?\s*\)", line.text
    )
    if direct:
        variable, positive = direct[2], direct[1] == "ifdef"
    elif compare:
        variable = compare[2]
        positive = (compare[1] == "ifeq") == (compare[3] == "yes")
    return FateCondition(
        _fate_dependencies(variable), positive, frozenset(range(line.first, line.last + 1))
    )


def _fate_scope(conditions: list[list[FateCondition]]) -> tuple[frozenset[str], frozenset[int]]:
    """Combine current branch dependencies and enclosing conditional provenance."""
    frames = [frame for group in conditions for frame in group]
    deps = frozenset().union(*(f.deps for f in frames if f.positive == f.active))
    return deps, frozenset().union(*(f.lines for f in frames))


def _fate_branch(conditions: list[list[FateCondition]], kind: str, line: FateLine) -> None:
    """Update branch groups so a single endif closes an entire else-if chain."""
    if kind == "open":
        conditions.append([_fate_condition(line)])
    elif kind == "close" and conditions:
        conditions.pop()
    elif kind == "else" and conditions:
        conditions[-1][-1].active = False
        tail = line.text[4:].strip()
        if tail:
            conditions[-1].append(_fate_condition(FateLine(tail, line.first, line.last)))


def _fate_freeze(token: FateToken, variables: dict[str, list[FateToken]]) -> FateToken:
    """Freeze undefined references while retaining unresolved target syntax and provenance."""
    text = FATE_VARIABLE.sub(
        lambda match: match[0] if match[1] in variables else f"$(:={match[1]})", token.text
    )
    return FateToken(text, token.lines)


def _fate_assign(parsed: FateFile, line: FateLine, groups: tuple[str, ...]) -> None:
    """Expand simple values now, freezing undefined $(NAME) references as $(:=NAME)."""
    name, operator, value = groups
    if operator != "+=":
        parsed.simple.discard(name)
    if operator == ":=":
        parsed.simple.add(name)
    tokens = _fate_tokens(value, line)
    if name in parsed.simple:
        tokens = _fate_expand(tokens, parsed.variables, parsed.overflows)
        tokens = [_fate_freeze(token, parsed.variables) for token in tokens]
    previous = parsed.variables.get(name, []) if operator == "+=" else []
    characters = _fate_characters(previous) + _fate_characters(tokens) + bool(previous and tokens)
    if len(previous) + len(tokens) > FATE_TOKEN_BUDGET or characters > FATE_CHAR_BUDGET:
        parsed.overflows.add(line.first)
        tokens = [FateToken(f"$({name})", frozenset({line.first}))]
        previous = []
    parsed.variables[name] = previous + tokens


def _fate_record(
    parsed: FateFile,
    line: FateLine,
    statement: tuple[str, tuple[str, ...]],
    scope: tuple[frozenset[str], frozenset[int]],
) -> None:
    """Record every local command variant and registration with dependency provenance."""
    kind, groups = statement
    if kind == "variable":
        _fate_assign(parsed, line, groups)
    elif kind in ("registration", "command"):
        targets = groups[2] if kind == "registration" else groups[0]
        guard = _fate_expand(_fate_tokens(groups[1], line), parsed.variables, parsed.overflows)
        guard_text = " ".join(t.text for t in guard)
        calls = re.findall(r"\$\(call\s+([A-Z][A-Z0-9_]*)", guard_text)
        if kind == "registration" and any(name != "ALLYES" for name in calls):
            parsed.needs_macros = True
        dependencies, classified = _fate_guard(guard_text, parsed.macros)
        provenance = scope[1] | frozenset().union(*(t.lines for t in guard))
        for target in _fate_expand(_fate_tokens(targets, line), parsed.variables, parsed.overflows):
            if not (target.text.startswith("fate-") or "$(" in target.text):
                continue
            entry = FateEntry(
                line,
                target,
                scope[0] | dependencies,
                groups[1] if kind == "command" else "",
                provenance,
                classified,
            )
            entries = parsed.registrations if kind == "registration" else parsed.commands
            entries.setdefault(target.text, []).append(entry)


def _fate_parse(source: str, macros: frozenset[str] = frozenset()) -> FateFile:
    """Parse bounded file-local make statements without executing templates or code."""
    parsed, conditions, depth = FateFile(macros=macros), [], 0
    for line in _fate_lines(source):
        statement = _fate_kind(line)
        kind, groups = statement
        if kind == "define":
            depth += 1
        elif kind == "endef":
            depth = max(0, depth - 1)
        elif depth:
            if kind == "command" and LAVFI_FORMAT.search(groups[1]):
                parsed.templates.append(line)
        elif kind in ("open", "close", "else"):
            _fate_branch(conditions, kind, line)
        else:
            _fate_record(parsed, line, statement, _fate_scope(conditions))
    return parsed


def _fate_touched(entry: FateEntry, added: set[int]) -> bool:
    """Check registration lines plus target, variable and conditional provenance."""
    return bool(
        added.intersection(range(entry.line.first, entry.line.last + 1))
        or added & (entry.target.lines | entry.provenance)
    )


def _fate_video(
    path: str,
    command: FateEntry,
    registrations: list[FateEntry],
    touched: set[int],
    report: FateReport,
) -> None:
    """Assess missing dependencies independently for each registration variant."""
    name = command.target.text
    if not registrations and _fate_touched(command, touched):
        report.lines.append(
            f"informational: {path}:{command.line.first}: {name}: registration not found"
        )
    for registration in registrations:
        missing = " ".join(dep for dep in FATE_REQUIRED if dep not in registration.deps)
        if missing:
            changed = _fate_touched(registration, touched)
            report.findings += int(changed)
            label = "finding" if changed else "pre-existing (untouched)"
            report.lines.append(
                f"{label}: {path}:{registration.line.first}: {name}: "
                f"lavfi video input needs {missing} in its registration"
            )


def _lavfi_words(command: str) -> list[str]:
    """Join adjacent shell fragments with one linear, non-backtracking character walk."""
    words, parts, quote, escaped, active = [], [], "", False, False
    for char in command:
        if escaped:
            parts.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            escaped, active = True, True
        elif quote:
            if char == quote:
                quote = ""
            else:
                parts.append(char)
        elif char in "\"'":
            quote, active = char, True
        elif char.isspace():
            if active:
                words.append("".join(parts))
                parts, active = [], False
        else:
            parts.append(char)
            active = True
    if active:
        words.append("".join(parts))
    return words


def _lavfi_skip(words: list[str], start: int) -> tuple[int, str | None]:
    """Walk input options, consuming values only when the next word is not an option."""
    skip = start
    for index in range(start, len(words)):
        if index < skip:
            continue
        word = words[index]
        if word == "-i":
            if index + 1 < len(words):
                return index + 2, words[index + 1]
            return index + 1, None
        if word == "-f" or not LAVFI_OPTION.fullmatch(word):
            return index, None
        skip = index + 1
        if skip < len(words) and not words[skip].startswith("-"):
            skip += 1
    return len(words), None


def _lavfi_graph(graph: str) -> list[str]:
    """Collect the first filter of every chain that has no input label."""
    names = []
    for raw in graph.split(";"):
        chain = raw.lstrip()
        if chain.startswith("["):
            continue
        match = LAVFI_NAME.match(chain)
        names.append(match[1] if match else "<unresolved>")
    return names


def _lavfi_inputs(command: str) -> list[str]:
    """Collect all unlabelled lavfi chain sources with a linear shell-word walk."""
    words = _lavfi_words(command)
    names: list[str] = []
    cursor = 0
    for index in range(len(words)):
        if index < cursor:
            continue
        if words[index] == "-f" and index + 1 < len(words) and words[index + 1] == "lavfi":
            stop, graph = _lavfi_skip(words, index + 2)
            if graph is not None:
                names.extend(_lavfi_graph(graph))
                cursor = stop
    return names


def _fate_kinds(command: FateEntry, sources: dict[str, str]) -> set[str]:
    """Classify every source in one command variant without changing report counts."""
    return {sources.get(name, "unknown") for name in _lavfi_inputs(command.command)}


def _fate_command(
    path: str, command: FateEntry, sources: dict[str, str], report: FateReport
) -> bool:
    """Classify every lavfi chain, counting audio-only and unclassified variants."""
    names = _lavfi_inputs(command.command)
    kinds = {sources.get(name, "unknown") for name in names}
    if "video" in kinds:
        return True
    if not kinds or kinds - {"video", "audio"}:
        report.unclassified += 1
        report.lines.append(
            f"informational: {path}:{command.line.first}: {command.target.text}: "
            f"unclassified lavfi source: {', '.join(names) or '<unresolved>'}"
        )
    if kinds == {"audio"}:
        report.audio += 1
    return False


def _fate_target_diagnostics(
    path: str, entry: FateEntry, touched: set[int], report: FateReport, registration: bool
) -> None:
    """Report one unclassified registration or touched unresolved target."""
    name = entry.target.text
    if registration and not entry.classified:
        report.lines.append(
            f"informational: {path}:{entry.line.first}: {name}: registration guard not classified"
        )
    if "$(" in name and _fate_touched(entry, touched):
        report.unresolved += 1
        report.lines.append(f"informational: {path}:{entry.line.first}: unresolved target {name}")


def _fate_diagnostics(path: str, parsed: FateFile, touched: set[int], report: FateReport) -> None:
    """Report touched unresolved targets, templates and bounded expansion failures."""
    for line in parsed.templates:
        if touched.intersection(range(line.first, line.last + 1)):
            report.lines.append(f"informational: {path}:{line.first}: template not checked")
    for number in sorted(parsed.overflows):
        report.lines.append(
            f"informational: {path}:{number}: expansion over {FATE_TOKEN_BUDGET} tokens "
            f"or {FATE_CHAR_BUDGET} characters"
        )
    for entries in (parsed.registrations, parsed.commands):
        for variants in entries.values():
            for entry in variants:
                _fate_target_diagnostics(
                    path, entry, touched, report, entries is parsed.registrations
                )


def _fate_check(
    path: str,
    parsed: FateFile,
    added: set[int],
    sources: dict[str, str],
    base: FateFile | None = None,
) -> FateReport:
    """Check each registration against all video variants and its own touched provenance."""
    report = FateReport()
    _fate_diagnostics(path, parsed, added, report)
    for name, variants in parsed.commands.items():
        if "$(" in name:
            continue
        video = [
            entry
            for entry in variants
            if LAVFI_FORMAT.search(entry.command) and _fate_command(path, entry, sources, report)
        ]
        if not video:
            continue
        report.checked += 1
        old_video = base is not None and any(
            "video" in _fate_kinds(entry, sources) for entry in base.commands.get(name, [])
        )
        newly_video = not old_video and any(_fate_touched(entry, added) for entry in video)
        registrations = parsed.registrations.get(name, [])
        touched = added | ({entry.line.first for entry in registrations} if newly_video else set())
        _fate_video(path, video[0], registrations, touched, report)
    return report


def _fate_file(
    path: str,
    source: str,
    added: set[int],
    sources: dict[str, str],
    context: FateContext | None = None,
) -> FateReport:
    """Expose pure head/base parsing, injected catalogs and deletion-aware checking."""
    context = context or FateContext()
    base = _fate_parse(context.base, context.macros) if context.base is not None else None
    return _fate_check(
        path, _fate_parse(source, context.macros), added | context.deleted, sources, base
    )


def _fate_summary(report: FateReport, rc: int = 0) -> Step:
    """Finish every fate-deps outcome with its classification and finding totals."""
    summary = (
        f"{report.checked} lavfi video test(s) checked, {report.audio} audio skipped, "
        f"{report.unclassified} unclassified, {report.unresolved} unresolved, "
        f"{report.findings} finding(s); "
        "FATE dependencies: wrapped_avframe decoder"
    )
    return Step("fate-deps", rc or int(report.findings > 0), "\n".join([*report.lines, summary]))


def _fate_sources(source: str) -> dict[str, str]:
    """Read source classifications solely from upstream filter declarations."""
    kinds = {"v": "video", "a": "audio", "av": "mixed"}
    return {
        name: kinds[kind]
        for kind, name in re.findall(
            r"extern\s+const\s+FFFilter\s+ff_(v|a|av)src_(\w+)\s*;", source
        )
    }


def _fate_macros(source: str) -> frozenset[str]:
    """Read final direct ALLYES wrapper definitions from head tests/Makefile."""
    names = set()
    for line in _fate_lines(source):
        kind, groups = _fate_kind(line)
        if kind != "variable":
            continue
        name, operator, value = groups
        names.discard(name)
        if operator == "=" and value.startswith("$(") and value.endswith(")"):
            args = _fate_arguments(value[2:-1])
            if args and args[0] == "call ALLYES":
                names.add(name)
    return frozenset(names)


def _fate_merge(report: FateReport, result: FateReport) -> None:
    """Accumulate one file's independent diagnostics and all classification totals."""
    report.checked += result.checked
    report.audio += result.audio
    report.unclassified += result.unclassified
    report.unresolved += result.unresolved
    report.findings += result.findings
    report.lines.extend(result.lines)


def _fate_makefile(tree: Path, head: str, catalogs: dict[str, str | Exception] | None) -> str:
    """Read each revision's Makefile once, retaining read errors for both FATE steps."""
    if catalogs is None:
        catalogs = {}
    if head not in catalogs:
        try:
            catalogs[head] = _git(tree, "show", f"{head}:tests/Makefile")
        except (OSError, RuntimeError, ValueError) as error:
            catalogs[head] = error
    source = catalogs[head]
    if isinstance(source, Exception):
        raise source
    return source


def _fate_needs_sources(parsed: FateFile) -> bool:
    """Identify registrations needing the head revision's filter source catalog."""
    return bool(parsed.templates) or any(
        LAVFI_FORMAT.search(entry.command)
        for variants in parsed.commands.values()
        for entry in variants
    )


def _fate_previous(
    tree: Path, base: str, name: str, existing: set[str], macros: frozenset[str] | None
) -> FateFile | None:
    """Parse an existing base file with the same macro definitions as its replacement."""
    if name not in existing:
        return None
    return _fate_parse(_git(tree, "show", f"{base}:{name}"), macros or frozenset())


def _fate_deps(
    tree: Path, base: str, head: str, catalogs: dict[str, str | Exception] | None = None
) -> Step:
    """Check changed FATE files with lazily loaded head filter and macro catalogs."""
    report, sources, macros, rc = FateReport(), None, None, 0
    try:
        names = _changed_paths(tree, base, head, "tests/fate/*.mak", added_or_modified=True)
        existing = set(
            _git(tree, "ls-tree", "-r", "--name-only", base, "--", "tests/fate").splitlines()
        )
        for name in names:
            patch = _git(tree, *DIFF_COMMON, "-U0", base, head, "--", name)
            source = _git(tree, "show", f"{head}:{name}")
            parsed = _fate_parse(source, macros or frozenset())
            if macros is None and parsed.needs_macros:
                macros = _fate_macros(_fate_makefile(tree, head, catalogs))
                parsed = _fate_parse(source, macros)
            if sources is None and _fate_needs_sources(parsed):
                sources = _fate_sources(_git(tree, "show", f"{head}:libavfilter/allfilters.c"))
            touched = _added_lines(patch) | _deleted_lines(patch)
            old_parsed = _fate_previous(tree, base, name, existing, macros)
            _fate_merge(report, _fate_check(name, parsed, touched, sources or {}, old_parsed))
    except (OSError, RuntimeError, ValueError) as error:
        rc = 2
        report.lines.append(str(error))
    return _fate_summary(report, rc)


FATE_SHAPE_RULES = ("registration", "pipe", "helper-pipe", "grep", "endian")
FATE_SHELL_TOKEN = re.compile(
    r"""\\.|'[^']*'|"(?:\\.|[^"\\])*"|\$\(|[0-9]*[<>]&(?:[0-9]+|-)|&>>?|>>?|<|"""
    r"""\|\||&&|[()|;&]|[^\\'"$()|;&<>]+|."""
)
FATE_GRAPH = re.compile(
    r"""(?:^|\s)(?:-lavfi|-vf|-filter_complex|-filter:v)\s+("(?:\\.|[^"\\])*"|'[^']*'|\S+)"""
)


@dataclass(frozen=True)
class FateShapeFinding:
    """One mechanical rule violation at an added physical source line."""

    rule: str
    line: int
    message: str


def _fate_shape_added(line: FateLine, added: set[int]) -> int | None:
    """Choose an added physical line within a logical line, if one exists."""
    return min(added.intersection(range(line.first, line.last + 1)), default=None)


def _fate_shell_operators(command: str) -> list[tuple[int, str]]:
    """Locate unquoted list and pipe operators outside balanced dollar expansions."""
    operators, depth = [], 0
    for match in FATE_SHELL_TOKEN.finditer(command):
        lexeme = match[0]
        if lexeme == "$(" or (depth and lexeme == "("):
            depth += 1
        elif depth and lexeme == ")":
            depth -= 1
        elif not depth and lexeme in {"|", "||", "&&", ";", "&"}:
            operators.append((match.start(), lexeme))
    return operators


def _fate_shape_producer(segment: str) -> str:
    """Choose the first producer after leading whole shell control words."""
    for word in segment.split():
        if word not in {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "("}:
            return word.strip("\"'")
    return ""


def _fate_shape_pipes(command: str, shell_grammar: bool) -> list[str]:
    """Report test producers while allowing only literal data-generator commands."""
    messages, start = [], 0
    for index, operator in _fate_shell_operators(command):
        first = _fate_shape_producer(command[start:index])
        test = first in {"run", "ffmpeg", "ffprobe"} or first.startswith("$")
        refused = test if shell_grammar else bool(first) and first not in {"printf", "echo", "cat"}
        if operator == "|" and refused:
            messages.append(f"{first}: pipeline hides producer exit status")
        start = index + len(operator)
    return messages


def _fate_shape_filter(item: str) -> str:
    """Remove leading input and trailing output labels from a graph filter."""
    item = re.sub(r"^(?:\s*\[[^\]]+\])*\s*", "", item).strip()
    return re.sub(r"(?:\s*\[[^\]]+\])+\s*$", "", item).strip()


def _fate_shape_filter_name(item: str) -> str:
    """Extract a filter class without its optional instance ID or arguments."""
    return item.split("=", 1)[0].split("@", 1)[0]


def _fate_shape_endian_chain(chain: str) -> list[str]:
    """Report forced endian formats handed to a non-converting next filter."""
    items = [_fate_shape_filter(item) for item in chain.split(",")]
    messages = []
    for item, consumer in zip(items, items[1:], strict=False):
        formats = re.match(r"(?:pix_fmts=)?([^:\[\]]+)", item.partition("=")[2])
        if (
            _fate_shape_filter_name(item) == "format"
            and formats
            and _fate_shape_filter_name(consumer) not in {"scale", "format"}
            and any(name.strip("\"' ").endswith(("le", "be")) for name in formats[1].split("|"))
        ):
            messages.append(f"{item}: endian format reaches consumer {consumer}")
    return messages


def _fate_shape_endian(command: str) -> list[str]:
    """Judge the next consumer of each forced endian format within its chain."""
    return [
        message
        for match in FATE_GRAPH.finditer(command)
        for chain in match[1].strip("\"'").split(";")
        for message in _fate_shape_endian_chain(chain)
    ]


def _fate_shape_commands(lines: list[FateLine]) -> list[tuple[str, str, FateLine]]:
    """Read target-specific and plain CMD assignments through the shared classifier."""
    commands = []
    for line in lines:
        kind, groups = _fate_kind(line)
        if kind == "command":
            commands.append((groups[0], groups[1], line))
        elif kind == "variable" and groups[0] == "CMD":
            commands.append(("", groups[2], line))
    return commands


def _fate_shape_library(command: str) -> str:
    """Return the matching library group for a direct run lib*/tests/ command."""
    words = command.split()
    if len(words) < 2 or words[0] != "run":
        return ""
    executable = words[1]
    if not executable.startswith("lib") or "/tests/" not in executable:
        return ""
    return "FATE_" + executable.split("/", 1)[0].upper()


def _fate_shape_registration(line: FateLine) -> tuple[str, list[str]]:
    """Extract literal append registrations using the existing FATE line kinds."""
    kind, groups = _fate_kind(line)
    if kind == "registration":
        return f"{groups[0]}-{groups[1]}", groups[2].split()
    if kind == "variable" and groups[1] == "+=":
        return groups[0], groups[2].split()
    return "", []


def _fate_shape_normalize(name: str) -> str:
    """Join conditional and enabled spellings of the same make variable."""
    return re.sub(r"-(?:\$\(.+\)|yes)$", "-yes", name)


FATE_SHAPE_REFERENCE = re.compile(r"\$\(([A-Za-z_]\w*(?:-yes|-\$\([^()]*\))?)\)")
FATE_SHAPE_FUNCTION = re.compile(r"\$\([^\s()]+\s")
FATE_SHAPE_ASSIGNMENT = re.compile(r"([A-Za-z_]\w*(?:-\$\(.+\)|-yes)?)\s*(\+=|:=|=)\s*(.*)")


def _fate_shape_ffmpeg(makefile: str) -> set[str]:
    """Discover variables whose targets require the ffmpeg executable."""
    variables = set()
    for line in _fate_lines(makefile):
        targets, separator, prerequisites = line.text.partition(":")
        if (
            separator
            and not FATE_SHAPE_ASSIGNMENT.fullmatch(line.text)
            and "ffmpeg$(PROGSSUF)$(EXESUF)" in prerequisites.split()
        ):
            variables.update(
                _fate_shape_normalize(name) for name in FATE_SHAPE_REFERENCE.findall(targets)
            )
    if not variables:
        raise ValueError("tests/Makefile: no ffmpeg$(PROGSSUF)$(EXESUF) prerequisite rule")
    return variables


def _fate_shape_references(text: str) -> list[str]:
    """Keep direct make references while skipping balanced function expressions."""
    names, depth, end = [], 0, 0
    for index, char in enumerate(text):
        if index < end:
            continue
        if depth:
            depth += (char == "(") - (char == ")")
            continue
        reference = FATE_SHAPE_REFERENCE.match(text, index)
        if reference:
            names.append(reference[1])
            end = reference.end()
        elif FATE_SHAPE_FUNCTION.match(text, index):
            depth, end = 1, index + 2
    return names


def _fate_shape_source_references(source: str) -> dict[str, set[str]]:
    """Track per-source references, replacing only unconditional non-self assignments."""
    references: dict[str, set[str]] = {}
    depth, defines = 0, 0
    for line in _fate_lines(source):
        kind, _ = _fate_kind(line)
        if kind == "define":
            defines += 1
        elif kind == "endef":
            defines = max(0, defines - 1)
        elif not defines:
            if kind == "open":
                depth += 1
            elif kind == "close":
                depth = max(0, depth - 1)
        assignment = FATE_SHAPE_ASSIGNMENT.fullmatch(line.text)
        if not assignment:
            continue
        destination = _fate_shape_normalize(assignment[1])
        if (
            assignment[2] != "+="
            and not depth
            and not defines
            and assignment[1].split("-", 1)[0] not in assignment[3]
        ):
            references[destination] = set()
        references.setdefault(destination, set()).update(
            _fate_shape_normalize(name) for name in _fate_shape_references(assignment[3])
        )
    return references


def _fate_shape_graph(sources: list[str]) -> dict[str, set[str]]:
    """Union per-source edges after depth-zero replacements outside defines without self names."""
    graph: dict[str, set[str]] = {}
    for source in sources:
        for destination, references in _fate_shape_source_references(source).items():
            for name in references:
                graph.setdefault(name, set()).add(destination)
    return graph


def _fate_shape_chain(variable: str, graph: dict[str, set[str]], ffmpeg: set[str]) -> list[str]:
    """Walk aggregation iteratively with a visited set and an edge-count bound."""
    start = _fate_shape_normalize(variable)
    pending, visited = [[start]], {start}
    bound = 1 + sum(len(destinations) for destinations in graph.values())
    for _ in range(bound):
        if not pending:
            break
        chain = pending.pop()
        if chain[-1] in ffmpeg:
            return chain
        for destination in sorted(graph.get(chain[-1], set())):
            if destination not in visited:
                visited.add(destination)
                pending.append([*chain, destination])
    return []


def _fate_shape_registrations(
    lines: list[FateLine],
    commands: list[tuple[str, str, FateLine]],
    added: set[int],
    graph: dict[str, set[str]],
    ffmpeg: set[str],
) -> list[FateShapeFinding]:
    """Pair added library registrations with their ffmpeg aggregation chains."""
    libraries = [
        (target, library)
        for targets, command, _ in commands
        if (library := _fate_shape_library(command))
        for target in targets.split()
        if target.startswith("fate-")
    ]
    findings = []
    for line in lines:
        variable, targets = _fate_shape_registration(line)
        number = _fate_shape_added(line, added)
        chain = _fate_shape_chain(variable, graph, ffmpeg) if number is not None else []
        if chain:
            for target, library in libraries:
                if target in targets:
                    message = (
                        f"{target}: {variable}: {' -> '.join(chain)}: "
                        f"register it under the matching {library} variable"
                    )
                    findings.append(FateShapeFinding("registration", number, message))
    return findings


def _fate_shape_cmd_rules(command: str) -> list[tuple[str, str]]:
    """Collect the independent CMD pipeline, grep and graph-endian rules."""
    findings = [("pipe", text) for text in _fate_shape_pipes(command, False)]
    if re.search(r"\bgrep\b", command):
        findings.append(("grep", "grep assertion needs a reference comparison"))
    findings.extend(("endian", text) for text in _fate_shape_endian(command))
    return findings


def _fate_shape_file(
    path: str,
    source: str,
    added: set[int],
    graph: dict[str, set[str]],
    ffmpeg: set[str],
) -> list[FateShapeFinding]:
    """Check shared logical lines and report only added physical provenance."""
    lines = _fate_lines(source, shell_grammar=path == "tests/fate-run.sh")
    if path == "tests/fate-run.sh":
        return [
            FateShapeFinding("helper-pipe", number, message)
            for line in lines
            if (number := _fate_shape_added(line, added)) is not None
            for message in _fate_shape_pipes(line.text, True)
        ]
    commands = _fate_shape_commands(lines)
    findings = _fate_shape_registrations(lines, commands, added, graph, ffmpeg)
    for target, command, line in commands:
        number = _fate_shape_added(line, added)
        if number is not None:
            findings.extend(
                FateShapeFinding(rule, number, f"{target or 'CMD'}: {message}")
                for rule, message in _fate_shape_cmd_rules(command)
            )
    return findings


def _fate_shape_report(
    files: int, findings: list[tuple[str, FateShapeFinding]], waiver: str
) -> Step:
    """Summarize every rule and retain findings when a reason waives rc 1."""
    counts = ", ".join(
        f"{rule}={sum(item.rule == rule for _, item in findings)}" for rule in FATE_SHAPE_RULES
    )
    summary = f"{files} file(s) checked; {counts}"
    reports = [
        f"finding: {path}:{item.line}: {item.rule}: {item.message}" for path, item in findings
    ]
    step = _waivable_step(
        "fate-shape", reports, waiver, summary, "fix FATE shape, or pass --fate-shape-reason"
    )
    if findings:
        step.output += "\n" + summary
    return step


def _fate_shape_revision(
    tree: Path, head: str, catalogs: dict[str, str | Exception] | None = None
) -> tuple[dict[str, set[str]], set[str]]:
    """Combine the shared Makefile catalog with every revision FATE fragment."""
    makefile = _fate_makefile(tree, head, catalogs)
    ffmpeg = _fate_shape_ffmpeg(makefile)
    listing = _git(
        tree,
        "grep",
        "--no-color",
        "--no-textconv",
        "--full-name",
        "--no-line-number",
        "--no-heading",
        "--no-break",
        "-H",
        "-z",
        "-e",
        "^",
        head,
        "--",
        ":(glob)tests/fate/*.mak",
        allow_no_match=True,
    )
    files: dict[str, list[str]] = {}
    for line in listing.splitlines():
        name, separator, text = line.partition("\0")
        if not separator:
            raise ValueError("FATE aggregation: malformed git grep record")
        files.setdefault(name, []).append(text)
    return _fate_shape_graph([makefile, *("\n".join(lines) for lines in files.values())]), ffmpeg


def _fate_shape(
    tree: Path,
    base: str,
    head: str,
    waiver: str,
    catalogs: dict[str, str | Exception] | None = None,
) -> Step:
    """Check added FATE and helper lines from zero-context range diffs."""
    findings = []
    try:
        names = _changed_paths(
            tree, base, head, "tests/fate/*.mak", "tests/fate-run.sh", added_or_modified=True
        )
        if not names:
            return Step(
                "fate-shape", 0, "no added or modified FATE file or fate-run.sh in the range"
            )
        graph, ffmpeg = _fate_shape_revision(tree, head, catalogs)
        for name in names:
            patch = _git(tree, *DIFF_COMMON, "-U0", base, head, "--", name)
            source = _git(tree, "show", f"{head}:{name}")
            findings.extend(
                (name, item)
                for item in _fate_shape_file(name, source, _added_lines(patch), graph, ffmpeg)
            )
    except (OSError, RuntimeError, ValueError) as error:
        return Step("fate-shape", 2, str(error))
    return _fate_shape_report(len(names), findings, waiver)


def is_test_path(name: str) -> bool:
    """Return whether a repository path has a tests directory component."""
    return "tests" in PurePosixPath(name).parts


def is_source_path(name: str) -> bool:
    """Return whether a repository path is a non-test library or fftools source."""
    parts = PurePosixPath(name).parts
    return (
        len(parts) > 1
        and name.endswith(SOURCE_SUFFIXES)
        and (parts[0].startswith("lib") or parts[0] == "fftools")
        and not is_test_path(name)
    )


def unquote_git_path(text: str) -> str:
    """Decode a C-style quoted Git path, leaving an unquoted path unchanged."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body = text[1:-1]
    decoded = bytearray()
    cursor = 0
    for match in GIT_QUOTED_ESCAPE.finditer(body):
        decoded.extend(body[cursor : match.start()].encode("utf-8", "surrogateescape"))
        code = match[1]
        decoded.append(int(code, 8) if len(code) == 3 else GIT_ESCAPE_BYTES[code])
        cursor = match.end()
    decoded.extend(body[cursor:].encode("utf-8", "surrogateescape"))
    return decoded.decode("utf-8", "surrogateescape")


def _header_path(line: str) -> str:
    """Return the path a --- a/ or +++ b/ file header names, or an empty string."""
    for marker, prefix in (("--- ", "a/"), ("+++ ", "b/")):
        if line.startswith(marker):
            name = unquote_git_path(line[len(marker) :].removesuffix("\t"))
            return name[len(prefix) :] if name.startswith(prefix) else ""
    return ""


def _patch_files(patch: str) -> list[tuple[str, list[str], list[str]]]:
    """Split a unified diff into per-file paths, hunk headers and added lines."""
    entries: list[tuple[str, list[str], list[str]]] = []
    in_header = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            entries.append(("", [], []))
            in_header = True
        elif not entries:
            continue
        elif HUNK.match(line):
            in_header = False
            entries[-1][1].append(line)
        elif in_header:
            path = _header_path(line)
            if path:
                _, hunks, added = entries[-1]
                entries[-1] = (path, hunks, added)
        elif line.startswith("+"):
            entries[-1][2].append(line[1:])
    return entries


def _hunk_functions(hunks: list[str]) -> list[str]:
    """Extract the first function name after the second @@ of each hunk header."""
    found: list[str] = []
    for line in hunks:
        parts = line.split("@@", 2)
        if len(parts) == 3:
            match = FUNCTION_NAME.search(parts[2])
            if match:
                found.append(match[1])
    return found


def reach_names(paths: list[str], patch: str) -> frozenset[str]:
    """Collect reachable source file stems and hunk-header function names."""
    names: set[str] = set()
    for path in paths:
        if is_source_path(path):
            stem = PurePosixPath(path).stem
            names.update((stem, LAVFI_PREFIX.sub("", stem)))
    for path, hunks, _ in _patch_files(patch):
        if is_source_path(path):
            names.update(_hunk_functions(hunks))
    return frozenset(names)


def test_words(patch: str, paths: list[str] | None = None) -> frozenset[str]:
    """Collect word tokens from changed test paths and their added lines."""
    words: set[str] = set()
    for path in paths or []:
        if is_test_path(path):
            words.update(WORD.findall(path))
    for path, _, added in _patch_files(patch):
        if is_test_path(path):
            words.update(WORD.findall(path))
            for line in added:
                words.update(WORD.findall(line))
    return frozenset(words)


def fix_word(message: str) -> str | None:
    """Return the first fix or defect word of a commit message outside Signed-off-by lines."""
    match = FIX_MESSAGE.search(_without_signoff(message))
    return match[0] if match else None


def _fix_commits(tree: Path, base: str, head: str) -> list[tuple[str, str, frozenset[str]]]:
    """Collect source fix commits in base..head with their reachable names."""
    records = _git(tree, "log", "-z", "--format=%H%n%B", f"{base}..{head}").split("\0")
    fixes: list[tuple[str, str, frozenset[str]]] = []
    for record in filter(None, records):
        commit, _, message = record.partition("\n")
        subject = message.partition("\n")[0].rstrip("\r")
        if not fix_word(message):
            continue
        paths = [
            path
            for path in _git(
                tree, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit
            ).split("\0")
            if path
        ]
        if any(is_source_path(path) for path in paths):
            patch = _git(
                tree,
                "diff-tree",
                "--no-commit-id",
                "-r",
                "-p",
                "-U0",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                commit,
            )
            fixes.append((commit, subject, reach_names(paths, patch)))
    return fixes


def _no_test_step(fixes: list[tuple[str, str, frozenset[str]]], reason: str) -> Step:
    """Report or waive source fix commits when no test path changed in the range."""
    headed = [f"{commit[:12]}: {subject}" for commit, subject, _ in fixes]
    if reason:
        return Step("regression-test", 0, "\n".join([f"waived: {reason}", *headed]))
    suffix = (
        "fixes C or assembly sources and no test path changed in the range; "
        "add a FATE or unit test, or pass --no-test-reason"
    )
    return Step("regression-test", 1, "\n".join(f"{item}: {suffix}" for item in headed))


def _regression_test(tree: Path, base: str, head: str, waiver: str) -> Step:
    """Require changed tests that reach each source fix commit by file or function name."""
    fixes = _fix_commits(tree, base, head)
    test_paths = [name for name in _changed_paths(tree, base, head) if is_test_path(name)]
    summary = f"{len(fixes)} fix commit(s), {len(test_paths)} test path(s) changed"
    if not fixes:
        return Step("regression-test", 0, summary)
    reason = waiver.strip()
    if not test_paths:
        return _no_test_step(fixes, reason)
    range_patch = _git(tree, *DIFF_COMMON, "-U0", base, head)
    words = test_words(range_patch, test_paths)
    unreached = [fix for fix in fixes if not fix[2] & words]
    if not unreached:
        return Step("regression-test", 0, summary)
    if reason:
        headed = [f"{commit[:12]}: {subject}" for commit, subject, _ in unreached]
        return Step("regression-test", 0, "\n".join([f"waived: {reason}", *headed]))
    lines = [
        f"{commit[:12]}: {subject}: test paths changed but none names "
        f"{', '.join(sorted(names))}; name the fixed file or function in a test, "
        "or pass --no-test-reason"
        for commit, subject, names in unreached
    ]
    return Step("regression-test", 1, "\n".join(lines))


def _without_signoff(message: str) -> str:
    """Return a commit message with Signed-off-by trailer lines removed, in any letter case."""
    return "\n".join(
        line for line in message.splitlines() if not line.lower().startswith("signed-off-by:")
    )


def uncited_diagnostic(message: str) -> str | None:
    """Return the first uncited compiler diagnostic flag in a workaround message."""
    text = _without_signoff(message)
    flag = DIAGNOSTIC_FLAG.search(text)
    if flag and WORKAROUND_WORDING.search(text) and not DIAGNOSTIC_CITATION.search(text):
        return flag[0]
    return None


def _diagnostic_citation(tree: Path, base: str, head: str) -> Step:
    """Require a bug report URL or reproducer for compiler-diagnostic workarounds."""
    log = _git(tree, "log", "-z", "--format=%H%n%B", f"{base}..{head}")
    findings: list[str] = []
    count = 0
    for record in log.split("\0"):
        if not record:
            continue
        count += 1
        commit, _, message = record.partition("\n")
        flag = uncited_diagnostic(message)
        if flag is not None:
            findings.append(
                f"{commit[:12]}: names {flag} as a workaround "
                "without a bug report URL or Reproducer: line"
            )
    if findings:
        return Step("diagnostic-citation", 1, "\n".join(findings))
    return Step("diagnostic-citation", 0, f"no uncited diagnostic workaround in {count} commits")


def unverified_claim(message: str) -> str | None:
    """Return the first blanket verification claim that is neither negated nor argued."""
    text = _without_signoff(message)
    for match in VERIFICATION_CLAIM.finditer(text):
        if NEGATED_CLAIM.search(text, max(0, match.start() - 24), match.start()):
            continue
        if match[0].lower().endswith("safe") and SAFETY_REASON.match(text, match.end()):
            continue
        return match[0]
    return None


def scope_claim(message: str) -> str | None:
    """Return the first multi-site scope claim in a commit message."""
    match = SCOPE_CLAIM.search(_without_signoff(message))
    return match[0] if match else None


def speed_claim(message: str) -> str | None:
    """Return the first encode or decode speed claim in a message without an end-to-end figure."""
    text = _without_signoff(message)
    if END_TO_END_MEASURE.search(text):
        return None
    for match in SPEED_CLAIM.finditer(text):
        if not SPEED_NEGATION.search(text, max(0, match.start() - 48), match.start()):
            return match[0]
    return None


def _unverified_findings(commit: str, message: str) -> list[str]:
    """Return the never-waivable claim findings of one commit message."""
    findings: list[str] = []
    blanket = unverified_claim(message)
    if blanket is not None:
        findings.append(
            f'{commit[:12]}: claims "{blanket}"; '
            "name the test command and platform that ran instead"
        )
    speed = speed_claim(message)
    if speed is not None:
        findings.append(
            f'{commit[:12]}: claims "{speed}" without an end-to-end measurement; '
            "name an ffmpeg -benchmark or fps result, or drop the claim"
        )
    return findings


def _source_header(line: str, old_source: bool) -> bool:
    """Return whether a +++ header line opens a library or fftools source file."""
    if line == "+++ /dev/null":
        return old_source
    return is_source_path(_header_path(line))


def _hunk_body(line: str, old_left: int, new_left: int) -> tuple[int, int] | None:
    """Return the hunk line counts left after one body line, or None when the hunk ended."""
    tag = line[:1]
    if not (old_left or new_left) or tag not in ("-", "+", " ", "\\"):
        return None
    old_used = tag in ("-", " ") and old_left > 0
    new_used = tag in ("+", " ") and new_left > 0
    return old_left - int(old_used), new_left - int(new_used)


def source_hunks(patch: str) -> int:
    """Count hunks of library or fftools source files, deletions included,
    reading headers only outside hunk bodies.
    """
    count = 0
    in_source = old_source = False
    left = (0, 0)
    for line in patch.splitlines():
        body = _hunk_body(line, *left)
        if body is not None:
            left = body
            continue
        left = (0, 0)
        lengths = HUNK_LENGTHS.match(line)
        if line.startswith("diff --git "):
            in_source = old_source = False
        elif lengths:
            count += int(in_source)
            left = (int(lengths[1] or "1"), int(lengths[2] or "1"))
        elif line.startswith("--- "):
            old_source = is_source_path(_header_path(line))
        elif line.startswith("+++ "):
            in_source = _source_header(line, old_source)
    return count


def _commit_claim_findings(tree: Path, commit: str, message: str) -> tuple[list[str], str | None]:
    """Check one commit's message and rename-aware diff for unverified or
    single-hunk scope claims.
    """
    unverified_findings = _unverified_findings(commit, message)
    scope_finding = None
    scoped = scope_claim(message)
    if scoped is not None:
        patch = _git(
            tree,
            "diff-tree",
            "--no-commit-id",
            "-r",
            "-p",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "-M",
            commit,
        )
        if source_hunks(patch) == 1:
            scope_finding = (
                f'{commit[:12]}: claims "{scoped}" but changes 1 source hunk; '
                "show each site or pass --claim-reason"
            )
    return unverified_findings, scope_finding


def _claim_scope(tree: Path, base: str, head: str, waiver: str) -> Step:
    """Reject blanket verification claims and unwaived single-hunk multi-site claims."""
    log = _git(tree, "log", "-z", "--format=%H%n%B", f"{base}..{head}")
    records = list(filter(None, log.split("\0")))
    if len(records) > MAX_CLAIM_COMMITS:
        raise RuntimeError(f"commits exceed MAX_CLAIM_COMMITS={MAX_CLAIM_COMMITS}: {len(records)}")
    unverified: list[str] = []
    scoped: list[str] = []
    for record in records:
        commit, _, message = record.partition("\n")
        rule_a, rule_b = _commit_claim_findings(tree, commit, message)
        unverified.extend(rule_a)
        if rule_b is not None:
            scoped.append(rule_b)
    if not (unverified or scoped):
        return Step("claim-scope", 0, f"no unsupported claim in {len(records)} commits")
    reason = waiver.strip()
    if scoped and reason:
        lines = [*unverified, f"waived: {reason}", *scoped]
        return Step("claim-scope", int(bool(unverified)), "\n".join(lines))
    return Step("claim-scope", 1, "\n".join([*unverified, *scoped]))


@dataclass(frozen=True)
class DocTerm:
    """A code behaviour whose added description must name one of its cases."""

    name: str
    code: re.Pattern[str]
    wording: re.Pattern[str]
    required: re.Pattern[str]
    advice: str


DOC_TERMS = (
    DocTerm(
        "non-RGB input",
        re.compile(
            r"!=\s*\w*COLOR_RGB\b|\b\w*COLOR_RGB\s*!="
            r"|!\s*\(\s*[\w.>-]+flags\s*&\s*AV_PIX_FMT_FLAG_RGB\s*\)"
            r"|!\s*\(\s*AV_PIX_FMT_FLAG_RGB\s*&\s*[\w.>-]+flags\s*\)"
        ),
        re.compile(r"\bYUV\b", re.IGNORECASE),
        re.compile(r"\bnon-RGB\b|\bgr[ae]y\b", re.IGNORECASE),
        "the code takes every non-RGB input, gray included; say non-RGB or name gray",
    ),
    DocTerm(
        "unspecified range",
        re.compile(
            r"\b(?:color|pixel)_range\s*[!=]=|[!=]=\s*[\w.>-]*\b(?:color|pixel)_range\b"
            r"|\bAVCOL_RANGE_\w+\s*[!=]=|[!=]=\s*AVCOL_RANGE_\w+"
        ),
        re.compile(r"\brange(?:in)?\b", re.IGNORECASE),
        re.compile(r"\b(?:unspecified|unknown|untagged)\b", re.IGNORECASE),
        "the code compares a colour range; say how an unspecified range is treated",
    ),
)


TEXI_INLINE = frozenset((
    "abbr", "acronym", "b", "code", "command", "dfn", "dots", "email", "emph", "env",
    "file", "i", "kbd", "key", "math", "option", "pxref", "r", "ref", "samp", "sc",
    "strong", "t", "tie", "uref", "url", "var", "verb", "w", "xref",
))  # fmt: skip
TEXI_FILLED = frozenset((
    "enumerate", "ftable", "ifclear", "ifhtml", "ifinfo", "ifnothtml", "ifnotinfo",
    "ifnottex", "ifset", "iftex", "itemize", "quotation", "table", "vtable",
))  # fmt: skip
TEXI_COMMAND = re.compile(r"@([A-Za-z]+)")
TEXI_END = re.compile(r"@end\s+([A-Za-z]+)\s*$")
# Raw block commands of texi2any 7.3 (Texinfo/Commands.pm, class 'raw'). Inside one,
# only a nested opener that its pattern matches and an @end naming the innermost raw
# block count (ParserNonXS.pm _process_raw_block_contents and
# _process_macro_block_contents); every other line is literal text.
TEXI_MACROS = re.compile(r"\s*@((?:line|r)?macro)(?:\s|$)")
TEXI_RAW = {
    "ignore": re.compile(r"\s*@(ignore)(?:@|\s|$)"),
    "linemacro": TEXI_MACROS,
    "macro": TEXI_MACROS,
    "rmacro": TEXI_MACROS,
    "verbatim": re.compile(r"\s*@(verbatim)(?:@|\s|$)"),
}
TEXI_RAW_END = re.compile(r"\s*@end\s+([A-Za-z][\w-]*)")
TEXI_FIRST_WORD = re.compile(r"@[A-Za-z]+\{[^}]*\}\S*|\S+")
WRAP_MIN_NEIGHBOUR = 50
SENTENCE_END = re.compile(r"(?:[.:;!?]|@\*)\s*$")


def is_doc_path(name: str) -> bool:
    """Return whether a repository path is a Texinfo file under doc/."""
    parts = PurePosixPath(name).parts
    return len(parts) > 1 and parts[0] == "doc" and name.endswith(".texi")


def doc_term_findings(code: str, docs: str) -> list[str]:
    """Return a finding per doc term the added code shows and the added docs omit."""
    findings: list[str] = []
    for term in DOC_TERMS:
        behaviour = term.code.search(code)
        wording = term.wording.search(docs)
        if behaviour and wording and not term.required.search(docs):
            findings.append(
                f'{term.name}: added code has "{behaviour[0].strip()}" and added docs say '
                f'"{wording[0]}"; {term.advice}'
            )
    return findings


def _added_code(tree: Path, head: str, path: str, hunks: list[str]) -> list[str]:
    """Return the added lines of a head source file with comments and literals blanked."""
    numbers = _added_lines("\n".join(hunks))
    if not numbers:
        return []
    code = _code_lines(_git(tree, "show", f"{head}:{path}"))
    return [code[number - 1] for number in sorted(numbers) if number <= len(code)]


def _doc_terms(tree: Path, base: str, head: str, waiver: str) -> Step:
    """Require added docs to name the cases that the added code covers."""
    range_patch = _git(tree, *DIFF_COMMON, "-U0", base, head)
    code: list[str] = []
    docs: list[str] = []
    for path, hunks, added in _patch_files(range_patch):
        if is_source_path(path):
            code.extend(_added_code(tree, head, path, hunks))
        elif is_doc_path(path):
            docs.extend(added)
    return _waivable_step(
        "doc-terms",
        doc_term_findings("\n".join(code), "\n".join(docs)),
        waiver,
        f"no omitted doc term in {len(docs)} added doc line(s)",
        "name the case in the docs, or pass --doc-reason",
    )


def _waivable_step(name: str, findings: list[str], waiver: str, clean: str, advice: str) -> Step:
    """Pass without findings, pass and list them under a non-blank waiver, else fail with advice."""
    if not findings:
        return Step(name, 0, clean)
    reason = waiver.strip()
    if reason:
        return Step(name, 0, "\n".join([f"waived: {reason}", *findings]))
    return Step(name, 1, "\n".join([*findings, advice]))


def _texi_raw_line(line: str, stack: list[str]) -> None:
    """Track one line inside the raw block on top of the stack the way texi2any reads it."""
    nested = TEXI_RAW[stack[-1]].match(line)
    end = TEXI_RAW_END.match(line)
    if nested:
        stack.append(nested[1])
    elif end and end[1] == stack[-1]:
        stack.pop()


def _texi_prose_line(line: str, blocks: frozenset[str], stack: list[str]) -> bool:
    """Track the open Texinfo blocks for one line and return whether it is filled prose."""
    if stack and stack[-1] in TEXI_RAW:
        _texi_raw_line(line, stack)
        return False
    end = TEXI_END.match(line)
    if end:
        if end[1] in stack:
            del stack[len(stack) - 1 - stack[::-1].index(end[1]) :]
        return False
    command = TEXI_COMMAND.match(line)
    if command and command[1] in blocks:
        stack.append(command[1])
        return False
    if command and not (command[1] in TEXI_INLINE and line[command.end() :].startswith("{")):
        return False
    return bool(line.strip()) and all(name in TEXI_FILLED for name in stack)


def _texi_blocks(lines: list[str]) -> frozenset[str]:
    """Collect Texinfo block names from TEXI_RAW and @end lines read outside raw blocks."""
    blocks = set(TEXI_RAW)
    stack: list[str] = []
    for line in lines:
        if stack:
            _texi_raw_line(line, stack)
        elif end := TEXI_END.match(line):
            blocks.add(end[1])
        elif (command := TEXI_COMMAND.match(line)) and command[1] in TEXI_RAW:
            stack.append(command[1])
    return frozenset(blocks)


def texi_prose(lines: list[str]) -> list[bool]:
    """Flag each Texinfo line that is filled prose, outside every block not in TEXI_FILLED.

    Block names come from @end lines read outside raw blocks; a raw block in TEXI_RAW
    always opens.
    """
    blocks = _texi_blocks(lines)
    stack: list[str] = []
    return [_texi_prose_line(line, blocks, stack) for line in lines]


def _unfilled(before: str, line: str, after: str) -> bool:
    """Return whether a prose line ends under half its neighbours' width with room for a word."""
    column = min(len(before), len(after))
    return (
        column >= WRAP_MIN_NEIGHBOUR
        and not SENTENCE_END.search(line)
        and 2 * len(line) < column
        and len(line) + 1 + len(TEXI_FIRST_WORD.findall(after)[0]) <= len(before)
    )


def wrap_findings(path: str, lines: list[str], added: set[int]) -> list[str]:
    """Return a finding per added prose line that breaks a filled paragraph early."""
    prose = texi_prose(lines)
    findings: list[str] = []
    for number in sorted(added):
        index = number - 1
        if not (0 < index < len(lines) - 1 and all(prose[index - 1 : index + 2])):
            continue
        before, line, after = (lines[at].rstrip() for at in (index - 1, index, index + 1))
        if _unfilled(before, line, after):
            findings.append(
                f'{path}:{number}: "{line.strip()}" ends a filled paragraph line early '
                f"({len(line)} of {len(before)} columns) and the next word fits on it"
            )
    return findings


def _doc_files(tree: Path, base: str, head: str) -> list[tuple[str, list[str], set[int]]]:
    """Return each changed doc/*.texi file with its head lines and added line numbers."""
    files: list[tuple[str, list[str], set[int]]] = []
    for path, hunks, _ in _patch_files(_git(tree, *DIFF_COMMON, "-U0", base, head)):
        added = _added_lines("\n".join(hunks))
        if is_doc_path(path) and added:
            files.append((path, _git(tree, "show", f"{head}:{path}").splitlines(), added))
    return files


def _doc_wrap(tree: Path, base: str, head: str, waiver: str) -> Step:
    """Fail added doc prose lines that end early inside a filled paragraph."""
    files = _doc_files(tree, base, head)
    return _waivable_step(
        "doc-wrap",
        [finding for path, lines, added in files for finding in wrap_findings(path, lines, added)],
        waiver,
        f"no early line break in {len(files)} changed doc file(s)",
        "join each line with the next one, or pass --doc-reason",
    )


def is_test_program_path(name: str) -> bool:
    """Return whether a repository path is a C file directly under a lib*/tests/ directory."""
    parts = PurePosixPath(name).parts
    return (
        len(parts) == 3
        and parts[0].startswith("lib")
        and parts[1] == "tests"
        and parts[2].endswith(".c")
    )


def testprogs_names(makefile: str) -> frozenset[str]:
    """Return the words that TESTPROGS assignments of a Makefile list."""
    names: set[str] = set()
    for line in _fate_lines(makefile):
        kind, groups = _fate_kind(line)
        if kind in ("variable", "registration") and groups[0] == "TESTPROGS":
            names.update(groups[-1].split())
    return frozenset(names)


def registered_test_programs(new_paths: list[str], makefiles: dict[str, str]) -> list[str]:
    """Return new lib*/tests C files whose stem their library Makefile lists in TESTPROGS."""
    programs: list[str] = []
    for path in new_paths:
        if not is_test_program_path(path):
            continue
        makefile = makefiles.get(f"{PurePosixPath(path).parts[0]}/Makefile", "")
        if PurePosixPath(path).stem in testprogs_names(makefile):
            programs.append(path)
    return programs


def _head_makefiles(tree: Path, head: str, paths: list[str]) -> dict[str, str]:
    """Read the head Makefile of each library that owns one of the paths."""
    names = sorted({f"{PurePosixPath(path).parts[0]}/Makefile" for path in paths})
    if not names:
        return {}
    listed = _git(tree, "ls-tree", "-z", "--name-only", head, "--", *names)
    return {name: _git(tree, "show", f"{head}:{name}") for name in filter(None, listed.split("\0"))}


def _test_program(tree: Path, base: str, head: str, waiver: str) -> Step:
    """Ask for a FATE sample and reference test instead of a new lib*/tests program."""
    changed = [
        name
        for name in _changed_paths(tree, base, head, "lib*/tests/*.c")
        if is_test_program_path(name)
    ]
    existing: set[str] = set()
    if changed:
        listed = _git(tree, "ls-tree", "-z", "--name-only", base, "--", *changed)
        existing = set(filter(None, listed.split("\0")))
    new_paths = [name for name in changed if name not in existing]
    programs = registered_test_programs(new_paths, _head_makefiles(tree, head, new_paths))
    if not programs:
        return Step(
            "test-program",
            0,
            f"no new registered program in {len(new_paths)} new lib*/tests file(s)",
        )
    findings = [
        f"{name}: new TESTPROGS program; upstream asks for a FATE test that runs an input "
        "sample and compares the output with a tests/ref reference"
        for name in programs
    ]
    advice = "add a FATE sample test with a tests/ref reference, or pass --test-program-reason"
    return _waivable_step("test-program", findings, waiver, "", advice)


@dataclass(frozen=True, slots=True)
class SemgrepMatch:
    """One validated Semgrep finding with its inclusive physical line span."""

    path: str
    start_line: int
    end_line: int
    check_id: str
    message_line: str


def is_c_source_path(name: str) -> bool:
    """Return whether a repository path is a non-test library or fftools C or header source."""
    return name.endswith((".c", ".h")) and is_source_path(name)


def _semgrep_span(item: dict[str, object]) -> tuple[int, int] | None:
    """Extract a positive inclusive (start_line, end_line) pair from a Semgrep result."""
    start, end = item.get("start"), item.get("end")
    if not (isinstance(start, dict) and isinstance(end, dict)):
        return None
    first, last = start.get("line"), end.get("line")
    if (
        isinstance(first, int)
        and not isinstance(first, bool)
        and isinstance(last, int)
        and not isinstance(last, bool)
        and 1 <= first <= last
    ):
        return first, last
    return None


def _parse_semgrep_match(item: object) -> SemgrepMatch | None:
    """Validate one Semgrep JSON result entry against the allow-listed schema."""
    if not isinstance(item, dict):
        return None
    path, check_id, extra = item.get("path"), item.get("check_id"), item.get("extra")
    span = _semgrep_span(item)
    # Semgrep prefixes check_id with the dotted rules path; rule ids carry no dot.
    rule_id = check_id.rpartition(".")[2] if isinstance(check_id, str) else ""
    message = extra.get("message") if isinstance(extra, dict) else None
    if (
        span is None
        or not (isinstance(path, str) and path)
        or not rule_id
        or not isinstance(message, str)
    ):
        return None
    return SemgrepMatch(path, span[0], span[1], rule_id, message.partition("\n")[0])


def _partial_parsing_path(error: object) -> str | None:
    """Return the file path of a PartialParsing error, or None for any other error."""
    if not isinstance(error, dict):
        return None
    error_type = error.get("type")
    is_partial = error_type == "PartialParsing" or (
        isinstance(error_type, list) and bool(error_type) and error_type[0] == "PartialParsing"
    )
    path = error.get("path")
    if is_partial and isinstance(path, str) and path:
        return path
    return None


def _read_semgrep_lists(
    raw_results: object,
    raw_errors: object,
    scanned_paths: frozenset[str] | None = None,
) -> tuple[list[SemgrepMatch], list[str]] | None:
    """Validate every result and PartialParsing error in Semgrep's JSON lists."""
    if not (isinstance(raw_results, list) and isinstance(raw_errors, list)):
        return None
    matches: list[SemgrepMatch] = []
    for raw in raw_results:
        match = _parse_semgrep_match(raw)
        if match is None or (scanned_paths is not None and match.path not in scanned_paths):
            return None
        matches.append(match)
    partial_paths: list[str] = []
    for raw in raw_errors:
        path = _partial_parsing_path(raw)
        if path is None:
            return None
        partial_paths.append(path)
    return matches, partial_paths


def _read_semgrep_json(
    json_path: Path, scanned_paths: frozenset[str] | None = None
) -> tuple[list[SemgrepMatch], list[str]] | None:
    """Allow-list Semgrep JSON and require paths.scanned to cover every requested path."""
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    paths = payload.get("paths")
    scanned = paths.get("scanned") if isinstance(paths, dict) else None
    if not isinstance(scanned, list) or not all(isinstance(path, str) for path in scanned):
        return None
    if scanned_paths is not None and not scanned_paths.issubset(scanned):
        return None
    raw_results, raw_errors = payload.get("results"), payload.get("errors")
    return _read_semgrep_lists(raw_results, raw_errors, scanned_paths)


def _c_rules_error(result: RunResult) -> Step:
    """Format an unrecognised semgrep_scan result as a non-waivable rc 2 step."""
    header = f"command rc: {result.returncode}"
    output = f"{header}\n{result.stdout}" if result.stdout else header
    return Step("c-rules", 2, output)


def _diff_sections(patch: str) -> list[str]:
    """Split a unified diff into one section string per diff --git file header."""
    sections: list[list[str]] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    return ["\n".join(lines) for lines in sections]


def _c_rules_touched(tree: Path, base: str, head: str) -> dict[str, set[int]]:
    """Pair changed C and header sources with rename-aware added and deletion lines."""
    raw_names = list(
        filter(
            None,
            _git(tree, *C_RULES_DIFF, "--name-only", "-z", base, head, "--", "*.c", "*.h").split(
                "\0"
            ),
        )
    )
    if len(raw_names) > MAX_PATHS:
        raise RuntimeError(f"changed paths exceed MAX_PATHS={MAX_PATHS}: {len(raw_names)}")
    paths = [name for name in raw_names if is_c_source_path(name)]
    if not paths:
        return {}
    sections = _diff_sections(_git(tree, *C_RULES_DIFF, "-U0", base, head, "--", "*.c", "*.h"))
    if len(raw_names) != len(sections):
        raise RuntimeError(
            f"git diff path/patch mismatch: {len(raw_names)} paths, {len(sections)} patches"
        )
    wanted = frozenset(paths)
    return {
        name: _added_lines(section) | _deleted_lines(section)
        for name, section in zip(raw_names, sections, strict=True)
        if name in wanted
    }


def _c_rules_report(
    matches: list[SemgrepMatch],
    partial_paths: list[str],
    touched_by_path: dict[str, set[int]],
    source_count: int,
    waiver: str,
) -> Step:
    """Classify allow-listed Semgrep matches against touched lines and apply any waiver."""
    findings: list[str] = []
    notes: list[str] = []
    for match in matches:
        if match.path not in touched_by_path:
            raise ValueError(f"unscanned result path: {match.path}")
        touched_lines = touched_by_path[match.path]
        touched = any(match.start_line <= line <= match.end_line for line in touched_lines)
        detail = f"{match.path}:{match.start_line}: {match.check_id}: {match.message_line}"
        if touched:
            findings.append(f"finding: {detail}")
        else:
            notes.append(f"pre-existing (untouched): {detail}")
    notes.extend(f"informational: {path}: PartialParsing" for path in partial_paths)
    if not findings:
        summary = f"0 finding(s) in {source_count} changed C/header source(s)"
        return Step("c-rules", 0, "\n".join([*notes, summary]))
    reason = waiver.strip()
    if reason:
        return Step("c-rules", 0, "\n".join([f"waived: {reason}", *findings, *notes]))
    advice = "fix the flagged path, or pass --c-rule-reason"
    return Step("c-rules", 1, "\n".join([*findings, *notes, advice]))


def _c_rules(tree: Path, base: str, head: str, waiver: str) -> Step:
    """Scan changed C/header sources and fail closed unless every requested path was scanned."""
    touched_by_path = _c_rules_touched(tree, base, head)
    if not touched_by_path:
        return Step("c-rules", 0, "no added or modified C or header source in the range")
    paths = list(touched_by_path)
    with tempfile.TemporaryDirectory(prefix="ffmpeg-c-rules-") as directory:
        json_path = Path(directory) / "semgrep.json"
        argv = [
            "bash",
            str(SEMGREP_SCAN),
            f"--rules={SEMGREP_FFMPEG_RULES}",
            f"--json-output={json_path}",
            "--max-target-bytes=0",
            *paths,
        ]
        result = _run(argv, tree, timeout=C_RULES_TIMEOUT)
        has_warning = any(line.startswith("semgrep_scan:") for line in result.stdout.splitlines())
        parsed = (
            _read_semgrep_json(json_path, frozenset(paths))
            if result.returncode in (0, 1) and not has_warning
            else None
        )
    if parsed is None:
        return _c_rules_error(result)
    return _c_rules_report(parsed[0], parsed[1], touched_by_path, len(paths), waiver)


DOCSTRING_COVERAGE = re.compile(
    r"^Docstring coverage: \d+\.\d{2}% "
    r"\(\d+/\d+ covered, counted: \d+, covered: \d+, threshold: \d+(?:\.\d+)?%\)$"
)


def docstring_result(returncode: int, output: str, waiver: str = "") -> Step:
    """Classify Praetor docstrings output through the coverage-line allow-list."""
    first_line = output.partition("\n")[0].removesuffix("\r")
    body = output.rstrip("\r\n")
    if returncode in (0, 1) and DOCSTRING_COVERAGE.fullmatch(first_line):
        if returncode == 0:
            return Step("docstrings", 0, body)
        reason = waiver.strip()
        if reason:
            return Step("docstrings", 0, f"waived: {reason}\n{body}")
        advice = "add a doc comment to each listed function, or pass --docstring-reason"
        return Step("docstrings", 1, f"{body}\n{advice}")
    header = f"engine exit status: {returncode}"
    return Step("docstrings", 2, f"{header}\n{body}" if body else header)


def _docstrings(tree: Path, base: str, head: str, waiver: str) -> Step:
    """Run the pinned Praetor engine's diff-scoped docstring check on base..head."""
    argv = [
        "bash",
        str(REPO_ROOT / ENGINE_SCRIPT),
        "docstrings",
        "check",
        f"--path={tree}",
        f"--base={base}",
        f"--head={head}",
    ]
    result = _run(argv, REPO_ROOT, timeout=DOCSTRINGS_TIMEOUT)
    return docstring_result(result.returncode, result.stdout, waiver)


def _collect(name: str, action: Callable[[], Step]) -> Step:
    """Run one check and capture unexpected tool or I/O errors as rc 2."""
    try:
        return action()
    except (OSError, RuntimeError, ValueError) as error:
        return Step(name, 2, str(error))


def _checks(tree: Path, base: str, head: str, waivers: Waivers) -> list[Step]:
    """Collect every independent check even when another check fails."""
    with tempfile.TemporaryDirectory(prefix="ffmpeg-upstream-lint-") as directory:
        scratch = Path(directory)
        catalogs: dict[str, str | Exception] = {}
        return [
            _collect("pre-commit", lambda: _precommit(tree, base, head, scratch)),
            _collect("commit-msg", lambda: _commit_messages(tree, base, head, scratch)),
            _collect("patcheck", lambda: _patcheck(tree, base, head, scratch)),
            _collect("developer.texi", lambda: _mechanical(tree, base, head)),
            _collect("fate-deps", lambda: _fate_deps(tree, base, head, catalogs)),
            _collect(
                "fate-shape", lambda: _fate_shape(tree, base, head, waivers.fate_shape, catalogs)
            ),
            _collect(
                "regression-test", lambda: _regression_test(tree, base, head, waivers.no_test)
            ),
            _collect("diagnostic-citation", lambda: _diagnostic_citation(tree, base, head)),
            _collect("claim-scope", lambda: _claim_scope(tree, base, head, waivers.claim)),
            _collect("doc-terms", lambda: _doc_terms(tree, base, head, waivers.doc)),
            _collect("doc-wrap", lambda: _doc_wrap(tree, base, head, waivers.doc)),
            _collect("test-program", lambda: _test_program(tree, base, head, waivers.test_program)),
            _collect("c-rules", lambda: _c_rules(tree, base, head, waivers.c_rule)),
            _collect("docstrings", lambda: _docstrings(tree, base, head, waivers.docstring)),
        ]


def _parser() -> argparse.ArgumentParser:
    """Build the command line parser with the range options and one waiver per step."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--no-test-reason",
        default="",
        help=(
            "why the fix commits in the range carry no test, or why the changed tests "
            "do not name them; waives the regression-test step"
        ),
    )
    parser.add_argument(
        "--claim-reason",
        default="",
        help="why a multi-site claim is backed by one source hunk; "
        "waives that claim-scope rule only",
    )
    parser.add_argument(
        "--doc-reason",
        default="",
        help="why the added docs may leave out a case the doc-terms step names or break a "
        "line early; waives the doc-terms and doc-wrap steps",
    )
    parser.add_argument(
        "--test-program-reason",
        default="",
        help="why a new lib*/tests program cannot be a FATE sample test; waives that step",
    )
    parser.add_argument(
        "--c-rule-reason",
        default="",
        help=(
            "why a finding in .semgrep-ffmpeg.yml does not apply to the touched path; "
            "waives findings of the c-rules step"
        ),
    )
    parser.add_argument(
        "--fate-shape-reason",
        default="",
        help="why a FATE shape finding is acceptable; waives findings, never Git or read errors",
    )
    parser.add_argument(
        "--docstring-reason",
        default="",
        help="why changed functions fall below the docstring threshold; waives that step",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run all upstream checks, print their statuses, and optionally save a report."""
    args = _parser().parse_args(argv)
    tree, base, head = args.tree.resolve(), args.base, args.head
    token = _DEADLINE.set(time.monotonic() + RUN_TIMEOUT)
    try:
        base, head = _prepare(tree, base, head)
        waivers = Waivers(
            no_test=args.no_test_reason,
            claim=args.claim_reason,
            doc=args.doc_reason,
            test_program=args.test_program_reason,
            c_rule=args.c_rule_reason,
            fate_shape=args.fate_shape_reason,
            docstring=args.docstring_reason,
        )
        steps = _checks(tree, base, head, waivers)
        _remaining(0)
    except (OSError, RuntimeError, ValueError) as error:
        steps = [Step("preflight", 2, str(error))]
    finally:
        _DEADLINE.reset(token)
    report = (
        f"tree: {tree}\nbase: {base}\nhead: {head}\n\n"
        + "\n\n".join(f"step: {step.name} rc: {step.rc}\n{step.output.rstrip()}" for step in steps)
        + "\n"
    )
    print(report, end="")
    if args.report:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(report, encoding="utf-8")
        except OSError as error:
            print(f"report: {error}", file=sys.stderr)
            return 2
    return 2 if steps[0].name == "preflight" else int(any(step.rc for step in steps))


if __name__ == "__main__":
    sys.exit(main())
