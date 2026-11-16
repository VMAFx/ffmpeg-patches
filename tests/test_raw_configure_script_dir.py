# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Replay sources relative to an executed script's own directory without running fixtures."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - only the fixed guard runs.
from pathlib import Path
from typing import NamedTuple

from scripts.raw_configure_body import BASH_VARIABLES
from tests.raw_configure_support import RawConfigureGuardBase

UNKNOWN_SOURCE = "relative script path after a source the guard cannot read; use an absolute path"
AMBIGUOUS = "assigned in a sourced file and elsewhere"
UNRESOLVED = "own-directory name assigned in a function, branch or loop"
UNSUBSTITUTED = "expansion the guard cannot substitute"
REBOUND = "can rebind the commands of the idiom"
NEWLINE = "directory path holds a newline"
NEWLINE_NAME = "resolved script file name holds a newline"
BENIGN_NEXT = {"tools/next.sh": "echo benign\n"}
DEFAULT = 'HERE=${LANE_TEST_DIR:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")}\n'
DOLLAR0 = 'HERE="$(dirname "$(readlink -f "$0")")"\n'
REALPATH = 'HERE=$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")\n'
CDPWD = 'HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)\n'
THERE = 'THERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)\n'
CDPWD_P = 'HERE="$(cd -P -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")" && pwd -P)"\n'
CD_LINK_DIR = 'HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)\n'
CD_EXTRA = 'HERE=$(cd "$(dirname "$(readlink -f "$0")")" && cd sub && pwd)\n'
SRC = '. "$HERE/lib.sh"\n'
SRC_THERE = '. "$THERE/lib.sh"\n'
UNREAD = '. "$UNSET_DIR/x.sh"\n'
RUN = "bash inner.sh\n"
CFG = "<cfg>\n"
WRITTEN = "own-directory name the script also writes another way"
OTHER = {"other/lib.sh": CFG}
PRINTF = "printf -v HERE '%s' <case>/other\n"
POSIX = "set -o posix\n"
LINK = "-> ../tools/run.sh"
RUN_NEXT = 'run_next() {\n  "$HERE/next.sh" check\n}\n'
HASH_DIRNAME = "{ builtin hash -p <case>/bin/dirname dirname; }\n"
OTHER_DIRNAME = {"bin/dirname": "echo <case>/other\n", "other/lib.sh": CFG}
ARGV0_OWN = 'BASH_ARGV0=$(dirname "$(readlink -f "$0")")\n'
ROOT_LIB = {"lib.sh": CFG}
PREFIXED = "script path in a function that the script calls with a prefix assignment"


class Case(NamedTuple):
    """One tools/run.sh body, its extra files and the expected guard verdict."""

    body: str
    code: int
    reason: str | None = None
    files: dict[str, str] | None = None
    env: dict[str, str] | None = None
    command: str = "bash tools/run.sh"


SCRIPT_DIR = {
    "default-form": Case(DEFAULT + SRC + RUN, 0),
    "dollar0-quoted": Case(DOLLAR0 + SRC + RUN, 0),
    "realpath-dashdash": Case(REALPATH + SRC + RUN, 0),
    "program-form": Case(DEFAULT + SRC + RUN, 0, command="./tools/run.sh"),
    "lib-runs-configure": Case(DEFAULT + SRC, 2, "tree/configure", {"tools/lib.sh": CFG}),
    "nested-program-configure": Case(
        DEFAULT + '"$HERE/next.sh" check\n', 2, "tools/next.sh", {"tools/next.sh": CFG}
    ),
    "lib-cd-then-relative": Case(DEFAULT + SRC + RUN, 2, "inner.sh", {"tools/lib.sh": "cd sub\n"}),
    "symlink-reads-target-dir": Case(
        DOLLAR0 + SRC,
        2,
        "tree/configure",
        {"link/run.sh": LINK, "tools/lib.sh": CFG},
        command="bash link/run.sh",
    ),
    "symlink-skips-link-dir": Case(
        DOLLAR0 + SRC,
        0,
        None,
        {"link/run.sh": LINK, "link/lib.sh": CFG},
        command="bash link/run.sh",
    ),
    "override-in-env": Case(DEFAULT + SRC + RUN, 2, UNKNOWN_SOURCE, env={"LANE_TEST_DIR": "/x"}),
    "override-assigned": Case("LANE_TEST_DIR=/x\n" + DEFAULT + SRC + RUN, 2, UNKNOWN_SOURCE),
    "override-assigned-lib-unresolved": Case(
        "LANE_TEST_DIR=/x\n" + DEFAULT + SRC, 2, UNKNOWN_SOURCE, {"tools/lib.sh": CFG}
    ),
    "override-sourced-before": Case(
        ". <case>/tools/pre.sh\n" + DEFAULT + SRC + RUN,
        2,
        UNKNOWN_SOURCE,
        {"tools/pre.sh": "LANE_TEST_DIR=/x\n"},
    ),
    "cd-before-assignment": Case("cd <case>\n" + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE),
    "cd-before-assignment-lib-unresolved": Case(
        "cd <case>\n" + DOLLAR0 + SRC, 2, UNKNOWN_SOURCE, {"tools/lib.sh": CFG}
    ),
    "unread-source-before-assignment": Case(
        '. "$UNSET_DIR/x.sh"\n' + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE
    ),
    "unread-source-before-lib-unresolved": Case(
        '. "$UNSET_DIR/x.sh"\n' + DOLLAR0 + SRC, 2, UNKNOWN_SOURCE, {"tools/lib.sh": CFG}
    ),
    "assigned-twice": Case(DOLLAR0 + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE),
    "append-assignment": Case(DOLLAR0 + "HERE+=/x\n" + SRC + RUN, 2, UNKNOWN_SOURCE),
    "lib-reassigns-name": Case(
        DOLLAR0 + SRC + 'bash "$HERE/inner.sh"\n', 2, AMBIGUOUS, {"tools/lib.sh": "HERE=/x\n"}
    ),
    "lib-reassigns-name-source": Case(
        DOLLAR0 + SRC + '. "$HERE/inner.sh"\n', 2, AMBIGUOUS, {"tools/lib.sh": "HERE=/x\n"}
    ),
    "sourced-not-executed": Case(DEFAULT + SRC + RUN, 2, UNKNOWN_SOURCE, command=". tools/run.sh"),
    "source-before-assignment": Case(SRC + DOLLAR0 + RUN, 2, UNKNOWN_SOURCE),
    "assignment-in-function": Case("f() {\n" + DOLLAR0 + "}\n" + SRC + RUN, 2, UNRESOLVED),
    "assignment-in-function-next-program": Case(
        "f() {\n" + DOLLAR0 + "}\nf\n" + '"$HERE/next.sh" check\n', 2, UNRESOLVED, BENIGN_NEXT
    ),
    "assignment-in-branch-next-program": Case(
        "if true; then\n" + DOLLAR0 + "fi\n" + '"$HERE/next.sh" check\n', 2, UNRESOLVED, BENIGN_NEXT
    ),
    "assignment-in-function-braced-default": Case(
        "f() {\n" + DOLLAR0 + "}\nf\n" + 'bash "${HERE:-.}/next.sh"\n', 2, UNRESOLVED, BENIGN_NEXT
    ),
    "assignment-in-function-longer-name": Case(
        "f() {\n" + DOLLAR0 + "}\nf\n" + 'bash "$HERE_X/next.sh"\n', 0, None, BENIGN_NEXT
    ),
    "local-in-function-reference": Case(
        "f() {\n  local " + DOLLAR0.lower() + '  bash "$here/next.sh"\n}\n',
        2,
        UNRESOLVED,
        BENIGN_NEXT,
    ),
    "local-in-function-unrelated-path": Case(
        "f() {\n  local " + DOLLAR0.lower() + '  echo "$here"\n}\n' + RUN, 0
    ),
    "override-assigned-next-program": Case(
        "LANE_TEST_DIR=/x\n" + DEFAULT + '"$HERE/next.sh" check\n',
        2,
        UNKNOWN_SOURCE,
        {"tools/next.sh": "echo benign\n"},
    ),
    "override-assigned-after": Case(DEFAULT + "LANE_TEST_DIR=/x\n" + SRC + RUN, 2, UNKNOWN_SOURCE),
    "unread-source-after-assignment-program": Case(
        DOLLAR0 + UNREAD + '"$HERE/next.sh" check\n',
        2,
        UNKNOWN_SOURCE,
        {"tools/next.sh": "echo benign\n"},
    ),
    "unread-source-after-assignment-lib": Case(DOLLAR0 + UNREAD + SRC, 2, UNKNOWN_SOURCE),
    "unread-source-keeps-configure-literal": Case(
        "CFG_PATH=<tree>/configure\n" + DOLLAR0 + UNREAD + '"$CFG_PATH" --prefix=/x\n',
        2,
        "tree/configure",
    ),
    "unread-source-keeps-plain-literal": Case(
        "NEXT=<case>/tools/next.sh\n" + DOLLAR0 + UNREAD + 'bash "$NEXT"\n',
        0,
        None,
        {"tools/next.sh": "echo benign\n"},
    ),
    "declare-lowercase": Case("declare -l " + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "export-assignment": Case("export " + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE),
    "local-in-function-keeps-top-level": Case(
        DEFAULT + "f() {\n  local " + DOLLAR0.lower() + "}\n" + SRC + RUN, 0
    ),
    "function-before-branch-assignment": Case(
        RUN_NEXT + "if true; then\n" + DOLLAR0 + "fi\nrun_next\n", 2, UNRESOLVED, BENIGN_NEXT
    ),
    "function-before-function-assignment": Case(
        'run_next() {\n  bash "${HERE}/next.sh"\n}\nf() {\n' + DOLLAR0 + "}\nf\nrun_next\n",
        2,
        UNRESOLVED,
        BENIGN_NEXT,
    ),
    "function-before-assignment-longer-name": Case(
        'run_next() {\n  "$HERE_X/next.sh"\n}\nif true; then\n' + DOLLAR0 + "fi\nrun_next\n",
        0,
        None,
        BENIGN_NEXT,
    ),
    "function-before-top-level-configure": Case(
        RUN_NEXT + DOLLAR0 + "run_next\n", 2, "tools/next.sh", {"tools/next.sh": CFG}
    ),
    "function-before-top-level-benign": Case(
        RUN_NEXT + DOLLAR0 + "run_next\n", 0, None, BENIGN_NEXT
    ),
    "function-before-uncertain-assignment": Case(
        RUN_NEXT + "cd <case>\n" + DOLLAR0 + "run_next\n", 2, UNKNOWN_SOURCE, BENIGN_NEXT
    ),
    "function-source-before-assignment": Case(
        'load() {\n  . "$HERE/lib.sh"\n}\n' + DOLLAR0 + "load\n", 2, UNKNOWN_SOURCE
    ),
    "function-before-assignment-operator-form": Case(
        'run_next() {\n  "${HERE:-.}/next.sh" check\n}\n' + DOLLAR0 + "run_next\n",
        2,
        UNSUBSTITUTED,
        {"tools/next.sh": CFG},
    ),
    "top-level-operator-form": Case(
        DOLLAR0 + '"${HERE%/}/next.sh" check\n', 2, UNSUBSTITUTED, {"tools/next.sh": CFG}
    ),
    "function-before-assignment-partial-expansion": Case(
        'run_next() {\n  bash "$HERE/$NEXT_NAME.sh"\n}\n' + DOLLAR0 + "run_next\n",
        2,
        UNSUBSTITUTED,
        BENIGN_NEXT,
    ),
    "cd-pwd-form": Case(CDPWD + SRC + RUN, 0),
    "cd-pwd-physical": Case(CDPWD_P + SRC + RUN, 0),
    "cd-pwd-lib-runs-configure": Case(CDPWD + SRC, 2, "tree/configure", {"tools/lib.sh": CFG}),
    "cd-pwd-symlink-reads-target-dir": Case(
        CDPWD + SRC,
        2,
        "tree/configure",
        {"link/run.sh": LINK, "tools/lib.sh": CFG},
        command="bash link/run.sh",
    ),
    "cd-link-dir-unresolved": Case(CD_LINK_DIR + SRC + RUN, 2, UNKNOWN_SOURCE),
    "cd-extra-command-unresolved": Case(CD_EXTRA + SRC + RUN, 2, UNKNOWN_SOURCE),
    "cd-pwd-two-names": Case(CDPWD + THERE + SRC_THERE + RUN, 0),
    "cd-pwd-two-names-lib-runs-configure": Case(
        CDPWD + THERE + SRC_THERE, 2, "tree/configure", {"tools/lib.sh": CFG}
    ),
    "subst-cd-keeps-relative-paths": Case("X=$(cd sub && bash ./inner.sh)\n", 2, "tree/configure"),
    "subst-cd-leaves-caller-dir": Case("X=$(cd sub && pwd)\n" + RUN, 0),
    "subshell-cd-leaves-caller-dir": Case("(cd sub && true)\n" + RUN, 0),
    "process-subst-cd-leaves-caller-dir": Case("true <(cd sub && pwd)\n" + RUN, 0),
    "brace-group-cd-moves-caller": Case("{ cd sub; }\n" + RUN, 2, "tree/configure"),
    "shadowed-pwd": Case('pwd() { echo "<case>/sub"; }\n' + CDPWD + SRC + RUN, 2, REBOUND),
    "shadowed-dirname": Case(
        'dirname() { echo "<case>/sub"; }\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "shadowed-cd-function-keyword": Case(
        'function cd { builtin cd "<case>/sub"; }\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "shadowed-realpath": Case(
        'realpath() { echo "<case>/sub/x"; }\n' + REALPATH + SRC + RUN, 2, REBOUND
    ),
    "shadowed-readlink-in-sourced-file": Case(
        '. "<case>/tools/shadow.sh"\n' + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
        {"tools/shadow.sh": 'readlink() { echo "<case>/sub/x"; }\n'},
    ),
    "alias-before": Case(
        "shopt -s expand_aliases\nalias pwd='echo <case>/sub'\n" + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "enable-before": Case("enable -n pwd\n" + CDPWD + SRC + RUN, 2, REBOUND),
    "hash-p-before": Case("hash -p /bin/echo dirname\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "path-before": Case("PATH=<case>/bin:$PATH\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "export-path-before": Case("export PATH=<case>/bin:$PATH\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "path-same-statement": Case("PATH=<case>/bin " + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "unset-path-before": Case("unset PATH\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "printf-v-path-before": Case(
        "printf -v PATH '%s' <case>/bin\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "nameref-path-before": Case(
        "declare -n P=PATH\nP=<case>/bin\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "bash-func-in-environment": Case(
        DOLLAR0 + SRC + RUN, 2, REBOUND, env={"BASH_FUNC_dirname%%": "() {  echo /x; }"}
    ),
    "bash-env-in-environment": Case(
        DOLLAR0 + SRC + RUN, 2, REBOUND, env={"BASH_ENV": "/nonexistent/rc"}
    ),
    "path-after-idiom": Case(DOLLAR0 + "PATH=<case>/bin:$PATH\n" + SRC + RUN, 0),
    "path-command-prefix": Case("PATH=/usr/bin:/bin true\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "unrelated-function": Case("helper() { echo hi; }\n" + DOLLAR0 + SRC + RUN, 0),
    "pwd-function-after-idiom": Case(CDPWD + "pwd() { echo x; }\n" + SRC + RUN, 0),
    "hash-r-before": Case("hash -r\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "for-path-before": Case(
        "for PATH in <case>/bin; do :; done\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "for-path-positional-before": Case("for PATH; do :; done\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "select-path-before": Case(
        "select PATH in <case>/bin; do break; done\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "colon-path-prefix-before": Case("PATH=<case>/bin :\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "export-path-prefix-before": Case(
        "PATH=<case>/bin export Y=1\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "eval-path-prefix-before": Case(
        "PATH=<case>/bin eval true\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "dot-path-prefix-before": Case(
        'PATH=<case>/bin . "<case>/tools/empty.sh"\n' + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
        {"tools/empty.sh": "X=1\n"},
    ),
    "c-style-for-path-before": Case(
        "for ((PATH=1; PATH<2; PATH++)); do :; done\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "arith-path-before": Case("(( PATH=1 ))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "arith-subscript-path-before": Case("(( PATH[0]=1 ))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "arith-expansion-path-before": Case(": $((PATH=5))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "assign-default-path-before": Case(": ${PATH:=<case>/bin}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "assign-default-plain-path-before": Case(
        ": ${PATH=<case>/bin}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "wait-p-path-before": Case("wait -p PATH\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "compgen-v-path-before": Case(
        "compgen -V PATH -W <case>/bin\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "coproc-path-before": Case("coproc PATH { true; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "read-subscript-path-before": Case("read PATH[0]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "unset-quoted-path-before": Case("unset 'PATH'\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "unset-double-quoted-path-before": Case('unset "PATH"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "unset-split-quoted-path-before": Case('unset P"ATH"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "export-quoted-path-before": Case(
        "export 'PATH=<case>/bin'\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "declare-quoted-path-before": Case(
        'declare "PATH=<case>/bin"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "for-other-before": Case("for d in a b; do :; done\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "function-path-prefix-before": Case(
        "f() { :; }\nPATH=<case>/bin f\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "default-expansion-path-before": Case(
        ": ${PATH:-<case>/bin}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "arith-other-before": Case("(( n=1 ))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "for-path-after-idiom": Case(DOLLAR0 + "for PATH in <case>/bin; do :; done\n" + SRC + RUN, 0),
    "let-comma-path-before": Case("let '0,PATH=1'\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "let-increment-path-before": Case("let PATH++\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "let-expansion-before": Case('let "$X"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "let-other-before": Case("let 'n=1'\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "printf-attached-path-before": Case(
        "printf -vPATH '%s' <case>/bin\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "read-attached-path-before": Case(
        "read -raPATH <<< <case>/bin\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "printf-attached-other-before": Case("printf -vX '%s' 1\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "arith-literal-value-path-before": Case(
        "x='PATH=7'\n: $((x))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "test-eq-path-before": Case("[[ 1 -eq PATH=7 ]]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "subscript-path-before": Case("a[PATH=7]=v\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "substring-offset-path-before": Case(
        's=abc\n: "${s:PATH=1:1}"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "integer-assign-path-before": Case(
        "declare -i n\nn='PATH=7'\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "echo-path-read-before": Case('echo "PATH is $PATH"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "path-compare-before": Case("[[ $PATH == /x ]]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "builtin-export-path-before": Case(
        "builtin export PATH=<case>/bin\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "command-export-path-before": Case(
        "command export PATH=<case>/bin\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "builtin-declare-nameref-before": Case(
        "builtin declare -n P=PATH\nP=<case>/bin\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "builtin-export-other-before": Case("builtin export X=1\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "declare-quoted-nameref-before": Case(
        'declare "-n" P=PATH\nP=<case>/bin\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "declare-quoted-integer-before": Case('declare "-i" n=1\n' + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "export-n-other-before": Case("export -n X\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "bash-argv0-before": Case(
        "BASH_ARGV0=<case>/other/anchor.sh\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "bash-cmds-before": Case(
        "BASH_CMDS[dirname]=<case>/bin/dirname\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "bash-cmds-cdpwd-before": Case(
        "BASH_CMDS[dirname]=<case>/bin/dirname\n" + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "bash-cmds-cdpwd-p-before": Case(
        "BASH_CMDS[realpath]=<case>/bin/realpath\n" + CDPWD_P + SRC + RUN, 2, REBOUND
    ),
    "bash-cmds-cdpwd-after-idiom": Case(
        CDPWD + "BASH_CMDS[dirname]=<case>/bin/dirname\n" + SRC + RUN, 0
    ),
    "bash-aliases-cdpwd-before": Case("BASH_ALIASES[cd]=false\n" + CDPWD + SRC + RUN, 2, REBOUND),
    "bash-aliases-before": Case("BASH_ALIASES[dirname]=false\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "execignore-before": Case("EXECIGNORE=/usr/bin/dirname\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "read-bash-cmds-before": Case(
        "read -r 'BASH_CMDS[dirname]' <<< <case>/bin/dirname\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "declare-argv0-before": Case(
        "declare BASH_ARGV0=<case>/other/anchor.sh\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "arith-argv0-before": Case(": $((BASH_ARGV0=7))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "printf-attached-argv0-before": Case(
        "printf -vBASH_ARGV0 '%s' <case>/other/anchor.sh\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "lc-all-prefix-before": Case("LC_ALL=C true\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "undocumented-bash-name-before": Case("BASH_CMDS_X=1\n" + DOLLAR0 + SRC + RUN, 0),
    "echo-path-assign-before": Case("echo 'PATH=/usr/bin'\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "printf-path-assign-before": Case(
        "printf '%s\\n' 'PATH=/usr/bin'\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "builtin-echo-path-assign-before": Case(
        "builtin echo 'PATH=/usr/bin'\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "subst-echo-path-before": Case(
        "x=$(echo 'PATH=7')\n: $((x))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "redirect-echo-path-before": Case("echo 'PATH=7' > f\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "pipe-echo-path-before": Case("echo 'PATH=7' | read -r x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "printf-v-capture-path-before": Case(
        "printf -v x '%s' 'PATH=7'\n: $((x))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "function-echo-path-before": Case("f() { echo 'PATH=7'; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "eval-echo-path-before": Case("eval \"echo 'PATH=7'\"\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "sourced-echo-capture-before": Case(
        'x=$(. "<case>/tools/print.sh")\n: $((x))\n' + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
        {"tools/print.sh": "echo 'PATH=7'\n"},
    ),
    "devstdout-readback-before": Case(
        "echo 'PATH=7'\nread -r x < /dev/stdout\n: $((x))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "exec-redirect-readback-before": Case(
        "exec 3>&1 >f\necho 'PATH=7'\nexec >&3\nread -r x < f\n: $((x))\n" + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
    ),
    "proc-fd-readback-before": Case(
        "echo 'PATH=7'\nx=$(< /proc/self/fd/1)\n: $((x))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "newline-name-symlink": Case(
        DOLLAR0 + SRC + RUN,
        2,
        NEWLINE_NAME,
        {"tools/\n": DOLLAR0 + SRC + RUN, "tools/run.sh": "-> \n", "lib.sh": CFG},
    ),
    "r8-cr-printf-dynamic-dest": Case(
        "v=${missing:-BASH_ARGV0}\nprintf -v \"$v\" '%s' <case>/other/anchor.sh\n"
        + DOLLAR0
        + SRC
        + RUN,
        2,
        REBOUND,
    ),
    "r8-cr-printf-dynamic-dest-cdpwd": Case(
        "v=${missing:-BASH_ARGV0}\nprintf -v \"$v\" '%s' <case>/other/anchor.sh\n"
        + CDPWD
        + SRC
        + RUN,
        2,
        REBOUND,
    ),
    "r8-cr-declare-i-expanded": Case(
        'p=PA\nq=TH\ndeclare -i n\nn="${p}${q}=7"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-cr-declare-i-expanded-cdpwd": Case(
        'p=PA\nq=TH\ndeclare -i n\nn="${p}${q}=7"\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r8-arith-expansion-dynamic": Case(
        "p=PA\nq=TH\n: $((${p}${q}=7))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-arith-command-dynamic": Case(
        "p=PA\nq=TH\n(( ${p}${q}=7 ))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-declare-i-value-expanded": Case(
        'p=PA\nq=TH\ndeclare -i n="${p}${q}=7"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-integer-append-expanded": Case(
        'p=PA\nq=TH\ndeclare -i n\nn+="${p}${q}=7"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-typeset-ix-expanded": Case(
        'p=PA\nq=TH\ntypeset -ix n\nn="${p}${q}=7"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-integer-from-env-var": Case("declare -i n\nn=$UNSET_X\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-integer-array-element": Case(
        'p=PA\nq=TH\ndeclare -ai a\na[0]="${p}${q}=7"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-subscript-arith-dynamic": Case(
        "p=PA\nq=TH\ndeclare -a a\na[${p}${q}=7]=1\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-dbracket-eq-dynamic": Case(
        "p=PA\nq=TH\n[[ ${p}${q}=7 -eq 7 ]]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-read-dynamic-dest": Case(
        'v=${missing:-BASH_ARGV0}\nread -r "$v" <<< <case>/other/anchor.sh\n' + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
    ),
    "r8-declare-dynamic-name": Case(
        'v=${missing:-BASH_ARGV0}\ndeclare "$v=<case>/other/anchor.sh"\n' + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
    ),
    "r8-printf-braced-dynamic": Case(
        "v=${missing:-BASH_ARGV0}\nprintf -v \"${v}\" '%s' <case>/other/anchor.sh\n"
        + DOLLAR0
        + SRC
        + RUN,
        2,
        REBOUND,
    ),
    "r8-printf-attached-dynamic": Case(
        "v=${missing:-BASH_ARGV0}\nprintf -v\"$v\" '%s' <case>/other/anchor.sh\n"
        + DOLLAR0
        + SRC
        + RUN,
        2,
        REBOUND,
    ),
    "r8-mapfile-dynamic-dest": Case(
        'v=${missing:-BASH_ARGV0}\nmapfile -t "$v" <<< <case>/other/anchor.sh\n'
        + DOLLAR0
        + SRC
        + RUN,
        2,
        REBOUND,
    ),
    "r8-export-dynamic-name": Case(
        'v=${missing:-PATH}\nexport "$v=<case>/other"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-mapfile-dynamic-path": Case(
        'v=${missing:-PATH}\nmapfile -t "$v" <<< <case>/other\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-subscript-read-dynamic": Case(
        'p=PA\nq=TH\ndeclare -a a\n: "${a[${p}${q}=7]}"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-substring-offset-dynamic": Case(
        'p=PA\nq=TH\nx=abc\n: "${x:${p}${q}=7}"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-getopts-dynamic-dest": Case(
        'v=${missing:-BASH_ARGV0}\ngetopts a "$v" -a\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-local-i-in-function": Case(
        'p=PA\nq=TH\nf() {\n  local -i n\n  n="${p}${q}=7"\n}\nf\n' + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
    ),
    "r8-dbracket-env-operand": Case("[[ $UNSET_X -eq 0 ]]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-indirect-assign-default": Case(
        'v=${missing:-BASH_ARGV0}\n: "${!v:=<case>/other/anchor.sh}"\n' + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
    ),
    "r8-read-array-dynamic": Case(
        'v=${missing:-PATH}\nread -r -a "$v" <<< <case>/other\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-wait-p-dynamic": Case(
        'v=${missing:-BASH_ARGV0}\ntrue &\nwait -p "$v" -n\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-compgen-V-dynamic": Case(
        'v=${missing:-PATH}\ncompgen -V "$v" -W <case>/other\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-unset-dynamic": Case('v=${missing:-PATH}\nunset "$v"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-printf-subscript-arith": Case(
        'p=PA\nq=TH\ndeclare -a a\nprintf -v "a[${p}${q}=7]" x\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-dollar-bracket-arith": Case(
        "p=PA\nq=TH\n: $[${p}${q}=7]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-cfor-dynamic": Case(
        "p=PA\nq=TH\nfor (( ${p}${q}=7; 0; )); do :; done\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-bare-ref-expansion": Case(
        "x=${missing:-PATH=7}\n: $((x))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-bare-ref-command": Case(
        "x=${missing:-PATH=7}\n(( x ))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-bare-ref-dbracket": Case(
        "x=${missing:-PATH=7}\n[[ x -eq 7 ]]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-bare-ref-subscript": Case(
        "x=${missing:-PATH=7}\ndeclare -a a\na[x]=1\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-bare-ref-integer": Case(
        "x=${missing:-PATH=7}\ndeclare -i n\nn=x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-bare-ref-let": Case("x=${missing:-PATH=7}\nlet y=x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-positional-arith": Case(": $(( $1 ))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-safe-integer-literal-sum": Case(
        'declare -i n\nn="1+2"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-safe-printf-literal-dest": Case("printf -v x '%s' y\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-safe-read-dynamic-prompt": Case(
        'm=hi\nread -r -p "$m" x <<< y\n' + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-safe-integer-number": Case("declare -i n\nn=3\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-safe-arith-read-only": Case("p=1\nq=2\n: $((p+q))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-safe-array-element": Case("declare -a a\na[0]=x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-safe-dbracket-literal": Case("[[ 1 -eq 1 ]]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-safe-dbracket-argc": Case("[[ $# -eq 0 ]]\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-safe-assoc-dynamic-subscript": Case(
        "declare -A m\nm[$PWD]=1\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r8-safe-arith-status": Case(": $(( $? + 1 ))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r8-safe-string-append": Case("n=1\nn+=x\n" + DOLLAR0 + SRC + RUN, 0),
    "r8-safe-literal-int-ref": Case("x=3\n: $((x + 1))\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-cr-read-herestring-arith": Case(
        'x=3\np=BASH_\nq=ARGV0\nread -r x <<< "${p}${q}=7"\n: $((x))\n' + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
    ),
    "r9-cr-read-herestring-arith-cdpwd": Case(
        'x=3\np=BASH_\nq=ARGV0\nread -r x <<< "${p}${q}=7"\n: $((x))\n' + CDPWD + SRC + RUN,
        2,
        REBOUND,
    ),
    "r9-cr-unset-assoc-subscript": Case(
        "declare -A a\nunset a\np=BASH_\nq=ARGV0\na[${p}${q}=7]=v\n" + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
    ),
    "r9-cr-unset-assoc-subscript-cdpwd": Case(
        "declare -A a\nunset a\np=BASH_\nq=ARGV0\na[${p}${q}=7]=v\n" + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r9-cr-printf-percent-n": Case("printf '%n' BASH_ARGV0\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-cr-printf-percent-n-cdpwd": Case(
        "printf '%n' BASH_ARGV0\n" + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r9-cr-subshell-assoc-subscript": Case(
        "(declare -A a)\np=BASH_\nq=ARGV0\na[${p}${q}=7]=v\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r9-cr-subshell-assoc-subscript-cdpwd": Case(
        "(declare -A a)\np=BASH_\nq=ARGV0\na[${p}${q}=7]=v\n" + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r9-allow-set-euo-pipefail": Case("set -euo pipefail\n" + DOLLAR0 + SRC + RUN, 0),
    "r9-allow-set-o-pairs": Case("set -o errexit -o nounset\n" + CDPWD + SRC + RUN, 0),
    "r9-allow-static-assigns": Case(
        "a=1\nb=x/y\nc='lit'\nd=\"$a-${b}\"\ne=$'t'\nf=\n" + DOLLAR0 + SRC + RUN, 0
    ),
    "r9-allow-plain-assign-list": Case("a=1 b=2\n" + DOLLAR0 + SRC + RUN, 0),
    "r9-allow-comment-colon-true": Case("# setup\n:\ntrue\n" + DOLLAR0 + SRC + RUN, 0),
    "r9-allow-source-before": Case(
        ". <case>/tools/pre.sh\n" + DOLLAR0 + SRC + RUN, 0, None, {"tools/pre.sh": "X=1\n"}
    ),
    "r9-sourced-echo-before": Case(". <case>/tools/inner.sh\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-cd-or-exit": Case("cd <case> || exit 1\n" + DOLLAR0 + SRC + RUN, 2, UNKNOWN_SOURCE),
    "r9-echo-names-line": Case("echo hi\n" + DOLLAR0 + SRC + RUN, 2, "after line 2 (echo)"),
    "r9-set-o-posix": Case("set -o posix\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-set-x": Case("set -x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-set-a": Case("set -a\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-quoted-set": Case("'set' -e\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-default-expansion-value": Case("X=${Y:-z}\n" + DOLLAR0 + SRC + RUN, 0),
    "r9-command-substitution-value": Case("X=$(true)\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-cdpath-assign": Case("CDPATH=/x\n" + CDPWD + SRC + RUN, 2, REBOUND),
    "r9-env-name-assign": Case("LANE_X=2\n" + DOLLAR0 + SRC + RUN, 2, REBOUND, env={"LANE_X": "1"}),
    "r9-redirected-true": Case("true > /dev/null\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-prefix-assign-true": Case("X=1 true\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-if-compound": Case("if true; then :; fi\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-array-assign": Case("a=(x)\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-subscript-assign": Case("a[0]=x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r9-function-named-set": Case("set() { :; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-plain-word": Case("export LANE_R10=squash_merge.sh\n" + DOLLAR0 + SRC + RUN, 0),
    "r10-export-name": Case("LANE_R10=1\nexport LANE_R10\n" + DOLLAR0 + SRC + RUN, 0),
    "r10-landing-squash-shape": Case(
        "set -euo pipefail\nexport LANE_CALLER=squash_merge.sh\n" + DOLLAR0 + SRC + RUN, 0
    ),
    "r10-landing-reland-shape": Case(
        "set -euo pipefail\nR=a/b; pr=$1; wt=$2\nT=${LANE_ROOT:-/home/x/triage-1}\n"
        + DOLLAR0
        + SRC
        + RUN,
        0,
    ),
    "r10-default-dash": Case("X=${Y-z}\n" + DOLLAR0 + SRC + RUN, 0),
    "r10-default-empty": Case("X=${Y:-}\n" + DOLLAR0 + SRC + RUN, 0),
    "r10-default-concat": Case("X=${Y:-/a}/b\n" + DOLLAR0 + SRC + RUN, 0),
    "r10-default-quoted": Case('X="${Y:-/a}"\n' + DOLLAR0 + SRC + RUN, 0),
    "r10-default-path-read": Case("X=${PATH:-/bin}\n" + DOLLAR0 + SRC + RUN, 0),
    "r10-function-longer-name-body": Case("f() { MYPATH=x; }\n" + DOLLAR0 + SRC + RUN, 0),
    "r10-function-bare-path-word": Case("f() { echo PATH; }\n" + DOLLAR0 + SRC + RUN, 0),
    "r10-export-bash-variable": Case("export IFS=x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-path": Case("export PATH=/x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-bash-name": Case("export BASH_ENV\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-ld-preload": Case("export LD_PRELOAD=/x.so\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-ld-name": Case(
        "LD_AUDIT=/x.so\nexport LD_AUDIT\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r10-export-loader-name": Case("export GCONV_PATH=/x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-env-name": Case(
        "export LANE_X=2\n" + DOLLAR0 + SRC + RUN, 2, REBOUND, env={"LANE_X": "1"}
    ),
    "r10-export-quoted-value": Case('export LANE_R10="$x"\n' + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-substitution": Case("export LANE_R10=$(true)\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-default-value": Case("export LANE_R10=${Y:-z}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-two-operands": Case("export A=1 B=2\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-append": Case("export LANE_R10+=x\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-export-flag-p": Case("export -p\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-default-assign-colon": Case("X=${Y:=z}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-default-assign": Case("X=${Y=z}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-default-alternative": Case("X=${Y:+z}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-default-indirect": Case("X=${!Y:-z}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-default-substitution": Case("X=${Y:-$(true)}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-default-nested": Case("X=${Y:-$Z}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-default-exit-operand": Case("exit ${Y:-1}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-default-path-target": Case("PATH=${Y:-/x}\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-function-ifs-body": Case("f() { IFS=x; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r10-function-printed-argv0": Case(
        "f() { printf '%s' 'BASH_ARGV0=/x'; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r11-export-keyword-fn": Case(
        "function export " + HASH_DIRNAME + "export LANE=ok\n" + CDPWD + SRC + RUN,
        2,
        REBOUND,
        OTHER_DIRNAME,
    ),
    "r11-export-keyword-fn-bare": Case(
        "function export " + HASH_DIRNAME + "export LANE\n" + CDPWD + SRC + RUN,
        2,
        REBOUND,
        OTHER_DIRNAME,
    ),
    "r11-export-keyword-fn-dollar0": Case(
        "function export " + HASH_DIRNAME + "export LANE=ok\n" + DOLLAR0 + SRC + RUN,
        2,
        REBOUND,
        OTHER_DIRNAME,
    ),
    "r11-export-paren-fn": Case(
        "export() " + HASH_DIRNAME + "export LANE=ok\n" + CDPWD + SRC + RUN, 2, None, OTHER_DIRNAME
    ),
    "r11-export-keyword-fn-noop": Case(
        "function export { :; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND
    ),
    "r11-declare-keyword-fn": Case("function declare { :; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r11-typeset-keyword-fn": Case("function typeset { :; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r11-readonly-keyword-fn": Case("function readonly { :; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r11-local-keyword-fn": Case("function local { :; }\n" + DOLLAR0 + SRC + RUN, 2, REBOUND),
    "r11-local-own-dir": Case("local " + DOLLAR0 + SRC + RUN, 2),
    "r11-export-flag-own-dir": Case(
        'export -r HERE=$(dirname "$(readlink -f "$0")")\n' + SRC + RUN, 2
    ),
    "r11-readonly-flag-own-dir": Case(
        'readonly -x HERE=$(dirname "$(readlink -f "$0")")\n' + SRC + RUN, 2
    ),
    "r11-argv0-own-dir": Case(ARGV0_OWN + CDPWD + SRC + RUN, 2, REBOUND, ROOT_LIB),
    "r11-argv0-own-dir-dollar0": Case(ARGV0_OWN + DOLLAR0 + SRC + RUN, 2, REBOUND, ROOT_LIB),
    "r11-path-own-dir": Case(
        'PATH=$(dirname "$(readlink -f "$0")")\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r11-ifs-own-dir": Case(
        'IFS=$(dirname "$(readlink -f "$0")")\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r11-cdpath-own-dir": Case(
        'CDPATH=$(dirname "$(readlink -f "$0")")\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r11-bash-env-own-dir": Case(
        'BASH_ENV=$(dirname "$(readlink -f "$0")")\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r11-home-own-dir": Case(
        'HOME=$(dirname "$(readlink -f "$0")")\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r11-ld-preload-own-dir": Case(
        'LD_PRELOAD=$(dirname "$(readlink -f "$0")")\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r11-loader-own-dir": Case(
        'GCONV_PATH=$(dirname "$(readlink -f "$0")")\n' + CDPWD + SRC + RUN, 2, REBOUND
    ),
    "r11-env-name-own-dir": Case(
        'LANE_X=$(dirname "$(readlink -f "$0")")\n. "$LANE_X/lib.sh"\n' + RUN,
        2,
        UNKNOWN_SOURCE,
        env={"LANE_X": "/x"},
    ),
    "r11-plain-own-dir-name": Case(
        'TOP=$(dirname "$(readlink -f "$0")")\n. "$TOP/lib.sh"\n' + RUN, 0
    ),
    "r12-printf-v-own-dir": Case(DOLLAR0 + PRINTF + SRC, 2, WRITTEN, OTHER),
    "r12-printf-v-quoted-name": Case(
        DOLLAR0 + "printf -v 'HERE' '%s' <case>/other\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-printf-v-cdpwd": Case(CDPWD + PRINTF + SRC, 2, WRITTEN, OTHER),
    "r12-printf-v-default": Case(DEFAULT + PRINTF + SRC, 2, WRITTEN, OTHER),
    "r12-read-own-dir": Case(DOLLAR0 + "read -r HERE <<< <case>/other\n" + SRC, 2, WRITTEN, OTHER),
    "r12-mapfile-own-dir": Case(
        DOLLAR0 + "mapfile -t HERE <<< <case>/other\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-arith-own-dir": Case(DOLLAR0 + "echo $((HERE=7))\n" + SRC, 2, WRITTEN, {"7/lib.sh": CFG}),
    "r12-for-own-dir": Case(
        DOLLAR0 + "for HERE in <case>/other; do :; done\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-assign-default-colon": Case(DOLLAR0 + ": ${HERE:=x}\n" + SRC, 2, WRITTEN),
    "r12-assign-default": Case(DOLLAR0 + ": ${HERE=x}\n" + SRC, 2, WRITTEN),
    "r12-unset-own-dir": Case(DOLLAR0 + "unset HERE\n" + SRC, 2, WRITTEN),
    "r12-nameref-own-dir": Case(
        DOLLAR0 + "declare -n R=HERE\nR=<case>/other\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-eval-string-own-dir": Case(
        DOLLAR0 + "eval 'HERE=<case>/other'\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-trap-debug-own-dir": Case(
        DOLLAR0 + "trap 'HERE=<case>/other' DEBUG\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-colon-prefix-posix": Case(
        DOLLAR0 + POSIX + "HERE=<case>/other :\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-source-prefix-posix": Case(
        DOLLAR0 + POSIX + 'HERE=<case>/other . "$HERE/lib.sh"\n' + SRC, 2, WRITTEN, OTHER
    ),
    "r12-eval-prefix-posix": Case(
        DOLLAR0 + POSIX + "HERE=<case>/other eval :\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-quoted-command-prefix": Case(
        DOLLAR0 + POSIX + "HERE=<case>/other ':'\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-redirect-only-prefix": Case(
        DOLLAR0 + "HERE=<case>/other >/dev/null\n" + SRC, 2, WRITTEN, OTHER
    ),
    "r12-unquoted-heredoc-default": Case(
        DOLLAR0 + "cat <<EOF\n${HERE:=x}\nEOF\n" + SRC, 2, WRITTEN
    ),
    "r12-append-own-dir": Case(DOLLAR0 + "HERE+=/../other\n" + SRC + RUN, 2, UNKNOWN_SOURCE, OTHER),
    "r12-index-own-dir": Case(
        DOLLAR0 + "HERE[0]=<case>/other\n" + SRC + RUN, 2, UNKNOWN_SOURCE, OTHER
    ),
    "r12-source-file-printf": Case(
        DOLLAR0 + '. "$HERE/w.sh"\n' + SRC,
        2,
        AMBIGUOUS,
        {"tools/w.sh": "printf -v HERE %s <case>/other\n", "other/lib.sh": CFG},
    ),
    "r12-override-append": Case(
        "LANE_TEST_DIR+=<case>/other\n" + DEFAULT + SRC + RUN, 2, UNKNOWN_SOURCE, OTHER
    ),
    "r12-literal-printf-run": Case(
        "X=<case>/tools\nprintf -v X '%s' <case>/other\n" + '. "$X/lib.sh"\n' + RUN,
        2,
        UNKNOWN_SOURCE,
        OTHER,
    ),
    "r12-keep-export-bare": Case(DOLLAR0 + "export HERE\n" + SRC + RUN, 0),
    "r12-keep-readonly-bare": Case(DOLLAR0 + "readonly HERE\n" + SRC + RUN, 0),
    "r12-keep-comment": Case(DOLLAR0 + "# printf -v HERE x\n" + SRC + RUN, 0),
    "r12-keep-braced-reads": Case(
        DOLLAR0
        + 'echo "${HERE%/}" "${HERE#/}" "${HERE:-x}" "${HERE:0:2}" "${#HERE}" "${HERE^^}" '
        + '"${HERE@Q}" "${HERE/a/b}"\n'
        + SRC
        + RUN,
        0,
    ),
    "r12-keep-quoted-heredoc": Case(
        DOLLAR0 + "python3 - <<'PY'\nHERE = 1\nprint(HERE)\nPY\n" + SRC + RUN, 0
    ),
    "r12-keep-program-prefix": Case(DOLLAR0 + "HERE=<case>/other env\n" + SRC + RUN, 0),
    "r12-keep-function-prefix": Case(
        DOLLAR0 + "g() { echo hi; }\nHERE=<case>/other g\n" + SRC + RUN, 0
    ),
    "r12-keep-command-prefix": Case(
        DOLLAR0 + POSIX + "HERE=<case>/other command :\n" + SRC + RUN, 0
    ),
    "r12-keep-landing-prefix": Case(
        DOLLAR0 + 'f() { :; }\nrun() {\n  HERE="$d/missing" MODE=always f\n}\n' + SRC + RUN, 0
    ),
    "r13-function-prefix-source": Case(
        DOLLAR0 + 'f() { . "$HERE/lib.sh"; }\nHERE=<case>/other f\n' + RUN, 2, PREFIXED, OTHER
    ),
    "r13-function-prefix-program": Case(
        DOLLAR0 + 'f() {\n  "$HERE/next.sh"\n}\nHERE=<case>/other f\n' + RUN,
        2,
        PREFIXED,
        {"tools/next.sh": "echo benign\n", "other/next.sh": CFG},
    ),
    "r13-function-prefix-callee": Case(
        DOLLAR0 + 'g() { . "$HERE/lib.sh"; }\nf() { g; }\nHERE=<case>/other f\n' + RUN,
        2,
        PREFIXED,
        OTHER,
    ),
    "r13-function-prefix-callee-later": Case(
        DOLLAR0 + 'f() { g; }\ng() { . "$HERE/lib.sh"; }\nHERE=<case>/other f\n' + RUN,
        2,
        PREFIXED,
        OTHER,
    ),
    "r13-function-prefix-cdpwd": Case(
        CDPWD + 'f() { . "$HERE/lib.sh"; }\nHERE=<case>/other f\n' + RUN, 2, PREFIXED, OTHER
    ),
    "r13-function-prefix-literal": Case(
        'X=<case>/tools\nf() { . "$X/lib.sh"; }\nX=<case>/other f\n' + RUN, 2, PREFIXED, OTHER
    ),
    "r13-function-prefix-nested-call": Case(
        DOLLAR0 + 'f() { . "$HERE/lib.sh"; }\nrun() {\n  HERE=<case>/other f\n}\nrun\n' + RUN,
        2,
        PREFIXED,
        OTHER,
    ),
    "r13-function-before-dir": Case(
        'f() { . "$HERE/lib.sh"; }\n' + DOLLAR0 + "HERE=<case>/other f\n" + RUN,
        2,
        PREFIXED,
        OTHER,
    ),
    "r13-function-in-sourced-file": Case(
        DOLLAR0 + '. "$HERE/fn.sh"\nHERE=<case>/other f\n' + RUN,
        2,
        PREFIXED,
        {"tools/fn.sh": 'f() { . "$HERE/lib.sh"; }\n', "other/lib.sh": CFG},
    ),
    "r13-keep-other-prefix": Case(
        DOLLAR0 + 'f() { . "$HERE/lib.sh"; }\nMODE=x f\n' + RUN, 0, files=OTHER
    ),
    "r13-keep-function-reads-only": Case(
        DOLLAR0 + 'f() { echo "$HERE"; }\nHERE=<case>/other f\n' + SRC + RUN, 0, files=OTHER
    ),
    "r13-keep-unrelated-function": Case(
        DOLLAR0 + 'g() { . "$HERE/lib.sh"; }\nf() { echo hi; }\nHERE=<case>/other f\ng\n' + RUN,
        0,
        files=OTHER,
    ),
    "r13-keep-landing-judge": Case(
        DOLLAR0
        + 'judge() {\n  python3 -B "$HERE/../x.py"\n}\n'
        + 'run() {\n  HERE="$d/missing" judge\n}\nrun\n'
        + SRC
        + RUN,
        0,
    ),
}


class TestRawConfigureScriptDir(RawConfigureGuardBase):
    """Resolve the own-directory idiom only where the guard knows the script directory."""

    def _case_dir(self, name: str, case: Case) -> Path:
        """Lay out benign root and tools scripts plus the case files under one directory."""
        base = self.root / name
        files = {
            "inner.sh": "echo benign\n",
            "sub/inner.sh": CFG,
            "tools/inner.sh": "echo benign\n",
            "tools/lib.sh": "X=1\n",
            "tools/run.sh": case.body,
            **(case.files or {}),
        }
        for rel, text in files.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if text.startswith("-> "):
                path.symlink_to(text[3:])
                continue
            body = text.replace("<cfg>", f"{self.tree}/configure --prefix=/x")
            body = body.replace("<tree>", str(self.tree))
            script = "#!/usr/bin/env bash\n" + body.replace("<case>", str(base))
            path.write_text(script, encoding="utf-8")
            path.chmod(0o755)
        return base

    def _run(
        self, base: Path, command: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Classify a Claude command from the case directory with a minimal environment."""
        run_env = {"PATH": os.defpath, "LC_ALL": "C.UTF-8", **(env or {})}
        return self.run_guard("claude", self.payload("claude", command, base), env=run_env)

    def test_bash_variables_documented(self) -> None:
        """Keep the 110 bash 5.3 manual variable names and exclude undocumented names."""
        self.assertEqual(len(BASH_VARIABLES), 110)
        self.assertLessEqual(
            {"_", "PATH", "BASH_ARGV0", "BASH_CMDS", "BASH_ALIASES", "EXECIGNORE", "IFS", "UID"},
            BASH_VARIABLES,
        )
        self.assertNotIn("BASH_CMDS_X", BASH_VARIABLES)

    def test_script_dir_shapes(self) -> None:
        """Replay all 346 own-directory shapes: resolved, marked uncertain or refused."""
        self.assertEqual(len(SCRIPT_DIR), 346)
        for name, case in SCRIPT_DIR.items():
            with self.subTest(shape=name):
                base = self._case_dir(name, case)
                result = self._run(base, case.command, case.env)
                self.assertEqual(result.returncode, case.code, result.stderr)
                if case.reason is not None:
                    self.assertIn(case.reason, result.stderr)
                self.assertFalse(self.marker.exists())

    def test_newline_script_dir_refuses(self) -> None:
        """Refuse own-directory references when the script directory path holds a newline."""
        for i, form in enumerate((DOLLAR0, CDPWD)):
            with self.subTest(i=i):
                base = self._case_dir(
                    f"newline-{i}",
                    Case(
                        form + SRC + RUN,
                        2,
                        files={
                            "tools/lib.sh": CFG,
                            "tools\n/run.sh": form + SRC + RUN,
                            "tools\n/lib.sh": "X=1\n",
                            "tools\n/inner.sh": "echo benign\n",
                        },
                    ),
                )
                result = self._run(base / "tools\n", "bash ./run.sh")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(NEWLINE, result.stderr)
                self.assertFalse(self.marker.exists())

    def test_unsuffixed_sibling_of_newline_dir_allowed(self) -> None:
        """Allow an unsuffixed sibling directory when only the newline directory holds configure."""
        base = self._case_dir(
            "newline-sibling", Case(DOLLAR0 + SRC + RUN, 0, files={"tools\n/lib.sh": CFG})
        )
        result = self._run(base / "tools", "bash ./run.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_unreadable_script_dir_source_refuses(self) -> None:
        """Keep refusing a resolved source the guard cannot read or that is no regular file."""
        cases = [("not-regular", "(not a regular file)")]
        if os.geteuid() != 0:
            cases.append(("unreadable", "(unreadable script)"))
        for name, reason in cases:
            with self.subTest(shape=name):
                base = self._case_dir(name, Case(DEFAULT + SRC + RUN, 2))
                lib = base / "tools/lib.sh"
                if name == "unreadable":
                    lib.chmod(0)
                else:
                    lib.unlink()
                    lib.mkdir()
                result = self._run(base, "bash tools/run.sh")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertFalse(self.marker.exists())
